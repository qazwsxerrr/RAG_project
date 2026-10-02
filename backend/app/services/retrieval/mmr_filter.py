import logging
from typing import List
import numpy as np
from backend.app.schemas.retrieval import RetrievedChunk

logger = logging.getLogger(__name__)

class MMRFilter:
    r"""阶段 6：最大边际相关性多样性过滤器 (MMR: Maximal Marginal Relevance)

    解决痛点：
    消除重排后排在前面的同质化连续切片与复读堆砌。
    在保证与 Query 相关性的同时，最大化切片之间的信息互补度与多样性。
    公式:
        MMR = argmax_{d_i \in R \setminus S} [ \lambda * Sim(d_i, Q) - (1 - \lambda) * max_{d_j \in S} Sim(d_i, d_j) ]
    """

    def filter(
        self,
        query_embedding: List[float],
        chunks: List[RetrievedChunk],
        top_k: int = 5,
        lambda_param: float = 0.7
    ) -> List[RetrievedChunk]:
        """执行 MMR 多样性贪心选择 (从候选集精选 5 条互补切片)"""
        if not chunks:
            return []

        if len(chunks) <= top_k:
            return chunks

        # 转换为 numpy 矩阵并归一化
        q_vec = np.array(query_embedding, dtype=np.float32)
        q_norm_val = np.linalg.norm(q_vec)
        if q_norm_val == 0:
            return chunks[:top_k]
        q_norm = q_vec / q_norm_val

        # 提取切片稠密向量
        cand_norms: List[np.ndarray] = []
        for c in chunks:
            if c.dense_embedding is not None and len(c.dense_embedding) > 0:
                vec = np.array(c.dense_embedding, dtype=np.float32)
                v_norm = np.linalg.norm(vec)
                cand_norms.append(vec / v_norm if v_norm > 0 else vec)
            else:
                cand_norms.append(np.zeros_like(q_norm))

        # 1. 计算各切片与 Query 的余弦相似度
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

                # MMR 综合打分
                mmr_score = lambda_param * sim_q - (1.0 - lambda_param) * max_sim_selected
                if mmr_score > best_score:
                    best_score = mmr_score
                    best_idx = idx

            if best_idx == -1:
                break

            selected_indices.append(best_idx)
            unselected_indices.remove(best_idx)

        selected_chunks = [chunks[i] for i in selected_indices]
        logger.info(
            f"【MMR 去重完成】输入 {len(chunks)} 条切片，依据 lambda={lambda_param} "
            f"精选出 {len(selected_chunks)} 条无冗余互补黄金切片。"
        )

        return selected_chunks

mmr_filter = MMRFilter()
