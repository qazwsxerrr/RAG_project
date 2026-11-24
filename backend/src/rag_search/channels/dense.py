"""src/rag_search/channels/dense.py —— 四路召回中的稠密向量召回通道。"""

import uuid
from typing import Any, Dict, List, Optional, Sequence
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from rag_kb.db.models import Document, DocumentChunk, KnowledgeBase
from rag_search.channels.base import apply_filters, make_candidate

NAME = "dense"


async def search(
    session: AsyncSession,
    query_dense: List[float],
    authz_filter_spec: Any = None,
    kb_ids: Optional[Sequence[uuid.UUID]] = None,
    category_scope: Optional[str] = None,
    tags_scope: Optional[List[str]] = None,
    top_k: int = 20,
) -> List[Dict[str, Any]]:
    """稠密向量召回 Top-K"""
    if not query_dense or len(query_dense) != 1024:
        return []

    distance_expr = DocumentChunk.dense_embedding.cosine_distance(query_dense)
    score_expr = (1.0 - distance_expr).label("score")
    eff_level_expr = func.greatest(KnowledgeBase.min_classification_level, Document.classification_level).label("eff_level")

    stmt = select(
        DocumentChunk,
        Document.title.label("doc_title"),
        KnowledgeBase.id.label("kb_id"),
        KnowledgeBase.name.label("kb_name"),
        eff_level_expr,
        score_expr,
    )
    stmt = apply_filters(
        stmt,
        authz_filter_spec=authz_filter_spec,
        kb_ids=kb_ids,
        category_scope=category_scope,
        tags_scope=tags_scope,
    )
    stmt = stmt.order_by(distance_expr.asc()).limit(top_k)

    res = await session.execute(stmt)
    rows = res.all()

    chunks: List[Dict[str, Any]] = []
    for idx, row in enumerate(rows):
        cand = make_candidate(
            chunk_obj=row[0],
            doc_title=row[1],
            kb_id=str(row[2]),
            kb_name=row[3],
            effective_classification_level=int(row[4]),
            score=float(row[5]),
            channel_name=NAME,
            rank=idx + 1,
        )
        chunks.append(cand)

    return chunks
