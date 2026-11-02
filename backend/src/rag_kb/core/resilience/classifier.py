"""src/rag_kb/core/resilience/classifier.py —— 四级结构化错误归因分类器。

严格遵循企业 Fail-Fast 原则：
- ERR_SECURITY: 输入注入 / 违规拦截 (业务合规阻断，严禁热切换！立即向上抛出)
- ERR_UPSTREAM_QUOTA: 429 配额耗尽 / 欠费 / Insufficient Quota (致命外部故障，立即毫秒级热切换候选节点)
- ERR_UPSTREAM_OUTAGE: 500/502/503/504/529 宕机与网关崩溃 (热切换候选节点)
- ERR_NETWORK_TIMEOUT: 建连超时 / 响应超时 (热切换候选节点)
- ERR_UNKNOWN: 其他未识别异常
"""

import httpx

def classify_error(exc: Exception) -> str:
    """
    将模型调用异常精确映射为四大错误等级，严格遵循企业 Fail-Fast 规范：
    """
    err_str = str(exc).lower()

    # 1. 业务安全违规与注入拦截 —— 严禁切换模型！
    if any(k in err_str for k in ("injection", "prompt guard", "security", "违禁", "敏感词", "prompt_injection")):
        return "ERR_SECURITY"

    # 2. 欠费与配额耗尽 (429 Insufficient Quota, Out of Credit 等)
    if any(k in err_str for k in ("insufficient_quota", "429", "balance", "quota_exceeded", "out of credit", "rate_limit_exceeded")):
        return "ERR_UPSTREAM_QUOTA"

    # 3. HTTP 状态码精准识别 (含 401/403 外部凭据失效与 5xx 宕机崩溃)
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code in (500, 502, 503, 504, 529, 401, 403):
            return "ERR_UPSTREAM_OUTAGE"
        if code == 429:
            return "ERR_UPSTREAM_QUOTA"

    if any(code in err_str for code in ("500", "502", "503", "504", "529", "401", "403", "bad gateway", "service unavailable", "gateway timeout", "api_key_disabled", "api key is disabled", "invalid_api_key", "invalid api key", "unauthorized", "forbidden")):
        return "ERR_UPSTREAM_OUTAGE"

    # 4. 网络传输与连接超时
    if isinstance(exc, (httpx.TimeoutException, TimeoutError)) or any(k in err_str for k in ("timeout", "timed out")):
        return "ERR_NETWORK_TIMEOUT"

    return "ERR_UNKNOWN"
