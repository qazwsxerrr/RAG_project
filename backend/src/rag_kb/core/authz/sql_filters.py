"""src/rag_kb/core/authz/sql_filters.py —— 将统一授权判定公式 (§2.4) 编译为参数化 SQLAlchemy 过滤谓词。

核心安全不变量：
1. 严禁出现 "/" 全放行或跳过隔离的分支；
2. 所有切片查询必须强制 JOIN Document 再以 (Document.kb_id = KnowledgeBase.id AND Document.tenant_id = KnowledgeBase.tenant_id) JOIN KnowledgeBase；
3. 部门子树匹配使用 PostgreSQL 16 原生 starts_with(path, prefix || '/') 按完整路径段匹配，免疫同名前缀兄弟部门与 LIKE 通配符伪造；
4. 强制双重交集：密级门槛 (effective_clearance >= GREATEST(kb.min_classification_level, doc.classification_level))
   AND ¬active_DENY AND (PUBLIC∧READ ∨ DEPARTMENT∧match_scope(scope_by_action[action]) ∨ active_ALLOW)；
5. 自然到期的 kb_grants (expires_at <= NOW()) 在 SQL 层即时失效，不依赖版本变更。
"""

import uuid
from typing import Any, List, Optional, Sequence
from sqlalchemy import and_, case, or_, not_, exists, select, func, false, literal
from rag_kb.core.constants import DataScope, KbAction
from rag_kb.db.models import Document, KnowledgeBase, KbGrant


def _normalize_path(raw: Optional[str]) -> str:
    if not raw:
        return ""
    s = raw.strip()
    if not s or s == "/":
        return ""
    if not s.startswith("/"):
        return ""
    parts = [p for p in s.split("/") if p]
    return "/" + "/".join(parts) if parts else ""


def build_grant_subject_match(ctx: Any):
    """构造 kb_grants 主体匹配条件（命中员工 / 任一有效角色 / 任一有效归属部门[含/不含子部门]）"""
    subject_conds = [
        and_(
            KbGrant.subject_type == "USER",
            KbGrant.subject_id == str(ctx.user_id),
        )
    ]

    roles = [r for r in (ctx.roles or []) if r]
    if roles:
        subject_conds.append(
            and_(
                KbGrant.subject_type == "ROLE",
                KbGrant.subject_id.in_(roles),
            )
        )

    clean_dept_paths = [_normalize_path(p) for p in (ctx.dept_paths or [])]
    clean_dept_paths = [p for p in clean_dept_paths if p]
    clean_dept_ids = [str(d) for d in (getattr(ctx, "dept_ids", None) or []) if d]

    if clean_dept_paths or clean_dept_ids:
        exact_conds = []
        if clean_dept_ids:
            exact_conds.append(KbGrant.subject_id.in_(clean_dept_ids))
        if clean_dept_paths:
            exact_conds.append(KbGrant.subject_dept_path.in_(clean_dept_paths))
        subject_conds.append(
            and_(
                KbGrant.subject_type == "DEPARTMENT",
                KbGrant.include_sub_depts.is_(False),
                or_(*exact_conds),
            )
        )

        if clean_dept_paths:
            sub_match_conds = []
            for user_path in clean_dept_paths:
                # 员工部门 user_path 位于授权部门 subject_dept_path 本级或其子部门下
                sub_match_conds.append(
                    or_(
                        KbGrant.subject_dept_path == user_path,
                        func.starts_with(literal(user_path), KbGrant.subject_dept_path + literal("/")),
                    )
                )
            subject_conds.append(
                and_(
                    KbGrant.subject_type == "DEPARTMENT",
                    KbGrant.include_sub_depts.is_(True),
                    KbGrant.subject_dept_path.isnot(None),
                    or_(*sub_match_conds),
                )
            )

    return or_(*subject_conds)


def _build_active_grant_exists(ctx: Any, action: str, effect: str):
    """构造未过期且命中当前用户的 kb_grants EXISTS 子查询"""
    return exists(
        select(literal(1)).where(
            KbGrant.tenant_id == KnowledgeBase.tenant_id,
            KbGrant.kb_id == KnowledgeBase.id,
            KbGrant.action == action,
            KbGrant.effect == effect,
            or_(KbGrant.expires_at.is_(None), KbGrant.expires_at > func.now()),
            build_grant_subject_match(ctx),
        )
    )


def _build_dept_scope_match(scope: str, dept_paths: Sequence[str]):
    """将 scope_by_action[action] 编译为对 KnowledgeBase.owner_dept_path 的 SQL 过滤谓词"""
    if scope == DataScope.ALL_ORG.value:
        return literal(True)
    if scope == DataScope.CUSTOM_ACL.value:
        return false()

    clean_paths = [_normalize_path(p) for p in dept_paths]
    clean_paths = [p for p in clean_paths if p]
    if not clean_paths:
        return false()

    if scope == DataScope.DEPT_AND_SUB.value:
        conds = [
            or_(
                KnowledgeBase.owner_dept_path == p,
                func.starts_with(KnowledgeBase.owner_dept_path, p + "/"),
            )
            for p in clean_paths
        ]
        return or_(*conds)

    return false()


def build_kb_access_condition(
    ctx: Any,
    action: str = KbAction.READ.value,
    kb_ids: Optional[Sequence[uuid.UUID]] = None,
):
    """将 AllowKB(user, action, kb) 编译为 SQLAlchemy 布尔表达式"""
    if not getattr(ctx, "is_active", False):
        return false()

    scope_by_action = getattr(ctx, "scope_by_action", None) or {}
    action_upper = str(action).upper().strip()
    if action_upper not in scope_by_action:
        return false()

    action_scope = scope_by_action[action_upper]
    effective_clearance = int(getattr(ctx, "effective_clearance", 0) or 0)
    if effective_clearance < 1:
        return false()

    deny_exists = _build_active_grant_exists(ctx, action_upper, "DENY")
    allow_exists = _build_active_grant_exists(ctx, action_upper, "ALLOW")

    visibility_branches = [allow_exists]
    if action_upper == KbAction.READ.value or action_scope == DataScope.ALL_ORG.value:
        visibility_branches.append(KnowledgeBase.visibility == "PUBLIC")

    dept_scope_cond = _build_dept_scope_match(action_scope, getattr(ctx, "dept_paths", []) or [])
    visibility_branches.append(
        and_(
            KnowledgeBase.visibility == "DEPARTMENT",
            dept_scope_cond,
        )
    )

    conds = [
        KnowledgeBase.tenant_id == ctx.tenant_id,
        KnowledgeBase.status == "ACTIVE",
        KnowledgeBase.min_classification_level <= effective_clearance,
        not_(deny_exists),
        or_(*visibility_branches),
    ]

    if kb_ids:
        conds.append(KnowledgeBase.id.in_(list(kb_ids)))

    return and_(*conds)


def build_kb_source_type_expr(ctx: Any, action: str = KbAction.READ.value):
    """返回知识库授权来源标签 (PUBLIC / DEPARTMENT / SHARED_ACL) 的 SQL 表达式"""
    action_upper = str(action).upper().strip()
    action_scope = (getattr(ctx, "scope_by_action", None) or {}).get(action_upper, "")
    dept_match = _build_dept_scope_match(action_scope, getattr(ctx, "dept_paths", []) or [])
    return case(
        (
            and_(
                KnowledgeBase.visibility == "PUBLIC",
                or_(literal(action_upper == KbAction.READ.value), literal(action_scope == DataScope.ALL_ORG.value)),
            ),
            literal("PUBLIC"),
        ),
        (and_(KnowledgeBase.visibility == "DEPARTMENT", dept_match), literal("DEPARTMENT")),
        else_=literal("SHARED_ACL"),
    )


def build_document_access_condition(
    ctx: Any,
    action: str = KbAction.READ.value,
    kb_ids: Optional[Sequence[uuid.UUID]] = None,
):
    """将 AllowDoc(user, action, kb, doc) 编译为 SQLAlchemy 布尔表达式（假设已 JOIN KnowledgeBase）"""
    effective_clearance = int(getattr(ctx, "effective_clearance", 0) or 0)
    kb_cond = build_kb_access_condition(
        ctx=ctx,
        action=action,
        kb_ids=kb_ids,
    )
    return and_(
        Document.tenant_id == ctx.tenant_id,
        Document.kb_id == KnowledgeBase.id,
        Document.tenant_id == KnowledgeBase.tenant_id,
        kb_cond,
        func.greatest(KnowledgeBase.min_classification_level, Document.classification_level) <= effective_clearance,
    )


def apply_document_authz_join_and_filter(
    stmt,
    ctx: Any,
    action: str = KbAction.READ.value,
    kb_ids: Optional[Sequence[uuid.UUID]] = None,
):
    """对以 Document 为主表（或已 JOIN Document）的查询强制关联 KnowledgeBase 并施加多维授权过滤"""
    stmt = stmt.join(
        KnowledgeBase,
        and_(
            Document.kb_id == KnowledgeBase.id,
            Document.tenant_id == KnowledgeBase.tenant_id,
        ),
    )
    stmt = stmt.where(
        build_document_access_condition(
            ctx=ctx,
            action=action,
            kb_ids=kb_ids,
        )
    )
    return stmt
