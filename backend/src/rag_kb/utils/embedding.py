"""src/rag_kb/utils/embedding.py —— 向量获取工具：封装 OpenAI 兼容 /embeddings 的批量调用。

按批请求（服务端限制单批 16~20 条），保证稠密向量与入参严格对齐。
入库的 embedder 节点与查询侧取向量都走这里。
严格遵守 Fail-Fast 原则，API 异常或凭据缺失坚决报错，绝不使用随机向量伪造数据。
"""

import time
import logging
from typing import List, Tuple
import httpx
from rag_kb.core.config import settings, derive_provider_from_url
from rag_kb.core.resilience import retry_on_transient
from rag_kb.core.logging.audit import record_audit_log

logger = logging.getLogger(__name__)

@retry_on_transient(service_name="EmbeddingService")
async def _post_batch_with_retry(client: httpx.AsyncClient, endpoint: str, headers: dict, payload: dict) -> httpx.Response:
    resp = await client.post(endpoint, headers=headers, json=payload)
    if resp.status_code != 200:
        resp.raise_for_status()
    return resp

class EmbeddingService:
    """稠密语义特征向量生成服务 (默认对接 SiliconFlow BAAI/bge-m3 1024 维 API)"""

    def __init__(self):
        self.base_url = settings.EMBEDDING_BASE_URL.rstrip("/")
        self.model_name = settings.EMBEDDING_MODEL
        self.dim = settings.EMBEDDING_DIM

    async def _resolve_credentials(self) -> Tuple[str, str]:
        """解析向量模型调用凭据 (Phase-6 §7.2: 强制通过 Java 鉴权中心换取 10min 短期租约，严禁回退本地 .env)"""
        try:
            from rag_kb.core.model_credentials import get_credential
            provider = derive_provider_from_url(self.base_url)
            cred = await get_credential(provider, scope="model:embedding")
            return cred.api_key, (cred.base_url or self.base_url).rstrip("/")
        except Exception as e:
            raise RuntimeError(
                f"【向量模型凭据错误】未能从鉴权中心获取 Embedding 短期租约凭据: {e} (Fail-Fast)"
            ) from e

    async def embed_texts(self, texts: List[str], batch_size: int = 16) -> List[List[float]]:
        """批量生成文本的 1024 维稠密特征向量"""
        if not texts:
            return []

        t0 = time.perf_counter()

        try:
            api_key, base_url = await self._resolve_credentials()

            for idx, t in enumerate(texts):
                if not t or not t.strip():
                    raise ValueError(f"【向量计算错误】第 {idx} 条输入文本为空，严禁对空文本生成语义向量！")

            all_embeddings: List[List[float]] = []
            endpoint = f"{base_url}/embeddings"
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json"
            }

            async with httpx.AsyncClient(timeout=60.0) as client:
                for i in range(0, len(texts), batch_size):
                    batch = texts[i:i + batch_size]
                    payload = {
                        "model": self.model_name,
                        "input": batch,
                        "encoding_format": "float"
                    }

                    try:
                        response = await _post_batch_with_retry(client, endpoint, headers, payload)
                    except Exception as e:
                        logger.error(f"【向量 API 请求异常】批次 {i // batch_size + 1} 重试耗尽失败: {e}")
                        raise RuntimeError(f"Embedding API 连接失败 (已重试 3 次): {e}") from e

                    if response.status_code != 200:
                        err_msg = f"【向量模型错误】HTTP {response.status_code}: {response.text}"
                        logger.error(err_msg)
                        raise RuntimeError(err_msg)

                    data = response.json()
                    if "data" not in data or not isinstance(data["data"], list):
                        raise RuntimeError(f"【向量格式错误】API 响应缺少 'data' 字段: {data}")

                    batch_embeddings = [item["embedding"] for item in data["data"]]
                    for vec in batch_embeddings:
                        if len(vec) != self.dim:
                            raise RuntimeError(f"【向量维度异常】期望 {self.dim} 维向量，实际返回 {len(vec)} 维！")

                    all_embeddings.extend(batch_embeddings)

            total_ms = int((time.perf_counter() - t0) * 1000)
            await record_audit_log(
                action="EMBEDDING_INFERENCE",
                resource_id="embedding-dense-bge",
                duration_ms=total_ms,
                status="SUCCESS",
                details={"model": self.model_name, "batch_count": len(texts)}
            )
            return all_embeddings

        except Exception as e:
            total_ms = int((time.perf_counter() - t0) * 1000)
            await record_audit_log(
                action="EMBEDDING_INFERENCE",
                resource_id="embedding-dense-bge",
                duration_ms=total_ms,
                status="FAILED",
                error_msg=str(e),
                details={"model": self.model_name, "batch_count": len(texts)}
            )
            raise

    async def embed_query(self, text: str) -> List[float]:
        """为单条查询生成 1024 维语义向量"""
        results = await self.embed_texts([text], batch_size=1)
        return results[0]

    async def embed_title(self, file_name: str, h1: str = "", h2: str = "") -> List[float]:
        """组装文档标题特征并计算 1024 维防重标题向量"""
        parts = [file_name.strip()]
        if h1 and h1.strip():
            parts.append(h1.strip())
        if h2 and h2.strip():
            parts.append(h2.strip())
        title_feature = " ".join(parts)
        return await self.embed_query(title_feature)

embedding_service = EmbeddingService()

async def embed_texts(texts: List[str], batch_size: int = 16) -> List[List[float]]:
    return await embedding_service.embed_texts(texts, batch_size)

async def embed_query(text: str) -> List[float]:
    return await embedding_service.embed_query(text)

async def embed_title(file_name: str, h1: str = "", h2: str = "") -> List[float]:
    return await embedding_service.embed_title(file_name, h1, h2)
