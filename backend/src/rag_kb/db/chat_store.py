"""src/rag_kb/db/chat_store.py —— 对话会话的数据访问层（DAO）。

把 chat_sessions / chat_messages 两张表的读写封装成 make_title()、create_session()、list_sessions()、
append_message()、delete_session() 等函数，向 api 层只暴露普通 dict，不交出 ORM 对象。
"""

import uuid
from typing import Dict, List, Optional, Any
from sqlalchemy import select, desc, delete
from rag_kb.db.session import AsyncSessionLocal
from rag_kb.db.models import ChatSession, ChatMessage

def make_title(query: str, max_len: int = 25) -> str:
    """根据首条用户查询自动生成会话标题"""
    clean = query.strip().replace("\n", " ")
    if len(clean) <= max_len:
        return clean
    return clean[:max_len] + "..."

async def create_session(tenant_id: str = "default_tenant", title: Optional[str] = None) -> Dict[str, Any]:
    """创建新会话"""
    from datetime import datetime, timezone
    session_id = uuid.uuid4()
    session_title = title or "新会话"
    now = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as db:
        new_sess = ChatSession(
            id=session_id,
            title=session_title,
            tenant_id=tenant_id,
            created_at=now,
            updated_at=now
        )
        db.add(new_sess)
        await db.commit()
        return {
            "id": str(new_sess.id),
            "title": new_sess.title,
            "tenant_id": new_sess.tenant_id,
            "created_at": new_sess.created_at.isoformat(),
            "updated_at": new_sess.updated_at.isoformat()
        }

async def list_sessions(tenant_id: str = "default_tenant", limit: int = 20, offset: int = 0) -> List[Dict[str, Any]]:
    """查询指定租户的会话列表"""
    async with AsyncSessionLocal() as db:
        stmt = (
            select(ChatSession)
            .where(ChatSession.tenant_id == tenant_id)
            .order_by(desc(ChatSession.updated_at))
            .limit(limit)
            .offset(offset)
        )
        res = await db.execute(stmt)
        sessions = res.scalars().all()
        return [
            {
                "id": str(s.id),
                "title": s.title,
                "tenant_id": s.tenant_id,
                "created_at": s.created_at.isoformat(),
                "updated_at": s.updated_at.isoformat()
            }
            for s in sessions
        ]

def to_session_uuid(session_id: str) -> uuid.UUID:
    """确保 session_id 为合法的 UUID，若为前端格式如 session_123 则生成确定性的命名空间 UUID"""
    try:
        return uuid.UUID(str(session_id))
    except (ValueError, AttributeError):
        return uuid.uuid5(uuid.NAMESPACE_DNS, str(session_id))

async def get_session(session_id: str, tenant_id: str = "default_tenant") -> Optional[Dict[str, Any]]:
    """获取单个会话详情"""
    async with AsyncSessionLocal() as db:
        sid = to_session_uuid(session_id)
        stmt = select(ChatSession).where(ChatSession.id == sid, ChatSession.tenant_id == tenant_id)
        res = await db.execute(stmt)
        sess = res.scalar_one_or_none()
        if not sess:
            return None
        return {
            "id": str(sess.id),
            "title": sess.title,
            "tenant_id": sess.tenant_id,
            "created_at": sess.created_at.isoformat(),
            "updated_at": sess.updated_at.isoformat()
        }

async def get_messages(session_id: str, limit: int = 50) -> List[Dict[str, Any]]:
    """获取会话的历史消息列表"""
    async with AsyncSessionLocal() as db:
        sid = to_session_uuid(session_id)
        stmt = (
            select(ChatMessage)
            .where(ChatMessage.session_id == sid)
            .order_by(ChatMessage.created_at.asc())
            .limit(limit)
        )
        res = await db.execute(stmt)
        msgs = res.scalars().all()
        return [
            {
                "id": str(m.id),
                "session_id": str(m.session_id),
                "role": m.role,
                "content": m.content,
                "citations": m.citations or [],
                "created_at": m.created_at.isoformat()
            }
            for m in msgs
        ]

async def append_message(
    session_id: str,
    role: str,
    content: str,
    citations: Optional[List[dict]] = None
) -> Dict[str, Any]:
    """向会话追加一条消息并自动更新会话更新时间"""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as db:
        sid = to_session_uuid(session_id)
        msg_id = uuid.uuid4()

        # 检查会话是否存在，若前端直接发起对话则自动补充会话记录
        stmt = select(ChatSession).where(ChatSession.id == sid)
        res = await db.execute(stmt)
        sess = res.scalar_one_or_none()
        if not sess:
            sess = ChatSession(
                id=sid,
                title=make_title(content) if role == "user" else "新会话",
                tenant_id="default_tenant",
                created_at=now,
                updated_at=now
            )
            db.add(sess)
            await db.flush()
        else:
            if role == "user" and sess.title == "新会话":
                sess.title = make_title(content)
            sess.updated_at = now

        new_msg = ChatMessage(
            id=msg_id,
            session_id=sid,
            role=role,
            content=content,
            citations=citations or [],
            created_at=now
        )
        db.add(new_msg)

        await db.commit()
        return {
            "id": str(new_msg.id),
            "session_id": str(new_msg.session_id),
            "role": new_msg.role,
            "content": new_msg.content,
            "citations": new_msg.citations,
            "created_at": new_msg.created_at.isoformat()
        }

async def delete_session(session_id: str, tenant_id: str = "default_tenant") -> bool:
    """删除会话及其名下所有历史消息"""
    async with AsyncSessionLocal() as db:
        sid = to_session_uuid(session_id)
        stmt = delete(ChatSession).where(ChatSession.id == sid, ChatSession.tenant_id == tenant_id)
        res = await db.execute(stmt)
        await db.commit()
        return res.rowcount > 0

add_message = append_message

class ChatStore:
    create_session = staticmethod(create_session)
    list_sessions = staticmethod(list_sessions)
    get_messages = staticmethod(get_messages)
    append_message = staticmethod(append_message)
    add_message = staticmethod(append_message)
    delete_session = staticmethod(delete_session)

chat_store = ChatStore()

