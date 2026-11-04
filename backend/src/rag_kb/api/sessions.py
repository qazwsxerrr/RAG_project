"""src/rag_kb/api/sessions.py —— 对话会话与历史消息 HTTP 端点层。

Phase-6 §5.4 严格规范：
- tenant_id 与 user_id 全部由当前登录用户的 JWT Token 派生，彻底删除 Query/Body 入参；
- 挂载 kb:retrieve 鉴权依赖；
- 所有端点强制校验 (tenant_id, user_id) 归属，防止同租户员工横向越权查看或删除他人会话 (BOLA)。
"""

from typing import Optional
from fastapi import APIRouter, HTTPException, Query, Depends
from pydantic import BaseModel
from rag_kb.api.deps import UserContext, require_permission
from rag_kb.db.chat_store import (
    create_session,
    list_sessions,
    get_session,
    get_messages,
    update_session,
    delete_session
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
    user: UserContext = Depends(require_permission("kb:retrieve"))
):
    """获取当前登录员工专属会话列表 (强制绑定 tenant_id + user_id)"""
    return await list_sessions(tenant_id=user.tenant_id, user_id=user.user_id, limit=limit, offset=offset)

@router.post("")
async def create_new_session(
    req: Optional[CreateSessionRequest] = None,
    user: UserContext = Depends(require_permission("kb:retrieve"))
):
    """创建新会话 (租户与用户归属严格绑定 Token)"""
    title = req.title if req else "新会话"
    return await create_session(tenant_id=user.tenant_id, user_id=user.user_id, title=title)

@router.delete("/{session_id}")
async def remove_session(
    session_id: str,
    user: UserContext = Depends(require_permission("kb:retrieve"))
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
    user: UserContext = Depends(require_permission("kb:retrieve"))
):
    """更新会话元数据（仅允许修改当前员工本人的会话）"""
    updated = await update_session(
        session_id=session_id,
        title=req.title,
        tenant_id=user.tenant_id,
        user_id=user.user_id
    )
    if not updated:
        raise HTTPException(status_code=404, detail="未找到该会话或无权修改他人会话")
    return updated

@router.get("/{session_id}/messages")
async def get_session_messages(
    session_id: str,
    limit: int = Query(50, ge=1, le=200),
    user: UserContext = Depends(require_permission("kb:retrieve"))
):
    """获取指定会话的历史消息列表 (校验会话存在且归属于当前登录员工)"""
    sess = await get_session(session_id=session_id, tenant_id=user.tenant_id, user_id=user.user_id)
    if not sess:
        raise HTTPException(status_code=404, detail="未找到该会话或无权访问他人会话 (BOLA Blocked)")
    return await get_messages(session_id=session_id, limit=limit)
