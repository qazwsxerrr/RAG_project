"""src/rag_kb/core/authz/guards.py —— 知识库、文档、流水线对象级守卫与 L3/L4 强审计。

全部对象级权限校验均直接复用 sql_filters.py 单一事实源 (SSOT) 谓词执行主键点查，杜绝内存版与 SQL 版规则漂移。
"""

import uuid
from typing import Any, Dict, Optional, Tuple
from fastapi import HTTPException, status
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from rag_kb.core.authz.models import EffectiveAuthzContext
from rag_kb.core.authz.sql_filters import (
    build_document_access_condition,
    build_kb_access_condition,
)
from rag_kb.core.constants import ACTION_TO_PERMISSION
from rag_kb.core.logging.audit import (
    AuditWriteError,
    record_denial_audit,
    record_mandatory_security_audit,
)
from rag_kb.db.models import Document, KnowledgeBase


def _describe_kb_denial_reason(ctx: EffectiveAuthzContext, kb: KnowledgeBase, action_upper: str) -> str:
    if not ctx.is_active:
        return "员工账号未启用"
    if kb.tenant_id != ctx.tenant_id:
        return "跨租户知识库访问被拒绝"
    if kb.status != "ACTIVE":
        return f"知识库当前状态为 {kb.status}，不可访问"
    if action_upper not in (ctx.scope_by_action or {}):
        req_perm = ACTION_TO_PERMISSION.get(action_upper, action_upper)
        return f"当前有效角色未授予 [{req_perm}] 操作权限"
    if int(ctx.effective_clearance) < int(kb.min_classification_level):
        return f"访问密级不足：需要 L{kb.min_classification_level}，当前有效访问等级为 L{ctx.effective_clearance}"
    return "未命中知识库可见域或被显式 DENY 拒绝策略拦截"


async def assert_kb_access(
    db: AsyncSession,
    ctx: EffectiveAuthzContext,
    kb: Optional[KnowledgeBase],
    action: str,
    audit_denial: bool = True,
) -> KnowledgeBase:
    """直接复用 sql_filters.build_kb_access_condition 主键点查 AllowKB(user, action, kb)"""
    if kb is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="【资源不存在】指定的知识库不存在")

    action_upper = str(action).upper().strip()
    hit = (
        await db.execute(
            select(KnowledgeBase.id).where(
                KnowledgeBase.id == kb.id,
                build_kb_access_condition(ctx=ctx, action=action_upper),
            )
        )
    ).scalar_one_or_none()

    if hit is None:
        reason = _describe_kb_denial_reason(ctx, kb, action_upper)
        if audit_denial:
            await record_denial_audit(
                action=f"AUTHZ_KB_{action_upper}_DENIED",
                resource_id=str(kb.id),
                error_msg=reason,
                details={
                    "kb_id": str(kb.id),
                    "kb_name": kb.name,
                    "kb_visibility": kb.visibility,
                    "kb_min_level": kb.min_classification_level,
                    "action": action_upper,
                    "user_clearance": ctx.effective_clearance,
                    "user_dept_paths": ctx.dept_paths,
                    "user_roles": ctx.roles,
                },
                tenant_id=ctx.tenant_id,
                user_id=ctx.user_id,
            )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"【越权访问拦截】无权对知识库执行 {action_upper} 操作：{reason}",
        )
    return kb


async def assert_doc_access(
    db: AsyncSession,
    ctx: EffectiveAuthzContext,
    doc: Optional[Document],
    action: str,
    expected_kb_id: Optional[uuid.UUID] = None,
    audit_denial: bool = True,
) -> Tuple[KnowledgeBase, int]:
    """直接复用 sql_filters.build_document_access_condition 主键点查 AllowDoc(user, action, kb, doc)"""
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="【资源不存在】未找到指定的文档")

    action_upper = str(action).upper().strip()
    if doc.tenant_id != ctx.tenant_id:
        if audit_denial:
            await record_denial_audit(
                action=f"AUTHZ_DOC_{action_upper}_DENIED",
                resource_id=str(doc.id),
                error_msg="跨租户文档访问被拦截",
                details={"doc_id": str(doc.id), "doc_tenant_id": doc.tenant_id},
                tenant_id=ctx.tenant_id,
                user_id=ctx.user_id,
            )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="【越权访问拦截】禁止跨租户访问文档")

    if expected_kb_id is not None and doc.kb_id != expected_kb_id:
        if audit_denial:
            await record_denial_audit(
                action=f"AUTHZ_DOC_{action_upper}_DENIED",
                resource_id=str(doc.id),
                error_msg="对象所属知识库与请求上下文不匹配",
                details={"doc_id": str(doc.id), "real_kb_id": str(doc.kb_id), "expected_kb_id": str(expected_kb_id)},
                tenant_id=ctx.tenant_id,
                user_id=ctx.user_id,
            )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="【越权访问拦截】文档真实所属知识库与请求上下文不匹配")

    eff_level_expr = func.greatest(KnowledgeBase.min_classification_level, Document.classification_level)
    row = (
        await db.execute(
            select(KnowledgeBase, eff_level_expr)
            .select_from(Document)
            .join(
                KnowledgeBase,
                and_(Document.kb_id == KnowledgeBase.id, Document.tenant_id == KnowledgeBase.tenant_id),
            )
            .where(
                Document.id == doc.id,
                build_document_access_condition(ctx=ctx, action=action_upper),
            )
        )
    ).first()

    if row is None:
        kb = (
            await db.execute(
                select(KnowledgeBase).where(KnowledgeBase.tenant_id == doc.tenant_id, KnowledgeBase.id == doc.kb_id)
            )
        ).scalar_one_or_none()
        if kb is None:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="【安全拦截】文档所属知识库不存在或跨租户不一致")
        eff_level = max(int(kb.min_classification_level), int(doc.classification_level))
        reason = (
            f"单文档密级不足：文档生效密级为 L{eff_level}，当前有效访问等级为 L{ctx.effective_clearance}"
            if int(ctx.effective_clearance) < eff_level
            else _describe_kb_denial_reason(ctx, kb, action_upper)
        )
        if audit_denial:
            await record_denial_audit(
                action=f"AUTHZ_DOC_{action_upper}_DENIED",
                resource_id=str(doc.id),
                error_msg=reason,
                details={
                    "doc_id": str(doc.id),
                    "kb_id": str(kb.id),
                    "effective_doc_level": eff_level,
                    "user_clearance": ctx.effective_clearance,
                },
                tenant_id=ctx.tenant_id,
                user_id=ctx.user_id,
            )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"【越权访问拦截】{reason}")

    return row[0], int(row[1])


async def assert_pipeline_state_access(
    db: AsyncSession,
    ctx: EffectiveAuthzContext,
    state: Dict[str, Any],
    action: str,
    audit_denial: bool = True,
) -> Tuple[KnowledgeBase, int]:
    """对内存态在途入库流水线执行鉴权：若数据库已存在文档行，统一以数据库当前归属与密级为单一事实源"""
    raw_doc_id = state.get("doc_id")
    if raw_doc_id:
        try:
            doc_uuid = uuid.UUID(str(raw_doc_id))
        except ValueError:
            doc_uuid = None
        if doc_uuid is not None:
            db_doc = await db.get(Document, doc_uuid)
            if db_doc is not None:
                kb, effective_level = await assert_doc_access(
                    db, ctx, db_doc, action, audit_denial=audit_denial
                )
                state["tenant_id"] = db_doc.tenant_id
                state["kb_id"] = str(db_doc.kb_id)
                state["classification_level"] = int(db_doc.classification_level)
                return kb, effective_level

    state_tenant = str(state.get("tenant_id") or "")
    if state_tenant and state_tenant != ctx.tenant_id:
        if audit_denial:
            await record_denial_audit(
                action=f"AUTHZ_PIPELINE_{str(action).upper()}_DENIED",
                resource_id=str(state.get("doc_id") or ""),
                error_msg="跨租户流水线访问被拦截",
                details={"state_tenant": state_tenant},
                tenant_id=ctx.tenant_id,
                user_id=ctx.user_id,
            )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="【越权访问拦截】无权访问其他租户的入库流水线")

    raw_kb_id = state.get("kb_id")
    if not raw_kb_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="【安全拦截】流水线状态缺少 kb_id")
    try:
        kb_uuid = uuid.UUID(str(raw_kb_id))
    except ValueError:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="【安全拦截】流水线 kb_id 格式非法")

    kb = (
        await db.execute(select(KnowledgeBase).where(KnowledgeBase.tenant_id == ctx.tenant_id, KnowledgeBase.id == kb_uuid))
    ).scalar_one_or_none()
    await assert_kb_access(db, ctx, kb, action, audit_denial=audit_denial)

    doc_lvl = int(state.get("classification_level") or kb.default_doc_level)
    effective_level = max(int(kb.min_classification_level), doc_lvl)
    if int(ctx.effective_clearance) < effective_level:
        reason = f"流水线文档生效密级 L{effective_level} 高于当前访问等级 L{ctx.effective_clearance}"
        if audit_denial:
            await record_denial_audit(
                action=f"AUTHZ_PIPELINE_{str(action).upper()}_DENIED",
                resource_id=str(state.get("doc_id") or ""),
                error_msg=reason,
                details={"kb_id": str(kb.id), "effective_doc_level": effective_level, "user_clearance": ctx.effective_clearance},
                tenant_id=ctx.tenant_id,
                user_id=ctx.user_id,
            )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"【越权访问拦截】{reason}")

    return kb, effective_level


async def audit_high_classification_read(
    ctx: EffectiveAuthzContext,
    action: str,
    doc_id: str,
    kb_id: str,
    effective_level: int,
    extra_details: Optional[Dict[str, Any]] = None,
) -> None:
    """L3/L4 高密文档交付首字节前强制留痕审计，失败立即 500 阻断交付"""
    if int(effective_level) < 3:
        return
    details = {
        "kb_id": str(kb_id),
        "effective_doc_level": int(effective_level),
        "user_clearance": ctx.effective_clearance,
        "username": ctx.username,
    }
    if extra_details:
        details.update(extra_details)
    try:
        await record_mandatory_security_audit(
            action=action,
            resource_id=str(doc_id),
            status="SUCCESS",
            details=details,
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
        )
    except AuditWriteError as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"【高密安全审计失败】高密内容交付已中止: {e}",
        ) from e
