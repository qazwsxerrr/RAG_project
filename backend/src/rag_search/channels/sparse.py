"""src/rag_search/channels/sparse.py —— 四路召回中的归一化稀疏向量召回通道。

基于 JSONB 字典存储的稀疏词频权重与 L2 归一化内积打分，精确匹配高频关键词与专有名词。
"""

from typing import Any, Dict, List, Optional
from sqlalchemy import select, func, cast, String, Float, literal_column
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession
from rag_kb.db.models import Document, DocumentChunk
from rag_search.channels.base import apply_filters, make_candidate

NAME = "sparse"

async def search(
    session: AsyncSession,
    query_sparse: Dict[str, float],
    department_scope: Optional[List[str]] = None,
    category_scope: Optional[str] = None,
    tags_scope: Optional[List[str]] = None,
    top_k: int = 20
) -> List[Dict[str, Any]]:
    """稀疏向量归一化词频内积召回 Top-K"""
    if not query_sparse:
        return []

    tokens = list(query_sparse.keys())
    if not tokens:
        return []

    token_array = cast(tokens, ARRAY(String))

    # 相关性初筛与有序截断，取前 100 条进入内存点积重排
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
        Document.raw_oss_url.label("doc_raw_oss_url")
    )
    stmt = apply_filters(stmt, department_scope, category_scope, tags_scope)
    stmt = stmt.where(DocumentChunk.sparse_vector.op("?|")(token_array))
    stmt = stmt.order_by(
        match_count_subq.desc(),
        weight_sum_subq.desc(),
        DocumentChunk.chunk_index.asc(),
        DocumentChunk.id.asc()
    ).limit(100)

    res = await session.execute(stmt)
    rows = res.all()

    # 计算 L2 归一化内积点积
    scored_candidates = []
    for row in rows:
        chunk_obj: DocumentChunk = row[0]
        doc_title: str = row[1]
        doc_oss_url: Optional[str] = row[2]
        doc_sp = chunk_obj.sparse_vector or {}

        dot_product = sum(query_sparse[k] * doc_sp.get(k, 0.0) for k in tokens if k in doc_sp)
        if dot_product > 0.0:
            scored_candidates.append((dot_product, chunk_obj, doc_title, doc_oss_url))

    scored_candidates.sort(key=lambda x: x[0], reverse=True)
    top_candidates = scored_candidates[:top_k]

    chunks: List[Dict[str, Any]] = []
    for idx, (score, chunk_obj, doc_title, doc_oss_url) in enumerate(top_candidates):
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
