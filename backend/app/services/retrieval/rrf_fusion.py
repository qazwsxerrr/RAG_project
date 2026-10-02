import logging
from typing import List, Dict, Optional
from backend.app.schemas.retrieval import RetrievedChunk

logger = logging.getLogger(__name__)

# 场景化动态加权矩阵 (严格对齐《企业级RAG架构研讨》与《架构设计方案》)
SCENE_WEIGHT_PROFILES: Dict[str, Dict[str, float]] = {
    # 理论概念对比/学术研讨/技术研发 (放大深层语义与假想生成)
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
    # 产品手册/机器排障/制度条款/合规风控 (放大精确关键词与专有名词)
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
    # 默认通用平衡场景与日常问答
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

class RRFFusion:
    """阶段 4：加权倒数排名融合器 (Weighted Reciprocal Rank Fusion)

    核心规则：
    1. 融合 4 路召回池（最多 75 条候选），根据排位与场景动态权重重新打分去重；
    2. 平滑常数 k=60，截取前 29 条候选输入下一阶段重排；
    3. 铁律：外网网络搜索结果严格物理隔离，坚决不进入库内 RRF 融合池！
    """

    def __init__(self, default_k: int = 60):
        self.default_k = default_k

    def fuse(
        self,
        recall_pools: Dict[str, List[RetrievedChunk]],
        scene_type: str = "general",
        top_n: int = 29,
        external_web_results: Optional[List[dict]] = None
    ) -> List[RetrievedChunk]:
        """执行加权 RRF 融合与去重"""
        # 铁律警示：若传入外网搜索结果，记录审计隔离日志
        if external_web_results:
            logger.info(f"【外网隔离铁律】接收到 {len(external_web_results)} 条外网搜索结果，已执行严格物理隔离，绝不参与库内 RRF 算分！")

        weights = SCENE_WEIGHT_PROFILES.get(scene_type, SCENE_WEIGHT_PROFILES["general"])
        k = self.default_k

        # 融合切片字典: chunk_id -> RetrievedChunk
        merged_chunks: Dict[str, RetrievedChunk] = {}
        rrf_scores: Dict[str, float] = {}

        total_candidates_count = 0
        for channel_name, chunks in recall_pools.items():
            w = weights.get(channel_name, 0.25)
            total_candidates_count += len(chunks)

            for rank_idx, chunk in enumerate(chunks):
                rank = rank_idx + 1
                c_key = str(chunk.chunk_id)

                if c_key not in merged_chunks:
                    # 深拷贝切片对象以保留原始数据
                    merged_chunk = chunk.model_copy(deep=True)
                    merged_chunk.channel_ranks = {}
                    merged_chunks[c_key] = merged_chunk
                    rrf_scores[c_key] = 0.0

                # 记录该切片在当前通道中的排位
                merged_chunks[c_key].channel_ranks[channel_name] = rank

                # 加权倒数排名累加: w_m * (1 / (k + rank))
                score_increment = w * (1.0 / (k + rank))
                rrf_scores[c_key] += score_increment

        # 将综合 RRF 得分赋值回切片对象并降序排序
        for c_key, final_score in rrf_scores.items():
            merged_chunks[c_key].score = round(final_score, 6)

        sorted_chunks = sorted(
            merged_chunks.values(),
            key=lambda x: x.score,
            reverse=True
        )

        fused_result = sorted_chunks[:top_n]
        logger.info(
            f"【RRF 融合完成】场景: '{scene_type}' (权重: {weights})，"
            f"候选池 {total_candidates_count} 条去重融合为 {len(sorted_chunks)} 条，截取前 {len(fused_result)} 条。"
        )

        return fused_result

rrf_fusion = RRFFusion()
