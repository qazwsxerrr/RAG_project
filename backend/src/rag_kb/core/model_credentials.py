"""backend/src/rag_kb/core/model_credentials.py —— 模型敏感密钥统一托管与短期租约客户端。

Phase-6 §7 (R4 模型密钥治理)：
- API Key 严禁硬编码入代码与 .env；
- Python 以服务身份 (X-Service-Id / X-Service-Token) 向 Java 鉴权中心换取 10min 租约凭据；
- 内存缓存至 expires_at - 60s，并发去重互斥锁；
- 鉴权中心不可达时 Fail-Fast 抛出真实异常并记录审计日志，严禁隐式回退本地或伪造虚假模型。
"""

import time
import asyncio
import logging
from typing import Dict, Any, Optional
import httpx
from pydantic import BaseModel
from rag_kb.core.config import settings
from rag_kb.core.logging.audit import record_audit_log

logger = logging.getLogger(__name__)

class ModelCredential(BaseModel):
    provider: str
    base_url: str
    api_key: str
    expires_at: int
    masked: str

    @property
    def is_expired(self) -> bool:
        """预留 60s 缓冲时间提前续期"""
        return time.time() >= (self.expires_at - 60)

_CREDENTIAL_CACHE: Dict[str, ModelCredential] = {}
_LEASE_LOCKS: Dict[str, asyncio.Lock] = {}
_GLOBAL_LOCK: Optional[asyncio.Lock] = None


def _get_global_lock() -> asyncio.Lock:
    global _GLOBAL_LOCK
    if _GLOBAL_LOCK is None:
        _GLOBAL_LOCK = asyncio.Lock()
    return _GLOBAL_LOCK


def get_cached_lease_info(provider: str, scope: str = "model:llm") -> Dict[str, Any]:
    """
    获取当前内存中已缓存的租约脱敏摘要 (仅暴露 provider / masked / expires_at，严禁返回明文 api_key)。
    供 /api/v1/system/llm/channels 大盘监控展示 (§7.2 & §8.6 用例 28)。
    """
    clean_provider = provider.lower().strip()
    cache_key = f"{clean_provider}:{scope}"
    cred = _CREDENTIAL_CACHE.get(cache_key)
    if cred is None:
        # 尝试匹配同 provider 下的任一活跃租约
        for k, v in _CREDENTIAL_CACHE.items():
            if k.startswith(f"{clean_provider}:"):
                cred = v
                break
    if cred:
        return {
            "provider": cred.provider,
            "masked": cred.masked,
            "expires_at": cred.expires_at,
            "masked_display": f"[{cred.provider}] {cred.masked} (exp:{cred.expires_at})"
        }
    return {
        "provider": clean_provider,
        "masked": f"[{clean_provider}]-lease-pending",
        "expires_at": 0,
        "masked_display": f"[{clean_provider}]-lease-managed"
    }


async def get_credential(provider: str, scope: str = "model:llm") -> ModelCredential:
    """
    根据 provider 和 scope 向鉴权中心获取/续期大模型调用短期租约凭据。
    具备本地内存缓存、到期前 60s 自动续期与高并发请求去重。
    """
    clean_provider = provider.lower().strip()
    cache_key = f"{clean_provider}:{scope}"

    # 1. 命中缓存且未过期直接返回
    cached = _CREDENTIAL_CACHE.get(cache_key)
    if cached and not cached.is_expired:
        return cached

    # 2. 获取该 key 的独立并发互斥锁，避免并发打穿 Java 鉴权中心 (§7.5)
    async with _get_global_lock():
        if cache_key not in _LEASE_LOCKS:
            _LEASE_LOCKS[cache_key] = asyncio.Lock()
        lock = _LEASE_LOCKS[cache_key]

    async with lock:
        # Double-check
        cached = _CREDENTIAL_CACHE.get(cache_key)
        if cached and not cached.is_expired:
            return cached

        # 3. 真实远程请求 Java 鉴权中心 /api/admin/internal/model-token
        endpoint = f"{settings.AUTH_CENTER_BASE_URL.rstrip('/')}/api/admin/internal/model-token"
        headers = {
            "X-Service-Id": settings.MODEL_KEY_SERVICE_ID,
            "X-Service-Token": settings.MODEL_KEY_SERVICE_SECRET,
            "Content-Type": "application/json"
        }
        payload = {
            "provider": clean_provider,
            "scopes": [scope]
        }

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(endpoint, headers=headers, json=payload)
                if resp.status_code != 200:
                    error_detail = resp.text
                    raise RuntimeError(
                        f"【模型凭据托管中心异常】换取 provider=[{clean_provider}] 凭据失败 (HTTP {resp.status_code}): {error_detail}"
                    )
                data = resp.json()
                cred = ModelCredential(
                    provider=data["provider"],
                    base_url=data["base_url"],
                    api_key=data["api_key"],
                    expires_at=data["expires_at"],
                    masked=data.get("masked", "sk-****")
                )
                _CREDENTIAL_CACHE[cache_key] = cred
                logger.info(f"【模型凭据租约】成功换取 [{clean_provider}] 租约凭据 (掩码: {cred.masked}, 到期时间: {cred.expires_at})")
                return cred
        except Exception as e:
            logger.error(f"【模型凭据致命错误】请求鉴权中心换取凭据异常: {e}", exc_info=True)
            try:
                await record_audit_log(
                    action="MODEL_CREDENTIAL_LEASE",
                    resource_id=f"{clean_provider}:{scope}",
                    status="FAILED",
                    error_msg=str(e),
                    details={"provider": clean_provider, "scope": scope}
                )
            except Exception:
                pass
            raise RuntimeError(f"【模型凭据托管失败】无法从 Java 鉴权中心获取 [{clean_provider}] 凭据: {str(e)} (Fail-Fast)")


def get_credential_sync(provider: str, scope: str = "model:llm") -> ModelCredential:
    """
    同步版短期租约获取 (供 LangGraph 同步节点或客户端延迟初始化使用)
    优先从内存缓存获取，未命中则同步直连 Java 内部服务换票
    """
    clean_provider = provider.lower().strip()
    cache_key = f"{clean_provider}:{scope}"

    cached = _CREDENTIAL_CACHE.get(cache_key)
    if cached and not cached.is_expired:
        return cached

    endpoint = f"{settings.AUTH_CENTER_BASE_URL.rstrip('/')}/api/admin/internal/model-token"
    headers = {
        "X-Service-Id": settings.MODEL_KEY_SERVICE_ID,
        "X-Service-Token": settings.MODEL_KEY_SERVICE_SECRET,
        "Content-Type": "application/json"
    }
    payload = {
        "provider": clean_provider,
        "scopes": [scope]
    }

    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(endpoint, headers=headers, json=payload)
            if resp.status_code != 200:
                raise RuntimeError(
                    f"【模型凭据托管中心异常】换取 provider=[{clean_provider}] 凭据失败 (HTTP {resp.status_code}): {resp.text}"
                )
            data = resp.json()
            cred = ModelCredential(
                provider=data["provider"],
                base_url=data["base_url"],
                api_key=data["api_key"],
                expires_at=data["expires_at"],
                masked=data.get("masked", "sk-****")
            )
            _CREDENTIAL_CACHE[cache_key] = cred
            logger.info(f"【模型凭据租约(同步)】成功换取 [{clean_provider}] 租约凭据 (掩码: {cred.masked})")
            return cred
    except Exception as e:
        logger.error(f"【模型凭据致命错误(同步)】请求鉴权中心换取凭据异常: {e}", exc_info=True)
        raise RuntimeError(f"【模型凭据托管失败】无法从 Java 鉴权中心获取 [{clean_provider}] 凭据: {str(e)} (Fail-Fast)")
