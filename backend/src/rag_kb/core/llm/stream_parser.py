"""src/rag_kb/core/llm/stream_parser.py —— 双协议自适应 SSE 流式打字机解析与思考流解耦引擎。

功能：
1. 原生支持新一代 Responses 协议 (POST /v1/responses) 的事件驱动 SSE 流:
   - 监听 response.reasoning_summary_text.delta -> 思考流专用通道
   - 监听 response.output_text.delta -> 正式回答专用通道 (100% 纯净，零标签污染)
2. 兼容传统 Chat Completions 协议 (POST /v1/chat/completions) 的 chunk 流:
   - 提取 choices[0].delta.reasoning_content 独立字段
   - 正文流中的 <think>...</think> 标签状态机切分
3. 向上层分发统一规范的 (event_type, token) 二元组：
   - event_type 为 'reasoning' (思考流) 或 'message' (正文流)。
"""

import json
from typing import AsyncGenerator, Tuple
import httpx

async def parse_sse_stream(response: httpx.Response) -> AsyncGenerator[Tuple[str, str], None]:
    """
    全自动双协议自适应 SSE 流式解析器。
    
    :param response: httpx.Response 流式响应对象
    :return: 异步生成器，依次产生 (event_type, token)
             event_type: 'reasoning'（深度思考/推理） | 'message'（最终回答正文）
    """
    in_think = False
    seen_think = False
    buffer = ""

    async for line in response.aiter_lines():
        line = line.strip()
        if not line or not line.startswith("data:"):
            continue

        raw = line[5:].strip()
        if raw == "[DONE]":
            break

        try:
            chunk = json.loads(raw)
        except Exception:
            continue

        # ==============================================================================
        # 分支 1：新一代 Responses 协议 (Grok / OpenAI 原生事件驱动，零标签污染)
        # ==============================================================================
        chunk_type = chunk.get("type", "")
        if chunk_type and isinstance(chunk_type, str) and chunk_type.startswith("response."):
            # 1.1 深度思考增量帧
            if chunk_type in ("response.reasoning_summary_text.delta", "response.reasoning_text.delta"):
                delta = chunk.get("delta", "")
                if delta:
                    yield ("reasoning", delta)
                continue

            # 1.2 正式回答打字机增量帧
            if chunk_type == "response.output_text.delta":
                delta = chunk.get("delta", "")
                if delta:
                    yield ("message", delta)
                continue

            # 其余事件 (如 response.created, output_item.added, completed 等) 为生命周期帧，安全忽略
            continue

        # ==============================================================================
        # 分支 2：传统 Chat Completions 协议 (OpenAI 经典 / DeepSeek / 通用兼容)
        # ==============================================================================
        choices = chunk.get("choices")
        if not choices:
            continue

        delta = choices[0].get("delta", {})

        # 2.1 显式 reasoning_content 字段 (DeepSeek 官方 / 硅基流动等)
        reasoning_token = delta.get("reasoning_content", "")
        if reasoning_token:
            yield ("reasoning", reasoning_token)

        # 2.2 提取正文内容并走 <think> 标签状态机
        raw_token = delta.get("content", "")
        if not raw_token:
            continue

        # 极速快道：如果已经完成思考闭合，后续内容 100% 为正式答案，直接直通推流无任何开销
        if seen_think and not in_think:
            yield ("message", raw_token)
            continue

        buffer += raw_token

        while buffer:
            if not in_think:
                if "<think>" in buffer:
                    before, after = buffer.split("<think>", 1)
                    if before:
                        yield ("message", before)
                    in_think = True
                    buffer = after
                elif "<" in buffer:
                    idx = buffer.find("<")
                    prefix_cand = buffer[idx:]
                    if "<think>".startswith(prefix_cand):
                        if idx > 0:
                            yield ("message", buffer[:idx])
                            buffer = prefix_cand
                        break
                    else:
                        yield ("message", buffer[:idx + 1])
                        buffer = buffer[idx + 1:]
                else:
                    yield ("message", buffer)
                    buffer = ""
            else:
                # 处于 <think> 思考块内部
                if "</think>" in buffer:
                    thought_part, after = buffer.split("</think>", 1)
                    if thought_part:
                        yield ("reasoning", thought_part)
                    in_think = False
                    seen_think = True
                    buffer = after.lstrip("\r\n")
                elif "</" in buffer:
                    idx = buffer.rfind("</")
                    prefix_cand = buffer[idx:]
                    if "</think>".startswith(prefix_cand):
                        if idx > 0:
                            yield ("reasoning", buffer[:idx])
                            buffer = prefix_cand
                        break
                    else:
                        yield ("reasoning", buffer[:idx + 2])
                        buffer = buffer[idx + 2:]
                else:
                    yield ("reasoning", buffer)
                    buffer = ""

    # 流结束时冲刷残余 buffer (仅对 Chat Completions 分支有效)
    if buffer:
        evt = "reasoning" if in_think else "message"
        yield (evt, buffer)
