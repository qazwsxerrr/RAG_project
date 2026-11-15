"""src/rag_kb/api/auth.py —— 用户身份回显与秒级访问吊销管控端点。

Phase-6 严格规范：
- 物理删除免认证签发端点 /dev-token 与相关 DTO，拒绝任何本地伪造身份入口；
- 提供 GET /api/v1/auth/me 回显通过验签的当前用户上下文；
- 提供管理员专属 POST/DELETE /api/v1/auth/revoke/{user_id} 基于真实 Redis 执行秒级停用吊销与恢复。
"""

import uuid
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from rag_kb.api.deps import (
    UserContext,
    get_current_user,
    require_permission,
    revoke_user_access,
    restore_user_access,
    REVOKED_USER_KEY_PREFIX,
    PERM_CHANGED_USER_KEY_PREFIX,
)
from rag_kb.core.config import settings
from rag_kb.core.constants import Permissions
from rag_kb.core.logging.audit import record_audit_log
from rag_kb.core.redis_client import get_redis_client
from rag_kb.db.session import get_db

router = APIRouter(prefix="/auth", tags=["统一鉴权与身份认证"])


class RevokeUserRequest(BaseModel):
    ttl_seconds: Optional[int] = Field(default=None, ge=600, le=604800)
    reason: Optional[str] = Field(default=None, max_length=200)


async def _snapshot_redis_auth_state(redis_cli, uid_str: str) -> dict:
    revoked_key = f"{REVOKED_USER_KEY_PREFIX}{uid_str}"
    perm_key = f"{PERM_CHANGED_USER_KEY_PREFIX}{uid_str}"
    prev_revoked_val, prev_perm_val = await redis_cli.mget(revoked_key, perm_key)
    prev_revoked_ttl = await redis_cli.ttl(revoked_key) if prev_revoked_val is not None else -1
    prev_perm_ttl = await redis_cli.ttl(perm_key) if prev_perm_val is not None else -1
    return {
        "revoked_key": revoked_key,
        "perm_key": perm_key,
        "prev_revoked_val": prev_revoked_val,
        "prev_perm_val": prev_perm_val,
        "prev_revoked_ttl": prev_revoked_ttl,
        "prev_perm_ttl": prev_perm_ttl,
    }


async def _restore_redis_auth_state(redis_cli, snapshot: dict) -> None:
    revoked_key = snapshot["revoked_key"]
    perm_key = snapshot["perm_key"]
    prev_revoked_val = snapshot["prev_revoked_val"]
    prev_perm_val = snapshot["prev_perm_val"]
    prev_revoked_ttl = snapshot["prev_revoked_ttl"]
    prev_perm_ttl = snapshot["prev_perm_ttl"]
    async with redis_cli.pipeline(transaction=True) as pipe:
        if prev_revoked_val is not None:
            restore_ttl = prev_revoked_ttl if prev_revoked_ttl and prev_revoked_ttl > 0 else settings.AUTH_REVOCATION_TTL_SECONDS
            pipe.set(revoked_key, prev_revoked_val, ex=restore_ttl)
        else:
            pipe.delete(revoked_key)
        if prev_perm_val is not None:
            restore_perm_ttl = prev_perm_ttl if prev_perm_ttl and prev_perm_ttl > 0 else settings.AUTH_REVOCATION_TTL_SECONDS
            pipe.set(perm_key, prev_perm_val, ex=restore_perm_ttl)
        else:
            pipe.delete(perm_key)
        await pipe.execute()


@router.get("/me", response_model=UserContext)
async def get_my_profile(user: UserContext = Depends(get_current_user)):
    """获取当前登录用户的身份上下文与数据权限范围 (基于 Java 签发的真实 JWT)"""
    return user


@router.post("/revoke/{target_user_id}")
async def revoke_user_sessions(
    target_user_id: str,
    req: Optional[RevokeUserRequest] = None,
    db: AsyncSession = Depends(get_db),
    admin: UserContext = Depends(require_permission(Permissions.SYSTEM_ADMIN)),
):
    """
    统一账号停用与秒级吊销闭环接口：
    1. 校验目标员工在 PostgreSQL sys_users 中存在，并将其 is_active 置为 FALSE；
    2. 同步向真实 Redis 写入 auth:revoked:{user_id} 秒级拦截存量 JWT、流式短票与在线 SSE 连接；
    3. 持久化记录安全审计日志。
    """
    try:
        target_uuid = uuid.UUID(target_user_id.strip())
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="无效的员工 user_id UUID 格式")

    res = await db.execute(
        text("UPDATE sys_users SET is_active = FALSE WHERE id = :uid RETURNING real_name"),
        {"uid": target_uuid},
    )
    row = res.first()
    if row is None:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"未在 sys_users 中找到员工账号 [{target_user_id}]",
        )

    ttl = req.ttl_seconds if req else None
    uid_str = str(target_uuid)
    redis_cli = get_redis_client()
    snapshot = await _snapshot_redis_auth_state(redis_cli, uid_str)

    try:
        await revoke_user_access(uid_str, ttl_seconds=ttl)
    except Exception:
        await db.rollback()
        raise

    try:
        await db.commit()
    except Exception:
        await _restore_redis_auth_state(redis_cli, snapshot)
        await db.rollback()
        raise

    await record_audit_log(
        action="AUTH_USER_REVOKE",
        resource_id=str(target_uuid),
        status="SUCCESS",
        details={
            "operator": admin.username,
            "target_username": row[0],
            "db_is_active": False,
            "ttl_seconds": ttl,
            "reason": req.reason if req else None,
        },
    )
    return {
        "status": "revoked",
        "user_id": str(target_uuid),
        "is_active": False,
        "message": f"账号 [{row[0]}] 已停用并执行秒级令牌吊销",
    }


@router.delete("/revoke/{target_user_id}")
async def restore_revoked_user(
    target_user_id: str,
    db: AsyncSession = Depends(get_db),
    admin: UserContext = Depends(require_permission(Permissions.SYSTEM_ADMIN)),
):
    """恢复指定员工的 sys_users.is_active=TRUE 并同步清除 Redis 吊销拦截标记"""
    try:
        target_uuid = uuid.UUID(target_user_id.strip())
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="无效的员工 user_id UUID 格式")

    res = await db.execute(
        text("UPDATE sys_users SET is_active = TRUE WHERE id = :uid RETURNING real_name"),
        {"uid": target_uuid},
    )
    row = res.first()
    if row is None:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"未在 sys_users 中找到员工账号 [{target_user_id}]",
        )

    uid_str = str(target_uuid)
    redis_cli = get_redis_client()
    snapshot = await _snapshot_redis_auth_state(redis_cli, uid_str)

    try:
        removed = await restore_user_access(uid_str)
    except Exception:
        await db.rollback()
        raise

    try:
        await db.commit()
    except Exception:
        await _restore_redis_auth_state(redis_cli, snapshot)
        await db.rollback()
        raise

    await record_audit_log(
        action="AUTH_USER_RESTORE",
        resource_id=str(target_uuid),
        status="SUCCESS",
        details={"operator": admin.username, "target_username": row[0], "db_is_active": True, "removed_keys": removed},
    )
    return {
        "status": "restored",
        "user_id": str(target_uuid),
        "is_active": True,
        "removed": removed,
    }
