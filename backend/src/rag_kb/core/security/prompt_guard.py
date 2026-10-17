"""src/rag_kb/core/security/prompt_guard.py —— 输入侧 Prompt 注入、越狱指令与违规违禁内容前置强拦截。

在 RAG 检索与问答生成前置拦截：
1. 忽略前置系统指令 (Ignore previous instructions)；
2. 刺探系统底层 Prompt (Output your system prompt)；
3. 角色扮演越狱 (DAN 模式 / 越狱破解)；
4. 涉黄、涉暴、涉枪涉爆、违纪违法等 5 万敏感词库前置嗅探；
5. 违规输入毫秒级直接拒答阻断，零浪费模型与检索算力，彻底杜绝有害请求。
"""

import re
import logging
from typing import Tuple, Optional
from rag_kb.core.security.aho_corasick import default_ac_automaton
from rag_kb.core.security.normalizer import normalize_text, clean_confusable_variant

logger = logging.getLogger(__name__)

INJECTION_PATTERNS = [
    # 英文提示词探测与越狱
    re.compile(r"(?i)\bignore\s+(all\s+)?(previous|prior|above|system)\s+(instructions|prompts?)\b"),
    re.compile(r"(?i)\b(show|reveal|display|output|print|repeat|tell\s+me)\s+(your\s+)?(system\s+prompt|system\s+instructions|instructions)\b"),
    re.compile(r"(?i)\b(you\s+are\s+now|act\s+as)\s+(DAN|jailbroken|unfiltered)\b"),
    re.compile(r"(?i)\b(dan|jailbreak)\s*(mode|模式)\b"),
    # 中文越狱与注入探测
    re.compile(r"(忽略|无视|取消|跳过)\s*(你之前|所有|上面的|一切|此前)?\s*(的)?\s*(前置|系统)?\s*(指令|设定|规则|提示|prompt)", re.IGNORECASE),
    re.compile(r"(输出|显示|复述|告诉我|打印|给出)\s*(你?的|系统?的)?\s*(底层|核心|全部)?\s*(System\s*Prompt|系统提示词|预设指令|核心人设|Prompt)", re.IGNORECASE),
    re.compile(r"(从现在开始|进入)?\s*(解除限制|越狱|开发者|DAN)\s*(模式)", re.IGNORECASE),
    re.compile(r"绕过\s*(所有)?\s*(安全审查|内容审查|敏感词过滤|限制)"),
]

def validate_prompt_security(query: str) -> Tuple[bool, Optional[str]]:
    """
    前置检测用户输入是否存在提示词注入、越狱攻击或涉黄涉暴违禁词汇。
    内置零宽字符清洗与对抗性符号插空消除 (如 色*情, 色 情, ｓｅｑｉｎｇ)。
    
    :param query: 用户输入的原始文本
    :return: (is_safe, error_reason)
    """
    if not query or not query.strip():
        return True, None

    cleaned_query = query.strip()
    norm_query = normalize_text(cleaned_query)
    compact_query = clean_confusable_variant(cleaned_query)

    # 1. 检测 Prompt 注入与越狱指令 (对原始串与标准化串分别匹配)
    for q_candidate in (cleaned_query, norm_query, compact_query):
        for pattern in INJECTION_PATTERNS:
            match = pattern.search(q_candidate)
            if match:
                matched_text = match.group(0)
                logger.warning(f"触发输入侧越狱防御拦截: '{matched_text}'")
                return False, f"检测到违规的提示词注入/越狱指令，系统已阻断该请求！"

    # 2. 嗅探 5 万开源敏感词库 (涉黄、涉毒、涉枪涉爆、违纪违法)
    for q_candidate in (cleaned_query, norm_query, compact_query):
        matches = default_ac_automaton.search(q_candidate)
        if matches:
            matched_words = [m[2] for m in matches]
            logger.warning(f"触发输入侧敏感违禁词拦截: {matched_words}")
            return False, "您的问题包含涉黄、暴恐或违法违禁等敏感内容，系统拒绝回答！"

    return True, None
