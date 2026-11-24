"""src/rag_kb/core/authz/kb_resolver.py —— 知识库能力矩阵解析、上传目标库校验与检索收窄校验。

全部知识库可见性与五动作能力矩阵均单条 SQL 复用 sql_filters.py 谓词计算 (SSOT)。
"""

import uuid
from typing import List, Optional, Sequence, Tuple
from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from rag_kb.core.authz.guards import assert_kb_access
from rag_kb.core.authz.models import AccessibleKnowledgeBase, EffectiveAuthzContext
from rag_kb.core.authz.sql_filters import (
    build_kb_access_condition,
    build_kb_source_type_expr,
)
from rag_kb.core.constants import KbAction
from rag_kb.core.logging.audit import record_denial_audit
from rag_kb.db.models import KnowledgeBase


async def list_accessible_kbs(
    db: AsyncSession,
    ctx: EffectiveAuthzContext,
    action: str = KbAction.READ.value,
) -> List[AccessibleKnowledgeBase]:
    """单条 SQL 返回当前员工对指定动作有权访问的全部知识库及逐库五动作能力矩阵"""
    action_upper = str(action).upper().strip()
    stmt = (
        select(
            KnowledgeBase,
            build_kb_source_type_expr(ctx, action_upper).label("source_type"),
            build_kb_access_condition(ctx, KbAction.READ.value).label("can_read"),
            build_kb_access_condition(ctx, KbAction.UPLOAD.value).label("can_upload"),
            build_kb_access_condition(ctx, KbAction.REVIEW.value).label("can_review"),
            build_kb_access_condition(ctx, KbAction.DELETE.value).label("can_delete"),
            build_kb_access_condition(ctx, KbAction.MANAGE.value).label("can_manage"),
        )
        .where(build_kb_access_condition(ctx, action_upper))
        .order_by(KnowledgeBase.visibility.asc(), KnowledgeBase.name.asc())
    )
    rows = (await db.execute(stmt)).all()

    results: List[AccessibleKnowledgeBase] = []
    for kb, source_type, can_read, can_upload, can_review, can_delete, can_manage in rows:
        allowed_levels: List[int] = []
        if can_upload:
            upper = min(int(kb.max_doc_level), int(ctx.effective_clearance))
            if upper >= int(kb.min_classification_level):
                allowed_levels = list(range(int(kb.min_classification_level), upper + 1))

        results.append(
            AccessibleKnowledgeBase(
                kb=kb,
                source_type=str(source_type or "SHARED_ACL"),
                can_read=bool(can_read),
                can_upload=bool(can_upload),
                can_review=bool(can_review),
                can_delete=bool(can_delete),
                can_manage=bool(can_manage),
                allowed_upload_levels=allowed_levels,
            )
        )
    return results


def validate_doc_classification_on_write(
    ctx: EffectiveAuthzContext,
    kb: KnowledgeBase,
    requested_level: Optional[int],
) -> int:
    """上传定密校验：落入 [kb.min, kb.max] 且 <= ctx.effective_clearance"""
    level = int(requested_level) if requested_level is not None else int(kb.default_doc_level)
    if level < 1 or level > 4:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="文档密级必须在 1..4 (L1-L4) 之间")
    if level < int(kb.min_classification_level) or level > int(kb.max_doc_level):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"【密级边界拦截】文档密级 L{level} 不在目标知识库 [{kb.name}] 允许区间 [L{kb.min_classification_level}, L{kb.max_doc_level}] 内",
        )
    if level > int(ctx.effective_clearance):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"【密级边界拦截】拟上传文档密级 L{level} 超出您当前的访问等级上限 L{ctx.effective_clearance}",
        )
    return level


async def resolve_authorized_target_kb(
    db: AsyncSession,
    ctx: EffectiveAuthzContext,
    kb_id: Optional[str] = None,
    classification_level: Optional[int] = None,
) -> Tuple[KnowledgeBase, int]:
    """解析上传目标知识库并强制执行 AllowKB(UPLOAD) + 定密边界校验"""
    target_kb: Optional[KnowledgeBase] = None

    if kb_id and str(kb_id).strip():
        try:
            kb_uuid = uuid.UUID(str(kb_id).strip())
        except ValueError:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="无效的 kb_id UUID 格式")
        target_kb = (
            await db.execute(
                select(KnowledgeBase).where(KnowledgeBase.tenant_id == ctx.tenant_id, KnowledgeBase.id == kb_uuid)
            )
        ).scalar_one_or_none()
        if target_kb is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="指定的知识库不存在或不属于当前租户")
    else:
        uploadable = await list_accessible_kbs(db, ctx, action=KbAction.UPLOAD.value)
        if not uploadable:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="【越权访问拦截】您当前没有任何具备 UPLOAD 权限的知识库")
        primary_dept_kbs = [
            item.kb
            for item in uploadable
            if ctx.dept_path
            and item.kb.visibility == "DEPARTMENT"
            and item.kb.owner_dept_path == ctx.dept_path
        ]
        if len(primary_dept_kbs) != 1:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="无法确定唯一的主部门默认上传知识库，请显式指定 kb_id",
            )
        target_kb = primary_dept_kbs[0]

    await assert_kb_access(db, ctx, target_kb, KbAction.UPLOAD.value, audit_denial=True)
    validated_level = validate_doc_classification_on_write(ctx, target_kb, classification_level)
    return target_kb, validated_level


async def validate_requested_kb_ids(
    db: AsyncSession,
    ctx: EffectiveAuthzContext,
    raw_kb_ids: Optional[Sequence[str]],
    action: str = KbAction.READ.value,
) -> Optional[List[uuid.UUID]]:
    """校验检索或列表接口显式传入的 kb_ids，任一无权或跨租户立即 403 并记审计"""
    if not raw_kb_ids:
        return None

    parsed_ids: List[uuid.UUID] = []
    seen = set()
    for raw in raw_kb_ids:
        s = str(raw or "").strip()
        if not s:
            continue
        try:
            u = uuid.UUID(s)
        except ValueError:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"无效的知识库 ID: {s}")
        if u not in seen:
            seen.add(u)
            parsed_ids.append(u)

    if not parsed_ids:
        return None

    for kb_uuid in parsed_ids:
        kb = (
            await db.execute(
                select(KnowledgeBase).where(KnowledgeBase.tenant_id == ctx.tenant_id, KnowledgeBase.id == kb_uuid)
            )
        ).scalar_one_or_none()
        if kb is None:
            await record_denial_audit(
                action=f"AUTHZ_KB_{str(action).upper()}_DENIED",
                resource_id=str(kb_uuid),
                error_msg="请求的知识库不存在或跨租户越权",
                details={"requested_kb_id": str(kb_uuid)},
                tenant_id=ctx.tenant_id,
                user_id=ctx.user_id,
            )
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"【越权访问拦截】无权访问指定的知识库: {kb_uuid}")
        await assert_kb_access(db, ctx, kb, action, audit_denial=True)

    return parsed_ids
