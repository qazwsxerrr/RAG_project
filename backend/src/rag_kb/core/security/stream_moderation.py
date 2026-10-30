"""src/rag_kb/core/security/stream_moderation.py —— 流式输出敏感词熔断、核心凭据守卫与隐私脱敏安全管道。

核心架构：
1. 【P0 核心凭据守卫 (Credential Guard)】：毫秒级嗅探云凭据 (AK/SK)、私钥、带密数据库连接串，命中即刻物理熔断掐断推流；
2. 【P0 合规红线熔断 (Circuit Breaker)】：Aho-Corasick 高效嗅探暴恐、涉毒、极端反动政治红线，命中坚决拒答截断；
3. 【P1 隐私数据动态掩码 (PII Masking)】：通过自适应前瞻微缓冲机制，将手机号 (138****5678)、身份证 (110101********2345)、邮箱、银行卡号实时星号掩码，确保有害内容 0% 漏向前端，兼顾正常打字机的流畅体验。
"""

import logging
from typing import AsyncGenerator, Optional, Tuple
from rag_kb.core.security.aho_corasick import default_ac_automaton, AhoCorasickAutomaton
from rag_kb.core.security.credential_guard import detect_credential_leak, CREDENTIAL_FUSE_MESSAGE
from rag_kb.core.security.pii_masker import StreamingPIIMasker, mask_all_pii

logger = logging.getLogger(__name__)

# 安全熔断截断提示语
FUSE_BREAKER_MESSAGE = "\n\n【系统安全拦截】检测到输出内容包含违规敏感信息，已强制终止响应！"

class StreamModerator:
    """流式安全合规、凭据泄漏熔断与 PII 隐私脱敏复合管道"""

    def __init__(
        self,
        automaton: Optional[AhoCorasickAutomaton] = None,
        buffer_window: int = 36,
    ):
        self.automaton = automaton or default_ac_automaton
        self.buffer_window = buffer_window

    def moderate_text(self, text: str) -> str:
        """
        非流式纯文本一次性安全合规检测与脱敏：
        1. 优先嗅探 P0 核心系统凭据与私钥 -> 命中则直接拒答；
        2. 嗅探 P0 违禁敏感词 -> 命中则直接拒答；
        3. 对合规文本执行 PII 隐私数据掩码 (手机号、身份证、邮箱、银行卡自动打码)。
        """
        if not text:
            return ""

        # 1. 嗅探核心凭据泄漏
        cred_leak = detect_credential_leak(text)
        if cred_leak:
            logger.critical(f"非流式输出检测到核心系统凭据泄漏 ({cred_leak})，触发物理拒答拦截！")
            return "【系统安全拦截】检测到内容包含企业核心系统密钥或连接凭据，已拒绝输出！"

        # 2. 嗅探 P0 暴恐、涉毒、极端政治敏感词
        if self.automaton.contains_any(text):
            hits = [m[2] for m in self.automaton.search(text)]
            logger.warning(f"非流式输出检测到违规敏感词: {hits}，触发即时拒答拦截！")
            return "【系统安全拦截】检测到内容包含违规敏感信息，已拒绝输出！"

        # 3. 执行 P1 隐私数据脱敏替换
        return mask_all_pii(text)

    async def filter_token_stream(
        self,
        token_stream: AsyncGenerator[str, None]
    ) -> AsyncGenerator[str, None]:
        """
        单流 Token 透明脱敏与熔断过滤管道
        """
        buffer = ""
        pii_masker = StreamingPIIMasker()

        async for chunk in token_stream:
            if not chunk:
                continue

            buffer += chunk

            # 超出滑动安全窗口时进行前置嗅探
            if len(buffer) > self.buffer_window:
                split_pos = len(buffer) - self.buffer_window
                head_part = buffer[:split_pos]
                buffer = buffer[split_pos:]

                combined_probe = head_part + buffer

                # 1. 嗅探 P0 凭据泄露
                cred_leak = detect_credential_leak(combined_probe)
                if cred_leak:
                    logger.critical(f"流式输出触发系统凭据熔断 ({cred_leak})！强制截断模型生成...")
                    yield CREDENTIAL_FUSE_MESSAGE
                    return

                # 2. 嗅探 P0 违规敏感词
                if self.automaton.contains_any(combined_probe):
                    hits = [m[2] for m in self.automaton.search(combined_probe)]
                    logger.warning(f"流式输出触发安全熔断！命中违规词: {hits}，正在强制截断模型生成...")
                    yield FUSE_BREAKER_MESSAGE
                    return

                # 3. 头部确认安全，送入 PII 掩码前瞻管道
                if head_part:
                    masked_output = pii_masker.process_chunk(head_part)
                    if masked_output:
                        yield masked_output

        # 流正常结束时，冲刷剩余缓冲区尾部
        if buffer:
            cred_leak = detect_credential_leak(buffer)
            if cred_leak:
                logger.critical(f"流尾冲刷检测到系统凭据泄露 ({cred_leak})，触发截断！")
                yield CREDENTIAL_FUSE_MESSAGE
                return

            if self.automaton.contains_any(buffer):
                hits = [m[2] for m in self.automaton.search(buffer)]
                logger.warning(f"流尾冲刷检测到违规敏感词: {hits}，触发即时截断！")
                yield FUSE_BREAKER_MESSAGE
                return

            masked_tail = pii_masker.process_chunk(buffer)
            if masked_tail:
                yield masked_tail

        # 冲刷 PII 缓冲区中未释放的数字
        final_flush = pii_masker.flush()
        if final_flush:
            yield final_flush

    async def filter_chat_stream(
        self,
        chat_stream: AsyncGenerator[Tuple[str, str], None]
    ) -> AsyncGenerator[Tuple[str, str], None]:
        """
        双流 (reasoning 与 message) 智能流式安全防护与脱敏管道：
        - reasoning 推理思考流实时原样透传；
        - message 答案流：
          1. 滑动微窗口探测 P0 凭据外泄与违规红线 -> 命中即物理断流；
          2. 安全流经 PII 掩码缓冲器 -> 输出星号遮盖后的脱敏答案。
        """
        buffer = ""
        pii_masker = StreamingPIIMasker()

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

                    # 1. 嗅探 P0 核心系统凭据
                    cred_leak = detect_credential_leak(combined_probe)
                    if cred_leak:
                        logger.critical(f"问答答案流触发系统凭据熔断 ({cred_leak})！强制终止大模型推流！")
                        yield ("message", CREDENTIAL_FUSE_MESSAGE)
                        return

                    # 2. 嗅探 P0 违规红线敏感词
                    if self.automaton.contains_any(combined_probe):
                        hits = [m[2] for m in self.automaton.search(combined_probe)]
                        logger.warning(f"问答答案流触发安全熔断！命中违规词: {hits}，强制终止大模型推流！")
                        yield ("message", FUSE_BREAKER_MESSAGE)
                        return

                    # 3. 头部送入 PII 掩码管道
                    if head_part:
                        masked_output = pii_masker.process_chunk(head_part)
                        if masked_output:
                            yield ("message", masked_output)

        if buffer:
            cred_leak = detect_credential_leak(buffer)
            if cred_leak:
                logger.critical(f"答案流尾部冲刷命中凭据泄露 ({cred_leak})，触发截断！")
                yield ("message", CREDENTIAL_FUSE_MESSAGE)
                return

            if self.automaton.contains_any(buffer):
                hits = [m[2] for m in self.automaton.search(buffer)]
                logger.warning(f"答案流尾部冲刷命中敏感词: {hits}，触发安全截断！")
                yield ("message", FUSE_BREAKER_MESSAGE)
                return

            masked_tail = pii_masker.process_chunk(buffer)
            if masked_tail:
                yield ("message", masked_tail)

        final_flush = pii_masker.flush()
        if final_flush:
            yield ("message", final_flush)

default_stream_moderator = StreamModerator()

async def moderate_stream(token_stream: AsyncGenerator[str, None]) -> AsyncGenerator[str, None]:
    """快捷流式过滤管道入口"""
    async for safe_token in default_stream_moderator.filter_token_stream(token_stream):
        yield safe_token
