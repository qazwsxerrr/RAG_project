"""src/rag_kb/core/logging/audit.py —— 企业级操作审计日志服务。

将关键业务动作（如文档入库完成、问答生成完毕、审核通过等）持久化至 sys_audit_logs 数据表。
遵循安全设计：
1. 自动绑定当前的 request_id、tenant_id 与 user_id；
2. 支持主协程异步调度与工作线程跨 Loop 线程安全投递，避免新建临时 EventLoop 跨线程借用连接池引发冲突；
3. 内部安全隔离，若审计日志写入偶发失败，记录 WARN 日志，绝不干扰主流程业务。
"""

import uuid
import asyncio
import logging
from typing import Optional, Dict, Any
from rag_kb.db.session import AsyncSessionLocal
from rag_kb.db.models import AuditLog
from rag_kb.core.logging.context import get_request_id, get_tenant_id, get_user_id

logger = logging.getLogger("rag_kb.audit")

# 记录应用主事件循环引用，供 asyncio.to_thread 工作线程安全投递异步审计任务
_MAIN_EVENT_LOOP: Optional[asyncio.AbstractEventLoop] = None


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


async def record_audit_log(
    action: str,
    resource_id: Optional[str] = None,
    duration_ms: int = 0,
    status: str = "SUCCESS",
    error_msg: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None,
    request_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
    user_id: Optional[str] = None
) -> Optional[uuid.UUID]:
    """异步写入审计日志记录"""
    if _MAIN_EVENT_LOOP is None:
        bind_main_event_loop()

    effective_req_id = request_id or get_request_id()
    effective_tenant_id = tenant_id or get_tenant_id()
    effective_user_id = user_id or get_user_id()

    try:
        async with AsyncSessionLocal() as session:
            log_entry = AuditLog(
                id=uuid.uuid4(),
                request_id=effective_req_id,
                tenant_id=effective_tenant_id,
                user_id=effective_user_id,
                action=action,
                resource_id=resource_id,
                duration_ms=duration_ms,
                status=status,
                error_msg=error_msg,
                details=details or {}
            )
            session.add(log_entry)
            await session.commit()
            return log_entry.id
    except Exception as e:
        logger.warning(f"[{effective_req_id}] 异步写入审计日志失败 (非阻塞): {e}")
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
    user_id: Optional[str] = None
) -> None:
    """
    同步安全写入审计日志记录：
    - 若当前线程已有运行中 EventLoop，直接 create_task；
    - 若当前处于 asyncio.to_thread 工作线程且主 Loop 存活，通过 run_coroutine_threadsafe 投递回主 Loop 执行（复用同一 asyncpg 连接池）；
    - 若为独立离线脚本（无主 Loop），使用独立短生命周期 Engine 写入并立即 dispose。
    """
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
            from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
            from rag_kb.core.config import settings
            temp_engine = create_async_engine(settings.async_database_url, echo=False)
            try:
                async with AsyncSession(temp_engine, expire_on_commit=False) as s:
                    s.add(AuditLog(
                        id=uuid.uuid4(),
                        request_id=effective_req_id,
                        tenant_id=effective_tenant_id,
                        user_id=effective_user_id,
                        action=action,
                        resource_id=resource_id,
                        duration_ms=duration_ms,
                        status=status,
                        error_msg=error_msg,
                        details=details or {}
                    ))
                    await s.commit()
            finally:
                await temp_engine.dispose()

        asyncio.run(_standalone_write())
    except Exception as e:
        logger.warning(f"[{effective_req_id}] 同步写入审计日志失败 (非阻塞): {e}")
