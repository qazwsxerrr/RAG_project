"""src/rag_kb/core/llm/client.py —— 企业级多 Agent 统一 LLM 网关门面。

职责：
1. 统一管理底层 HTTP 连接池与生命周期；
2. 严格贯彻 Fail-Fast 原则（4xx 鉴权/参数错误立即快速失败）；
3. 集成统一瞬态重试策略（网络波动与 5xx/429 指数退避重试）；
4. 提供标准非流式 chat()、同步 chat_sync()、流式打字机 stream_chat() 与强类型结构化 chat_json()；
5. 【双协议引擎与 .env 动态切换】：
   - 默认首选新一代 Responses 协议 (POST /v1/responses，官方 Agent 原生，零 <think> 标签污染)；
   - 兼容传统 Chat Completions 协议 (POST /v1/chat/completions，全球通用兼容)；
   - 通过 .env 中的 LLM_API_BACKEND="responses" | "chat_completions" 无缝平滑切换。
"""

import asyncio
import json
import logging
import time
from typing import AsyncGenerator, Dict, Any, List, Optional, Tuple, Type, TypeVar
from pydantic import BaseModel
import httpx

from rag_kb.core.config import settings, ModelNodeConfig, ResilientLLMConfig, load_resilient_llm_config
from rag_kb.core.resilience.retry import retry_on_transient, is_transient_error
from rag_kb.core.resilience.classifier import classify_error
from rag_kb.core.resilience.circuit_breaker import ModelHealthTracker
from rag_kb.core.resilience.alert import trigger_webhook_alert
from rag_kb.core.logging.context import get_request_id, get_tenant_id, get_user_id
from rag_kb.core.logging.audit import record_audit_log
from rag_kb.core.llm.stream_parser import parse_sse_stream
from rag_kb.core.network.egress import validate_outbound_url

T = TypeVar("T", bound=BaseModel)
logger = logging.getLogger("rag_kb.llm_gateway")

class LLMGateway:
    """企业级统一大模型调用网关"""

    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None
        self._sync_client: Optional[httpx.Client] = None

    def get_client(self) -> httpx.AsyncClient:
        """获取或惰性初始化全局共享的 httpx.AsyncClient 客户端长连接池 (集成精细化分级超时)"""
        if self._client is None or self._client.is_closed:
            limits = httpx.Limits(max_keepalive_connections=50, max_connections=100)
            timeout = httpx.Timeout(connect=3.0, read=60.0, write=10.0, pool=5.0)
            self._client = httpx.AsyncClient(limits=limits, timeout=timeout)
        return self._client

    def get_sync_client(self) -> httpx.Client:
        """获取或惰性初始化全局共享的同步 httpx.Client 客户端长连接池"""
        if self._sync_client is None or self._sync_client.is_closed:
            limits = httpx.Limits(max_keepalive_connections=50, max_connections=100)
            timeout = httpx.Timeout(connect=3.0, read=60.0, write=10.0, pool=5.0)
            self._sync_client = httpx.Client(limits=limits, timeout=timeout)
        return self._sync_client

    async def close(self):
        """关闭底层连接池 (用于应用停机优雅释放)"""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None
        if self._sync_client and not self._sync_client.is_closed:
            self._sync_client.close()
            self._sync_client = None

    def _prepare_request(
        self,
        messages: List[Dict[str, Any]],
        model: Optional[str] = None,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        temperature: float = 0.2,
        max_tokens: Optional[int] = None,
        stream: bool = False,
        backend: Optional[str] = None,
        provider: Optional[str] = None,
        **kwargs
    ) -> Tuple[str, Dict[str, str], Dict[str, Any], str]:
        """请求入参归一化与鉴权凭据检查 (支持 Responses 与 Chat Completions 双协议自适应)"""
        final_api_key = api_key or ""
        final_base_url = (base_url if base_url is not None else settings.active_llm_base_url).rstrip("/")
        final_model = model if model is not None else settings.active_llm_model

        effective_backend = (backend or settings.active_llm_api_backend).strip().lower()
        if effective_backend in ("responses", "response"):
            effective_backend = "responses"
        else:
            effective_backend = "chat_completions"

        if not final_api_key or final_api_key.startswith("your_") or "*" in final_api_key:
            # Phase-6 §7.5: 100% 从集中鉴权中心短期租约动态注入凭据，严禁回退本地 .env
            try:
                from rag_kb.core.model_credentials import get_credential_sync
                eff_provider = provider or ("kedaya" if "kedaya" in final_base_url.lower() else "siliconflow")
                cred = get_credential_sync(eff_provider, scope="model:llm")
                final_api_key = cred.api_key
                if (not base_url or not base_url.strip()) and cred.base_url:
                    final_base_url = cred.base_url.rstrip("/")
            except Exception as e:
                raise RuntimeError(f"【LLM 网关致命错误】从 Java 鉴权中心换取模型租约失败: {e}！(Fail-Fast)")

        headers = {
            "Authorization": f"Bearer {final_api_key}",
            "Content-Type": "application/json"
        }

        # 1. 新一代 Responses 协议分支 (POST /v1/responses)
        if effective_backend == "responses":
            endpoint = f"{final_base_url}/responses"
            sys_msg = next((m.get("content") for m in messages if m.get("role") == "system"), None)
            non_sys_msgs = [m for m in messages if m.get("role") != "system"]

            # 系统提示词归一化为 instructions 字符串
            sys_instruction = None
            if sys_msg is not None:
                if isinstance(sys_msg, str):
                    sys_instruction = sys_msg
                elif isinstance(sys_msg, list):
                    tp = [p.get("text", "") for p in sys_msg if isinstance(p, dict) and p.get("type") in ("text", "input_text")]
                    sys_instruction = "\n".join(tp) if tp else ""
                else:
                    sys_instruction = str(sys_msg)

            # 探测是否包含多模态图片
            has_multimodal = any(
                isinstance(m.get("content"), list) and any(
                    isinstance(p, dict) and p.get("type") in ("image_url", "input_image")
                    for p in m.get("content")
                )
                for m in non_sys_msgs
            )

            if not has_multimodal and len(non_sys_msgs) == 1:
                # 纯文本单轮调用极速快道：提取为规范的 input 纯文本字符串
                content = non_sys_msgs[0].get("content")
                if isinstance(content, str):
                    input_data = content
                elif isinstance(content, list):
                    text_parts = [
                        p.get("text", "") for p in content
                        if isinstance(p, dict) and p.get("type") in ("text", "input_text")
                    ]
                    input_data = "\n".join(text_parts) if text_parts else ""
                else:
                    input_data = str(content)
            elif non_sys_msgs:
                # 多模态图文调用 或 多轮对话：转换为官方 Responses API 原生规范
                sanitized_msgs = []
                for m in non_sys_msgs:
                    c = m.get("content")
                    role = m.get("role", "user")
                    if isinstance(c, list):
                        converted_parts = []
                        for p in c:
                            if not isinstance(p, dict):
                                converted_parts.append({"type": "input_text", "text": str(p)})
                                continue
                            p_type = p.get("type", "")
                            if p_type in ("text", "input_text"):
                                converted_parts.append({
                                    "type": "input_text",
                                    "text": p.get("text", "")
                                })
                            elif p_type in ("image_url", "input_image"):
                                raw_img = p.get("image_url")
                                if isinstance(raw_img, dict):
                                    url_str = raw_img.get("url", "")
                                else:
                                    url_str = str(raw_img or "")
                                img_part = {
                                    "type": "input_image",
                                    "image_url": url_str
                                }
                                if "detail" in p:
                                    img_part["detail"] = p["detail"]
                                converted_parts.append(img_part)
                            else:
                                converted_parts.append(p)
                        sanitized_msgs.append({"role": role, "content": converted_parts})
                    elif isinstance(c, str):
                        sanitized_msgs.append({"role": role, "content": c})
                    else:
                        sanitized_msgs.append({"role": role, "content": str(c)})
                input_data = sanitized_msgs
            else:
                input_data = messages

            payload = {
                "model": final_model,
                "input": input_data,
                "stream": stream,
                **kwargs
            }
            if sys_instruction:
                payload["instructions"] = sys_instruction
            if temperature is not None:
                payload["temperature"] = temperature
            if max_tokens is not None:
                payload["max_tokens"] = max_tokens

        # 2. 传统 Chat Completions 协议分支
        else:
            endpoint = f"{final_base_url}/chat/completions"
            payload = {
                "model": final_model,
                "messages": messages,
                "temperature": temperature,
                "stream": stream,
                **kwargs
            }
            if max_tokens is not None:
                payload["max_tokens"] = max_tokens

        # 出网安全与 SSRF 防御校验 (SECURITY_MODEL.md 方向二)
        validate_outbound_url(endpoint)

        return endpoint, headers, payload, effective_backend

    @staticmethod
    def _extract_content(data: Dict[str, Any], backend: str) -> str:
        """从非流式响应中自适应提取纯净回答文本"""
        if backend == "responses":
            output_items = data.get("output", [])
            for item in output_items:
                if item.get("type") == "message":
                    content_parts = item.get("content", [])
                    if isinstance(content_parts, list):
                        text_list = []
                        for part in content_parts:
                            if isinstance(part, dict) and "text" in part:
                                text_list.append(part["text"])
                            elif isinstance(part, str):
                                text_list.append(part)
                        return "".join(text_list).strip()
                    elif isinstance(content_parts, str):
                        return content_parts.strip()
            # 兼容部分中转平台下发的 output_text 根属性
            if "output_text" in data and isinstance(data["output_text"], str):
                return data["output_text"].strip()
            raise RuntimeError(f"【LLM 异常响应】Responses 协议返回未包含有效的 message/output 节点: {data}")

        # Chat completions
        choices = data.get("choices")
        if not choices:
            raise RuntimeError(f"【LLM 异常响应】服务返回未包含 choices 节点: {data}")
        return choices[0].get("message", {}).get("content", "") or ""

    # ==================== 1. 标准非流式调用 (纯文本与 VLM 视觉理解通用) ====================
    @retry_on_transient(service_name="LLMGateway.chat")
    async def chat(
        self,
        messages: List[Dict[str, Any]],
        model: Optional[str] = None,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        temperature: float = 0.2,
        max_tokens: Optional[int] = None,
        backend: Optional[str] = None,
        **kwargs
    ) -> str:
        """
        通用非流式调用：支持文本问答、查询改写、HyDE 假想文档以及 VLM 多模态图文调用。
        根据 .env 中 LLM_API_BACKEND 自动适配 Responses 或 Chat Completions。
        """
        client = self.get_client()
        endpoint, headers, payload, eff_backend = self._prepare_request(
            messages=messages,
            model=model,
            base_url=base_url,
            api_key=api_key,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=False,
            backend=backend,
            **kwargs
        )

        resp = await client.post(endpoint, headers=headers, json=payload)
        if resp.status_code != 200:
            err_msg = resp.text
            logger.error(f"[LLMGateway] HTTP {resp.status_code} 调用失败 ({eff_backend}): {err_msg}")
            resp.raise_for_status()

        data = resp.json()
        content = self._extract_content(data, eff_backend)
        return content

    @retry_on_transient(service_name="LLMGateway.chat_sync")
    def chat_sync(
        self,
        messages: List[Dict[str, Any]],
        model: Optional[str] = None,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        temperature: float = 0.2,
        max_tokens: Optional[int] = None,
        backend: Optional[str] = None,
        **kwargs
    ) -> str:
        """
        同步非流式通用调用：用于同步流水线节点、离线切图解析以及多模态 VLM 同步服务调用。
        """
        client = self.get_sync_client()
        endpoint, headers, payload, eff_backend = self._prepare_request(
            messages=messages,
            model=model,
            base_url=base_url,
            api_key=api_key,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=False,
            backend=backend,
            **kwargs
        )

        resp = client.post(endpoint, headers=headers, json=payload)
        if resp.status_code != 200:
            err_msg = resp.text
            logger.error(f"[LLMGateway] HTTP {resp.status_code} 同步调用失败 ({eff_backend}): {err_msg}")
            resp.raise_for_status()

        data = resp.json()
        content = self._extract_content(data, eff_backend)
        return content

    # ==================== 2. 流式打字机分流调用 ====================
    async def stream_chat(
        self,
        messages: List[Dict[str, Any]],
        model: Optional[str] = None,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        temperature: float = 0.2,
        max_tokens_budget: int = 4096,
        backend: Optional[str] = None,
        **kwargs
    ) -> AsyncGenerator[Tuple[str, str], None]:
        """
        通用流式分流调用：
        - 自动解耦 reasoning (思考流) 与 message (回答正文)；
        - 支持 Responses 原生事件流与 Chat Completions 协议流；
        - 内置单次请求 Token 预算熔断，防止死循环或成本 DoS。
        """
        client = self.get_client()
        endpoint, headers, payload, eff_backend = self._prepare_request(
            messages=messages,
            model=model,
            base_url=base_url,
            api_key=api_key,
            temperature=temperature,
            stream=True,
            backend=backend,
            **kwargs
        )

        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                async with client.stream("POST", endpoint, headers=headers, json=payload) as resp:
                    if resp.status_code != 200:
                        err_text = await resp.aread()
                        if resp.status_code in (429, 502, 503, 504) and attempt < max_attempts:
                            delay = 2 ** (attempt - 1)
                            logger.warning(
                                f"[LLMGateway] stream_chat 遇到瞬态 HTTP {resp.status_code}，将在 {delay}s 后重试 (第 {attempt}/{max_attempts} 次)..."
                            )
                            await asyncio.sleep(delay)
                            continue
                        raise RuntimeError(
                            f"【LLM 流式调用异常】HTTP {resp.status_code} ({eff_backend}): {err_text.decode(errors='ignore')}"
                        )

                    token_counter = 0
                    async for event_type, token in parse_sse_stream(resp):
                        token_counter += 1
                        if token_counter > max_tokens_budget:
                            logger.warning(f"[LLMGateway] 达到单次请求 Token 预算上限 ({max_tokens_budget})，主动阻断流式响应！")
                            break
                        yield (event_type, token)
                    return
            except Exception as exc:
                if is_transient_error(exc) and attempt < max_attempts:
                    delay = 2 ** (attempt - 1)
                    logger.warning(
                        f"[LLMGateway] stream_chat 建连瞬态异常: {type(exc).__name__} ({exc})，将在 {delay}s 后重试 (第 {attempt}/{max_attempts} 次)..."
                    )
                    await asyncio.sleep(delay)
                    continue
                raise

    # ==================== 3. 强类型结构化 JSON 输出调用 ====================
    async def chat_json(
        self,
        messages: List[Dict[str, Any]],
        schema: Type[T],
        model: Optional[str] = None,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        temperature: float = 0.1,
        backend: Optional[str] = None,
        **kwargs
    ) -> T:
        """
        强类型结构化输出调用：用于 Agent 意图识别、参数决策、工具调用。
        自动注入 JSON Schema 约束，直接返回校验后的 Pydantic 模型实例。
        """
        schema_dict = schema.model_json_schema()
        schema_prompt = (
            "\n\n【输出约束】：请必须且仅返回合法的 JSON 格式，严格符合以下 JSON Schema：\n"
            f"{json.dumps(schema_dict, ensure_ascii=False)}"
        )

        injected_messages = [dict(m) for m in messages]
        if injected_messages:
            injected_messages[-1]["content"] = str(injected_messages[-1]["content"]) + schema_prompt

        raw_json_str = await self.chat(
            messages=injected_messages,
            model=model,
            base_url=base_url,
            api_key=api_key,
            temperature=temperature,
            backend=backend,
            **kwargs
        )

        cleaned_json = raw_json_str.strip()
        if cleaned_json.startswith("```"):
            lines = cleaned_json.split("\n")
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            cleaned_json = "\n".join(lines).strip()

        return schema.model_validate_json(cleaned_json)

# ==================== Phase-7: 高可用弹性多模型网关门面 ====================

class ResilientLLMGateway(LLMGateway):
    """
    具备动态多模型热切换、全链路指标观测与容灾熔断的企业级 LLM 网关门面 (Phase-7)
    """

    def __init__(self, config: Optional[ResilientLLMConfig] = None):
        super().__init__()
        self.config = config or load_resilient_llm_config(settings)
        self.health_tracker = ModelHealthTracker(
            window_size=self.config.sliding_window_size,
            cooldown_seconds=self.config.circuit_breaker_cooldown_seconds,
            error_threshold_percent=self.config.error_threshold_percentage
        )

    def reload_config(self, config: Optional[ResilientLLMConfig] = None):
        """支持配置热重载"""
        self.config = config or load_resilient_llm_config(settings)
        self.health_tracker.window_size = self.config.sliding_window_size
        self.health_tracker.cooldown_seconds = self.config.circuit_breaker_cooldown_seconds
        self.health_tracker.error_threshold_percent = self.config.error_threshold_percentage

    def _get_candidate_nodes(
        self,
        model: Optional[str] = None,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        backend: Optional[str] = None
    ) -> List[ModelNodeConfig]:
        """获取目标执行候选链：若调用方显式指定 endpoint/model 则以指定参数优先，否则按候选池顺序调度"""
        if model or base_url or api_key:
            eff_url = base_url or self.config.primary.base_url
            eff_provider = "kedaya" if "kedaya" in eff_url.lower() else "siliconflow"
            return [
                ModelNodeConfig(
                    name="caller-override",
                    model=model or self.config.primary.model,
                    base_url=eff_url,
                    api_key=api_key or self.config.primary.api_key,
                    provider=eff_provider,
                    backend=backend or self.config.primary.backend
                )
            ]
        return [self.config.primary] + self.config.fallbacks

    async def stream_chat(
        self,
        messages: List[Dict[str, Any]],
        model: Optional[str] = None,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        temperature: float = 0.2,
        max_tokens_budget: int = 4096,
        backend: Optional[str] = None,
        **kwargs
    ) -> AsyncGenerator[Tuple[str, str], None]:
        candidate_nodes = self._get_candidate_nodes(model, base_url, api_key, backend)
        req_id = get_request_id()
        last_error = None

        for idx, node in enumerate(candidate_nodes):
            if not self.health_tracker.is_node_available(node.name):
                logger.warning(f"[{req_id}][LLMGateway] 节点 [{node.name}] 处于熔断冷却中，自动跳过。")
                continue

            t0 = time.perf_counter()
            first_token_time = None
            token_yielded = False

            try:
                logger.info(f"[{req_id}][LLMGateway] 尝试由节点 [{node.name} ({node.model})] 执行流式推理...")
                client = self.get_client()
                eff_api_key = node.api_key
                eff_base_url = node.base_url
                if not eff_api_key:
                    from rag_kb.core.model_credentials import get_credential
                    cred = await get_credential(node.provider or "kedaya", scope="model:llm")
                    eff_api_key = cred.api_key
                    eff_base_url = cred.base_url or eff_base_url
                endpoint, headers, payload, eff_backend = self._prepare_request(
                    messages=messages,
                    model=node.model,
                    base_url=eff_base_url,
                    api_key=eff_api_key,
                    temperature=temperature,
                    stream=True,
                    backend=node.backend,
                    provider=node.provider,
                    **kwargs
                )

                async with client.stream("POST", endpoint, headers=headers, json=payload) as resp:
                    if resp.status_code != 200:
                        err_text = await resp.aread()
                        raise httpx.HTTPStatusError(
                            f"HTTP {resp.status_code} ({eff_backend}): {err_text.decode(errors='ignore')}",
                            request=resp.request,
                            response=resp
                        )

                    token_counter = 0
                    stream_iter = parse_sse_stream(resp).__aiter__()
                    while True:
                        if first_token_time is None:
                            # 首字超时控制：8.0 秒内未接收到首个 Token，判定主节点假死，主动掐断并切换
                            try:
                                event = await asyncio.wait_for(stream_iter.__anext__(), timeout=8.0)
                            except asyncio.TimeoutError:
                                raise httpx.ReadTimeout("首字响应超时 (TTFT > 8.0s)，判定上游节点假死")
                            except StopAsyncIteration:
                                break
                            first_token_time = time.perf_counter()
                            token_yielded = True
                        else:
                            try:
                                event = await stream_iter.__anext__()
                            except StopAsyncIteration:
                                break

                        event_type, token = event
                        token_counter += 1
                        if token_counter > max_tokens_budget:
                            logger.warning(f"[{req_id}][LLMGateway] 达到单次请求 Token 预算上限 ({max_tokens_budget})，主动阻断流式响应！")
                            break
                        yield (event_type, token)

                total_ms = int((time.perf_counter() - t0) * 1000)
                ttft_ms = int((first_token_time - t0) * 1000) if first_token_time else total_ms

                self.health_tracker.record_success(node.name, total_ms, ttft_ms)
                logger.info(f"[{req_id}][LLM_INFERENCE] 节点 [{node.name}] 流式完成 | TTFT: {ttft_ms}ms | 总耗时: {total_ms}ms")

                await record_audit_log(
                    action="LLM_INFERENCE",
                    resource_id=node.name,
                    duration_ms=total_ms,
                    status="SUCCESS",
                    details={
                        "model": node.model,
                        "ttft_ms": ttft_ms,
                        "backend": eff_backend,
                        "failover": idx > 0
                    }
                )
                return

            except Exception as exc:
                last_error = exc
                total_ms = int((time.perf_counter() - t0) * 1000)
                err_category = classify_error(exc)
                self.health_tracker.record_failure(node.name, total_ms, err_category)

                logger.error(f"[{req_id}][LLM_FAILOVER] 节点 [{node.name}] 发生故障 ({err_category}): {exc} | 耗时: {total_ms}ms")

                await record_audit_log(
                    action="LLM_INFERENCE",
                    resource_id=node.name,
                    duration_ms=total_ms,
                    status="FAILED",
                    error_msg=str(exc),
                    details={"error_category": err_category, "model": node.model}
                )

                if self.health_tracker.should_alert(node.name):
                    err_rate, avg_lat, p95_lat = self.health_tracker.get_sla_stats(node.name)
                    await trigger_webhook_alert(
                        node_name=node.name,
                        err_category=err_category,
                        detail=str(exc),
                        error_rate=err_rate,
                        avg_latency=avg_lat,
                        p95_latency=p95_lat,
                        webhook_url=self.config.alert_webhook_url
                    )

                # 安全拦截必须 Fail-Fast，严禁切换
                if err_category == "ERR_SECURITY":
                    raise exc

                # 若流式已经开始向前端抛出 Token，中途中断不能静默切换
                if token_yielded:
                    raise RuntimeError(f"流式输出中途传输中断: {exc}") from exc

                # 还有备选节点则热切换
                if idx < len(candidate_nodes) - 1:
                    next_node = candidate_nodes[idx + 1]
                    logger.warning(
                        f"[{req_id}][LLM_FAILOVER] 毫秒级热切换: [{node.name}] -> [{next_node.name}] (原因: {err_category})"
                    )
                    await record_audit_log(
                        action="LLM_FAILOVER",
                        resource_id=f"{node.name}->{next_node.name}",
                        status="SWITCHED",
                        details={"from_node": node.name, "to_node": next_node.name, "reason": err_category}
                    )
                    continue
                else:
                    raise RuntimeError(f"【LLM 网关灾难性故障】所有候选模型均不可用！最后错误: {last_error}") from exc

    async def chat(
        self,
        messages: List[Dict[str, Any]],
        model: Optional[str] = None,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        temperature: float = 0.2,
        max_tokens: Optional[int] = None,
        backend: Optional[str] = None,
        **kwargs
    ) -> str:
        candidate_nodes = self._get_candidate_nodes(model, base_url, api_key, backend)
        req_id = get_request_id()
        last_error = None

        for idx, node in enumerate(candidate_nodes):
            if not self.health_tracker.is_node_available(node.name):
                logger.warning(f"[{req_id}][LLMGateway] 节点 [{node.name}] 处于熔断冷却中，自动跳过。")
                continue

            t0 = time.perf_counter()
            try:
                client = self.get_client()
                eff_api_key = node.api_key
                eff_base_url = node.base_url
                if not eff_api_key:
                    from rag_kb.core.model_credentials import get_credential
                    cred = await get_credential(node.provider or "kedaya", scope="model:llm")
                    eff_api_key = cred.api_key
                    eff_base_url = cred.base_url or eff_base_url
                endpoint, headers, payload, eff_backend = self._prepare_request(
                    messages=messages,
                    model=node.model,
                    base_url=eff_base_url,
                    api_key=eff_api_key,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    stream=False,
                    backend=node.backend,
                    provider=node.provider,
                    **kwargs
                )

                resp = await client.post(endpoint, headers=headers, json=payload)
                if resp.status_code != 200:
                    err_msg = resp.text
                    logger.error(f"[{req_id}][LLMGateway] HTTP {resp.status_code} 调用失败 ({eff_backend}): {err_msg}")
                    resp.raise_for_status()

                data = resp.json()
                content = self._extract_content(data, eff_backend)
                total_ms = int((time.perf_counter() - t0) * 1000)

                self.health_tracker.record_success(node.name, total_ms)
                logger.info(f"[{req_id}][LLM_INFERENCE] 节点 [{node.name}] 调用成功 | 耗时: {total_ms}ms")

                await record_audit_log(
                    action="LLM_INFERENCE",
                    resource_id=node.name,
                    duration_ms=total_ms,
                    status="SUCCESS",
                    details={"model": node.model, "backend": eff_backend, "failover": idx > 0}
                )
                return content

            except Exception as exc:
                last_error = exc
                total_ms = int((time.perf_counter() - t0) * 1000)
                err_category = classify_error(exc)
                self.health_tracker.record_failure(node.name, total_ms, err_category)

                logger.error(f"[{req_id}][LLM_FAILOVER] 节点 [{node.name}] 故障 ({err_category}): {exc} | 耗时: {total_ms}ms")

                await record_audit_log(
                    action="LLM_INFERENCE",
                    resource_id=node.name,
                    duration_ms=total_ms,
                    status="FAILED",
                    error_msg=str(exc),
                    details={"error_category": err_category, "model": node.model}
                )

                if self.health_tracker.should_alert(node.name):
                    err_rate, avg_lat, p95_lat = self.health_tracker.get_sla_stats(node.name)
                    await trigger_webhook_alert(
                        node_name=node.name,
                        err_category=err_category,
                        detail=str(exc),
                        error_rate=err_rate,
                        avg_latency=avg_lat,
                        p95_latency=p95_lat,
                        webhook_url=self.config.alert_webhook_url
                    )

                if err_category == "ERR_SECURITY":
                    raise exc

                if idx < len(candidate_nodes) - 1:
                    next_node = candidate_nodes[idx + 1]
                    logger.warning(
                        f"[{req_id}][LLM_FAILOVER] 毫秒级热切换: [{node.name}] -> [{next_node.name}] (原因: {err_category})"
                    )
                    await record_audit_log(
                        action="LLM_FAILOVER",
                        resource_id=f"{node.name}->{next_node.name}",
                        status="SWITCHED",
                        details={"from_node": node.name, "to_node": next_node.name, "reason": err_category}
                    )
                    continue
                else:
                    raise RuntimeError(f"【LLM 网关灾难性故障】所有候选模型均不可用！最后错误: {last_error}") from exc

    def chat_sync(
        self,
        messages: List[Dict[str, Any]],
        model: Optional[str] = None,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        temperature: float = 0.2,
        max_tokens: Optional[int] = None,
        backend: Optional[str] = None,
        **kwargs
    ) -> str:
        candidate_nodes = self._get_candidate_nodes(model, base_url, api_key, backend)
        last_error = None

        for idx, node in enumerate(candidate_nodes):
            if not self.health_tracker.is_node_available(node.name):
                logger.warning(f"[LLMGateway] 节点 [{node.name}] 处于熔断冷却中，自动跳过。")
                continue

            t0 = time.perf_counter()
            try:
                client = self.get_sync_client()
                endpoint, headers, payload, eff_backend = self._prepare_request(
                    messages=messages,
                    model=node.model,
                    base_url=node.base_url,
                    api_key=node.api_key,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    stream=False,
                    backend=node.backend,
                    provider=node.provider,
                    **kwargs
                )

                resp = client.post(endpoint, headers=headers, json=payload)
                if resp.status_code != 200:
                    err_msg = resp.text
                    logger.error(f"[LLMGateway] HTTP {resp.status_code} 同步调用失败 ({eff_backend}): {err_msg}")
                    resp.raise_for_status()

                data = resp.json()
                content = self._extract_content(data, eff_backend)
                total_ms = int((time.perf_counter() - t0) * 1000)

                self.health_tracker.record_success(node.name, total_ms)
                logger.info(f"[LLM_INFERENCE] 节点 [{node.name}] 同步调用成功 | 耗时: {total_ms}ms")
                return content

            except Exception as exc:
                last_error = exc
                total_ms = int((time.perf_counter() - t0) * 1000)
                err_category = classify_error(exc)
                self.health_tracker.record_failure(node.name, total_ms, err_category)
                logger.error(f"[LLM_FAILOVER] 节点 [{node.name}] 同步调用故障 ({err_category}): {exc} | 耗时: {total_ms}ms")

                if err_category == "ERR_SECURITY":
                    raise exc

                if idx < len(candidate_nodes) - 1:
                    next_node = candidate_nodes[idx + 1]
                    logger.warning(
                        f"[LLM_FAILOVER] 同步调用热切换: [{node.name}] -> [{next_node.name}] (原因: {err_category})"
                    )
                    continue
                else:
                    raise RuntimeError(f"【LLM 网关灾难性故障】所有候选模型均不可用！最后错误: {last_error}") from exc

# 全局单例导出 (默认使用具备热切换与可观测性的 ResilientLLMGateway)
llm_gateway = ResilientLLMGateway()
