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
from typing import AsyncGenerator, Dict, Any, List, Optional, Tuple, Type, TypeVar
from pydantic import BaseModel
import httpx

from rag_kb.core.config import settings
from rag_kb.core.resilience.retry import retry_on_transient, is_transient_error
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
        """获取或惰性初始化全局共享的 httpx.AsyncClient 客户端长连接池"""
        if self._client is None or self._client.is_closed:
            limits = httpx.Limits(max_keepalive_connections=50, max_connections=100)
            self._client = httpx.AsyncClient(limits=limits, timeout=90.0)
        return self._client

    def get_sync_client(self) -> httpx.Client:
        """获取或惰性初始化全局共享的同步 httpx.Client 客户端长连接池"""
        if self._sync_client is None or self._sync_client.is_closed:
            limits = httpx.Limits(max_keepalive_connections=50, max_connections=100)
            self._sync_client = httpx.Client(limits=limits, timeout=90.0)
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
        **kwargs
    ) -> Tuple[str, Dict[str, str], Dict[str, Any], str]:
        """请求入参归一化与鉴权凭据检查 (支持 Responses 与 Chat Completions 双协议自适应)"""
        final_api_key = api_key if api_key is not None else settings.active_llm_api_key
        final_base_url = (base_url if base_url is not None else settings.active_llm_base_url).rstrip("/")
        final_model = model if model is not None else settings.active_llm_model

        effective_backend = (backend or settings.active_llm_api_backend).strip().lower()
        if effective_backend in ("responses", "response"):
            effective_backend = "responses"
        else:
            effective_backend = "chat_completions"

        if not final_api_key or final_api_key.startswith("your_") or "*" in final_api_key:
            raise RuntimeError("【LLM 网关致命错误】未配置有效的大模型 API_KEY！(Fail-Fast)")

        headers = {
            "Authorization": f"Bearer {final_api_key}",
            "Content-Type": "application/json"
        }

        # 1. 新一代 Responses 协议分支
        if effective_backend == "responses":
            endpoint = f"{final_base_url}/responses"
            sys_msg = next((m.get("content") for m in messages if m.get("role") == "system"), None)
            non_sys_msgs = [m for m in messages if m.get("role") != "system"]

            # 单条纯文本用户提问直接作为 input 字符串；多轮对话作为 input 数组
            if len(non_sys_msgs) == 1 and isinstance(non_sys_msgs[0].get("content"), str):
                input_data = non_sys_msgs[0]["content"]
            elif non_sys_msgs:
                input_data = non_sys_msgs
            else:
                input_data = messages

            payload = {
                "model": final_model,
                "input": input_data,
                "stream": stream,
                **kwargs
            }
            if sys_msg and isinstance(sys_msg, str):
                payload["instructions"] = sys_msg
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
            return ""

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

# 全局单例导出
llm_gateway = LLMGateway()
