"""src/rag_kb/api/knowledge_bases.py —— 当前员工可用知识库查询接口。

暴露 GET /api/v1/knowledge-bases：
返回当前员工具备指定动作权限（默认 READ；支持 UPLOAD 等）的知识库列表，
包含来源标记（PUBLIC / DEPARTMENT / SHARED_ACL）、密级区间、逐库五动作能力矩阵以及允许上传的密级列表。
"""

from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from rag_kb.api.deps import EffectiveAuthzContext, get_current_user_with_db
from rag_kb.api.schemas import AccessibleKnowledgeBaseSchema
from rag_kb.core.authz import list_accessible_kbs
from rag_kb.core.constants import ACTION_TO_PERMISSION, KbAction, Permissions
from rag_kb.db.session import get_db

router = APIRouter(prefix="/knowledge-bases", tags=["知识库目录"])

VALID_ACTIONS = {a.value for a in KbAction}


@router.get("", response_model=List[AccessibleKnowledgeBaseSchema])
async def get_my_accessible_knowledge_bases(
    action: Optional[str] = Query(
        default="READ",
        description="筛选动作类型: READ | UPLOAD | REVIEW | DELETE | MANAGE",
    ),
    db: AsyncSession = Depends(get_db),
    user: EffectiveAuthzContext = Depends(get_current_user_with_db),
):
    """获取当前员工有权访问/操作的知识库列表（按请求动作校验对应操作权限点并由 PDP 实时裁决）"""
    action_upper = (action or "READ").strip().upper()
    if action_upper not in VALID_ACTIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"不支持的 action 参数: {action}，仅支持 {sorted(VALID_ACTIONS)}",
        )

    required_perm = ACTION_TO_PERMISSION[action_upper]
    has_required_perm = user.has_permission(required_perm) or (
        action_upper == KbAction.MANAGE.value and user.has_permission(Permissions.SYSTEM_ADMIN)
    )
    if not has_required_perm:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"【权限不足】查询 action={action_upper} 知识库列表需要 [{required_perm}] 权限点",
        )

    items = await list_accessible_kbs(db, user, action=action_upper)
    return [
        AccessibleKnowledgeBaseSchema(
            id=str(item.kb.id),
            tenant_id=item.kb.tenant_id,
            name=item.kb.name,
            description=item.kb.description,
            owner_department_id=str(item.kb.owner_department_id) if item.kb.owner_department_id else None,
            owner_dept_path=item.kb.owner_dept_path,
            visibility=item.kb.visibility,
            source_type=item.source_type,
            min_classification_level=item.kb.min_classification_level,
            default_doc_level=item.kb.default_doc_level,
            max_doc_level=item.kb.max_doc_level,
            status=item.kb.status,
            can_read=item.can_read,
            can_upload=item.can_upload,
            can_review=item.can_review,
            can_delete=item.can_delete,
            can_manage=item.can_manage,
            allowed_upload_levels=item.allowed_upload_levels,
        )
        for item in items
    ]
