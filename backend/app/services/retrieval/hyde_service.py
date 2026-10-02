import json
import logging
import asyncio
from typing import List, Optional
import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config import settings
from backend.app.schemas.retrieval import RetrievedChunk
from backend.app.services.retrieval.embedding_service import embedding_service
from backend.app.services.retrieval.multi_retriever import multi_retriever

logger = logging.getLogger(__name__)

class HyDEService:
    """阶段 3：假设性搜索 (HyDE: Hypothetical Document Embeddings)

    核心机制：
    1. 针对口语化、模糊提问，调用大模型按技术专家口吻生成 150~250 字的假想文档；
    2. 将假想文档整体向量化并在知识库中二次召回 Top-15 切片；
    3. 设置可配置超时熔断保护（默认 90s，支持 grok 等大型推理模型完整输出），超时时记录 Warning，不阻塞主召回链路。
    """

    def __init__(self):
        self.api_key = settings.active_llm_api_key
        self.base_url = settings.active_llm_base_url.rstrip("/")
        self.model_name = settings.active_llm_model
        self.timeout_seconds = getattr(settings, "HYDE_TIMEOUT_SECONDS", 90.0)

    async def generate_hypothetical_document(self, query: str) -> str:
        """调用大模型流式生成 150~250 字假想技术解答"""
        if not self.api_key:
            raise RuntimeError("【HyDE 错误】未配置大模型 API_KEY！")

        system_prompt = (
            "你是一名资深的技术架构师与专业知识库解答顾问。请针对用户提出的问题，直接撰写一段 150~250 字的专业、严谨的技术假想解答文档。"
            "该假想解答用于高维向量空间特征匹配，请包含核心术语、技术要点和关键概念。\n"
            "【输出约束】：\n"
            "1. 仅输出假想解答正文，严禁包含任何开场白或解释性废话；\n"
            "2. 字数严格控制在 150~250 字之间。"
        )

        endpoint = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": self.model_name,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"问题：{query.strip()}"}
            ],
            "temperature": 0.3,
            "max_tokens": 300,
            "stream": True
        }

        tokens: List[str] = []
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            async with client.stream("POST", endpoint, headers=headers, json=payload) as resp:
                if resp.status_code != 200:
                    err_bytes = await resp.aread()
                    raise RuntimeError(f"【HyDE 生成失败】HTTP {resp.status_code}: {err_bytes.decode(errors='ignore')}")
                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line or not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if raw == "[DONE]":
                        break
                    try:
                        chunk = json.loads(raw)
                        delta = chunk["choices"][0].get("delta", {})
                        c = delta.get("content")
                        if c:
                            tokens.append(c)
                    except Exception:
                        continue

        content = "".join(tokens).strip()
        return content

    async def search_with_circuit_breaker(
        self,
        session: AsyncSession,
        query: str,
        department_scope: Optional[List[str]] = None,
        category_scope: Optional[str] = None,
        tags_scope: Optional[List[str]] = None,
        top_k: int = 15
    ) -> List[RetrievedChunk]:
        """执行带 10 秒超时熔断保护的 HyDE 召回"""
        try:
            return await asyncio.wait_for(
                self._execute_hyde(
                    session=session,
                    query=query,
                    department_scope=department_scope,
                    category_scope=category_scope,
                    tags_scope=tags_scope,
                    top_k=top_k
                ),
                timeout=self.timeout_seconds
            )
        except asyncio.TimeoutError:
            logger.warning(f"【HyDE 熔断触发】假想文档生成/检索超过 {self.timeout_seconds}s，放弃该路召回以保护主流程。")
            return []

    async def _execute_hyde(
        self,
        session: AsyncSession,
        query: str,
        department_scope: Optional[List[str]] = None,
        category_scope: Optional[str] = None,
        tags_scope: Optional[List[str]] = None,
        top_k: int = 15
    ) -> List[RetrievedChunk]:
        """内部执行假想文档生成与向量召回"""
        # 1. 生成假想文本
        hypo_doc = await self.generate_hypothetical_document(query)
        if not hypo_doc:
            return []

        logger.info(f"【HyDE 假想生成完成】字数: {len(hypo_doc)}，开始向量化匹配...")

        # 2. 向量化假想文本
        hypo_vec = await embedding_service.embed_query(hypo_doc)

        # 3. 稠密召回 Top-K
        chunks = await multi_retriever.dense_search(
            session=session,
            query_dense=hypo_vec,
            department_scope=department_scope,
            category_scope=category_scope,
            tags_scope=tags_scope,
            top_k=top_k
        )

        # 标记渠道与排位
        for idx, c in enumerate(chunks):
            c.channel_ranks = {"hyde": idx + 1}

        return chunks

hyde_service = HyDEService()
