"""src/rag_kb/core/logging/audit.py —— 企业级操作与强一致安全审计日志服务。

严格遵循方案 §3.3.2 安全审计规范：
1. record_security_audit_in_tx(db, ...): 授权变更与单文档改密审计必须与业务写操作处于同一数据库事务，失败立即抛出异常回滚；
2. record_mandatory_security_audit(...): L3/L4 高密内容交付前必须持久化访问记录，写入失败立即抛出 AuditWriteError 阻断内容交付（绝不吞异常只记 warning）；
3. record_denial_audit(...): 403 越权拦截留痕，即使审计写入异常也绝不能把拒绝变成放行；
4. record_audit_log / record_audit_log_sync: 一般性业务可观测日志。
"""

import uuid
import asyncio
import logging
from typing import Optional, Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession
from rag_kb.db.session import AsyncSessionLocal
from rag_kb.db.models import AuditLog
from rag_kb.core.logging.context import get_request_id, get_tenant_id, get_user_id

logger = logging.getLogger("rag_kb.audit")

_MAIN_EVENT_LOOP: Optional[asyncio.AbstractEventLoop] = None


class AuditWriteError(RuntimeError):
    """安全审计写入失败异常（用于强一致审计与 L3/L4 交付前阻断）"""


def bind_main_event_loop(loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
    """在应用 lifespan 启动或主协程首次调用时绑定主事件循环"""
    global _MAIN_EVENT_LOOP
    if loop is not None:
        _MAIN_EVENT_LOOP = loop
        return
    try:
        _MAIN_EVENT_LOOP = asyncio.get_running_loop()
    except RuntimeError:
        pass


def _build_audit_entry(
    action: str,
    resource_id: Optional[str] = None,
    duration_ms: int = 0,
    status: str = "SUCCESS",
    error_msg: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None,
    request_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> AuditLog:
    return AuditLog(
        id=uuid.uuid4(),
        request_id=request_id or get_request_id(),
        tenant_id=tenant_id or get_tenant_id(),
        user_id=user_id or get_user_id(),
        action=action,
        resource_id=resource_id,
        duration_ms=duration_ms,
        status=status,
        error_msg=error_msg,
        details=details or {},
    )


async def record_security_audit_in_tx(
    db: AsyncSession,
    action: str,
    resource_id: Optional[str] = None,
    duration_ms: int = 0,
    status: str = "SUCCESS",
    error_msg: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None,
    request_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> uuid.UUID:
    """
    在调用方的业务数据库事务内写入安全审计记录并 flush：
    - 若写入失败直接抛出 AuditWriteError，促使外层业务事务完整回滚；
    - 严禁吞异常或返回 None。
    """
    entry = _build_audit_entry(
        action=action,
        resource_id=resource_id,
        duration_ms=duration_ms,
        status=status,
        error_msg=error_msg,
        details=details,
        request_id=request_id,
        tenant_id=tenant_id,
        user_id=user_id,
    )
    try:
        db.add(entry)
        await db.flush()
        return entry.id
    except Exception as e:
        logger.error(f"[{entry.request_id}] 同事务安全审计写入失败 (将回滚业务变更): {e}", exc_info=True)
        raise AuditWriteError(f"安全审计写入失败，变更已中止: {e}") from e


async def record_mandatory_security_audit(
    action: str,
    resource_id: Optional[str] = None,
    duration_ms: int = 0,
    status: str = "SUCCESS",
    error_msg: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None,
    request_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
    user_id: Optional[str] = None,
    db: Optional[AsyncSession] = None,
) -> uuid.UUID:
    """
    强制落盘安全审计（专用于 L3/L4 高密文档读取、预览、渲染、切图及高密问答交付前留痕）：
    - 必须在交付首字节前持久化提交；
    - 写入失败必须显式抛出 AuditWriteError 阻断访问，严禁只记 warning 后放行。
    """
    entry = _build_audit_entry(
        action=action,
        resource_id=resource_id,
        duration_ms=duration_ms,
        status=status,
        error_msg=error_msg,
        details=details,
        request_id=request_id,
        tenant_id=tenant_id,
        user_id=user_id,
    )
    try:
        if db is not None:
            db.add(entry)
            await db.commit()
            return entry.id
        async with AsyncSessionLocal() as session:
            session.add(entry)
            await session.commit()
            return entry.id
    except Exception as e:
        logger.error(f"[{entry.request_id}] 高密访问强制安全审计落盘失败 (阻断交付): {e}", exc_info=True)
        raise AuditWriteError(f"安全审计留痕失败，已中止高密内容交付: {e}") from e


async def record_denial_audit(
    action: str,
    resource_id: Optional[str] = None,
    error_msg: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None,
    request_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> Optional[uuid.UUID]:
    """
    越权拒绝事件审计留痕：
    - 使用独立事务落库，确保外层抛 403 时不被业务回滚抹除；
    - 审计写入失败记录 ERROR 日志，但绝不影响调用方继续抛出 403 拒绝（绝不将拒绝变为放行）。
    """
    entry = _build_audit_entry(
        action=action,
        resource_id=resource_id,
        duration_ms=0,
        status="FORBIDDEN",
        error_msg=error_msg,
        details=details,
        request_id=request_id,
        tenant_id=tenant_id,
        user_id=user_id,
    )
    try:
        async with AsyncSessionLocal() as session:
            session.add(entry)
            await session.commit()
            return entry.id
    except Exception as e:
        logger.error(f"[{entry.request_id}] 越权拒绝事件审计写入异常 (维持拒绝响应): {e}", exc_info=True)
        return None


async def record_audit_log(
    action: str,
    resource_id: Optional[str] = None,
    duration_ms: int = 0,
    status: str = "SUCCESS",
    error_msg: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None,
    request_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> Optional[uuid.UUID]:
    """一般性异步业务审计日志写入"""
    if _MAIN_EVENT_LOOP is None:
        bind_main_event_loop()

    entry = _build_audit_entry(
        action=action,
        resource_id=resource_id,
        duration_ms=duration_ms,
        status=status,
        error_msg=error_msg,
        details=details,
        request_id=request_id,
        tenant_id=tenant_id,
        user_id=user_id,
    )
    try:
        async with AsyncSessionLocal() as session:
            session.add(entry)
            await session.commit()
            return entry.id
    except Exception as e:
        logger.warning(f"[{entry.request_id}] 异步写入审计日志失败 (非阻塞): {e}")
        return None


def record_audit_log_sync(
    action: str,
    resource_id: Optional[str] = None,
    duration_ms: int = 0,
    status: str = "SUCCESS",
    error_msg: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None,
    request_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> None:
    """同步安全写入一般性业务审计日志"""
    effective_req_id = request_id or get_request_id()
    effective_tenant_id = tenant_id or get_tenant_id()
    effective_user_id = user_id or get_user_id()

    coro_kwargs = dict(
        action=action,
        resource_id=resource_id,
        duration_ms=duration_ms,
        status=status,
        error_msg=error_msg,
        details=details,
        request_id=effective_req_id,
        tenant_id=effective_tenant_id,
        user_id=effective_user_id,
    )

    try:
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None

        if running_loop and running_loop.is_running():
            running_loop.create_task(record_audit_log(**coro_kwargs))
            return

        if _MAIN_EVENT_LOOP and _MAIN_EVENT_LOOP.is_running():
            asyncio.run_coroutine_threadsafe(record_audit_log(**coro_kwargs), _MAIN_EVENT_LOOP)
            return

        async def _standalone_write():
            from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession as StandaloneSession
            from rag_kb.core.config import settings
            temp_engine = create_async_engine(settings.async_database_url, echo=False)
            try:
                async with StandaloneSession(temp_engine, expire_on_commit=False) as s:
                    s.add(_build_audit_entry(**coro_kwargs))
                    await s.commit()
            finally:
                await temp_engine.dispose()

        asyncio.run(_standalone_write())
    except Exception as e:
        logger.warning(f"[{effective_req_id}] 同步写入审计日志失败 (非阻塞): {e}")
