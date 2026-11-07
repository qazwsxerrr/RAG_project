"""src/rag_kb/core/rate_limit.py —— 基于 Redis + Lua 原子脚本的高并发限流、并发流护栏与幂等锁。

完全对齐 new-command.md 与 对话.txt：
1. Lua 原子滑动窗口频率限制（ZSET）：
   - 单次 RTT 在 Redis 内原子完成：清理过期记录 -> 判断当前窗口计数 -> 仅在未超限时写入唯一请求 ID；
   - 被 429 拦截的请求绝不计入滑动窗口，杜绝“越刷越锁死”漏洞；
   - 精确返回剩余冷却秒数并附加 HTTP Retry-After 响应头。
2. 单用户流式问答并发护栏（Active Stream Guard）：
   - 基于 Redis ZSET 记录活跃 stream_id（带超时自动清理防异常死锁），限制同一员工同时进行的流式问答数。
3. 文档上传短时分布式防重锁（SET NX EX）：
   - 基于 (department_path, file_sha256) 拦截前端连点双击导致的重复 OSS 上传与重复入队。
4. 严格 Fail-Fast：若 Redis 连接失败，直接抛出真实异常，严禁隐式跳过限流伪装正常。
"""

import time
import uuid
import math
import hashlib
import logging
from contextlib import asynccontextmanager
from typing import Optional, Tuple
from fastapi import Request, HTTPException, status

from rag_kb.core.config import settings
from rag_kb.core.redis_client import get_redis_client
from rag_kb.core.logging.audit import record_audit_log

logger = logging.getLogger(__name__)

# Lua 脚本 1：原子滑动窗口限流（返回 {allowed(0/1), current_count, retry_after_ms}）
_SLIDING_WINDOW_LUA = """
local key = KEYS[1]
local now_ms = tonumber(ARGV[1])
local window_ms = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local member = ARGV[4]

local cutoff = now_ms - window_ms
redis.call('ZREMRANGEBYSCORE', key, '-inf', cutoff)
local count = redis.call('ZCARD', key)

if count >= limit then
    local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
    local retry_ms = window_ms
    if oldest and #oldest >= 2 then
        retry_ms = math.max(1000, window_ms - (now_ms - tonumber(oldest[2])))
    end
    return {0, count, retry_ms}
end

redis.call('ZADD', key, now_ms, member)
redis.call('PEXPIRE', key, window_ms + 1000)
return {1, count + 1, 0}
"""

# Lua 脚本 2：原子获取并发流槽位（清理过期僵尸流 -> 判断活跃数 -> 登记当前流 ID）
_ACQUIRE_CONCURRENCY_LUA = """
local key = KEYS[1]
local now_ms = tonumber(ARGV[1])
local ttl_ms = tonumber(ARGV[2])
local max_conc = tonumber(ARGV[3])
local stream_id = ARGV[4]

redis.call('ZREMRANGEBYSCORE', key, '-inf', now_ms - ttl_ms)
local active = redis.call('ZCARD', key)
if active >= max_conc then
    return {0, active}
end

redis.call('ZADD', key, now_ms, stream_id)
redis.call('PEXPIRE', key, ttl_ms + 5000)
return {1, active + 1}
"""

_RELEASE_CONCURRENCY_LUA = """
local key = KEYS[1]
local stream_id = ARGV[1]
redis.call('ZREM', key, stream_id)
return 1
"""

_RELEASE_LOCK_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""


import ipaddress

def _is_trusted_proxy(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_loopback or ip.is_private
    except ValueError:
        return False

def extract_client_ip(request: Request) -> str:
    """
    安全提取客户端真实 IP：
    仅当直连对端 request.client.host 属于本机回环或私有反向代理网段时，才采信 X-Real-IP / X-Forwarded-For；
    否则直接返回底层 socket IP，杜绝公网直接伪造代理标头绕过频率限制。
    """
    direct_host = request.client.host if request.client else ""
    if direct_host and _is_trusted_proxy(direct_host):
        real_ip = request.headers.get("x-real-ip")
        if real_ip and real_ip.strip():
            return real_ip.strip()
        forwarded_for = request.headers.get("x-forwarded-for")
        if forwarded_for and forwarded_for.strip():
            return forwarded_for.split(",")[0].strip()
    if direct_host:
        return direct_host
    return "unknown_ip"


async def check_sliding_window_limit(
    scope: str,
    subject: str,
    limit: int,
    window_seconds: int = 60,
) -> Tuple[bool, int, int]:
    """
    执行 Redis Lua 原子滑动窗口校验。
    返回: (allowed, current_count, retry_after_seconds)
    """
    redis_cli = get_redis_client()
    now_ms = int(time.time() * 1000)
    window_ms = max(1, int(window_seconds)) * 1000
    member = f"{now_ms}:{uuid.uuid4().hex[:10]}"
    key = f"rag:rl:{scope}:{subject}"

    res = await redis_cli.eval(
        _SLIDING_WINDOW_LUA,
        1,
        key,
        str(now_ms),
        str(window_ms),
        str(max(1, int(limit))),
        member,
    )
    allowed = int(res[0]) == 1
    current_count = int(res[1])
    retry_after_sec = max(1, math.ceil(int(res[2]) / 1000.0)) if not allowed else 0
    return allowed, current_count, retry_after_sec


async def enforce_user_rate_limit(
    request: Request,
    tenant_id: str,
    user_id: str,
    scope: str,
    limit: int,
    window_seconds: Optional[int] = None,
) -> None:
    """对已认证用户与客户端 IP 执行限流拦截，超限抛出标准 HTTP 429 + Retry-After"""
    win_sec = window_seconds or settings.RATE_LIMIT_WINDOW_SECONDS
    subject = f"{tenant_id}:{user_id}" if user_id else f"ip:{extract_client_ip(request)}"

    allowed, count, retry_sec = await check_sliding_window_limit(
        scope=scope,
        subject=subject,
        limit=limit,
        window_seconds=win_sec,
    )
    if not allowed:
        detail_msg = (
            f"【高并发限流拦截】当前操作频率过高（限制 {limit} 次/{win_sec}秒），"
            f"请在 {retry_sec} 秒后重试。"
        )
        logger.warning(f"[RateLimit] 触发限流拦截 scope={scope}, subject={subject}, count={count}/{limit}")
        await record_audit_log(
            action="RATE_LIMIT_EXCEEDED",
            resource_id=scope,
            status="BLOCKED",
            error_msg=detail_msg,
            details={"scope": scope, "subject": subject, "limit": limit, "window_seconds": win_sec},
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=detail_msg,
            headers={"Retry-After": str(retry_sec)},
        )


async def acquire_chat_stream_slot(
    tenant_id: str,
    user_id: str,
    max_concurrent: Optional[int] = None,
    ttl_seconds: Optional[int] = None,
) -> str:
    """获取单用户流式问答并发槽位，超限立即抛出 HTTP 429，成功返回槽位凭证 stream_id"""
    redis_cli = get_redis_client()
    max_c = max_concurrent or settings.MAX_CONCURRENT_CHAT_STREAMS_PER_USER
    ttl_s = ttl_seconds or settings.ACTIVE_CHAT_STREAM_TTL_SECONDS
    now_ms = int(time.time() * 1000)
    stream_id = uuid.uuid4().hex
    key = f"rag:conc:chat:{tenant_id}:{user_id}"

    res = await redis_cli.eval(
        _ACQUIRE_CONCURRENCY_LUA,
        1,
        key,
        str(now_ms),
        str(int(ttl_s * 1000)),
        str(int(max_c)),
        stream_id,
    )
    if int(res[0]) != 1:
        active_cnt = int(res[1])
        detail_msg = (
            f"【并发护栏拦截】您当前已有 {active_cnt} 个智能问答正在流式生成中（上限 {max_c} 个），"
            "请等待当前回答完成后再提问。"
        )
        logger.warning(f"[ConcurrencyGuard] 拦截超额并发流 user={tenant_id}:{user_id}, active={active_cnt}")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=detail_msg,
            headers={"Retry-After": "3"},
        )
    return stream_id


async def release_chat_stream_slot(tenant_id: str, user_id: str, stream_id: str) -> None:
    """释放单用户流式问答并发槽位"""
    if not stream_id:
        return
    redis_cli = get_redis_client()
    key = f"rag:conc:chat:{tenant_id}:{user_id}"
    await redis_cli.eval(_RELEASE_CONCURRENCY_LUA, 1, key, stream_id)


@asynccontextmanager
async def upload_dedup_lock(department_path: str, file_sha256: str, ttl_seconds: Optional[int] = None):
    """
    文档上传短时分布式防抖幂等锁（SET NX EX）。
    防止高并发或前端连点在数秒内将同一份文件重复上传入队多次。
    若内部抛出任何异常，在上下文退出时通过 Lua 脚本校验 token 原子释放锁；
    若成功执行完毕，则保留 TTL 自然防抖。
    """
    redis_cli = get_redis_client()
    ttl = ttl_seconds or settings.UPLOAD_DEDUP_LOCK_TTL_SECONDS
    dept_hash = hashlib.sha256(department_path.encode("utf-8")).hexdigest()[:12]
    lock_key = f"rag:lock:upload:{dept_hash}:{file_sha256}"
    token = uuid.uuid4().hex

    acquired = await redis_cli.set(lock_key, token, nx=True, ex=max(1, int(ttl)))
    if not acquired:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="【重复提交拦截】相同文件正在上传入队处理中，请勿短时间内重复点击提交。",
            headers={"Retry-After": str(ttl)},
        )
    success = False
    try:
        yield lock_key
        success = True
    finally:
        if not success:
            try:
                await redis_cli.eval(_RELEASE_LOCK_LUA, 1, lock_key, token)
            except Exception as lock_err:
                logger.warning(f"[RateLimit] 异常释放防重锁失败 ({lock_key}): {lock_err}")


async def release_upload_dedup_lock_immediately(department_path: str, file_sha256: str) -> None:
    """当上传流程在中途（如 OSS 异常）报错未入队时，立即清理防重锁以便用户重试"""
    redis_cli = get_redis_client()
    dept_hash = hashlib.sha256(department_path.encode("utf-8")).hexdigest()[:12]
    lock_key = f"rag:lock:upload:{dept_hash}:{file_sha256}"
    await redis_cli.delete(lock_key)
