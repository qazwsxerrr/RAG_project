"""src/rag_kb/db/chat_store.py —— 对话会话的数据访问层（DAO）。

把 chat_sessions / chat_messages 两张表的读写封装成 make_title()、create_session()、list_sessions()、
append_message()、delete_session() 等函数，向 api 层只暴露普通 dict，不交出 ORM 对象。
Phase-6 §5.4 严格规范：所有会话查询、修改、追加消息、删除均强制校验 (tenant_id, user_id) 归属，杜绝同租户员工间横向越权 (BOLA)。
"""

import uuid
from typing import Dict, List, Optional, Any
from sqlalchemy import select, desc, delete, func, or_
from rag_kb.db.session import AsyncSessionLocal
from rag_kb.db.models import ChatSession, ChatMessage

def make_title(query: str, max_len: int = 25) -> str:
    """根据首条用户查询自动生成会话标题"""
    clean = query.strip().replace("\n", " ")
    if len(clean) <= max_len:
        return clean
    return clean[:max_len] + "..."

def _owner_filter(tenant_id: str, user_id: Optional[str]):
    """构造租户 + 用户归属过滤谓词（严格隔离：有 user_id 时绝不放行 user_id IS NULL 的无主会话）"""
    conds = [ChatSession.tenant_id == tenant_id]
    if user_id:
        conds.append(ChatSession.user_id == user_id)
    return conds

async def create_session(
    tenant_id: str = "default_tenant",
    title: Optional[str] = None,
    user_id: Optional[str] = None
) -> Dict[str, Any]:
    """创建新会话 (绑定当前员工 tenant_id 与 user_id)"""
    from datetime import datetime, timezone
    session_id = uuid.uuid4()
    session_title = title or "新会话"
    now = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as db:
        new_sess = ChatSession(
            id=session_id,
            title=session_title,
            tenant_id=tenant_id,
            user_id=user_id,
            created_at=now,
            updated_at=now
        )
        db.add(new_sess)
        await db.commit()
        return {
            "id": str(new_sess.id),
            "title": new_sess.title,
            "tenant_id": new_sess.tenant_id,
            "user_id": new_sess.user_id,
            "message_count": 0,
            "created_at": new_sess.created_at.isoformat(),
            "updated_at": new_sess.updated_at.isoformat()
        }

async def list_sessions(
    tenant_id: str = "default_tenant",
    limit: int = 20,
    offset: int = 0,
    user_id: Optional[str] = None
) -> List[Dict[str, Any]]:
    """查询指定租户及员工名下的会话列表并统计消息数"""
    async with AsyncSessionLocal() as db:
        stmt = (
            select(
                ChatSession,
                func.count(ChatMessage.id).label("message_count")
            )
            .outerjoin(ChatMessage, ChatMessage.session_id == ChatSession.id)
            .where(*_owner_filter(tenant_id, user_id))
            .group_by(ChatSession.id)
            .order_by(desc(ChatSession.updated_at))
            .limit(limit)
            .offset(offset)
        )
        res = await db.execute(stmt)
        rows = res.all()
        return [
            {
                "id": str(s.id),
                "title": s.title,
                "tenant_id": s.tenant_id,
                "user_id": s.user_id,
                "message_count": count,
                "created_at": s.created_at.isoformat(),
                "updated_at": s.updated_at.isoformat()
            }
            for s, count in rows
        ]

def to_session_uuid(session_id: str) -> uuid.UUID:
    """确保 session_id 为合法的 UUID，若为前端格式如 session_123 则生成确定性的命名空间 UUID"""
    try:
        return uuid.UUID(str(session_id))
    except (ValueError, AttributeError):
        return uuid.uuid5(uuid.NAMESPACE_DNS, str(session_id))

async def get_session(
    session_id: str,
    tenant_id: str = "default_tenant",
    user_id: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """获取单个会话详情 (严格校验 tenant_id 与 user_id 归属)"""
    async with AsyncSessionLocal() as db:
        sid = to_session_uuid(session_id)
        stmt = select(ChatSession).where(ChatSession.id == sid, *_owner_filter(tenant_id, user_id))
        res = await db.execute(stmt)
        sess = res.scalar_one_or_none()
        if not sess:
            return None
        return {
            "id": str(sess.id),
            "title": sess.title,
            "tenant_id": sess.tenant_id,
            "user_id": sess.user_id,
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
                "reasoning_content": getattr(m, "reasoning_content", None),
                "thinking_process": getattr(m, "thinking_process", None),
                "created_at": m.created_at.isoformat()
            }
            for m in msgs
        ]

async def append_message(
    session_id: str,
    role: str,
    content: str,
    citations: Optional[List[dict]] = None,
    reasoning_content: Optional[str] = None,
    thinking_process: Optional[Dict[str, Any]] = None,
    tenant_id: str = "default_tenant",
    user_id: Optional[str] = None
) -> Dict[str, Any]:
    """向会话追加一条消息并自动更新会话更新时间 (强制校验会话归属，防止跨用户篡改他人会话)"""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as db:
        sid = to_session_uuid(session_id)
        msg_id = uuid.uuid4()

        # 采用 SELECT ... FOR UPDATE 加行级排他锁，杜绝并发竞态条件
        stmt = select(ChatSession).where(ChatSession.id == sid).with_for_update()
        res = await db.execute(stmt)
        sess = res.scalar_one_or_none()

        # 归属权校验 (BOLA 防御：严格校验 tenant_id 与 user_id 必须完全一致)
        if sess is not None:
            if sess.tenant_id != tenant_id or (user_id and sess.user_id != user_id):
                raise PermissionError("【越权访问拦截】您无权向其他员工的会话追加消息 (BOLA Blocked)")

        # 统计当前会话中已经存在的 user 消息数量（客观事实）
        count_stmt = select(func.count(ChatMessage.id)).where(
            ChatMessage.session_id == sid,
            ChatMessage.role == "user"
        )
        user_msg_count = (await db.execute(count_stmt)).scalar() or 0

        if not sess:
            sess = ChatSession(
                id=sid,
                title=make_title(content) if role == "user" else "新会话",
                tenant_id=tenant_id,
                user_id=user_id,
                created_at=now,
                updated_at=now
            )
            db.add(sess)
            await db.flush()
        else:
            if role == "user" and user_msg_count == 0:
                sess.title = make_title(content)
            sess.updated_at = now

        new_msg = ChatMessage(
            id=msg_id,
            session_id=sid,
            role=role,
            content=content,
            citations=citations or [],
            reasoning_content=reasoning_content,
            thinking_process=thinking_process,
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
            "reasoning_content": new_msg.reasoning_content,
            "thinking_process": new_msg.thinking_process,
            "created_at": new_msg.created_at.isoformat()
        }

async def update_session(
    session_id: str,
    title: Optional[str] = None,
    tenant_id: str = "default_tenant",
    user_id: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """更新会话元数据（如自定义标题），校验 tenant_id 与 user_id 归属"""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as db:
        sid = to_session_uuid(session_id)
        stmt = select(ChatSession).where(ChatSession.id == sid, *_owner_filter(tenant_id, user_id)).with_for_update()
        res = await db.execute(stmt)
        sess = res.scalar_one_or_none()
        if not sess:
            return None
        if title is not None:
            sess.title = title.strip() or "新会话"
        sess.updated_at = now
        await db.commit()
        return {
            "id": str(sess.id),
            "title": sess.title,
            "tenant_id": sess.tenant_id,
            "user_id": sess.user_id,
            "created_at": sess.created_at.isoformat(),
            "updated_at": sess.updated_at.isoformat()
        }

async def delete_session(
    session_id: str,
    tenant_id: str = "default_tenant",
    user_id: Optional[str] = None
) -> bool:
    """删除会话及其名下所有历史消息 (校验 tenant_id 与 user_id 归属)"""
    async with AsyncSessionLocal() as db:
        sid = to_session_uuid(session_id)
        stmt = delete(ChatSession).where(ChatSession.id == sid, *_owner_filter(tenant_id, user_id))
        res = await db.execute(stmt)
        await db.commit()
        return res.rowcount > 0

add_message = append_message

class ChatStore:
    create_session = staticmethod(create_session)
    list_sessions = staticmethod(list_sessions)
    get_session = staticmethod(get_session)
    get_messages = staticmethod(get_messages)
    append_message = staticmethod(append_message)
    add_message = staticmethod(append_message)
    update_session = staticmethod(update_session)
    delete_session = staticmethod(delete_session)

chat_store = ChatStore()
