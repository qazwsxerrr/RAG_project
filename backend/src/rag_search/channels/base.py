"""src/rag_search/channels/base.py —— 四路召回通道的公共基座。

四条通道查的都是同一张 chunks 表，差别只在怎么算分与排序，所以把行转候选 make_candidate、
业务过滤 apply_filters 等基础操作统一收敛在此处。
定义贯穿融合 / 重排 / MMR 的标准候选字典数据结构契约。
"""

from typing import Any, Dict, List, Optional
from sqlalchemy import select, or_, cast, String
from sqlalchemy.dialects.postgresql import ARRAY
from rag_kb.db.models import Document, DocumentChunk

def apply_filters(
    stmt,
    department_scope: Optional[List[str]] = None,
    category_scope: Optional[str] = None,
    tags_scope: Optional[List[str]] = None
):
    """注入状态、部门权限、分类及多维标签前置过滤谓词"""
    stmt = stmt.join(Document, DocumentChunk.document_id == Document.id)
    stmt = stmt.where(
        Document.overall_status.in_(["active", "completed"]),
        Document.is_active.is_(True)
    )

    if department_scope:
        dept_conditions = [
            DocumentChunk.department_path.startswith(dept.strip())
            for dept in department_scope
            if dept and dept.strip()
        ]
        if dept_conditions:
            stmt = stmt.where(or_(*dept_conditions))

    if category_scope and category_scope.strip() and category_scope.strip() != "全部":
        stmt = stmt.where(Document.category == category_scope.strip())

    if tags_scope:
        clean_tags = [t.strip() for t in tags_scope if t and t.strip()]
        if clean_tags:
            stmt = stmt.where(Document.tags.op("?|")(cast(clean_tags, ARRAY(String))))

    return stmt

def make_candidate(
    chunk_obj: DocumentChunk,
    doc_title: str,
    doc_oss_url: Optional[str],
    score: float,
    channel_name: str,
    rank: int
) -> Dict[str, Any]:
    """将数据库切片实体标准化为统一的候选切片字典"""
    target_page = (chunk_obj.page_idx or 0) + 1
    return {
        "chunk_id": str(chunk_obj.id),
        "document_id": str(chunk_obj.document_id),
        "document_title": doc_title,
        "chunk_index": chunk_obj.chunk_index,
        "chunk_label": chunk_obj.chunk_label,
        "content": chunk_obj.content,
        "page_idx": chunk_obj.page_idx,
        "display_page": target_page,
        "bbox": chunk_obj.bbox or [],
        "breadcrumb": chunk_obj.breadcrumb or [],
        "asset_url": chunk_obj.asset_url,
        "raw_oss_url": doc_oss_url,
        "department_path": chunk_obj.department_path,
        "score": round(float(score), 6),
        "channel": channel_name,
        "channel_ranks": {channel_name: rank},
        "dense_embedding": chunk_obj.dense_embedding,
        "remark": None
    }
