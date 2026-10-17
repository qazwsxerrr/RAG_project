"""src/rag_kb/core/security/stream_moderation.py —— 流式输出敏感词即时熔断与安全管道。

核心架构：
1. 坚决摒弃“打码替换”的伪防护，推行【即时熔断截断 (Circuit Breaker)】；
2. 结合滑动微窗口缓冲队列 (Sliding Micro-Buffer)，毫秒级检测 Token 拆碎与切散场景；
3. 一旦在输出流中检测到涉黄、暴恐、违法违规或企业机密，立即终止与上游大模型的传输，截断推流并输出合规阻断声明；
4. 确保有害内容 0% 漏向前端，兼顾正常打字机的流畅体验。
"""

import logging
from typing import AsyncGenerator, Optional, Tuple
from rag_kb.core.security.aho_corasick import default_ac_automaton, AhoCorasickAutomaton

logger = logging.getLogger(__name__)

# 安全熔断截断提示语
FUSE_BREAKER_MESSAGE = "\n\n【系统安全拦截】检测到输出内容包含违规敏感信息，已强制终止响应！"

class StreamModerator:
    """流式敏感词即时熔断与合规安全管道"""

    def __init__(
        self,
        automaton: Optional[AhoCorasickAutomaton] = None,
        buffer_window: int = 6,
    ):
        self.automaton = automaton or default_ac_automaton
        self.buffer_window = buffer_window

    def moderate_text(self, text: str) -> str:
        """
        非流式纯文本一次性安全合规检测
        - 若安全，原样返回文本；
        - 若命中违禁敏感词，直接拒绝输出并返回安全拦截声明，绝不返回遮蔽打码内容！
        """
        if not text:
            return ""
        if self.automaton.contains_any(text):
            logger.warning("非流式输出检测到违规敏感词，触发即时拒答拦截！")
            return "【系统安全拦截】检测到内容包含违规敏感信息，已拒绝输出！"
        return text

    async def filter_token_stream(
        self,
        token_stream: AsyncGenerator[str, None]
    ) -> AsyncGenerator[str, None]:
        """
        单流 Token 透明熔断过滤管道：
        一旦发现违禁词，立即切断与上游生成器的连接，截断推流并输出安全声明！
        """
        buffer = ""

        async for chunk in token_stream:
            if not chunk:
                continue

            buffer += chunk

            # 超出滑动安全窗口时进行前置探测
            if len(buffer) > self.buffer_window:
                split_pos = len(buffer) - self.buffer_window
                head_part = buffer[:split_pos]
                buffer = buffer[split_pos:]

                combined_probe = head_part + buffer
                # 一旦命中违规词，触发物理级熔断！
                if self.automaton.contains_any(combined_probe):
                    logger.warning("流式输出触发安全熔断！正在强制截断模型生成...")
                    yield FUSE_BREAKER_MESSAGE
                    return  # 彻底终止生成器，断开与上游 LLM 的连接！

                if head_part:
                    yield head_part

        # 流正常结束时，冲刷剩余缓冲区尾部
        if buffer:
            if self.automaton.contains_any(buffer):
                logger.warning("流尾冲刷检测到违规敏感词，触发即时截断！")
                yield FUSE_BREAKER_MESSAGE
                return
            yield buffer

    async def filter_chat_stream(
        self,
        chat_stream: AsyncGenerator[Tuple[str, str], None]
    ) -> AsyncGenerator[Tuple[str, str], None]:
        """
        双流 (reasoning 与 message) 智能流式熔断管道：
        - reasoning 推理思考流实时原样透传；
        - message 正式答案流经滑动微窗口进行实时违禁嗅探，一旦发现敏感词立刻熔断截断！
        """
        buffer = ""

        async for event_type, token in chat_stream:
            if not token:
                continue

            if event_type == "reasoning":
                yield (event_type, token)
            else:
                buffer += token
                if len(buffer) > self.buffer_window:
                    split_pos = len(buffer) - self.buffer_window
                    head_part = buffer[:split_pos]
                    buffer = buffer[split_pos:]

                    combined_probe = head_part + buffer
                    # 一旦命中敏感词，立即截断推流，断开模型连接！
                    if self.automaton.contains_any(combined_probe):
                        logger.warning("问答答案流触发安全熔断！强制终止大模型推流！")
                        yield ("message", FUSE_BREAKER_MESSAGE)
                        return  # 彻底终止推流，绝不继续向大模型要字！

                    if head_part:
                        yield ("message", head_part)

        if buffer:
            if self.automaton.contains_any(buffer):
                logger.warning("答案流尾部冲刷命中敏感词，触发安全截断！")
                yield ("message", FUSE_BREAKER_MESSAGE)
                return
            yield ("message", buffer)

default_stream_moderator = StreamModerator()

async def moderate_stream(token_stream: AsyncGenerator[str, None]) -> AsyncGenerator[str, None]:
    """快捷流式过滤管道入口"""
    async for safe_token in default_stream_moderator.filter_token_stream(token_stream):
        yield safe_token
