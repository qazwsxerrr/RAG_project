"""src/rag_search/channels/sparse.py —— 四路召回中的归一化稀疏向量召回通道。"""

import uuid
from typing import Any, Dict, List, Optional, Sequence
from sqlalchemy import select, func, cast, String, Float, literal_column
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession

from rag_kb.db.models import Document, DocumentChunk, KnowledgeBase
from rag_search.channels.base import apply_filters, make_candidate

NAME = "sparse"


async def search(
    session: AsyncSession,
    query_sparse: Dict[str, float],
    authz_filter_spec: Any = None,
    kb_ids: Optional[Sequence[uuid.UUID]] = None,
    category_scope: Optional[str] = None,
    tags_scope: Optional[List[str]] = None,
    top_k: int = 20,
) -> List[Dict[str, Any]]:
    """稀疏向量归一化词频内积召回 Top-K"""
    if not query_sparse:
        return []

    tokens = list(query_sparse.keys())
    if not tokens:
        return []

    token_array = cast(tokens, ARRAY(String))
    eff_level_expr = func.greatest(KnowledgeBase.min_classification_level, Document.classification_level).label("eff_level")

    match_count_subq = (
        select(func.count())
        .select_from(func.unnest(token_array).alias("tok"))
        .where(DocumentChunk.sparse_vector.op("?")(literal_column("tok")))
        .scalar_subquery()
    )
    weight_sum_subq = (
        select(func.coalesce(func.sum(cast(DocumentChunk.sparse_vector.op("->>")(literal_column("tok")), Float)), 0.0))
        .select_from(func.unnest(token_array).alias("tok"))
        .where(DocumentChunk.sparse_vector.op("?")(literal_column("tok")))
        .scalar_subquery()
    )

    stmt = select(
        DocumentChunk,
        Document.title.label("doc_title"),
        KnowledgeBase.id.label("kb_id"),
        KnowledgeBase.name.label("kb_name"),
        eff_level_expr,
    )
    stmt = apply_filters(
        stmt,
        authz_filter_spec=authz_filter_spec,
        kb_ids=kb_ids,
        category_scope=category_scope,
        tags_scope=tags_scope,
    )
    stmt = stmt.where(DocumentChunk.sparse_vector.op("?|")(token_array))
    stmt = stmt.order_by(
        match_count_subq.desc(),
        weight_sum_subq.desc(),
        DocumentChunk.chunk_index.asc(),
        DocumentChunk.id.asc(),
    ).limit(100)

    res = await session.execute(stmt)
    rows = res.all()

    scored_candidates = []
    for row in rows:
        chunk_obj: DocumentChunk = row[0]
        doc_sp = chunk_obj.sparse_vector or {}
        dot_product = sum(query_sparse[k] * doc_sp.get(k, 0.0) for k in tokens if k in doc_sp)
        if dot_product > 0.0:
            scored_candidates.append((dot_product, chunk_obj, row[1], str(row[2]), row[3], int(row[4])))

    scored_candidates.sort(key=lambda x: x[0], reverse=True)
    top_candidates = scored_candidates[:top_k]

    chunks: List[Dict[str, Any]] = []
    for idx, (score, chunk_obj, doc_title, kb_id, kb_name, eff_level) in enumerate(top_candidates):
        cand = make_candidate(
            chunk_obj=chunk_obj,
            doc_title=doc_title,
            kb_id=kb_id,
            kb_name=kb_name,
            effective_classification_level=eff_level,
            score=score,
            channel_name=NAME,
            rank=idx + 1,
        )
        chunks.append(cand)

    return chunks
