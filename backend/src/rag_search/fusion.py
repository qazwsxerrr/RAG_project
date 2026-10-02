"""src/rag_search/fusion.py —— 检索链路的融合层：加权倒数排名动态融合 (RRF)。

四路分数量纲不同无法直接加权求和，因此根据通道排位与场景动态权重累加 RRF 分数：
Score = sum(w_channel * (1 / (k + rank)))
多路都命中的分块会被显著顶到前面。对应检索图中的 fuse 节点。
"""

import copy
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

SCENE_WEIGHT_PROFILES: Dict[str, Dict[str, float]] = {
    # 理论概念对比/学术研讨/技术研发
    "academic": {
        "dense": 0.40,
        "hyde": 0.30,
        "bm25": 0.15,
        "sparse": 0.15,
    },
    "technical": {
        "dense": 0.40,
        "hyde": 0.30,
        "bm25": 0.15,
        "sparse": 0.15,
    },
    # 产品手册/机器排障/制度条款/合规风控
    "troubleshooting": {
        "bm25": 0.45,
        "sparse": 0.35,
        "dense": 0.10,
        "hyde": 0.10,
    },
    "compliance": {
        "bm25": 0.45,
        "sparse": 0.35,
        "dense": 0.10,
        "hyde": 0.10,
    },
    # 默认通用平衡场景
    "general": {
        "dense": 0.30,
        "sparse": 0.25,
        "bm25": 0.25,
        "hyde": 0.20,
    },
    "qa": {
        "dense": 0.30,
        "sparse": 0.25,
        "bm25": 0.25,
        "hyde": 0.20,
    }
}

def count_by_channel(candidates: List[Dict[str, Any]]) -> Dict[str, int]:
    """统计各召回通道的候选命中数量（用于观测分析）"""
    counts: Dict[str, int] = {}
    for c in candidates:
        ch = c.get("channel", "unknown")
        counts[ch] = counts.get(ch, 0) + 1
    return counts

def fuse(
    candidates: List[Dict[str, Any]],
    scene_type: str = "general",
    k: int = 60,
    top_n: int = 29
) -> List[Dict[str, Any]]:
    """将四路候选切片按场景化加权倒数排名合并去重并排序"""
    if not candidates:
        return []

    weights = SCENE_WEIGHT_PROFILES.get(scene_type, SCENE_WEIGHT_PROFILES["general"])

    merged_chunks: Dict[str, Dict[str, Any]] = {}
    rrf_scores: Dict[str, float] = {}

    for cand in candidates:
        c_id = str(cand["chunk_id"])
        ch_name = cand.get("channel", "dense")
        ranks = cand.get("channel_ranks", {})
        rank = ranks.get(ch_name, cand.get("rank", 1))

        if c_id not in merged_chunks:
            item = copy.deepcopy(cand)
            item["channel_ranks"] = {}
            merged_chunks[c_id] = item
            rrf_scores[c_id] = 0.0

        merged_chunks[c_id]["channel_ranks"][ch_name] = rank

        w = weights.get(ch_name, 0.25)
        rrf_scores[c_id] += w * (1.0 / (k + rank))

    # 按 RRF 得分降序排序
    sorted_items = sorted(
        merged_chunks.values(),
        key=lambda x: rrf_scores[str(x["chunk_id"])],
        reverse=True
    )

    for item in sorted_items:
        c_id = str(item["chunk_id"])
        item["score"] = round(rrf_scores[c_id], 6)

    return sorted_items[:top_n]
