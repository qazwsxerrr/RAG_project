"""src/rag_kb/core/llm/client.py —— 企业级多 Agent 统一 LLM 网关门面。

职责：
1. 统一管理底层 HTTP 连接池与生命周期；
2. 严格贯彻 Fail-Fast 原则（4xx 鉴权/参数错误立即快速失败）；
3. 集成统一瞬态重试与多模型热切换策略（Primary -> Fallbacks 权重编排与模态过滤）；
4. 提供标准非流式 chat()、同步 chat_sync()、流式打字机 stream_chat() 与强类型结构化 chat_json()；
5. 【双协议引擎】：原生支持 Responses (POST /v1/responses) 与 Chat Completions (POST /v1/chat/completions)。
"""

import asyncio
import json
import logging
import time
from typing import AsyncGenerator, Dict, Any, List, Optional, Tuple, Type, TypeVar
from pydantic import BaseModel
import httpx

from rag_kb.core.config import (
    settings,
    ModelNodeConfig,
    ResilientLLMConfig,
    load_resilient_llm_config,
    derive_provider_from_url
)
from rag_kb.core.resilience.retry import retry_on_transient, is_transient_error
from rag_kb.core.resilience.classifier import classify_error, is_fatal_quota_exhausted
from rag_kb.core.resilience.circuit_breaker import ModelHealthTracker
from rag_kb.core.resilience.alert import trigger_webhook_alert, trigger_webhook_alert_sync
from rag_kb.core.logging.context import get_request_id
from rag_kb.core.logging.audit import record_audit_log, record_audit_log_sync
from rag_kb.core.llm.stream_parser import parse_sse_stream
from rag_kb.core.network.egress import validate_outbound_url

T = TypeVar("T", bound=BaseModel)
logger = logging.getLogger("rag_kb.llm_gateway")


class LLMGateway:
    """企业级统一大模型调用基础网关"""

    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None
        self._sync_client: Optional[httpx.Client] = None

    def get_client(self) -> httpx.AsyncClient:
        """获取或惰性初始化全局共享的 httpx.AsyncClient 客户端长连接池"""
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

    def _build_protocol_request(
        self,
        messages: List[Dict[str, Any]],
        final_model: str,
        final_base_url: str,
        final_api_key: str,
        temperature: float = 0.2,
        max_tokens: Optional[int] = None,
        stream: bool = False,
        backend: Optional[str] = None,
        **kwargs
    ) -> Tuple[str, Dict[str, str], Dict[str, Any], str]:
        """纯协议组装与出网 SSRF 安全校验（无网络 I/O 副作用）"""
        effective_backend = (backend or settings.active_llm_api_backend).strip().lower()
        effective_backend = "responses" if effective_backend in ("responses", "response") else "chat_completions"

        headers = {
            "Authorization": f"Bearer {final_api_key}",
            "Content-Type": "application/json"
        }

        if effective_backend == "responses":
            endpoint = f"{final_base_url}/responses"
            sys_msg = next((m.get("content") for m in messages if m.get("role") == "system"), None)
            non_sys_msgs = [m for m in messages if m.get("role") != "system"]

            sys_instruction = None
            if sys_msg is not None:
                if isinstance(sys_msg, str):
                    sys_instruction = sys_msg
                elif isinstance(sys_msg, list):
                    tp = [p.get("text", "") for p in sys_msg if isinstance(p, dict) and p.get("type") in ("text", "input_text")]
                    sys_instruction = "\n".join(tp) if tp else ""
                else:
                    sys_instruction = str(sys_msg)

            has_multimodal = any(
                isinstance(m.get("content"), list) and any(
                    isinstance(p, dict) and p.get("type") in ("image_url", "input_image")
                    for p in m.get("content")
                )
                for m in non_sys_msgs
            )

            if not has_multimodal and len(non_sys_msgs) == 1:
                content = non_sys_msgs[0].get("content")
                if isinstance(content, str):
                    input_data: Any = content
                elif isinstance(content, list):
                    text_parts = [
                        p.get("text", "") for p in content
                        if isinstance(p, dict) and p.get("type") in ("text", "input_text")
                    ]
                    input_data = "\n".join(text_parts) if text_parts else ""
                else:
                    input_data = str(content)
            elif non_sys_msgs:
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
                                converted_parts.append({"type": "input_text", "text": p.get("text", "")})
                            elif p_type in ("image_url", "input_image"):
                                raw_img = p.get("image_url")
                                url_str = raw_img.get("url", "") if isinstance(raw_img, dict) else str(raw_img or "")
                                img_part: Dict[str, Any] = {"type": "input_image", "image_url": url_str}
                                if "detail" in p:
                                    img_part["detail"] = p["detail"]
                                converted_parts.append(img_part)
                            else:
                                converted_parts.append(p)
                        sanitized_msgs.append({"role": role, "content": converted_parts})
                    else:
                        sanitized_msgs.append({"role": role, "content": c if isinstance(c, str) else str(c)})
                input_data = sanitized_msgs
            else:
                input_data = messages

            payload: Dict[str, Any] = {
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

        validate_outbound_url(endpoint)
        return endpoint, headers, payload, effective_backend

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
        """同步请求组装（未显式传 api_key 时同步向鉴权中心换取短期租约）"""
        final_api_key = api_key or ""
        final_base_url = (base_url if base_url is not None else settings.active_llm_base_url).rstrip("/")
        final_model = model if model is not None else settings.active_llm_model

        if not final_api_key or final_api_key.startswith("your_") or "*" in final_api_key:
            try:
                from rag_kb.core.model_credentials import get_credential_sync
                eff_provider = derive_provider_from_url(final_base_url, explicit_provider=provider)
                cred = get_credential_sync(eff_provider, scope="model:llm")
                final_api_key = cred.api_key
                if (not base_url or not base_url.strip()) and cred.base_url:
                    final_base_url = cred.base_url.rstrip("/")
            except Exception as e:
                raise RuntimeError(f"【LLM 网关致命错误】从 Java 鉴权中心换取模型租约失败: {e}！(Fail-Fast)") from e

        return self._build_protocol_request(
            messages=messages,
            final_model=final_model,
            final_base_url=final_base_url,
            final_api_key=final_api_key,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=stream,
            backend=backend,
            **kwargs
        )

    async def _prepare_request_async(
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
        scope: str = "model:llm",
        **kwargs
    ) -> Tuple[str, Dict[str, str], Dict[str, Any], str]:
        """异步请求组装（全程异步向鉴权中心换取短期租约，绝不阻塞事件循环）"""
        final_api_key = api_key or ""
        final_base_url = (base_url if base_url is not None else settings.active_llm_base_url).rstrip("/")
        final_model = model if model is not None else settings.active_llm_model

        if not final_api_key or final_api_key.startswith("your_") or "*" in final_api_key:
            try:
                from rag_kb.core.model_credentials import get_credential
                eff_provider = derive_provider_from_url(final_base_url, explicit_provider=provider)
                cred = await get_credential(eff_provider, scope=scope)
                final_api_key = cred.api_key
                if (not base_url or not base_url.strip()) and cred.base_url:
                    final_base_url = cred.base_url.rstrip("/")
            except Exception as e:
                raise RuntimeError(f"【LLM 网关致命错误】从 Java 鉴权中心换取模型租约失败: {e}！(Fail-Fast)") from e

        return self._build_protocol_request(
            messages=messages,
            final_model=final_model,
            final_base_url=final_base_url,
            final_api_key=final_api_key,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=stream,
            backend=backend,
            **kwargs
        )

    @staticmethod
    def _extract_content(data: Dict[str, Any], backend: str) -> str:
        """从非流式响应中自适应提取纯净回答文本"""
        if backend == "responses":
            for item in data.get("output", []):
                if item.get("type") == "message":
                    content_parts = item.get("content", [])
                    if isinstance(content_parts, list):
                        text_list = [
                            part["text"] if isinstance(part, dict) and "text" in part else (part if isinstance(part, str) else "")
                            for part in content_parts
                        ]
                        return "".join(text_list).strip()
                    if isinstance(content_parts, str):
                        return content_parts.strip()
            if "output_text" in data and isinstance(data["output_text"], str):
                return data["output_text"].strip()
            raise RuntimeError(f"【LLM 异常响应】Responses 协议返回未包含有效的 message/output 节点: {data}")

        choices = data.get("choices")
        if not choices:
            raise RuntimeError(f"【LLM 异常响应】服务返回未包含 choices 节点: {data}")
        return choices[0].get("message", {}).get("content", "") or ""

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
        """基础单节点异步非流式调用"""
        client = self.get_client()
        endpoint, headers, payload, eff_backend = await self._prepare_request_async(
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
            logger.error(f"[LLMGateway] HTTP {resp.status_code} 调用失败 ({eff_backend}): {resp.text}")
            resp.raise_for_status()
        return self._extract_content(resp.json(), eff_backend)

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
        """基础单节点同步非流式调用"""
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
            logger.error(f"[LLMGateway] HTTP {resp.status_code} 同步调用失败 ({eff_backend}): {resp.text}")
            resp.raise_for_status()
        return self._extract_content(resp.json(), eff_backend)

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
        """基础单节点流式分流调用"""
        client = self.get_client()
        endpoint, headers, payload, eff_backend = await self._prepare_request_async(
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
                            logger.warning(f"[LLMGateway] stream_chat 瞬态 HTTP {resp.status_code}，{delay}s 后重试 ({attempt}/{max_attempts})...")
                            await asyncio.sleep(delay)
                            continue
                        raise RuntimeError(f"【LLM 流式调用异常】HTTP {resp.status_code} ({eff_backend}): {err_text.decode(errors='ignore')}")

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
                    logger.warning(f"[LLMGateway] stream_chat 建连瞬态异常: {exc}，{delay}s 后重试 ({attempt}/{max_attempts})...")
                    await asyncio.sleep(delay)
                    continue
                raise

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
        """强类型结构化输出调用：自动注入 JSON Schema 约束并返回校验后的 Pydantic 实例"""
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
    """具备动态多模型热切换、全链路指标观测与容灾熔断的企业级 LLM 网关门面 (Phase-7)"""

    def __init__(self, config: Optional[ResilientLLMConfig] = None):
        super().__init__()
        self.config = config or load_resilient_llm_config(settings)
        self.health_tracker = ModelHealthTracker(
            window_size=self.config.sliding_window_size,
            window_seconds=self.config.sliding_window_seconds,
            cooldown_seconds=self.config.circuit_breaker_cooldown_seconds,
            error_threshold_percent=self.config.error_threshold_percentage,
            min_calls=self.config.circuit_breaker_min_calls,
            consecutive_failures_threshold=self.config.circuit_breaker_consecutive_failures
        )

    def reload_config(self, config: Optional[ResilientLLMConfig] = None):
        """支持配置热重载"""
        self.config = config or load_resilient_llm_config(settings)
        self.health_tracker.window_size = self.config.sliding_window_size
        self.health_tracker.window_seconds = self.config.sliding_window_seconds
        self.health_tracker.cooldown_seconds = self.config.circuit_breaker_cooldown_seconds
        self.health_tracker.error_threshold_percent = self.config.error_threshold_percentage
        self.health_tracker.min_calls = self.config.circuit_breaker_min_calls
        self.health_tracker.consecutive_failures_threshold = self.config.circuit_breaker_consecutive_failures

    @staticmethod
    def _detect_requires_vision(messages: Optional[List[Dict[str, Any]]]) -> bool:
        """检查上下文输入中是否包含多模态图片"""
        if not messages:
            return False
        for m in messages:
            content = m.get("content")
            if isinstance(content, list):
                for p in content:
                    if isinstance(p, dict) and p.get("type") in ("image_url", "input_image"):
                        return True
        return False

    def _get_candidate_nodes(
        self,
        messages: Optional[List[Dict[str, Any]]] = None,
        model: Optional[str] = None,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        backend: Optional[str] = None,
        require_vision: Optional[bool] = None
    ) -> List[ModelNodeConfig]:
        """按模态能力、is_active 状态与 weight 权重降序编排候选执行链"""
        if require_vision is None:
            require_vision = self._detect_requires_vision(messages)

        filtered_fallbacks = [
            f for f in self.config.fallbacks
            if f.is_active and (not require_vision or f.supports_vision)
        ]
        filtered_fallbacks.sort(key=lambda x: x.weight, reverse=True)

        if model or base_url or api_key:
            eff_url = base_url or self.config.primary.base_url
            try:
                eff_provider = derive_provider_from_url(eff_url)
            except Exception:
                eff_provider = self.config.primary.provider or "managed"

            caller_supports_vision = require_vision or any(
                k in (model or "").lower() for k in ("vl", "grok", "vision", "4o")
            )
            caller_node = ModelNodeConfig(
                name="caller-override",
                model=model or self.config.primary.model,
                base_url=eff_url,
                api_key=api_key or "",
                provider=eff_provider,
                backend=backend or self.config.primary.backend,
                weight=999,
                is_active=True,
                supports_vision=caller_supports_vision
            )
            return [caller_node] + [f for f in filtered_fallbacks if f.name != caller_node.name]

        candidates: List[ModelNodeConfig] = []
        if self.config.primary.is_active and (not require_vision or self.config.primary.supports_vision):
            candidates.append(self.config.primary)
        candidates.extend([f for f in filtered_fallbacks if f.name != self.config.primary.name])
        return candidates

    async def _handle_node_failure_async(
        self,
        req_id: str,
        node: ModelNodeConfig,
        idx: int,
        candidate_nodes: List[ModelNodeConfig],
        total_ms: int,
        exc: Exception,
        err_category: str
    ) -> None:
        """统一处理异步链路节点失败：释放许可、更新状态机、写入审计、推送告警、判断是否 Fail-Fast 或记录切换"""
        immediate_trip = (err_category == "ERR_UPSTREAM_QUOTA" and is_fatal_quota_exhausted(exc))
        self.health_tracker.release_execution_permit(node.name)
        self.health_tracker.record_failure(node.name, total_ms, err_category, immediate_trip=immediate_trip)

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
            sent = await trigger_webhook_alert(
                node_name=node.name,
                err_category=err_category,
                detail=str(exc),
                error_rate=err_rate,
                avg_latency=avg_lat,
                p95_latency=p95_lat,
                webhook_url=self.config.alert_webhook_url
            )
            if sent:
                self.health_tracker.mark_alert_sent(node.name)

        if err_category in ("ERR_SECURITY", "ERR_BAD_REQUEST"):
            raise exc

        if idx < len(candidate_nodes) - 1:
            next_node = candidate_nodes[idx + 1]
            logger.warning(f"[{req_id}][LLM_FAILOVER] 毫秒级热切换: [{node.name}] -> [{next_node.name}] (原因: {err_category})")
            await record_audit_log(
                action="LLM_FAILOVER",
                resource_id=f"{node.name}->{next_node.name}",
                status="SWITCHED",
                details={"from_node": node.name, "to_node": next_node.name, "reason": err_category}
            )

    def _handle_node_failure_sync(
        self,
        node: ModelNodeConfig,
        idx: int,
        candidate_nodes: List[ModelNodeConfig],
        total_ms: int,
        exc: Exception,
        err_category: str
    ) -> None:
        """统一处理同步/工作线程链路节点失败：跨线程安全落库与同步 Webhook 推送 + 频控冷却标记"""
        immediate_trip = (err_category == "ERR_UPSTREAM_QUOTA" and is_fatal_quota_exhausted(exc))
        self.health_tracker.release_execution_permit(node.name)
        self.health_tracker.record_failure(node.name, total_ms, err_category, immediate_trip=immediate_trip)

        logger.error(f"[LLM_FAILOVER] 节点 [{node.name}] 同步调用故障 ({err_category}): {exc} | 耗时: {total_ms}ms")

        record_audit_log_sync(
            action="LLM_INFERENCE",
            resource_id=node.name,
            duration_ms=total_ms,
            status="FAILED",
            error_msg=str(exc),
            details={"error_category": err_category, "model": node.model}
        )

        if self.health_tracker.should_alert(node.name):
            err_rate, avg_lat, p95_lat = self.health_tracker.get_sla_stats(node.name)
            sent = trigger_webhook_alert_sync(
                node_name=node.name,
                err_category=err_category,
                detail=str(exc),
                error_rate=err_rate,
                avg_latency=avg_lat,
                p95_latency=p95_lat,
                webhook_url=self.config.alert_webhook_url
            )
            if sent:
                self.health_tracker.mark_alert_sent(node.name)

        if err_category in ("ERR_SECURITY", "ERR_BAD_REQUEST"):
            raise exc

        if idx < len(candidate_nodes) - 1:
            next_node = candidate_nodes[idx + 1]
            logger.warning(f"[LLM_FAILOVER] 同步调用热切换: [{node.name}] -> [{next_node.name}] (原因: {err_category})")
            record_audit_log_sync(
                action="LLM_FAILOVER",
                resource_id=f"{node.name}->{next_node.name}",
                status="SWITCHED",
                details={"from_node": node.name, "to_node": next_node.name, "reason": err_category}
            )

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
        require_vision = self._detect_requires_vision(messages)
        candidate_nodes = self._get_candidate_nodes(messages, model, base_url, api_key, backend, require_vision=require_vision)
        if not candidate_nodes:
            raise RuntimeError("【LLM 网关灾难性故障】未匹配到任何满足模态与活跃条件的模型候选节点！")

        req_id = get_request_id()
        scope = "model:vlm" if require_vision else "model:llm"
        last_error: Optional[Exception] = None

        for idx, node in enumerate(candidate_nodes):
            if not self.health_tracker.acquire_execution_permit(node.name):
                logger.warning(f"[{req_id}][LLMGateway] 节点 [{node.name}] 处于熔断冷却或半开探测受阻，自动跳过。")
                continue

            node_retried = False
            while True:
                t0 = time.perf_counter()
                first_token_time: Optional[float] = None
                token_yielded = False

                try:
                    logger.info(f"[{req_id}][LLMGateway] 尝试由节点 [{node.name} ({node.model})] 执行流式推理...")
                    client = self.get_client()
                    endpoint, headers, payload, eff_backend = await self._prepare_request_async(
                        messages=messages,
                        model=node.model,
                        base_url=node.base_url,
                        api_key=node.api_key,
                        temperature=temperature,
                        stream=True,
                        backend=node.backend,
                        provider=node.provider,
                        scope=scope,
                        **kwargs
                    )

                    ttft_timeout = getattr(self.config, "ttft_timeout_seconds", 12.0)
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
                                try:
                                    event = await asyncio.wait_for(stream_iter.__anext__(), timeout=ttft_timeout)
                                except asyncio.TimeoutError:
                                    raise httpx.ReadTimeout(f"首字响应超时 (TTFT > {ttft_timeout}s)，判定上游节点假死")
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
                        details={"model": node.model, "ttft_ms": ttft_ms, "backend": eff_backend, "failover": idx > 0}
                    )
                    return

                except Exception as exc:
                    last_error = exc
                    total_ms = int((time.perf_counter() - t0) * 1000)
                    err_category = classify_error(exc)

                    if token_yielded:
                        self.health_tracker.release_execution_permit(node.name)
                        self.health_tracker.record_failure(node.name, total_ms, err_category)
                        raise RuntimeError(f"流式输出中途传输中断: {exc}") from exc

                    if not node_retried and err_category == "ERR_UPSTREAM_OUTAGE":
                        node_retried = True
                        logger.warning(f"[{req_id}][LLMGateway] 节点 [{node.name}] 遇到瞬态故障 ({exc})，执行 250ms 极速重试...")
                        await asyncio.sleep(0.25)
                        continue

                    await self._handle_node_failure_async(req_id, node, idx, candidate_nodes, total_ms, exc, err_category)
                    break

        raise RuntimeError(f"【LLM 网关灾难性故障】所有候选模型均不可用或处于熔断隔离！最后错误: {last_error}")

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
        require_vision = self._detect_requires_vision(messages)
        candidate_nodes = self._get_candidate_nodes(messages, model, base_url, api_key, backend, require_vision=require_vision)
        if not candidate_nodes:
            raise RuntimeError("【LLM 网关灾难性故障】未匹配到任何满足模态与活跃条件的模型候选节点！")

        req_id = get_request_id()
        scope = "model:vlm" if require_vision else "model:llm"
        last_error: Optional[Exception] = None

        for idx, node in enumerate(candidate_nodes):
            if not self.health_tracker.acquire_execution_permit(node.name):
                logger.warning(f"[{req_id}][LLMGateway] 节点 [{node.name}] 处于熔断隔离中，自动跳过。")
                continue

            node_retried = False
            while True:
                t0 = time.perf_counter()
                try:
                    client = self.get_client()
                    endpoint, headers, payload, eff_backend = await self._prepare_request_async(
                        messages=messages,
                        model=node.model,
                        base_url=node.base_url,
                        api_key=node.api_key,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        stream=False,
                        backend=node.backend,
                        provider=node.provider,
                        scope=scope,
                        **kwargs
                    )

                    non_stream_timeout = getattr(self.config, "non_stream_timeout_seconds", 25.0)
                    resp = await client.post(endpoint, headers=headers, json=payload, timeout=non_stream_timeout)
                    if resp.status_code != 200:
                        logger.error(f"[{req_id}][LLMGateway] HTTP {resp.status_code} 调用失败 ({eff_backend}): {resp.text}")
                        resp.raise_for_status()

                    content = self._extract_content(resp.json(), eff_backend)
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

                    if not node_retried and err_category == "ERR_UPSTREAM_OUTAGE":
                        node_retried = True
                        logger.warning(f"[{req_id}][LLMGateway] 节点 [{node.name}] 瞬态异常 ({exc})，执行 250ms 重试...")
                        await asyncio.sleep(0.25)
                        continue

                    await self._handle_node_failure_async(req_id, node, idx, candidate_nodes, total_ms, exc, err_category)
                    break

        raise RuntimeError(f"【LLM 网关灾难性故障】所有候选模型均不可用或处于熔断隔离！最后错误: {last_error}")

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
        require_vision = self._detect_requires_vision(messages)
        candidate_nodes = self._get_candidate_nodes(messages, model, base_url, api_key, backend, require_vision=require_vision)
        if not candidate_nodes:
            raise RuntimeError("【LLM 网关灾难性故障】未匹配到任何满足模态与活跃条件的模型候选节点！")

        scope = "model:vlm" if require_vision else "model:llm"
        last_error: Optional[Exception] = None

        for idx, node in enumerate(candidate_nodes):
            if not self.health_tracker.acquire_execution_permit(node.name):
                logger.warning(f"[LLMGateway] 节点 [{node.name}] 处于熔断冷却中，自动跳过。")
                continue

            node_retried = False
            while True:
                t0 = time.perf_counter()
                try:
                    client = self.get_sync_client()
                    eff_api_key = node.api_key
                    eff_base_url = node.base_url
                    if not eff_api_key:
                        from rag_kb.core.model_credentials import get_credential_sync
                        cred = get_credential_sync(node.provider or "kedaya", scope=scope)
                        eff_api_key = cred.api_key
                        eff_base_url = cred.base_url or eff_base_url

                    endpoint, headers, payload, eff_backend = self._build_protocol_request(
                        messages=messages,
                        final_model=node.model,
                        final_base_url=eff_base_url.rstrip("/"),
                        final_api_key=eff_api_key,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        stream=False,
                        backend=node.backend,
                        **kwargs
                    )

                    non_stream_timeout = getattr(self.config, "non_stream_timeout_seconds", 25.0)
                    resp = client.post(endpoint, headers=headers, json=payload, timeout=non_stream_timeout)
                    if resp.status_code != 200:
                        logger.error(f"[LLMGateway] HTTP {resp.status_code} 同步调用失败 ({eff_backend}): {resp.text}")
                        resp.raise_for_status()

                    content = self._extract_content(resp.json(), eff_backend)
                    total_ms = int((time.perf_counter() - t0) * 1000)

                    self.health_tracker.record_success(node.name, total_ms)
                    logger.info(f"[LLM_INFERENCE] 节点 [{node.name}] 同步调用成功 | 耗时: {total_ms}ms")

                    record_audit_log_sync(
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

                    if not node_retried and err_category == "ERR_UPSTREAM_OUTAGE":
                        node_retried = True
                        logger.warning(f"[LLMGateway] 节点 [{node.name}] 同步瞬态异常 ({exc})，执行 250ms 重试...")
                        time.sleep(0.25)
                        continue

                    self._handle_node_failure_sync(node, idx, candidate_nodes, total_ms, exc, err_category)
                    break

        raise RuntimeError(f"【LLM 网关灾难性故障】所有候选模型均不可用或处于熔断隔离！最后错误: {last_error}")


# 全局单例导出
llm_gateway = ResilientLLMGateway()
