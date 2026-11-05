"""src/rag_kb/core/resilience/classifier.py —— 五级结构化精确错误归因分类器。

严格遵循企业 Fail-Fast 原则：
- ERR_SECURITY: 输入注入 / 违规违禁拦截 (业务合规阻断，严禁热切换！立即向上抛出)
- ERR_UPSTREAM_AUTH: 401/403 凭据未授权 / API Key 禁用 (致命凭据故障，0等待立即跳闸 OPEN 并热切换)
- ERR_UPSTREAM_QUOTA: 429 配额耗尽 / 欠费 / Insufficient Quota (外部配额故障，立即热切换)
- ERR_BAD_REQUEST: 400/404/413/422 请求参数非法 / 上下文超长 (客户端请求错误，不重试全部节点，不触发节点跳闸，直接抛出)
- ERR_UPSTREAM_OUTAGE: 500/502/503/504/529 宕机与网关崩溃 (首字未出前允许单节点微重试 1 次后热切换)
- ERR_NETWORK_TIMEOUT: 建连超时 / 首字响应超时 (热切换候选节点)
- ERR_UNKNOWN: 其他未识别异常
"""

import asyncio
import httpx
from rag_kb.core.security.prompt_guard import PromptSecurityError

def is_fatal_quota_exhausted(exc: Exception) -> bool:
    """判定 429 异常是否属于账户欠费/额度耗尽（确定性不可用，应单次 0 等待立即跳闸 OPEN；区别于瞬时并发 RPM/TPM 限流）"""
    err_str = str(exc).lower()
    resp_text = ""
    if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
        try:
            resp_text = exc.response.text.lower()
        except Exception:
            resp_text = ""
    search_str = f"{err_str} {resp_text}".strip()
    return any(k in search_str for k in (
        "insufficient_quota",
        "quota_exceeded",
        "out of credit",
        "balance is zero",
        "insufficient balance",
        "account_deactivated",
        "arrears",
        "欠费",
        "余额不足",
    ))


def classify_error(exc: Exception) -> str:
    """
    将模型调用异常精确映射为结构化错误等级，严格杜绝裸子串误判：
    """
    # 1. 结构化/业务安全异常优先识别 (严禁切换模型！)
    if isinstance(exc, PromptSecurityError):
        return "ERR_SECURITY"

    err_str = str(exc).lower()
    resp_text = ""
    if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
        try:
            resp_text = exc.response.text.lower()
        except Exception:
            resp_text = ""
    search_str = f"{err_str} {resp_text}".strip()

    # 检查 HTTP 状态码对象
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        # 优先检查 400 系列中的安全拦截 (如 content_filter, prompt_injection, data_inspection_failed)
        if status in (400, 422):
            if any(k in search_str for k in ("prompt_injection", "content_filter", "data_inspection_failed", "sensitive_words", "jailbreak")):
                return "ERR_SECURITY"
            return "ERR_BAD_REQUEST"

        if status in (401, 403):
            return "ERR_UPSTREAM_AUTH"

        if status == 429:
            return "ERR_UPSTREAM_QUOTA"

        if status in (500, 502, 503, 504, 529):
            return "ERR_UPSTREAM_OUTAGE"

        if status in (404, 413):
            return "ERR_BAD_REQUEST"

    # 非 HTTPStatusError 时的安全拦截匹配 (精准特征词，绝不匹配裸单词 'security')
    if any(k in search_str for k in (
        "prompt injection",
        "prompt guard",
        "prompt_injection",
        "content_filter",
        "data_inspection_failed",
        "违规",
        "违禁",
        "敏感词",
        "越狱指令"
    )):
        return "ERR_SECURITY"

    # 401/403 凭据未授权特征词
    if any(k in search_str for k in (
        "api_key_disabled",
        "api key is disabled",
        "invalid_api_key",
        "invalid api key",
        "unauthorized",
        "forbidden",
        "authentication failed",
        "http 401",
        "http 403",
        "status_code: 401",
        "status_code: 403",
        "code: 401",
        "code: 403",
    )):
        return "ERR_UPSTREAM_AUTH"

    # 429 欠费与配额耗尽特征词
    if any(k in search_str for k in (
        "insufficient_quota",
        "quota_exceeded",
        "out of credit",
        "rate_limit_exceeded",
        "rate limit exceeded",
        "http 429",
        "status_code: 429",
        "code: 429",
        "too many requests"
    )):
        return "ERR_UPSTREAM_QUOTA"

    # 400/422/413 客户端参数错误特征词
    if any(k in search_str for k in (
        "context_length_exceeded",
        "maximum context length",
        "invalid_request_error",
        "bad request",
        "http 400",
        "status_code: 400",
        "code: 400",
        "http 422",
        "status_code: 422",
    )):
        return "ERR_BAD_REQUEST"

    # 网络连接与超时
    if isinstance(exc, (httpx.TimeoutException, TimeoutError, asyncio.TimeoutError)) or any(k in search_str for k in (
        "timeout",
        "timed out",
        "readtimeout",
        "connecttimeout",
        "pooltimeout"
    )):
        return "ERR_NETWORK_TIMEOUT"

    # 5xx 服务端宕机与网关崩溃特征词 (避免裸数字碰撞，使用复合短语)
    if any(k in search_str for k in (
        "500 internal server error",
        "502 bad gateway",
        "503 service unavailable",
        "504 gateway timeout",
        "529 site overloaded",
        "bad gateway",
        "service unavailable",
        "gateway timeout",
        "server error",
        "http 500",
        "http 502",
        "http 503",
        "http 504",
        "status_code: 500",
        "status_code: 502",
        "status_code: 503",
        "status_code: 504"
    )):
        return "ERR_UPSTREAM_OUTAGE"

    return "ERR_UNKNOWN"
