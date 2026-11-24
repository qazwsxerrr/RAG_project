"""src/rag_kb/services/mcp/auth_guard.py —— FastMCP 横切安全守门员 (@mcp_secure_tool)。

所有对外暴露的 FastMCP 工具强制经过本模块拦截：
1. 统一凭据提取：从 HTTP 请求头显式读取 Authorization: Bearer <token> 与 X-Request-Id；
2. RS256 强制验签与 Redis 秒级吊销校验：调用 deps.get_current_user 执行公钥/JWKS 验签与 auth:revoked / auth:perm_changed_at 核验；
3. On-Behalf-Of 双重权限交集校验：用户自身 RBAC 权限点 ∩ 令牌签名委托 Agent 的 MCP_AGENT_TOOL_POLICIES_JSON 工具能力白名单；
4. Redis Lua 原子滑动窗口频率限流：按 mcp:{tool_name} 与 {tenant_id}:{user_id} 强制防刷；
5. 全链路 TraceContext 穿透、sys_audit_logs 审计落库与未预期内部异常 error_id 堆栈屏蔽。
"""

import json
import time
import functools
import logging
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple
from fastapi import HTTPException
from fastmcp.server.dependencies import get_http_headers

from rag_kb.api.deps import EffectiveAuthzContext, get_current_user
from rag_kb.core.config import settings
from rag_kb.core.logging.audit import record_audit_log
from rag_kb.core.logging.context import (
    set_trace_context,
    reset_trace_context,
    get_request_id,
)
from rag_kb.core.rate_limit import check_sliding_window_limit
from rag_kb.core.security.prompt_guard import PromptSecurityError

logger = logging.getLogger("rag_kb.mcp.auth_guard")


@dataclass
class MCPCallerContext:
    """经过 @mcp_secure_tool 完整核验后的 MCP 调用上下文"""
    user: EffectiveAuthzContext
    agent_id: str
    request_id: str
    tool_name: str


_current_mcp_caller: ContextVar[Optional[MCPCallerContext]] = ContextVar(
    "current_mcp_caller", default=None
)


def get_current_mcp_caller() -> MCPCallerContext:
    """在 @mcp_secure_tool 修饰的工具函数内部安全获取已验签的调用方上下文"""
    ctx = _current_mcp_caller.get()
    if ctx is None:
        raise RuntimeError("【MCP 安全上下文缺失】工具未挂载 @mcp_secure_tool 守门员装饰器！")
    return ctx


def load_mcp_agent_policies() -> Dict[str, List[str]]:
    """
    解析 settings.MCP_AGENT_TOOL_POLICIES_JSON 中的 Agent 工具白名单配置。
    若配置了非法 JSON 格式，坚决 Fail-Fast 抛出 ValueError。
    """
    raw = settings.MCP_AGENT_TOOL_POLICIES_JSON
    if not raw or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("MCP_AGENT_TOOL_POLICIES_JSON 顶层必须为 JSON 对象 (dict)")
        policies: Dict[str, List[str]] = {}
        for agent_key, tools in parsed.items():
            if not isinstance(tools, list):
                raise ValueError(f"Agent [{agent_key}] 的工具白名单必须为数组 (list)")
            policies[str(agent_key).strip()] = [str(t).strip() for t in tools if str(t).strip()]
        return policies
    except Exception as e:
        raise ValueError(f"【MCP 策略配置解析失败】MCP_AGENT_TOOL_POLICIES_JSON 格式非法: {e}") from e


def extract_mcp_credentials() -> Tuple[str, str, Optional[str]]:
    """
    从 HTTP 请求上下文 Header 提取 (bearer_header, claimed_agent_id, incoming_request_id)。
    显式传入 include={"authorization", "x-agent-id", "x-request-id"} 防止 FastMCP 默认剥离 Authorization 头。
    """
    raw_headers = get_http_headers(
        include={"authorization", "x-agent-id", "x-request-id"}
    ) or {}
    http_headers = {str(k).lower(): str(v) for k, v in raw_headers.items()}

    auth_val = (http_headers.get("authorization") or "").strip()
    claimed_agent_id = (http_headers.get("x-agent-id") or "").strip()
    incoming_req_id = (http_headers.get("x-request-id") or "").strip() or None
    return auth_val, claimed_agent_id, incoming_req_id


def enforce_on_behalf_of(
    user: EffectiveAuthzContext,
    claimed_agent_id: Optional[str],
    tool_name: str,
    required_perm: str,
) -> str:
    """
    严格执行 SECURITY_MODEL.md 方向三 On-Behalf-Of 权限交集校验：
    Effective Permission = 用户自身 RBAC 权限 (user.has_permission(required_perm))
                           ∩ 签名委托 Agent 的工具能力白名单 (MCP_AGENT_TOOL_POLICIES_JSON)

    安全不变量：
    - 受控 Agent 身份必须源自 RS256 签名 JWT 中的委托声明 (user.delegated_agent_id: act.sub / agent_id / azp / client_id)；
    - 严禁仅凭调用方自报的明文 X-Agent-Id 选择白名单；若自报标识与签名委托声明不一致，立即按身份伪造拦截。
    """
    # 1. 校验用户自身 RBAC 权限点
    if required_perm and not user.has_permission(required_perm):
        raise PermissionError(
            f"【权限不足】调用 MCP 工具 [{tool_name}] 需要 [{required_perm}] 权限点，当前账号角色缺乏该权限"
        )

    claimed_id = (claimed_agent_id or "").strip()
    signed_agent_id = (getattr(user, "delegated_agent_id", None) or "").strip()
    policies = load_mcp_agent_policies()

    if signed_agent_id and claimed_id and claimed_id != signed_agent_id:
        raise PermissionError(
            f"【On-Behalf-Of 身份伪造拦截】自报 Agent 标识 [{claimed_id}] 与令牌签名委托声明 "
            f"[{signed_agent_id}] 不一致，严禁通过篡改自报标识越权！"
        )

    if not policies:
        return signed_agent_id or "direct-user-client"

    if not signed_agent_id:
        if claimed_id:
            raise PermissionError(
                f"【On-Behalf-Of 凭据缺失】拒绝采信未经令牌签名委托声明验证的自报 Agent 标识 [{claimed_id}]"
            )
        raise PermissionError(
            f"【On-Behalf-Of 凭据缺失】当前已启用 Agent 工具白名单策略，调用工具 [{tool_name}] "
            "必须携带包含签名委托声明 (act/agent_id) 的受托令牌"
        )

    allowed_tools: Optional[List[str]] = None
    if signed_agent_id in policies:
        allowed_tools = policies[signed_agent_id]
    elif "*" in policies:
        allowed_tools = policies["*"]
    else:
        raise PermissionError(
            f"【On-Behalf-Of 越界拦截】未注册的受控 Agent 标识 [{signed_agent_id}]，禁止调用 MCP 工具 [{tool_name}]"
        )

    if "*" not in allowed_tools and tool_name not in allowed_tools:
        raise PermissionError(
            f"【On-Behalf-Of 越界拦截】Agent [{signed_agent_id}] 的能力白名单仅允许 {allowed_tools}，"
            f"严禁越界调用工具 [{tool_name}]（即使被代理用户拥有该权限点亦不可提权）"
        )

    return signed_agent_id


def mcp_secure_tool(required_perm: str):
    """
    FastMCP 工具统一安全治理装饰器：
    自动完成 HTTP Header RS256 验签 -> On-Behalf-Of 交集校验 -> Redis Lua 滑动窗口限流 -> TraceContext 穿透 -> 审计落库 -> 异常脱敏。
    """
    def decorator(func: Callable) -> Callable:
        tool_name = func.__name__

        @functools.wraps(func)
        async def wrapper(*args, **kwargs) -> Any:
            bearer_header, claimed_agent_id, incoming_req_id = extract_mcp_credentials()

            trace_token = set_trace_context(request_id=incoming_req_id)
            req_id = get_request_id()
            t0 = time.perf_counter()
            user: Optional[EffectiveAuthzContext] = None
            agent_id = claimed_agent_id or "unverified-client"
            caller_token = None

            try:
                # 1. RS256 强制验签 + Redis 秒级吊销/权限变更边界核验
                user = await get_current_user(authorization=bearer_header)

                # 2. On-Behalf-Of (用户权限 ∩ 已核验签名委托 Agent 工具白名单) 交集核验
                agent_id = enforce_on_behalf_of(
                    user=user,
                    claimed_agent_id=claimed_agent_id,
                    tool_name=tool_name,
                    required_perm=required_perm,
                )

                # 3. Redis Lua 原子滑动窗口频率限制
                limit_val = max(1, int(settings.RATE_LIMIT_MCP_PER_MINUTE))
                win_sec = max(1, int(settings.RATE_LIMIT_WINDOW_SECONDS))
                rl_subject = f"{user.tenant_id}:{user.user_id}"
                allowed, current_cnt, retry_sec = await check_sliding_window_limit(
                    scope=f"mcp:{tool_name}",
                    subject=rl_subject,
                    limit=limit_val,
                    window_seconds=win_sec,
                )
                if not allowed:
                    rl_msg = (
                        f"【高并发限流拦截】MCP 工具 [{tool_name}] 调用频率过高"
                        f"（限制 {limit_val} 次/{win_sec}秒），请在 {retry_sec} 秒后重试。"
                    )
                    await record_audit_log(
                        action="MCP_TOOL_CALL",
                        resource_id=tool_name,
                        duration_ms=int((time.perf_counter() - t0) * 1000),
                        status="RATE_LIMITED",
                        error_msg=rl_msg,
                        details={
                            "agent_id": agent_id,
                            "subject": rl_subject,
                            "count": current_cnt,
                            "limit": limit_val,
                            "retry_after_seconds": retry_sec,
                        },
                    )
                    raise PermissionError(rl_msg)

                # 4. 绑定当前协程 MCPCallerContext 并执行核心业务工具
                caller_ctx = MCPCallerContext(
                    user=user,
                    agent_id=agent_id,
                    request_id=req_id,
                    tool_name=tool_name,
                )
                caller_token = _current_mcp_caller.set(caller_ctx)
                result = await func(*args, **kwargs)

                duration_ms = int((time.perf_counter() - t0) * 1000)
                await record_audit_log(
                    action="MCP_TOOL_CALL",
                    resource_id=tool_name,
                    duration_ms=duration_ms,
                    status="SUCCESS",
                    details={
                        "agent_id": agent_id,
                        "user_id": user.user_id,
                        "dept_path": user.dept_path,
                    },
                )
                return result

            except HTTPException as http_exc:
                duration_ms = int((time.perf_counter() - t0) * 1000)
                detail_str = str(http_exc.detail)
                status_tag = "UNAUTHORIZED" if http_exc.status_code == 401 else "FORBIDDEN" if http_exc.status_code == 403 else "BLOCKED"
                await record_audit_log(
                    action="MCP_TOOL_CALL",
                    resource_id=tool_name,
                    duration_ms=duration_ms,
                    status=status_tag,
                    error_msg=detail_str,
                    details={"agent_id": agent_id, "http_status": http_exc.status_code},
                )
                raise PermissionError(detail_str) from http_exc

            except (PermissionError, PromptSecurityError, ValueError) as biz_exc:
                duration_ms = int((time.perf_counter() - t0) * 1000)
                err_str = str(biz_exc)
                if "高并发限流拦截" not in err_str:
                    await record_audit_log(
                        action="MCP_TOOL_CALL",
                        resource_id=tool_name,
                        duration_ms=duration_ms,
                        status="BLOCKED",
                        error_msg=err_str,
                        details={"agent_id": agent_id},
                    )
                raise

            except Exception as unexpected_exc:
                # 未预期系统内部异常：后台记录真实堆栈并写入审计表，对外屏蔽堆栈仅返回 error_id
                duration_ms = int((time.perf_counter() - t0) * 1000)
                logger.exception(
                    f"[{req_id}][MCP_INTERNAL_ERROR] 工具 [{tool_name}] 未预期内部异常: {unexpected_exc}"
                )
                await record_audit_log(
                    action="MCP_TOOL_CALL",
                    resource_id=tool_name,
                    duration_ms=duration_ms,
                    status="FAILED",
                    error_msg=str(unexpected_exc),
                    details={"agent_id": agent_id, "error_id": req_id},
                )
                raise RuntimeError(
                    f"【MCP 工具内部执行异常】请凭 error_id={req_id} 联系系统管理员排查"
                ) from None

            finally:
                if caller_token is not None:
                    _current_mcp_caller.reset(caller_token)
                reset_trace_context(trace_token)

        return wrapper

    return decorator
