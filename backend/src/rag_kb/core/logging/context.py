"""src/rag_kb/core/logging/context.py —— 协程局部分布式链路追踪上下文。

使用 Python contextvars.ContextVar 实现跨协程/跨异步节点的 RequestId、TenantId、UserId 隐式穿透。
同时提供 TraceLogFilter 供标准 logging Formatter 动态抓取链路标识。
"""

import uuid
from dataclasses import dataclass, field
from contextvars import ContextVar, Token
from typing import Optional, Dict, Any
import logging

@dataclass
class TraceContext:
    """全链路追踪上下文元数据实体"""
    request_id: str
    tenant_id: str = "default_tenant"
    user_id: Optional[str] = None
    step_name: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)

_current_trace_context: ContextVar[Optional[TraceContext]] = ContextVar("current_trace_context", default=None)

def generate_request_id(prefix: str = "req") -> str:
    """生成标准化全局唯一请求标识 (例如: req-a1b2c3d4-e5f6)"""
    return f"{prefix}-{uuid.uuid4().hex[:16]}"

def get_trace_context() -> TraceContext:
    """获取当前协程的链路追踪上下文；若未初始化则自动生成兜底上下文"""
    ctx = _current_trace_context.get()
    if ctx is None:
        ctx = TraceContext(request_id=generate_request_id())
        _current_trace_context.set(ctx)
    return ctx

def set_trace_context(
    request_id: Optional[str] = None,
    tenant_id: str = "default_tenant",
    user_id: Optional[str] = None,
    step_name: Optional[str] = None,
    **extra
) -> Token:
    """为当前协程设置链路追踪上下文，并返回恢复 Token"""
    req_id = request_id.strip() if (request_id and request_id.strip()) else generate_request_id()
    ctx = TraceContext(
        request_id=req_id,
        tenant_id=tenant_id or "default_tenant",
        user_id=user_id,
        step_name=step_name,
        extra=extra
    )
    return _current_trace_context.set(ctx)

def reset_trace_context(token: Token):
    """恢复上下文状态"""
    _current_trace_context.reset(token)

def get_request_id() -> str:
    """快速获取当前协程的 request_id"""
    return get_trace_context().request_id

def get_tenant_id() -> str:
    """快速获取当前协程的 tenant_id"""
    return get_trace_context().tenant_id

def get_user_id() -> Optional[str]:
    """快速获取当前协程的 user_id"""
    return get_trace_context().user_id

class TraceLogFilter(logging.Filter):
    """向每一条 LogRecord 自动注入 request_id 与 tenant_id 属性的日志过滤器"""

    def filter(self, record: logging.LogRecord) -> bool:
        ctx = _current_trace_context.get()
        if ctx:
            record.request_id = ctx.request_id
            record.tenant_id = ctx.tenant_id
            record.step_name = ctx.step_name or "-"
        else:
            record.request_id = "-"
            record.tenant_id = "-"
            record.step_name = "-"
        return True

def setup_logging(level: int = logging.INFO):
    """
    配置全局企业级结构化日志体系：
    - 统一定义包含 [%(request_id)s] [%(tenant_id)s] 的标准日志 Formatter；
    - 向 root logger 以及所有 handler 挂载 TraceLogFilter；
    - 消除业务代码中手动拼接 request_id 的冗余。
    """
    fmt = "%(asctime)s [%(levelname)s] [%(request_id)s] [%(tenant_id)s] %(name)s: %(message)s"
    formatter = logging.Formatter(fmt, datefmt="%Y-%m-%d %H:%M:%S")

    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    if not root_logger.handlers:
        console_handler = logging.StreamHandler()
        root_logger.addHandler(console_handler)

    filter_instance = TraceLogFilter()
    for handler in root_logger.handlers:
        handler.setFormatter(formatter)
        if not any(isinstance(f, TraceLogFilter) for f in handler.filters):
            handler.addFilter(filter_instance)
