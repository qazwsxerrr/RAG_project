"""src/rag_kb/api/sessions.py —— 对话会话与历史消息 HTTP 端点层（含逐条来源权限实时复核）。"""

from typing import Optional
from fastapi import APIRouter, HTTPException, Query, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from rag_kb.api.deps import EffectiveAuthzContext, require_permission
from rag_kb.core.constants import Permissions
from rag_kb.db.session import get_db
from rag_kb.db.chat_store import (
    create_session,
    list_sessions,
    get_session,
    get_authorized_messages,
    update_session,
    delete_session,
)

router = APIRouter(prefix="/sessions", tags=["会话历史"])


class CreateSessionRequest(BaseModel):
    title: Optional[str] = "新会话"


class UpdateSessionRequest(BaseModel):
    title: Optional[str] = None


@router.get("")
async def get_sessions(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: EffectiveAuthzContext = Depends(require_permission(Permissions.KB_RETRIEVE)),
):
    """获取当前登录员工专属会话列表 (强制绑定 tenant_id + user_id)"""
    return await list_sessions(tenant_id=user.tenant_id, user_id=user.user_id, limit=limit, offset=offset)


@router.post("")
async def create_new_session(
    req: Optional[CreateSessionRequest] = None,
    user: EffectiveAuthzContext = Depends(require_permission(Permissions.KB_RETRIEVE)),
):
    """创建新会话 (租户与用户归属严格绑定 Token)"""
    title = req.title if req else "新会话"
    return await create_session(tenant_id=user.tenant_id, user_id=user.user_id, title=title)


@router.delete("/{session_id}")
async def remove_session(
    session_id: str,
    user: EffectiveAuthzContext = Depends(require_permission(Permissions.KB_RETRIEVE)),
):
    """删除会话 (仅允许删除当前员工本人的会话)"""
    ok = await delete_session(session_id=session_id, tenant_id=user.tenant_id, user_id=user.user_id)
    if not ok:
        raise HTTPException(status_code=404, detail="未找到该会话或无权删除他人会话")
    return {"status": "success", "message": f"会话 {session_id} 已删除"}


@router.patch("/{session_id}")
async def update_session_info(
    session_id: str,
    req: UpdateSessionRequest,
    user: EffectiveAuthzContext = Depends(require_permission(Permissions.KB_RETRIEVE)),
):
    """更新会话元数据（仅允许修改当前员工本人的会话）"""
    updated = await update_session(
        session_id=session_id,
        title=req.title,
        tenant_id=user.tenant_id,
        user_id=user.user_id,
    )
    if not updated:
        raise HTTPException(status_code=404, detail="未找到该会话或无权修改他人会话")
    return updated


@router.get("/{session_id}/messages")
async def get_session_messages(
    session_id: str,
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    user: EffectiveAuthzContext = Depends(require_permission(Permissions.KB_RETRIEVE)),
):
    """获取经当前 READ 权限逐条复核的会话历史消息列表（失权或无法核实来源的助手回答自动屏蔽）"""
    sess = await get_session(session_id=session_id, tenant_id=user.tenant_id, user_id=user.user_id)
    if not sess:
        raise HTTPException(status_code=404, detail="未找到该会话或无权访问他人会话")
    return await get_authorized_messages(session_id=session_id, ctx=user, limit=limit, db=db)
