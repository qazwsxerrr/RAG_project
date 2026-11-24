"""src/rag_kb/db/chat_store.py —— 对话会话的数据访问层（含完整来源文档 ID 持久化与当前 READ 实时复核）。

严格遵循方案 §3.3.3：
1. append_message 保存生成每条助手回答实际使用的完整 source_doc_ids（不只是展示引用），且落库前剥离 raw_oss_url；
2. get_authorized_messages / build_authorized_history 在回看与构造多轮问答上下文时，逐条按当前 READ 权限复核：
   - 若助手消息 source_doc_ids 为 None（无法核实来源的旧回答）或任一来源文档已失权/删除，
     绝不返回或复用该回答的正文、引用、思考过程及摘要！
"""

import uuid
from typing import Dict, List, Optional, Any
from fastapi import HTTPException
from sqlalchemy import select, desc, delete, func
from sqlalchemy.ext.asyncio import AsyncSession

from rag_kb.core.authz import EffectiveAuthzContext, assert_doc_access
from rag_kb.core.constants import KbAction
from rag_kb.db.session import AsyncSessionLocal
from rag_kb.db.models import ChatSession, ChatMessage, Document


def make_title(query: str, max_len: int = 25) -> str:
    clean = query.strip().replace("\n", " ")
    if len(clean) <= max_len:
        return clean
    return clean[:max_len] + "..."


def _owner_filter(tenant_id: str, user_id: Optional[str]):
    conds = [ChatSession.tenant_id == tenant_id]
    if user_id:
        conds.append(ChatSession.user_id == user_id)
    return conds


def _sanitize_citations_for_storage(citations: Optional[List[dict]]) -> List[dict]:
    if not citations:
        return []
    cleaned = []
    for c in citations:
        if not isinstance(c, dict):
            continue
        item = dict(c)
        item.pop("raw_oss_url", None)
        cleaned.append(item)
    return cleaned


async def create_session(
    tenant_id: str = "default_tenant",
    title: Optional[str] = None,
    user_id: Optional[str] = None,
) -> Dict[str, Any]:
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
            updated_at=now,
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
            "updated_at": new_sess.updated_at.isoformat(),
        }


async def list_sessions(
    tenant_id: str = "default_tenant",
    limit: int = 20,
    offset: int = 0,
    user_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    async with AsyncSessionLocal() as db:
        stmt = (
            select(
                ChatSession,
                func.count(ChatMessage.id).label("message_count"),
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
                "updated_at": s.updated_at.isoformat(),
            }
            for s, count in rows
        ]


def to_session_uuid(session_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(session_id))
    except (ValueError, AttributeError):
        return uuid.uuid5(uuid.NAMESPACE_DNS, str(session_id))


async def get_session(
    session_id: str,
    tenant_id: str = "default_tenant",
    user_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
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
            "updated_at": sess.updated_at.isoformat(),
        }


async def _is_assistant_message_authorized(
    db: AsyncSession,
    msg: ChatMessage,
    ctx: EffectiveAuthzContext,
) -> bool:
    """
    复核单条助手消息的所有来源文档是否仍对当前用户具备 READ 权限：
    - source_doc_ids is None：旧回答无法核实来源 -> 拒绝展示/复用；
    - source_doc_ids == []：确无文档来源（如纯合规拦截提示或无命中回答） -> 允许；
    - 任一来源文档不存在、已停用或当前用户失去 READ 权限 -> 整条回答拦截！
    """
    if msg.source_doc_ids is None:
        return False
    if not isinstance(msg.source_doc_ids, list):
        return False

    doc_ids_to_check = set()
    for raw_id in msg.source_doc_ids:
        if raw_id:
            doc_ids_to_check.add(str(raw_id))
    for cit in (msg.citations or []):
        if isinstance(cit, dict) and cit.get("document_id"):
            doc_ids_to_check.add(str(cit["document_id"]))

    for raw_doc_id in doc_ids_to_check:
        try:
            doc_uuid = uuid.UUID(raw_doc_id)
        except ValueError:
            return False
        doc = await db.get(Document, doc_uuid)
        if doc is None or not doc.is_active:
            return False
        try:
            await assert_doc_access(db, ctx, doc, KbAction.READ.value, audit_denial=False)
        except HTTPException:
            return False
    return True


async def get_authorized_messages(
    session_id: str,
    ctx: EffectiveAuthzContext,
    limit: int = 50,
    db: Optional[AsyncSession] = None,
) -> List[Dict[str, Any]]:
    """
    获取经当前用户 READ 权限逐条复核的会话历史消息：
    - 任一来源失权或无法核实来源的助手回答将被完全过滤，绝不返回其正文、引用、推理与摘要；
    - 返回引文中严格不含 raw_oss_url。
    """
    async def _run(session: AsyncSession) -> List[Dict[str, Any]]:
        sid = to_session_uuid(session_id)
        stmt = (
            select(ChatMessage)
            .where(ChatMessage.session_id == sid)
            .order_by(ChatMessage.created_at.asc())
            .limit(limit)
        )
        msgs = (await session.execute(stmt)).scalars().all()
        authorized: List[Dict[str, Any]] = []
        for m in msgs:
            if m.role == "assistant":
                ok = await _is_assistant_message_authorized(session, m, ctx)
                if not ok:
                    continue
            authorized.append({
                "id": str(m.id),
                "session_id": str(m.session_id),
                "role": m.role,
                "content": m.content,
                "citations": _sanitize_citations_for_storage(m.citations),
                "reasoning_content": getattr(m, "reasoning_content", None),
                "thinking_process": getattr(m, "thinking_process", None),
                "created_at": m.created_at.isoformat(),
            })
        return authorized

    if db is not None:
        return await _run(db)
    async with AsyncSessionLocal() as session:
        return await _run(session)


async def build_authorized_history(
    session_id: str,
    ctx: EffectiveAuthzContext,
    max_messages: int = 6,
    db: Optional[AsyncSession] = None,
) -> List[Dict[str, str]]:
    """
    服务端构造多轮问答历史上下文：
    从最新消息按时间倒序向前分页读取，逐条经当前 READ 权限复核，攒够 max_messages 条后再反转为时间正序返回，
    避免超长会话截断最新对话，同时彻底阻断失权文档内容经由历史上下文泄漏至下一轮回答！
    """
    if max_messages <= 0:
        msgs = await get_authorized_messages(session_id=session_id, ctx=ctx, limit=500, db=db)
        return [
            {"role": m["role"], "content": m["content"]}
            for m in msgs
            if m.get("role") in ("user", "assistant") and m.get("content")
        ]

    async def _run(session: AsyncSession) -> List[Dict[str, str]]:
        sid = to_session_uuid(session_id)
        collected_desc: List[Dict[str, str]] = []
        batch_size = max(20, max_messages * 4)
        offset = 0

        while len(collected_desc) < max_messages:
            stmt = (
                select(ChatMessage)
                .where(ChatMessage.session_id == sid)
                .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
                .offset(offset)
                .limit(batch_size)
            )
            batch = (await session.execute(stmt)).scalars().all()
            if not batch:
                break
            for m in batch:
                if m.role not in ("user", "assistant") or not m.content:
                    continue
                if m.role == "assistant":
                    ok = await _is_assistant_message_authorized(session, m, ctx)
                    if not ok:
                        continue
                collected_desc.append({"role": m.role, "content": m.content})
                if len(collected_desc) >= max_messages:
                    break
            if len(batch) < batch_size:
                break
            offset += batch_size

        collected_desc.reverse()
        return collected_desc

    if db is not None:
        return await _run(db)
    async with AsyncSessionLocal() as session:
        return await _run(session)


async def get_messages(session_id: str, limit: int = 50) -> List[Dict[str, Any]]:
    """内部只读查询原始消息（对外 HTTP 路由必须使用 get_authorized_messages）"""
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
                "citations": _sanitize_citations_for_storage(m.citations),
                "source_doc_ids": m.source_doc_ids,
                "reasoning_content": getattr(m, "reasoning_content", None),
                "thinking_process": getattr(m, "thinking_process", None),
                "created_at": m.created_at.isoformat(),
            }
            for m in msgs
        ]


async def append_message(
    session_id: str,
    role: str,
    content: str,
    citations: Optional[List[dict]] = None,
    source_doc_ids: Optional[List[str]] = None,
    reasoning_content: Optional[str] = None,
    thinking_process: Optional[Dict[str, Any]] = None,
    tenant_id: str = "default_tenant",
    user_id: Optional[str] = None,
) -> Dict[str, Any]:
    """向会话追加一条消息（强制归属校验 + 剥离 raw_oss_url + 记录完整 source_doc_ids）"""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    safe_citations = _sanitize_citations_for_storage(citations)
    normalized_sources: Optional[List[str]] = None
    if source_doc_ids is not None:
        normalized_sources = sorted({str(d) for d in source_doc_ids if d})
    elif role == "user":
        normalized_sources = []

    async with AsyncSessionLocal() as db:
        sid = to_session_uuid(session_id)
        msg_id = uuid.uuid4()

        stmt = select(ChatSession).where(ChatSession.id == sid).with_for_update()
        res = await db.execute(stmt)
        sess = res.scalar_one_or_none()

        if sess is not None:
            if sess.tenant_id != tenant_id or (user_id and sess.user_id != user_id):
                raise PermissionError("【越权访问拦截】您无权向其他员工的会话追加消息")

        count_stmt = select(func.count(ChatMessage.id)).where(
            ChatMessage.session_id == sid,
            ChatMessage.role == "user",
        )
        user_msg_count = (await db.execute(count_stmt)).scalar() or 0

        if not sess:
            sess = ChatSession(
                id=sid,
                title=make_title(content) if role == "user" else "新会话",
                tenant_id=tenant_id,
                user_id=user_id,
                created_at=now,
                updated_at=now,
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
            citations=safe_citations,
            source_doc_ids=normalized_sources,
            reasoning_content=reasoning_content,
            thinking_process=thinking_process,
            created_at=now,
        )
        db.add(new_msg)

        await db.commit()
        return {
            "id": str(new_msg.id),
            "session_id": str(new_msg.session_id),
            "role": new_msg.role,
            "content": new_msg.content,
            "citations": new_msg.citations,
            "source_doc_ids": new_msg.source_doc_ids,
            "reasoning_content": new_msg.reasoning_content,
            "thinking_process": new_msg.thinking_process,
            "created_at": new_msg.created_at.isoformat(),
        }


async def update_session(
    session_id: str,
    title: Optional[str] = None,
    tenant_id: str = "default_tenant",
    user_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
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
            "updated_at": sess.updated_at.isoformat(),
        }


async def delete_session(
    session_id: str,
    tenant_id: str = "default_tenant",
    user_id: Optional[str] = None,
) -> bool:
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
    get_authorized_messages = staticmethod(get_authorized_messages)
    build_authorized_history = staticmethod(build_authorized_history)
    append_message = staticmethod(append_message)
    add_message = staticmethod(append_message)
    update_session = staticmethod(update_session)
    delete_session = staticmethod(delete_session)


chat_store = ChatStore()
