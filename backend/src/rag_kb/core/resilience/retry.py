"""src/rag_kb/core/resilience/retry.py —— 全局统一弹性重试工厂。

严格遵循企业 Fail-Fast 原则：
- 致命错误（400 参数非法、401 凭据缺失/欠费、403 权限拒绝、404 资源未找到）立即快速失败，绝不重试；
- 仅在网络抖动、超时、429 并发限流或 502/503/504 上游临时故障时执行指数退避重试 (1s -> 2s -> 4s)。
"""

import logging
from typing import Callable, Any
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception
import httpx
import oss2

logger = logging.getLogger("rag_kb.resilience")

def is_transient_error(exc: Exception) -> bool:
    """
    精准识别瞬态异常 (Transient Error):
    - 网络连接丢失、DNS 异常、连接/读取超时
    - HTTP 429 (限流)、HTTP 502/503/504 (临时网关/服务器故障)
    - 阿里云 OSS 传输层网络故障与服务端故障
    
    坚决排除 400/401/403/404 等业务与鉴权致命错误！
    """
    # 1. 网络层连接或超时
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError, ConnectionResetError, TimeoutError)):
        return True

    # 2. HTTP 状态码精准识别
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status in (429, 502, 503, 504, 529):
            return True
        return False  # 400, 401, 403, 404 等业务错误坚决不重试

    # 3. 阿里云 OSS 瞬态异常
    if isinstance(exc, (oss2.exceptions.RequestError, oss2.exceptions.ServerError)):
        return True

    return False

def retry_on_transient(
    service_name: str = "ExternalAPI",
    max_attempts: int = 3,
    min_wait: float = 1.0,
    max_wait: float = 4.0
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """
    通用瞬态故障指数退避重试装饰器工厂
    
    :param service_name: 调用的外部服务标识，用于日志记录
    :param max_attempts: 最大尝试次数 (默认 3 次)
    :param min_wait: 初始退避等待秒数
    :param max_wait: 最大退避等待秒数
    """
    return retry(
        stop=stop_after_attempt(max_attempts),
        wait=wait_exponential(multiplier=min_wait, max=max_wait),
        retry=retry_if_exception(is_transient_error),
        reraise=True,
        before_sleep=lambda retry_state: logger.warning(
            f"[{service_name}] 遭遇临时网络波动 ({retry_state.outcome.exception()})，"
            f"正在执行第 {retry_state.attempt_number} 次指数退避重试..."
        )
    )
