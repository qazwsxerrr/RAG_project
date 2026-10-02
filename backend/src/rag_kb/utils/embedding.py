"""src/rag_kb/utils/embedding.py —— 向量获取工具：封装 OpenAI 兼容 /embeddings 的批量调用。

按批请求（服务端限制单批 16~20 条），保证稠密向量与入参严格对齐。
入库的 embedder 节点与查询侧取向量都走这里。
严格遵守 Fail-Fast 原则，API 异常或凭据缺失坚决报错，绝不使用随机向量伪造数据。
"""

import logging
from typing import List
import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from rag_kb.core.config import settings

logger = logging.getLogger(__name__)

retry_embedding_post = retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    retry=retry_if_exception_type((httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError)),
    reraise=True
)

@retry_embedding_post
async def _post_batch_with_retry(client: httpx.AsyncClient, endpoint: str, headers: dict, payload: dict) -> httpx.Response:
    resp = await client.post(endpoint, headers=headers, json=payload)
    if resp.status_code in [429, 500, 502, 503, 504]:
        resp.raise_for_status()
    return resp

class EmbeddingService:
    """稠密语义特征向量生成服务 (默认对接 SiliconFlow BAAI/bge-m3 1024 维 API)"""

    def __init__(self):
        self.api_key = settings.EMBEDDING_API_KEY
        self.base_url = settings.EMBEDDING_BASE_URL.rstrip("/")
        self.model_name = settings.EMBEDDING_MODEL
        self.dim = settings.EMBEDDING_DIM

    def _validate_config(self):
        if not self.api_key or not self.api_key.strip():
            raise RuntimeError(
                "【向量模型错误】未配置 EMBEDDING_API_KEY！"
                "根据企业级准则，严禁使用本地随机向量兜底，请在 .env 中正确配置 API 密钥。"
            )

    async def embed_texts(self, texts: List[str], batch_size: int = 16) -> List[List[float]]:
        """批量生成文本的 1024 维稠密特征向量"""
        self._validate_config()
        if not texts:
            return []

        for idx, t in enumerate(texts):
            if not t or not t.strip():
                raise ValueError(f"【向量计算错误】第 {idx} 条输入文本为空，严禁对空文本生成语义向量！")

        all_embeddings: List[List[float]] = []
        endpoint = f"{self.base_url}/embeddings"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
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

        return all_embeddings

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
