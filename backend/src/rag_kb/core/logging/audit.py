"""src/rag_kb/core/logging/audit.py —— 企业级操作审计日志服务。

将关键业务动作（如文档入库完成、问答生成完毕、审核通过等）异步持久化至 sys_audit_logs 数据表。
遵循安全设计：
1. 自动绑定当前的 request_id、tenant_id 与 user_id；
2. 内部安全隔离，若审计日志写入偶发失败（如 DB 瞬间抖动），记录 WARN 日志，绝不干扰主流程业务。
"""

import uuid
import logging
from typing import Optional, Dict, Any
from rag_kb.db.session import AsyncSessionLocal
from rag_kb.db.models import AuditLog
from rag_kb.core.logging.context import get_request_id, get_tenant_id, get_user_id

logger = logging.getLogger("rag_kb.audit")

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
    """
    异步写入审计日志记录
    
    :param action: 操作类型 (如 "DOCUMENT_INGEST", "RETRIEVAL_SEARCH", "CHAT_STREAM")
    :param resource_id: 操作目标资源标识 (如 doc_id 或 session_id)
    :param duration_ms: 操作耗时毫秒
    :param status: 操作结果状态 ("SUCCESS", "FAILED", "BLOCKED")
    :param error_msg: 失败错误简述
    :param details: 扩展上下文元数据 (JSON)
    :param request_id: 可选指定 request_id (默认从当前协程上下文提取)
    :param tenant_id: 可选指定 tenant_id (默认从当前协程上下文提取)
    :param user_id: 可选指定 user_id (默认从当前协程上下文提取)
    """
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
