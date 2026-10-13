"""src/rag_kb/core/llm/stream_parser.py —— SSE 流式打字机协议解析与思考流解耦引擎。

功能：
1. 实时读取 HTTP 逐行 SSE 帧并反序列化 JSON；
2. 兼容厂商下发的 reasoning_content 独立字段（如 DeepSeek 官方 / 硅基流动）；
3. 状态机解析正文流中的 <think>...</think> 标签，提供极速快道直通；
4. 向上层分发统一的 (event_type, token) 元组，其中 event_type 为 'reasoning' 或 'message'。
"""

import json
from typing import AsyncGenerator, Tuple
import httpx

async def parse_sse_stream(response: httpx.Response) -> AsyncGenerator[Tuple[str, str], None]:
    """
    统一 SSE 流式打字机解析器。
    
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

        choices = chunk.get("choices")
        if not choices:
            continue

        delta = choices[0].get("delta", {})

        # 1. 优先提取显式 reasoning_content 字段 (DeepSeek 官方 / SiliconFlow 等标准)
        reasoning_token = delta.get("reasoning_content", "")
        if reasoning_token:
            yield ("reasoning", reasoning_token)

        # 2. 提取正文内容并走 <think> 标签状态机
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

    # 流结束时冲刷残余 buffer
    if buffer:
        evt = "reasoning" if in_think else "message"
        yield (evt, buffer)
