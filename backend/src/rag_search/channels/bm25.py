"""src/rag_search/channels/bm25.py —— 四路召回中的全文倒排索引召回通道。

面向专有名词、编号、生僻术语这类向量召回易漏的场景，基于 PostgreSQL TSVector GIN 倒排索引
与 ts_rank_cd 函数做字面精确召回。不依赖 embedding 接口，是系统的兜底召回路径。
"""

from typing import Any, Dict, List, Optional
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from rag_kb.db.models import Document, DocumentChunk
from rag_search.channels.base import apply_filters, make_candidate

NAME = "bm25"

async def search(
    session: AsyncSession,
    tsquery_tokens: str,
    department_scope: Optional[List[str]] = None,
    category_scope: Optional[str] = None,
    tags_scope: Optional[List[str]] = None,
    top_k: int = 20
) -> List[Dict[str, Any]]:
    """全文倒排关键词召回 Top-K"""
    tokens = [t.strip() for t in tsquery_tokens.split() if t.strip()]
    if not tokens:
        return []

    or_tsquery = " | ".join(tokens)
    query_expr = func.to_tsquery("simple", or_tsquery)
    rank_expr = func.ts_rank_cd(DocumentChunk.tsv_content, query_expr).label("score")

    stmt = select(
        DocumentChunk,
        Document.title.label("doc_title"),
        Document.raw_oss_url.label("doc_raw_oss_url"),
        rank_expr
    )
    stmt = apply_filters(stmt, department_scope, category_scope, tags_scope)
    stmt = stmt.where(DocumentChunk.tsv_content.op("@@")(query_expr))
    stmt = stmt.order_by(rank_expr.desc()).limit(top_k)

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
