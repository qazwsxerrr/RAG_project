"""src/rag_kb/core/logging/middleware.py —— FastAPI 统一链路追踪与错误掩码中间件。

职责：
1. 拦截并提取请求头中的 X-Request-Id（如无则自动生成标准 UUID 标识），并在响应头中统一回写；
2. 跨协程注入 ContextVar 链路上下文；
3. 精准统计全链路 HTTP 耗时打点；
4. 异常脱敏机制：遇到内部未捕获异常时，在后台记录完整真实堆栈并关联 request_id，面向前端统一屏蔽堆栈仅回传脱敏的 error_id。
"""

import time
import logging
from typing import Callable
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response, JSONResponse, StreamingResponse
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from rag_kb.core.config import settings
from rag_kb.core.logging.context import set_trace_context, reset_trace_context, get_request_id

logger = logging.getLogger("rag_kb.access")

async def _wrap_streaming_body(
    body_iterator,
    token,
    req_id: str,
    method: str,
    path: str,
    status_code: int,
    start_time: float
):
    """确保在整个流式传输期间 ContextVar 保持活跃，并在流彻底关闭时统计真实耗时与恢复上下文"""
    try:
        async for chunk in body_iterator:
            yield chunk
    finally:
        duration_ms = int((time.perf_counter() - start_time) * 1000)
        logger.info(
            f"[{req_id}] {method} {path} status={status_code} "
            f"streaming_finished cost={duration_ms}ms"
        )
        reset_trace_context(token)

class TraceLoggingMiddleware(BaseHTTPMiddleware):
    """企业级统一请求追踪与错误掩码中间件"""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        incoming_req_id = request.headers.get("x-request-id")
        incoming_tenant_id = request.headers.get("x-tenant-id") or settings.DEFAULT_TENANT_ID
        incoming_user_id = request.headers.get("x-user-id")

        token = set_trace_context(
            request_id=incoming_req_id,
            tenant_id=incoming_tenant_id,
            user_id=incoming_user_id
        )
        req_id = get_request_id()

        start_time = time.perf_counter()
        is_streaming = False

        try:
            response = await call_next(request)

            # 统一回写链路追踪 Header
            response.headers["X-Request-Id"] = req_id

            if isinstance(response, StreamingResponse):
                # 对流式打字机响应包装其 body_iterator，将 reset 与耗时打点延迟至流结束
                response.body_iterator = _wrap_streaming_body(
                    response.body_iterator,
                    token=token,
                    req_id=req_id,
                    method=request.method,
                    path=request.url.path,
                    status_code=response.status_code,
                    start_time=start_time
                )
                is_streaming = True
                return response

            duration_ms = int((time.perf_counter() - start_time) * 1000)
            logger.info(
                f"[{req_id}] {request.method} {request.url.path} "
                f"status={response.status_code} cost={duration_ms}ms"
            )
            return response

        except (StarletteHTTPException, RequestValidationError):
            # 业务 HTTPException (400, 401, 403, 404) 或 Pydantic 校验异常放行给框架处理
            duration_ms = int((time.perf_counter() - start_time) * 1000)
            logger.warning(f"[{req_id}] 业务请求异常: {request.method} {request.url.path} cost={duration_ms}ms")
            raise

        except Exception as exc:
            # 内部未处理异常：后台记录真实完整堆栈，面向客户端安全屏蔽堆栈仅返回 error_id
            duration_ms = int((time.perf_counter() - start_time) * 1000)
            logger.exception(
                f"[{req_id}] 服务端未捕获异常: {exc} | path={request.method} {request.url.path} cost={duration_ms}ms"
            )
            content = {
                "error": True,
                "error_id": req_id,
                "message": "服务处理异常，请联系系统管理员并提供该 error_id",
                "status_code": 500
            }
            return JSONResponse(
                status_code=500,
                content=content,
                headers={"X-Request-Id": req_id}
            )

        finally:
            if not is_streaming:
                reset_trace_context(token)
