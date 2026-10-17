"""src/rag_kb/core/security/normalizer.py —— 文本安全预归一化与对抗变体消除器。

用于防御黑产与恶意攻击者通过以下手段绕过敏感词检测：
1. 插入零宽字符 (Zero-Width Characters，如 \u200b, \u200c, \ufeff)；
2. 插入特殊符号插空干扰 (如 色*情, 色_情, 色 情, 炸~药)；
3. 全角字符与全角半角混用 (如 ｓｅｑｉｎｇ, ＡＰＩ)；
4. 多种 Unicode 变体字符标准化 (NFKC 标准化)。
"""

import re
import unicodedata

# 匹配零宽字符、不可见控制字符
ZERO_WIDTH_PATTERN = re.compile(r"[\u200b-\u200f\u202a-\u202e\ufeff\x00-\x1f\x7f]")

# 匹配常见插空混淆标点与空白字符 (仅保留中文、英文字母、数字)
CONFUSABLE_PUNCT_PATTERN = re.compile(r"[ *_\-.~#@!$%^&*()+=\[\]{}|\\:;\"'<>,?/，。！？；：、“”‘’（）《》【】·~—]")

def normalize_text(text: str) -> str:
    """
    通用文本标准化：
    - NFKC 规范化 (全角转半角，如 Ａ->A, １->1)
    - 消除不可见零宽字符与隐蔽控制符
    """
    if not text:
        return ""
    # 1. NFKC 标准化 (处理全角半角及兼容字符)
    normalized = unicodedata.normalize("NFKC", text)
    # 2. 剥离零宽与隐藏控制字符
    cleaned = ZERO_WIDTH_PATTERN.sub("", normalized)
    return cleaned

def clean_confusable_variant(text: str) -> str:
    """
    提取纯净连续语义串 (去除中间插入的空格与干扰符号)：
    用于对抗 '色*情'、'色_情'、'色 情' 等符号插空绕过。
    """
    if not text:
        return ""
    base_clean = normalize_text(text)
    # 剔除干扰标点与空格，紧凑拼接
    compact = CONFUSABLE_PUNCT_PATTERN.sub("", base_clean)
    return compact
