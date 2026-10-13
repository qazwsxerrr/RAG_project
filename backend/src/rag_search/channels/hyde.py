"""src/rag_search/channels/hyde.py —— 四路召回中的假设性搜索通道 (HyDE)。

针对口语化或模糊提问，先让 LLM 编一段严谨专业的技术假想解答文档，
再用假想文档的 1024 维向量执行稠密召回。
配备超时熔断保护，超时或异常时安全熔断降级为空结果，不阻塞主召回链路。
"""

import json
import logging
import asyncio
from typing import Any, Dict, List, Optional
import httpx
from sqlalchemy.ext.asyncio import AsyncSession
from rag_kb.core.config import settings
from rag_kb.core.llm import llm_gateway
from rag_kb.utils.embedding import embedding_service
from rag_search.channels import dense

logger = logging.getLogger(__name__)

NAME = "hyde"

async def generate_hypothetical_document(query: str, timeout_seconds: float = 90.0) -> str:
    """调用大模型网关生成 150~250 字假想技术解答"""
    system_prompt = (
        "你是一名资深的技术架构师与专业知识库解答顾问。请针对用户提出的问题，直接撰写一段 150~250 字的专业、严谨的技术假想解答文档。"
        "该假想解答用于高维向量空间特征匹配，请包含核心术语、技术要点和关键概念。\n"
        "【输出约束】：\n"
        "1. 仅输出假想解答正文，严禁包含任何开场白或解释性废话；\n"
        "2. 字数严格控制在 150~250 字之间。"
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"问题：{query.strip()}"}
    ]

    doc = await llm_gateway.chat(
        messages=messages,
        temperature=0.3,
        max_tokens=300
    )
    return doc.strip()

async def search(
    session: AsyncSession,
    query: str,
    department_scope: Optional[List[str]] = None,
    category_scope: Optional[str] = None,
    tags_scope: Optional[List[str]] = None,
    top_k: int = 15
) -> List[Dict[str, Any]]:
    """执行带超时熔断保护的 HyDE 假想搜索"""
    timeout_sec = getattr(settings, "HYDE_TIMEOUT_SECONDS", 90.0)

    async def _execute() -> List[Dict[str, Any]]:
        hypo_doc = await generate_hypothetical_document(query, timeout_seconds=timeout_sec)
        if not hypo_doc:
            return []
        
        # 向量化假想文档
        hypo_dense = await embedding_service.embed_query(hypo_doc)
        
        # 复用稠密召回通道
        raw_candidates = await dense.search(
            session=session,
            query_dense=hypo_dense,
            department_scope=department_scope,
            category_scope=category_scope,
            tags_scope=tags_scope,
            top_k=top_k
        )

        # 重标为 hyde 通道
        hyde_candidates: List[Dict[str, Any]] = []
        for idx, cand in enumerate(raw_candidates):
            c = dict(cand)
            c["channel"] = NAME
            c["channel_ranks"] = {NAME: idx + 1}
            hyde_candidates.append(c)

        return hyde_candidates

    try:
        return await asyncio.wait_for(_execute(), timeout=timeout_sec)
    except asyncio.TimeoutError:
        logger.warning(f"【HyDE 熔断】假想问答推理超过 {timeout_sec} 秒阈值，触发熔断保护，跳过本路。")
        return []
    except Exception as e:
        logger.warning(f"【HyDE 异常】假想生成失败，安全跳过: {e}")
        return []
