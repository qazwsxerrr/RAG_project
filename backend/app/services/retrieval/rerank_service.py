import logging
from typing import List, Tuple
import httpx
from backend.app.core.config import settings
from backend.app.schemas.retrieval import RetrievedChunk

logger = logging.getLogger(__name__)

class RerankService:
    """阶段 5：BGE-Reranker 交叉编码深度精排器与 Remark 丢弃诊断

    核心机制：
    1. 调用 SiliconFlow BAAI/bge-reranker-v2-m3 真实 API 对 29 条候选切片做全向交叉注意力打分；
    2. 精选出置信度最高的前 20 条切片；
    3. Remark 丢弃诊断机制：对初始召回排名前 3 但跌出前 20 的切片标记审计标签并输出监控日志。
    严格遵循 Fail-Fast 原则，绝不伪造重排分数。
    """

    def __init__(self):
        self.api_key = settings.RERANK_API_KEY
        self.base_url = settings.RERANK_BASE_URL.rstrip("/")
        self.model_name = settings.RERANK_MODEL

    def _validate_config(self):
        if not self.api_key or not self.api_key.strip():
            raise RuntimeError("【重排模型错误】未配置 RERANK_API_KEY，根据企业级红线准则，严禁使用假数据兜底！")

    async def rerank(
        self,
        query: str,
        chunks: List[RetrievedChunk],
        top_n: int = 20
    ) -> Tuple[List[RetrievedChunk], List[RetrievedChunk]]:
        """执行交叉重排精选与丢弃审计诊断

        返回: (reranked_chunks, dropped_chunks_with_remark)
        """
        self._validate_config()

        if not chunks:
            return [], []

        clean_query = query.strip()
        if not clean_query:
            raise ValueError("【重排错误】输入 query 为空，无法计算交叉注意力打分！")

        documents = [c.content for c in chunks]
        endpoint = f"{self.base_url}/rerank"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": self.model_name,
            "query": clean_query,
            "documents": documents,
            "top_n": min(top_n, len(chunks)),
            "return_documents": False
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                res = await client.post(endpoint, headers=headers, json=payload)
            except Exception as e:
                logger.error(f"【Rerank 网络异常】请求失败: {e}")
                raise RuntimeError(f"BGE Rerank API 连接失败: {e}") from e

            if res.status_code != 200:
                raise RuntimeError(f"【Rerank 接口错误】HTTP {res.status_code}: {res.text}")

            data = res.json()
            if "results" not in data or not isinstance(data["results"], list):
                raise RuntimeError(f"【Rerank 格式异常】响应缺少 'results' 字段: {data}")

            # 提取前 top_n 精排切片
            reranked_chunks: List[RetrievedChunk] = []
            selected_indices = set()

            for item in data["results"]:
                idx = item["index"]
                score = float(item["relevance_score"])
                selected_indices.add(idx)

                c = chunks[idx].model_copy(deep=True)
                c.score = round(score, 6)
                reranked_chunks.append(c)

            # Remark 丢弃诊断机制
            # 检查在前序召回排名前 3 (如 dense<=3, bm25<=3, sparse<=3)，但在重排中掉出前 20 的切片
            dropped_chunks: List[RetrievedChunk] = []
            for orig_idx, c in enumerate(chunks):
                if orig_idx not in selected_indices:
                    dropped_copy = c.model_copy(deep=True)
                    # 检查是否在前序三路进入前 3
                    for ch, rank in dropped_copy.channel_ranks.items():
                        if rank <= 3:
                            dropped_copy.remark = f"{ch}_recall_drop_in_rerank"
                            logger.warning(
                                f"【Rerank 丢弃审计】切片 {dropped_copy.chunk_id} ({dropped_copy.chunk_label}) "
                                f"在初始通道 '{ch}' 排名第 {rank}，但在 Rerank 交叉精排中跌出前 20！"
                                f"已标记 remark='{dropped_copy.remark}' 并记入监控流水。"
                            )
                            dropped_chunks.append(dropped_copy)
                            break

            logger.info(
                f"【Rerank 精排完成】输入候选 {len(chunks)} 条，精选 Top-{len(reranked_chunks)}，"
                f"审计检出丢弃关注切片 {len(dropped_chunks)} 条。"
            )

            return reranked_chunks, dropped_chunks

rerank_service = RerankService()
