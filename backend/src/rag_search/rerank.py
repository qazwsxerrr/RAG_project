"""src/rag_search/rerank.py —— 检索链路的 cross-encoder 交叉精排器与丢弃审计诊断。

处在融合之后、MMR 之前，调用云端 BGE-Reranker-v2-m3 接口对「查询 - 候选切片」逐条深度计算交叉注意力打分，
避免本地加载 4GB+ PyTorch 显存膨胀。提供 Remark 丢弃诊断机制。
"""

import copy
import time
import logging
from typing import Any, Dict, List, Tuple
import httpx
from rag_kb.core.config import settings, derive_provider_from_url
from rag_kb.core.resilience import retry_on_transient
from rag_kb.core.logging.audit import record_audit_log

logger = logging.getLogger(__name__)

@retry_on_transient(service_name="Reranker.post")
async def _post_rerank_with_retry(client: httpx.AsyncClient, endpoint: str, headers: dict, payload: dict) -> httpx.Response:
    res = await client.post(endpoint, headers=headers, json=payload)
    if res.status_code != 200:
        res.raise_for_status()
    return res

class Reranker:
    """云端 Cross-Encoder 重排器"""

    def __init__(self):
        self.base_url = settings.RERANK_BASE_URL.rstrip("/")
        self.model_name = settings.RERANK_MODEL
        self.enabled = settings.RERANK_ENABLED

    async def _resolve_credentials(self) -> Tuple[str, str]:
        """解析精排模型凭据 (Phase-6 §7.2: 强制通过 Java 鉴权中心换取 10min 短期租约，严禁回退本地 .env)"""
        try:
            from rag_kb.core.model_credentials import get_credential
            provider = derive_provider_from_url(self.base_url)
            cred = await get_credential(provider, scope="model:rerank")
            return cred.api_key, (cred.base_url or self.base_url).rstrip("/")
        except Exception as e:
            raise RuntimeError(
                f"【精排模型凭据错误】未能从鉴权中心获取 Rerank 短期租约凭据: {e} (Fail-Fast)"
            ) from e

    async def rerank(
        self,
        query: str,
        chunks: List[Dict[str, Any]],
        top_n: int = 20
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """执行交叉精排与丢弃审计诊断"""
        if not self.enabled or not chunks:
            return chunks[:top_n], []

        t0 = time.perf_counter()

        try:
            api_key, base_url = await self._resolve_credentials()

            clean_query = query.strip()
            documents = [c.get("content", "") for c in chunks]
            endpoint = f"{base_url}/rerank"
            headers = {
                "Authorization": f"Bearer {api_key}",
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
                    res = await _post_rerank_with_retry(client, endpoint, headers, payload)
                except Exception as e:
                    logger.error(f"【Rerank 网络异常】请求失败: {e}")
                    raise RuntimeError(f"BGE Rerank API 连接失败: {e}") from e

                if res.status_code != 200:
                    raise RuntimeError(f"【Rerank 接口错误】HTTP {res.status_code}: {res.text}")

                data = res.json()
                if "results" not in data or not isinstance(data["results"], list):
                    raise RuntimeError(f"【Rerank 格式异常】响应缺少 'results' 字段: {data}")

                reranked_chunks: List[Dict[str, Any]] = []
                selected_indices = set()

                for item in data["results"]:
                    idx = item["index"]
                    score = float(item["relevance_score"])
                    selected_indices.add(idx)

                    c = copy.deepcopy(chunks[idx])
                    c["score"] = round(score, 6)
                    reranked_chunks.append(c)

                # Remark 丢弃诊断机制
                dropped_chunks: List[Dict[str, Any]] = []
                for orig_idx, c in enumerate(chunks):
                    if orig_idx not in selected_indices:
                        dropped_copy = copy.deepcopy(c)
                        ranks = dropped_copy.get("channel_ranks", {})
                        for ch, rank in ranks.items():
                            if rank <= 3:
                                dropped_copy["remark"] = f"{ch}_recall_drop_in_rerank"
                                logger.warning(
                                    f"【Rerank 丢弃审计】切片 {dropped_copy['chunk_id']} ({dropped_copy.get('chunk_label', '')}) "
                                    f"在初始通道 '{ch}' 排名第 {rank}，但在 Rerank 交叉精排中跌出前 {top_n}！"
                                )
                        dropped_chunks.append(dropped_copy)

                total_ms = int((time.perf_counter() - t0) * 1000)
                await record_audit_log(
                    action="RERANK_INFERENCE",
                    resource_id="reranker-cross-encoder",
                    duration_ms=total_ms,
                    status="SUCCESS",
                    details={"model": self.model_name, "docs_count": len(chunks)}
                )
                return reranked_chunks, dropped_chunks

        except Exception as e:
            total_ms = int((time.perf_counter() - t0) * 1000)
            await record_audit_log(
                action="RERANK_INFERENCE",
                resource_id="reranker-cross-encoder",
                duration_ms=total_ms,
                status="FAILED",
                error_msg=str(e),
                details={"model": self.model_name, "docs_count": len(chunks)}
            )
            raise

_reranker: Reranker = None

def get_reranker() -> Reranker:
    global _reranker
    if _reranker is None:
        _reranker = Reranker()
    return _reranker

async def rerank(
    query: str,
    chunks: List[Dict[str, Any]],
    top_n: int = 20
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    reranker = get_reranker()
    return await reranker.rerank(query, chunks, top_n)
