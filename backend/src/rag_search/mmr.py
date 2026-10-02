"""src/rag_search/mmr.py —— 检索流程的去重精选：最大边际相关性多样性过滤器 (MMR)。

重排后的前 N 条常是相邻或同质化重复的切片，mmr_select 采用：
MMR = λ * Sim(d_i, Q) - (1 - λ) * max_{d_j in S} Sim(d_i, d_j)
贪心挑选，兼顾相关性与信息互补多样性。对应检索图中的 mmr 节点。
"""

import copy
import logging
from typing import Any, Dict, List
import numpy as np

logger = logging.getLogger(__name__)

def mmr_select(
    query_dense: List[float],
    chunks: List[Dict[str, Any]],
    top_k: int = 5,
    lambda_param: float = 0.7,
    strip_vector: bool = True
) -> List[Dict[str, Any]]:
    """执行 MMR 多样性贪心选择"""
    if not chunks:
        return []

    if len(chunks) <= top_k or not query_dense:
        return _format_results(chunks[:top_k], strip_vector)

    # 校验是否全量拥有稠密向量，若有缺失则降级为截断
    has_embeddings = all(
        c.get("dense_embedding") is not None and len(c.get("dense_embedding")) == len(query_dense)
        for c in chunks
    )
    if not has_embeddings:
        logger.warning("【MMR 提示】候选切片缺少稠密特征向量，自动降级为按重排得分截断。")
        return _format_results(chunks[:top_k], strip_vector)

    q_vec = np.array(query_dense, dtype=np.float32)
    q_norm_val = np.linalg.norm(q_vec)
    if q_norm_val == 0:
        return _format_results(chunks[:top_k], strip_vector)
    q_norm = q_vec / q_norm_val

    cand_norms: List[np.ndarray] = []
    for c in chunks:
        vec = np.array(c["dense_embedding"], dtype=np.float32)
        v_norm = np.linalg.norm(vec)
        cand_norms.append(vec / v_norm if v_norm > 0 else vec)

    # 1. 计算与 Query 的余弦相似度
    sim_to_query = [float(np.dot(q_norm, c_vec)) for c_vec in cand_norms]

    selected_indices: List[int] = []
    unselected_indices: List[int] = list(range(len(chunks)))

    for _ in range(min(top_k, len(chunks))):
        best_score = -float("inf")
        best_idx = -1

        for idx in unselected_indices:
            sim_q = sim_to_query[idx]

            # 计算与已选切片的最大相似度
            if not selected_indices:
                max_sim_selected = 0.0
            else:
                max_sim_selected = max(
                    float(np.dot(cand_norms[idx], cand_norms[sel_idx]))
                    for sel_idx in selected_indices
                )

            mmr_score = lambda_param * sim_q - (1.0 - lambda_param) * max_sim_selected
            if mmr_score > best_score:
                best_score = mmr_score
                best_idx = idx

        if best_idx == -1:
            break

        selected_indices.append(best_idx)
        unselected_indices.remove(best_idx)

    selected_chunks = [chunks[i] for i in selected_indices]
    return _format_results(selected_chunks, strip_vector)

def _format_results(chunks: List[Dict[str, Any]], strip_vector: bool) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    for c in chunks:
        item = copy.deepcopy(c)
        if strip_vector and "dense_embedding" in item:
            item["dense_embedding"] = None
        results.append(item)
    return results
