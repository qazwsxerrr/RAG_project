"""src/rag_kb/core/llm/client.py —— 企业级多 Agent 统一 LLM 网关门面。

职责：
1. 统一管理底层 HTTP 连接池与生命周期；
2. 严格贯彻 Fail-Fast 原则（4xx 鉴权/参数错误立即快速失败）；
3. 集成统一瞬态重试策略（网络波动与 5xx/429 指数退避重试）；
4. 提供标准非流式 chat()、流式打字机 stream_chat() 与强类型结构化 chat_json()；
5. 同时支持纯文本 Agent 与多模态 VLM 视觉大模型调用。
"""

import json
import logging
from typing import AsyncGenerator, Dict, Any, List, Optional, Tuple, Type, TypeVar
from pydantic import BaseModel
import httpx

from rag_kb.core.config import settings
from rag_kb.core.resilience.retry import retry_on_transient
from rag_kb.core.llm.stream_parser import parse_sse_stream

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
        **kwargs
    ) -> Tuple[str, Dict[str, str], Dict[str, Any]]:
        """请求入参归一化与鉴权凭据检查 (严格 Fail-Fast)"""
        final_api_key = api_key if api_key is not None else settings.active_llm_api_key
        final_base_url = (base_url if base_url is not None else settings.active_llm_base_url).rstrip("/")
        final_model = model if model is not None else settings.active_llm_model

        if not final_api_key or final_api_key.startswith("your_") or "*" in final_api_key:
            raise RuntimeError("【LLM 网关致命错误】未配置有效的大模型 API_KEY！(Fail-Fast)")

        endpoint = f"{final_base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {final_api_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": final_model,
            "messages": messages,
            "temperature": temperature,
            "stream": stream,
            **kwargs
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens

        return endpoint, headers, payload

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
        **kwargs
    ) -> str:
        """
        通用非流式调用：支持文本问答、查询改写、HyDE 假想文档以及 VLM 多模态图文调用。
        """
        client = self.get_client()
        endpoint, headers, payload = self._prepare_request(
            messages=messages,
            model=model,
            base_url=base_url,
            api_key=api_key,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=False,
            **kwargs
        )

        resp = await client.post(endpoint, headers=headers, json=payload)
        if resp.status_code != 200:
            err_msg = resp.text
            logger.error(f"[LLMGateway] HTTP {resp.status_code} 调用失败: {err_msg}")
            resp.raise_for_status()

        data = resp.json()
        choices = data.get("choices")
        if not choices:
            raise RuntimeError(f"【LLM 异常响应】服务返回未包含 choices 节点: {data}")

        content = choices[0].get("message", {}).get("content", "")
        return content or ""

    @retry_on_transient(service_name="LLMGateway.chat_sync")
    def chat_sync(
        self,
        messages: List[Dict[str, Any]],
        model: Optional[str] = None,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        temperature: float = 0.2,
        max_tokens: Optional[int] = None,
        **kwargs
    ) -> str:
        """
        同步非流式通用调用：用于同步流水线节点、离线切图解析以及多模态 VLM 同步服务调用。
        """
        client = self.get_sync_client()
        endpoint, headers, payload = self._prepare_request(
            messages=messages,
            model=model,
            base_url=base_url,
            api_key=api_key,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=False,
            **kwargs
        )

        resp = client.post(endpoint, headers=headers, json=payload)
        if resp.status_code != 200:
            err_msg = resp.text
            logger.error(f"[LLMGateway] HTTP {resp.status_code} 同步调用失败: {err_msg}")
            resp.raise_for_status()

        data = resp.json()
        choices = data.get("choices")
        if not choices:
            raise RuntimeError(f"【LLM 异常响应】服务返回未包含 choices 节点: {data}")

        content = choices[0].get("message", {}).get("content", "")
        return content or ""

    # ==================== 2. 流式打字机分流调用 ====================
    async def stream_chat(
        self,
        messages: List[Dict[str, Any]],
        model: Optional[str] = None,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        temperature: float = 0.2,
        max_tokens_budget: int = 4096,
        **kwargs
    ) -> AsyncGenerator[Tuple[str, str], None]:
        """
        通用流式分流调用：
        - 自动解耦 reasoning (思考流) 与 message (回答正文)；
        - 内置单次请求 Token 预算熔断，防止死循环或成本 DoS。
        """
        client = self.get_client()
        endpoint, headers, payload = self._prepare_request(
            messages=messages,
            model=model,
            base_url=base_url,
            api_key=api_key,
            temperature=temperature,
            stream=True,
            **kwargs
        )

        async with client.stream("POST", endpoint, headers=headers, json=payload) as resp:
            if resp.status_code != 200:
                err_text = await resp.aread()
                raise RuntimeError(
                    f"【LLM 流式调用异常】HTTP {resp.status_code}: {err_text.decode(errors='ignore')}"
                )

            token_counter = 0
            async for event_type, token in parse_sse_stream(resp):
                token_counter += 1
                if token_counter > max_tokens_budget:
                    logger.warning(f"[LLMGateway] 达到单次请求 Token 预算上限 ({max_tokens_budget})，主动阻断流式响应！")
                    break
                yield (event_type, token)

    # ==================== 3. 强类型结构化 JSON 输出调用 ====================
    async def chat_json(
        self,
        messages: List[Dict[str, Any]],
        schema: Type[T],
        model: Optional[str] = None,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        temperature: float = 0.1,
        **kwargs
    ) -> T:
        """
        强类型结构化输出调用：用于 Agent 意图识别、参数决策、工具调用。
        自动注入 JSON Schema 约束，直接返回校验后的 Pydantic 对象实例。
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
            response_format={"type": "json_object"},
            **kwargs
        )

        # 剥离可能存在的 markdown 代码块包裹 (如 ```json ... ```)
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
