"""src/rag_kb/utils/tokenize.py —— BM25 中文分词与稀疏特征工具。

用 jieba 搜索引擎模式在 Python 侧切词：cut_for_index() 产出空格分隔词串，供 to_tsvector 建索引；
build_tsquery() 产出 OR 连接的 to_tsquery 表达式。索引侧与查询侧共用同一套切词，保证词形一致。
同时提供归一化稀疏向量计算工具 extract_sparse_vector。
"""

import re
import math
from typing import Dict, List
import jieba

# 注册技术领域高频词，防止专有名词被拆碎
COMMON_DOMAIN_WORDS = ["微服务", "高可用", "大模型", "知识库", "分布式", "中间件", "架构设计"]
for w in COMMON_DOMAIN_WORDS:
    jieba.add_word(w)

# 过滤纯标点符号与特殊字符
STOP_PUNCT_PATTERN = re.compile(r"^[\s!\"#$%&'()*+,-./:;<=>?@\[\\\]^_`{|}~·，。！？；：、“”‘’（）《》【】—…]+$")

def cut_for_index(text: str) -> str:
    """提取空格分隔的中文分词序列，专供 PostgreSQL to_tsvector('simple', text) 写入"""
    if not text or not text.strip():
        return ""

    tokens: List[str] = []
    # 使用搜索引擎模式增强召回细粒度词项
    for w in jieba.cut_for_search(text):
        w_clean = w.strip()
        if not w_clean or STOP_PUNCT_PATTERN.match(w_clean):
            continue
        # 移除 PostgreSQL tsvector 可能引起解析异常的单引号
        tokens.append(w_clean.replace("'", ""))

    return " ".join(tokens)

def build_tsquery(query: str) -> str:
    """将用户检索短语切分为通过 '|' (OR) 连接的 to_tsquery 表达式"""
    if not query or not query.strip():
        return ""

    tokens: List[str] = []
    for w in jieba.cut_for_search(query):
        w_clean = w.strip()
        if not w_clean or STOP_PUNCT_PATTERN.match(w_clean):
            continue
        tokens.append(w_clean.replace("'", ""))

    if not tokens:
        return ""
    # 采用逻辑或连接各关键词
    return " | ".join(tokens)

def normalize_sparse_vector(sparse_dict: Dict[str, float]) -> Dict[str, float]:
    r"""对稀疏向量词频字典执行 L2 模长归一化"""
    if not sparse_dict:
        return {}

    squared_sum = sum(w ** 2 for w in sparse_dict.values())
    norm = math.sqrt(squared_sum)

    if norm == 0:
        return sparse_dict

    return {token: round(weight / norm, 6) for token, weight in sparse_dict.items()}

def extract_sparse_vector(text: str) -> Dict[str, float]:
    """计算文本词项对数阻尼词频 (TF) 权重，并执行 L2 归一化"""
    if not text or not text.strip():
        return {}

    words = [w.strip() for w in jieba.cut_for_search(text) if w.strip()]
    raw_freq: Dict[str, float] = {}

    for w in words:
        if STOP_PUNCT_PATTERN.match(w):
            continue
        raw_freq[w] = raw_freq.get(w, 0.0) + 1.0

    tf_weights = {token: 1.0 + math.log(count) for token, count in raw_freq.items()}
    return normalize_sparse_vector(tf_weights)
