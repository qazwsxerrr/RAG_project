"""src/rag_kb/core/redis_client.py —— 企业级真实 Redis 异步连接池与 Fail-Fast 健康自检中心。

严格遵循 AGENTS.md 红线准则：
1. 必须且仅能直连真实 Redis 服务端（WSL 本地或容器集群）；
2. 严禁任何形式的内存字典伪造回退（禁止 FakeRedis / MemoryFallback）；
3. 连接池按当前 asyncio 事件循环安全隔离，避免 pytest 多循环或生命周期切换引发跨 Loop 异常；
4. 启动与调用阶段若 Redis 不可达，立即抛出真实异常中断（Fail-Fast）。
"""

import asyncio
import logging
from typing import Dict, Optional
import redis.asyncio as aioredis
from rag_kb.core.config import settings

logger = logging.getLogger(__name__)

# 按事件循环隔离客户端缓存，兼容 uvicorn 常驻单循环与 pytest 多函数级事件循环
_LOOP_CLIENTS: Dict[int, aioredis.Redis] = {}


def get_redis_client() -> aioredis.Redis:
    """获取绑定至当前运行事件循环的异步 Redis 客户端"""
    try:
        loop = asyncio.get_running_loop()
        loop_id = id(loop)
    except RuntimeError:
        loop_id = 0

    client = _LOOP_CLIENTS.get(loop_id)
    if client is None:
        if not settings.REDIS_URL or not settings.REDIS_URL.strip():
            raise RuntimeError("【Redis 配置缺失】未配置有效的 REDIS_URL，拒绝回退内存模式 (Fail-Fast)！")
        client = aioredis.from_url(
            settings.REDIS_URL.strip(),
            decode_responses=True,
            max_connections=64,
            socket_connect_timeout=5.0,
            socket_timeout=10.0,
            health_check_interval=30,
        )
        _LOOP_CLIENTS[loop_id] = client
    return client


async def verify_redis_on_startup() -> None:
    """应用启动时强校验 Redis 连通性，不可达立即 Fail-Fast 报错终止启动，并幂等初始化消费者组"""
    try:
        client = get_redis_client()
        pong = await client.ping()
        if not pong:
            raise RuntimeError("Redis PING 返回 False")
        # 幂等初始化 Streams 消费者组 (MKSTREAM)
        try:
            await client.xgroup_create(
                name=settings.INGEST_STREAM_KEY,
                groupname=settings.INGEST_CONSUMER_GROUP,
                id="0",
                mkstream=True,
            )
        except Exception as xg_err:
            if "BUSYGROUP" not in str(xg_err):
                logger.warning(f"[Redis] 初始化消费者组提示: {xg_err}")
        logger.info(f"[Redis] 已成功连接真实 Redis 服务 ({settings.REDIS_URL}) 并确认消费者组。")
    except Exception as e:
        logger.error(f"[Redis] 启动连通性自检失败 ({settings.REDIS_URL}): {e}", exc_info=True)
        raise RuntimeError(
            f"【启动 Redis 自检失败】无法连接真实 Redis 服务 ({settings.REDIS_URL}): {e} (Fail-Fast)"
        ) from e


async def check_redis_health() -> str:
    """供 /health 探活路由实时探测真实 Redis 在线状态"""
    client = get_redis_client()
    pong = await client.ping()
    return "online" if pong else "offline"


async def close_redis_clients() -> None:
    """优雅释放当前已创建的异步 Redis 连接池"""
    try:
        loop = asyncio.get_running_loop()
        target_id: Optional[int] = id(loop)
    except RuntimeError:
        target_id = None

    if target_id is not None and target_id in _LOOP_CLIENTS:
        client = _LOOP_CLIENTS.pop(target_id)
        try:
            await client.aclose()
        except Exception:
            pass
