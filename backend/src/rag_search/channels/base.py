"""src/rag_search/channels/base.py —— 四路召回通道的公共基座（全面接入多维权限 SQL PDP）。

核心安全不变量：
1. 彻底移除 "/" 全放行逻辑；
2. 强制 JOIN Document 与 JOIN KnowledgeBase，统一委托 rag_kb.core.authz.sql_filters 施加逐动作组织范围 + KB ACL + L1-L4 密级前置过滤；
3. 缺少 authz_filter_spec 时坚决 Fail-Closed 抛错拦截；
4. make_candidate 不再输出 raw_oss_url，附加 kb_id / kb_name / classification_level。
"""

import uuid
from typing import Any, Dict, List, Optional, Sequence
from sqlalchemy import cast, String
from sqlalchemy.dialects.postgresql import ARRAY

from rag_kb.core.authz.models import format_kb_full_path
from rag_kb.core.authz.sql_filters import apply_document_authz_join_and_filter
from rag_kb.core.constants import KbAction
from rag_kb.db.models import Document, DocumentChunk


def apply_filters(
    stmt,
    authz_filter_spec: Any = None,
    kb_ids: Optional[Sequence[uuid.UUID]] = None,
    category_scope: Optional[str] = None,
    tags_scope: Optional[List[str]] = None,
):
    """注入状态、多维权限（租户 + 多部门 + KB ACL + L1-L4 密级）、分类及标签前置过滤谓词"""
    if authz_filter_spec is None:
        raise ValueError("【数据隔离安全拦截】缺少 EffectiveAuthzContext 授权过滤上下文 (Fail-Closed)")

    stmt = stmt.join(Document, DocumentChunk.document_id == Document.id)
    stmt = stmt.where(
        Document.overall_status.in_(["active", "completed"]),
        Document.is_active.is_(True),
    )

    stmt = apply_document_authz_join_and_filter(
        stmt=stmt,
        ctx=authz_filter_spec,
        action=KbAction.READ.value,
        kb_ids=kb_ids,
    )

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
    kb_id: str,
    kb_name: str,
    effective_classification_level: int,
    score: float,
    channel_name: str,
    rank: int,
) -> Dict[str, Any]:
    """将数据库切片实体标准化为统一的候选切片字典（彻底剥离 raw_oss_url）"""
    target_page = (chunk_obj.page_idx or 0) + 1
    return {
        "chunk_id": str(chunk_obj.id),
        "document_id": str(chunk_obj.document_id),
        "document_title": doc_title,
        "kb_id": str(kb_id),
        "kb_name": kb_name,
        "kb_full_path": format_kb_full_path(chunk_obj.department_path, kb_name),
        "classification_level": int(effective_classification_level),
        "chunk_index": chunk_obj.chunk_index,
        "chunk_label": chunk_obj.chunk_label,
        "content": chunk_obj.content,
        "page_idx": chunk_obj.page_idx,
        "display_page": target_page,
        "bbox": chunk_obj.bbox or [],
        "breadcrumb": chunk_obj.breadcrumb or [],
        "asset_url": chunk_obj.asset_url,
        "is_table": bool(getattr(chunk_obj, "is_table", False)),
        "table_html": getattr(chunk_obj, "table_html", None),
        "department_path": chunk_obj.department_path,
        "score": round(float(score), 6),
        "channel": channel_name,
        "channel_ranks": {channel_name: rank},
        "dense_embedding": chunk_obj.dense_embedding,
        "remark": None,
    }
