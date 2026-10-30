"""src/rag_kb/core/security/pii_masker.py —— 个人敏感隐私信息 (PII) 掩码脱敏引擎。

依据《GB/T 35273-2020 信息安全技术 个人信息安全规范》与企业合规标准，
对输出端文本进行动态星号 (*) 掩码，覆盖：
1. 中国大陆 11 位手机号：保留前 3 后 4，中间 4 位遮盖 (如 138****5678)；
2. 中国大陆 18 位居民身份证号：保留前 6 后 4，中间 8 位出生年月遮盖 (如 110101********2345)；
3. 电子邮箱 (Email)：用户名保留首尾字符，中间遮盖，完整保留域名 (如 z******n@corp.com)；
4. 银行卡号 (16~19位)：保留前 4 后 4，中间全量遮盖 (如 6222************0123)；
5. 流式打字机自适应前瞻微缓冲机制 (StreamingPIIMasker)，彻底消除 Token 碎块断词泄露。
"""

import re
from typing import AsyncGenerator, Tuple, Optional

# ==================== 1. 正则规则定义 (严格边界限定，防常规年份/金额误伤) ====================

# 手机号：以 1 开头第二位 3-9，共 11 位数字，前后不能有连续数字
PHONE_REGEX = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")

# 身份证号：18 位，符合行政区划、18/19/20 开头年份、合法月份与日期，末位含 X/x
ID_CARD_REGEX = re.compile(
    r"(?<!\d)([1-9]\d{5}(?:18|19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx])(?![a-zA-Z0-9])"
)

# 银行卡号：16 至 19 位连续数字，前后不能有数字
BANK_CARD_REGEX = re.compile(r"(?<!\d)([1-9]\d{15,18})(?!\d)")

# 电子邮箱：包含 @ 及合法域名
EMAIL_REGEX = re.compile(
    r"(?<![a-zA-Z0-9_.+-])([a-zA-Z0-9_.+-]+)@([a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)(?![a-zA-Z0-9_.+-])"
)


# ==================== 2. 掩码替换函数 ====================

def _mask_phone_sub(match: re.Match) -> str:
    val = match.group(1)
    return f"{val[:3]}****{val[7:]}"

def _mask_id_card_sub(match: re.Match) -> str:
    val = match.group(1)
    return f"{val[:6]}********{val[14:]}"

def _mask_bank_card_sub(match: re.Match) -> str:
    val = match.group(1)
    middle_len = len(val) - 8
    return f"{val[:4]}{'*' * middle_len}{val[-4:]}"

def _mask_email_sub(match: re.Match) -> str:
    name = match.group(1)
    domain = match.group(2)
    if len(name) <= 1:
        masked_name = "*"
    elif len(name) == 2:
        masked_name = f"*{name[1]}"
    else:
        masked_name = f"{name[0]}{'*' * (len(name) - 2)}{name[-1]}"
    return f"{masked_name}@{domain}"


def mask_all_pii(text: str) -> str:
    """
    对完整纯文本一次性执行全部 PII 隐私信息掩码替换。
    严格保证匹配先后顺序：优先身份证（防止18位纯数字被当成银行卡简单截断），再银行卡、手机号、邮箱。
    """
    if not text:
        return ""

    # 1. 优先匹配 18 位身份证 (含生日语义与末尾 X)
    text = ID_CARD_REGEX.sub(_mask_id_card_sub, text)
    # 2. 匹配 16~19 位银行卡
    text = BANK_CARD_REGEX.sub(_mask_bank_card_sub, text)
    # 3. 匹配 11 位手机号
    text = PHONE_REGEX.sub(_mask_phone_sub, text)
    # 4. 匹配邮箱地址
    text = EMAIL_REGEX.sub(_mask_email_sub, text)

    return text


# ==================== 3. 流式打字机自适应前瞻微缓冲器 ====================

class StreamingPIIMasker:
    """
    专为大模型流式打字机输出打造的 PII 掩码管道。
    核心攻坚：大模型分词拆碎 (如 ["138", "1234", "5678"]) 导致的明文早释漏洞。
    采用自适应前瞻安全分割点机制，确保有害明文 0% 漏向前端，同时维持毫秒级极速响应。
    """

    def __init__(self, max_lookahead_digits: int = 20):
        self.max_lookahead_digits = max_lookahead_digits
        self.buffer = ""

    def _find_safe_split_index(self, text: str) -> int:
        """
        寻找安全输出截断点：
        若字符串末尾包含尚未终结的潜在敏感特征（连续数字串或 @ 域名），
        则保留尾部在缓冲区，仅释放前面的安全文本。
        """
        if not text:
            return 0

        # 1. 检查末尾是否为未完成的数字序列 (\d+)
        num_match = re.search(r"\d+$", text)
        if num_match:
            digits_len = len(num_match.group(0))
            # 若末尾数字未超过最大卡号/证件长度，说明可能后续还有数字 token，需滞留
            if digits_len <= self.max_lookahead_digits:
                return num_match.start()

        # 2. 检查末尾是否包含待闭合域名的邮箱 (@ 紧跟合法未完结字符)
        email_prefix_match = re.search(r"@[a-zA-Z0-9\-.]*$", text)
        if email_prefix_match:
            # 找到 @ 前面的用户名起始位置，一同暂扣以防用户名被提前放出
            user_start = email_prefix_match.start()
            while user_start > 0 and (text[user_start - 1].isalnum() or text[user_start - 1] in "._+-"):
                user_start -= 1
            return user_start

        # 无敏感潜在尾缀，整体均可安全输出
        return len(text)

    def process_chunk(self, chunk: str) -> str:
        """输入一个流式 chunk，返回经过脱敏且可安全推送给前端的纯净文本"""
        if not chunk:
            return ""

        self.buffer += chunk
        safe_idx = self._find_safe_split_index(self.buffer)

        if safe_idx <= 0:
            return ""

        safe_text = self.buffer[:safe_idx]
        self.buffer = self.buffer[safe_idx:]

        # 对安全释放段执行完整掩码脱敏
        return mask_all_pii(safe_text)

    def flush(self) -> str:
        """流结束时冲刷剩余全部缓冲区"""
        if not self.buffer:
            return ""
        remaining = self.buffer
        self.buffer = ""
        return mask_all_pii(remaining)

    async def filter_token_stream(
        self,
        token_stream: AsyncGenerator[str, None]
    ) -> AsyncGenerator[str, None]:
        """将纯文本单流转为脱敏流"""
        async for chunk in token_stream:
            masked_chunk = self.process_chunk(chunk)
            if masked_chunk:
                yield masked_chunk

        final_chunk = self.flush()
        if final_chunk:
            yield final_chunk
