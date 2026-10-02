"""src/rag_search/channels/dense.py —— 四路召回中的稠密向量召回通道。

用查询向量按余弦距离召回语义相近的 chunk，基于 pgvector 1024 维 HNSW 索引。
严格强制执行组织架构权限隔离与激活状态校验。
"""

from typing import Any, Dict, List, Optional
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from rag_kb.db.models import Document, DocumentChunk
from rag_search.channels.base import apply_filters, make_candidate

NAME = "dense"

async def search(
    session: AsyncSession,
    query_dense: List[float],
    department_scope: Optional[List[str]] = None,
    category_scope: Optional[str] = None,
    tags_scope: Optional[List[str]] = None,
    top_k: int = 20
) -> List[Dict[str, Any]]:
    """稠密向量召回 Top-K"""
    if not query_dense or len(query_dense) != 1024:
        return []

    distance_expr = DocumentChunk.dense_embedding.cosine_distance(query_dense)
    score_expr = (1.0 - distance_expr).label("score")

    stmt = select(
        DocumentChunk,
        Document.title.label("doc_title"),
        Document.raw_oss_url.label("doc_raw_oss_url"),
        score_expr
    )
    stmt = apply_filters(stmt, department_scope, category_scope, tags_scope)
    stmt = stmt.order_by(distance_expr.asc()).limit(top_k)

    res = await session.execute(stmt)
    rows = res.all()

    chunks: List[Dict[str, Any]] = []
    for idx, row in enumerate(rows):
        chunk_obj: DocumentChunk = row[0]
        doc_title: str = row[1]
        doc_oss_url: Optional[str] = row[2]
        score: float = float(row[3])

        cand = make_candidate(
            chunk_obj=chunk_obj,
            doc_title=doc_title,
            doc_oss_url=doc_oss_url,
            score=score,
            channel_name=NAME,
            rank=idx + 1
        )
        chunks.append(cand)

    return chunks
