"""src/rag_kb/core/logging/__init__.py —— 企业级日志与可观测性模块导出。
"""

from rag_kb.core.logging.context import (
    TraceContext,
    generate_request_id,
    get_trace_context,
    set_trace_context,
    reset_trace_context,
    get_request_id,
    get_tenant_id,
    get_user_id,
    TraceLogFilter,
    setup_logging,
)
from rag_kb.core.logging.middleware import TraceLoggingMiddleware
from rag_kb.core.logging.audit import record_audit_log, record_audit_log_sync, bind_main_event_loop

__all__ = [
    "TraceContext",
    "generate_request_id",
    "get_trace_context",
    "set_trace_context",
    "reset_trace_context",
    "get_request_id",
    "get_tenant_id",
    "get_user_id",
    "TraceLogFilter",
    "setup_logging",
    "TraceLoggingMiddleware",
    "record_audit_log",
    "record_audit_log_sync",
    "bind_main_event_loop",
]
