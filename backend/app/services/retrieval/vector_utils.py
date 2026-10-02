import math
import re
from typing import Dict, List
import jieba

# 过滤纯标点与无意义空白
STOP_PUNCT_PATTERN = re.compile(r"^[\s!\"#$%&'()*+,-./:;<=>?@\[\\\]^_`{|}~·，。！？；：、“”‘’（）《》【】—…]+$")

def normalize_sparse_vector(sparse_dict: Dict[str, float]) -> Dict[str, float]:
    r"""对稀疏向量词频字典执行 L2 模长归一化

    $$\hat{\mathbf{v}} = \frac{\mathbf{v}}{\|\mathbf{v}\|_2} = \frac{\mathbf{v}}{\sqrt{\sum_{i} v_i^2}}$$
    """
    if not sparse_dict:
        return {}

    squared_sum = sum(w ** 2 for w in sparse_dict.values())
    norm = math.sqrt(squared_sum)

    if norm == 0:
        return sparse_dict

    return {token: round(weight / norm, 6) for token, weight in sparse_dict.items()}

def extract_sparse_vector(text: str) -> Dict[str, float]:
    """使用 jieba 分词计算文本词频 (TF) 权重，并执行 L2 模长归一化

    返回规范 JSONB 字典: {"token": normalized_weight}
    """
    if not text or not text.strip():
        return {}

    words = [w.strip() for w in jieba.cut(text) if w.strip()]
    raw_freq: Dict[str, float] = {}

    for w in words:
        if STOP_PUNCT_PATTERN.match(w):
            continue
        # 词频加权 (TF 采用对数阻尼公式: 1 + log(tf))
        raw_freq[w] = raw_freq.get(w, 0.0) + 1.0

    tf_weights = {token: 1.0 + math.log(count) for token, count in raw_freq.items()}
    return normalize_sparse_vector(tf_weights)

def format_tsvector_tokens(text: str) -> str:
    """提取空格分隔的中文分词序列，专供 PostgreSQL to_tsvector('simple', text) 写入

    保证原生 PostgreSQL 16 GIN 倒排索引能够直接针对中文词元建立索引并检索
    """
    if not text or not text.strip():
        return ""

    tokens: List[str] = []
    for w in jieba.cut(text):
        w_clean = w.strip()
        if not w_clean or STOP_PUNCT_PATTERN.match(w_clean):
            continue
        # 移除 PostgreSQL tsvector 可能引起解析异常的特殊保留单引号
        tokens.append(w_clean.replace("'", ""))

    return " ".join(tokens)
