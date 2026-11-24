"""src/rag_search/channels/bm25.py —— 四路召回中的全文倒排索引召回通道。"""

import uuid
from typing import Any, Dict, List, Optional, Sequence
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from rag_kb.db.models import Document, DocumentChunk, KnowledgeBase
from rag_search.channels.base import apply_filters, make_candidate

NAME = "bm25"


async def search(
    session: AsyncSession,
    tsquery_tokens: str,
    authz_filter_spec: Any = None,
    kb_ids: Optional[Sequence[uuid.UUID]] = None,
    category_scope: Optional[str] = None,
    tags_scope: Optional[List[str]] = None,
    top_k: int = 20,
) -> List[Dict[str, Any]]:
    """全文倒排关键词召回 Top-K"""
    tokens = [t.strip() for t in tsquery_tokens.split() if t.strip()]
    if not tokens:
        return []

    or_tsquery = " | ".join(tokens)
    query_expr = func.to_tsquery("simple", or_tsquery)
    rank_expr = func.ts_rank_cd(DocumentChunk.tsv_content, query_expr).label("score")
    eff_level_expr = func.greatest(KnowledgeBase.min_classification_level, Document.classification_level).label("eff_level")

    stmt = select(
        DocumentChunk,
        Document.title.label("doc_title"),
        KnowledgeBase.id.label("kb_id"),
        KnowledgeBase.name.label("kb_name"),
        eff_level_expr,
        rank_expr,
    )
    stmt = apply_filters(
        stmt,
        authz_filter_spec=authz_filter_spec,
        kb_ids=kb_ids,
        category_scope=category_scope,
        tags_scope=tags_scope,
    )
    stmt = stmt.where(DocumentChunk.tsv_content.op("@@")(query_expr))
    stmt = stmt.order_by(rank_expr.desc()).limit(top_k)

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
