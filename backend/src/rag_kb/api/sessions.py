"""src/rag_kb/api/sessions.py —— 对话会话与历史消息 HTTP 端点层。

前缀 /api/v1/sessions：
- GET /sessions: 查询租户下所有历史会话列表
- POST /sessions: 创建新会话
- DELETE /sessions/{session_id}: 删除指定会话及其消息
- GET /sessions/{session_id}/messages: 查询会话内的历史多轮对话
"""

from typing import List, Optional
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from rag_kb.db.chat_store import (
    create_session,
    list_sessions,
    get_messages,
    delete_session
)

router = APIRouter(prefix="/sessions", tags=["会话历史"])

class CreateSessionRequest(BaseModel):
    title: Optional[str] = "新会话"
    tenant_id: str = "default_tenant"

@router.get("")
async def get_sessions(
    tenant_id: str = Query("default_tenant"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0)
):
    """获取会话列表"""
    return await list_sessions(tenant_id=tenant_id, limit=limit, offset=offset)

@router.post("")
async def create_new_session(req: Optional[CreateSessionRequest] = None):
    """创建新会话"""
    title = req.title if req else "新会话"
    tenant_id = req.tenant_id if req else "default_tenant"
    return await create_session(tenant_id=tenant_id, title=title)

@router.delete("/{session_id}")
async def remove_session(session_id: str, tenant_id: str = Query("default_tenant")):
    """删除会话"""
    ok = await delete_session(session_id=session_id, tenant_id=tenant_id)
    if not ok:
        raise HTTPException(status_code=404, detail="未找到该会话或已被删除")
    return {"status": "success", "message": f"会话 {session_id} 已删除"}

@router.get("/{session_id}/messages")
async def get_session_messages(
    session_id: str,
    limit: int = Query(50, ge=1, le=200)
):
    """获取指定会话的历史消息列表"""
    return await get_messages(session_id=session_id, limit=limit)
