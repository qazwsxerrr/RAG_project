"""src/rag_kb/api/deps.py —— 跨路由共用的 FastAPI 鉴权依赖与双版本实时核验中心 (cv=2)。

核心安全不变量：
1. 强制无状态 Bearer RS256 验签 + cv=2 契约校验，拒绝 cv=1 旧令牌与旧版流式短票；
2. 彻底清除 allowed_department_prefixes 与 derive_secure_department_scope 硬编码；
3. 每次请求核对 PostgreSQL 真实 sys_users.is_active、authz_version、租户 policy_epoch 及自然到期时间，
   并核对 Redis 秒级吊销/时间戳边界（即使 Redis 为旧值或写入失败，旧版本 JWT 仍被数据库版本检查 401 拦截）；
4. 流式短票绑定 user_id + doc_id + purpose + cv=2，每次使用重验双版本、自然到期与对象权限。
"""

import json
import secrets
import time
import uuid
from pathlib import Path
from typing import Optional, List, Any, Callable, Literal, Sequence
import jwt
from fastapi import Header, HTTPException, status, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from rag_kb.core.authz import (
    EffectiveAuthzContext,
    get_db_version_and_expiry_error,
    invalidate_authz_cache,
    is_path_under_prefix,
    load_effective_authz_context,
)
from rag_kb.core.config import settings
from rag_kb.core.constants import Permissions, VALID_DATA_SCOPES
from rag_kb.core.redis_client import get_redis_client
from rag_kb.db.session import AsyncSessionLocal, get_db

# 全局公钥缓存
_PUBLIC_KEY_CACHE: Optional[str] = None
_JWKS_CLIENT: Optional[jwt.PyJWKClient] = None

REVOKED_USER_KEY_PREFIX = "auth:revoked:"
PERM_CHANGED_USER_KEY_PREFIX = "auth:perm_changed_at:"
USER_AUTHZ_VER_KEY_PREFIX = "auth:user_ver:"
TENANT_POLICY_EPOCH_KEY_PREFIX = "auth:policy_epoch:"
STREAM_TICKET_KEY_PREFIX = "auth:stream_ticket:"
SESSION_AUTH_ERROR_DETAILS = {
    "auth_revoked": "账号权限已变更、到期或停用，数据流已断开",
    "auth_expired": "访问凭据已过期，请续期后继续",
}


async def revoke_user_access(
    user_id: str,
    ttl_seconds: Optional[int] = None,
    authz_version: Optional[int] = None,
) -> None:
    """向真实 Redis 写入秒级停用吊销标记并推进毫秒边界"""
    clean_uid = (user_id or "").strip()
    if not clean_uid:
        raise ValueError("user_id 不能为空")
    ttl = max(600, int(ttl_seconds or settings.AUTH_REVOCATION_TTL_SECONDS))
    redis_cli = get_redis_client()
    now_ms = int(time.time() * 1000)
    async with redis_cli.pipeline(transaction=True) as pipe:
        pipe.set(f"{REVOKED_USER_KEY_PREFIX}{clean_uid}", "1", ex=ttl)
        pipe.set(
            f"{PERM_CHANGED_USER_KEY_PREFIX}{clean_uid}",
            str(now_ms),
            ex=max(ttl, int(settings.AUTH_REVOCATION_TTL_SECONDS)),
        )
        if authz_version is not None:
            pipe.set(
                f"{USER_AUTHZ_VER_KEY_PREFIX}{clean_uid}",
                str(int(authz_version)),
                ex=max(ttl, int(settings.AUTH_REVOCATION_TTL_SECONDS)),
            )
        await pipe.execute()
    invalidate_authz_cache(user_id=clean_uid)


async def restore_user_access(
    user_id: str,
    authz_version: Optional[int] = None,
) -> int:
    """解除指定员工的 Redis 吊销拦截标记，并强制推进 perm_changed_at 毫秒边界"""
    clean_uid = (user_id or "").strip()
    if not clean_uid:
        return 0
    redis_cli = get_redis_client()
    ttl = max(600, int(settings.AUTH_REVOCATION_TTL_SECONDS))
    now_ms = int(time.time() * 1000)
    async with redis_cli.pipeline(transaction=True) as pipe:
        pipe.delete(f"{REVOKED_USER_KEY_PREFIX}{clean_uid}")
        pipe.set(f"{PERM_CHANGED_USER_KEY_PREFIX}{clean_uid}", str(now_ms), ex=ttl)
        if authz_version is not None:
            pipe.set(f"{USER_AUTHZ_VER_KEY_PREFIX}{clean_uid}", str(int(authz_version)), ex=ttl)
        results = await pipe.execute()
    invalidate_authz_cache(user_id=clean_uid)
    return int(results[0] or 0)


async def publish_tenant_policy_epoch_to_redis(tenant_id: str, committed_epoch: int) -> None:
    """将数据库已提交的租户策略版本号发布至 Redis（写入失败立即抛异常 Fail-Fast）"""
    clean_tid = (tenant_id or "").strip()
    if not clean_tid:
        raise ValueError("tenant_id 不能为空")
    redis_cli = get_redis_client()
    ttl = max(86400, int(settings.AUTH_REVOCATION_TTL_SECONDS))
    await redis_cli.set(
        f"{TENANT_POLICY_EPOCH_KEY_PREFIX}{clean_tid}",
        str(int(committed_epoch)),
        ex=ttl,
    )
    invalidate_authz_cache(tenant_id=clean_tid)


def _is_iat_ms_stale(raw_perm_changed_val: Any, token_iat_ms: Optional[int]) -> bool:
    if token_iat_ms is None:
        return True
    changed_ms = int(raw_perm_changed_val)
    return int(token_iat_ms) <= changed_ms


async def get_session_auth_error(
    user_or_id: Any,
    token_iat_ms: Optional[int] = None,
    token_exp: Optional[int] = None,
    watched_doc_ids: Optional[Sequence[uuid.UUID]] = None,
) -> Optional[Literal["auth_revoked", "auth_expired"]]:
    """
    供活跃 SSE 长连接在发送受保护事件前实时复核凭证与权限状态：
    1. 核对 PostgreSQL 实时 is_active、authz_version、租户 policy_epoch 与自然到期点（若传入 EffectiveAuthzContext）；
    2. 核对 Redis 秒级吊销键与 perm_changed_at 毫秒边界；
    3. 核对 token_exp 是否已到期。
    """
    ctx: Optional[EffectiveAuthzContext] = user_or_id if isinstance(user_or_id, EffectiveAuthzContext) else None
    clean_uid = (ctx.user_id if ctx is not None else str(user_or_id or "")).strip()
    eff_iat_ms = ctx.token_iat_ms if (ctx is not None and token_iat_ms is None) else token_iat_ms
    eff_exp = ctx.token_exp if (ctx is not None and token_exp is None) else token_exp

    if not clean_uid or eff_iat_ms is None or eff_iat_ms <= 0:
        return "auth_revoked"

    # 1. PostgreSQL 实时双版本、停用与自然到期复核
    if ctx is not None:
        async with AsyncSessionLocal() as db:
            db_err = await get_db_version_and_expiry_error(db, ctx, watched_doc_ids=watched_doc_ids)
            if db_err:
                return db_err  # type: ignore[return-value]

    # 2. Redis 秒级吊销与毫秒边界复核
    redis_cli = get_redis_client()
    revoked_val, perm_changed_val = await redis_cli.mget(
        f"{REVOKED_USER_KEY_PREFIX}{clean_uid}",
        f"{PERM_CHANGED_USER_KEY_PREFIX}{clean_uid}",
    )
    if revoked_val is not None:
        return "auth_revoked"
    if perm_changed_val is not None and _is_iat_ms_stale(perm_changed_val, token_iat_ms=eff_iat_ms):
        return "auth_revoked"
    if eff_exp is not None and int(time.time()) >= int(eff_exp):
        return "auth_expired"
    return None


async def assert_token_not_revoked_or_stale(
    user_id: str,
    token_iat_ms: Optional[int] = None,
) -> None:
    """校验 Redis 中的秒级停用吊销与毫秒级权限变更边界"""
    clean_uid = (user_id or "").strip()
    if not clean_uid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="【鉴权失败】缺少有效的用户标识",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if token_iat_ms is None or token_iat_ms <= 0:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="【鉴权失败】令牌缺少有效的毫秒级签发时间声明 (iat_ms)",
            headers={"WWW-Authenticate": "Bearer"},
        )
    redis_cli = get_redis_client()
    revoked_val, perm_changed_val = await redis_cli.mget(
        f"{REVOKED_USER_KEY_PREFIX}{clean_uid}",
        f"{PERM_CHANGED_USER_KEY_PREFIX}{clean_uid}",
    )
    if revoked_val is not None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="【鉴权失败】当前账号已被停用或令牌已吊销",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if perm_changed_val is not None and _is_iat_ms_stale(perm_changed_val, token_iat_ms=token_iat_ms):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="【鉴权失败】账号角色或部门权限已更新，请使用最新凭据重试",
            headers={"WWW-Authenticate": "Bearer"},
        )


async def issue_stream_ticket(user: EffectiveAuthzContext, doc_id: str, purpose: str) -> tuple[str, int]:
    """签发绑定 user_id + doc_id + purpose + cv=2 的短期受限只读票据"""
    if purpose not in ("preview", "events"):
        raise ValueError(f"不支持的短票用途: {purpose}")
    await assert_token_not_revoked_or_stale(user.user_id, token_iat_ms=user.token_iat_ms)
    ticket = secrets.token_urlsafe(32)
    ttl = max(15, min(300, int(settings.STREAM_TICKET_TTL_SECONDS)))
    redis_cli = get_redis_client()
    record = {
        "cv": 2,
        "purpose": purpose,
        "doc_id": str(doc_id),
        "issued_at": int(time.time()),
        "user": user.to_ticket_snapshot(),
    }
    await redis_cli.set(
        f"{STREAM_TICKET_KEY_PREFIX}{ticket}",
        json.dumps(record, ensure_ascii=False),
        ex=ttl,
    )
    return ticket, ttl


async def verify_stream_ticket(
    ticket: str,
    expected_doc_id: str,
    expected_purpose: str,
    db: Optional[AsyncSession] = None,
) -> EffectiveAuthzContext:
    """
    校验单资源受限短票 (cv=2)：
    1. 拒绝旧版 cv!=2 短票；
    2. 严格比对 doc_id、purpose 与最大有效时长上限；
    3. 实时核对 PostgreSQL 双版本/账号状态/自然到期并重算 EffectiveAuthzContext，再核对 Redis 吊销状态；
    4. 路由层在拿到上下文后继续执行 assert_doc_access 对象级复核（单文档改密或 ACL 撤销即时拦截！）。
    """
    clean_ticket = (ticket or "").strip()
    if not clean_ticket:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="【鉴权失败】缺少有效的流式访问短票",
        )
    redis_cli = get_redis_client()
    redis_key = f"{STREAM_TICKET_KEY_PREFIX}{clean_ticket}"
    raw = await redis_cli.get(redis_key)
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="【鉴权失败】流式访问短票不存在或已过期，请重新打开",
        )
    data = json.loads(raw)
    if int(data.get("cv") or 0) != 2:
        await redis_cli.delete(redis_key)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="【鉴权失败】旧版流式短票已作废，请刷新重新获取",
        )
    if data.get("doc_id") != str(expected_doc_id) or data.get("purpose") != expected_purpose:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="【越权访问拦截】该短票仅绑定指定文档与单一用途，严禁跨资源复用",
        )
    if data.get("issued_at") is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="【鉴权失败】流式短票缺少必填时间戳字段",
        )
    issued_at = int(data["issued_at"])
    now_sec = int(time.time())
    max_lifetime = max(60, int(settings.STREAM_TICKET_MAX_LIFETIME_SECONDS))
    elapsed = max(0, now_sec - issued_at)
    if elapsed >= max_lifetime:
        await redis_cli.delete(redis_key)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="【鉴权失败】单次预览会话已达最大有效时长，请重新打开文档",
        )

    user_snap = data.get("user") or {}
    if int(user_snap.get("cv") or 0) != 2:
        await redis_cli.delete(redis_key)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="【鉴权失败】短票用户契约版本非法，请重新获取",
        )

    async def _load(session: AsyncSession) -> EffectiveAuthzContext:
        return await load_effective_authz_context(
            db=session,
            user_id=str(user_snap["user_id"]),
            tenant_id=str(user_snap["tenant_id"]),
            username=str(user_snap.get("username") or ""),
            idp_roles=list(user_snap.get("idp_roles") or user_snap.get("roles") or []),
            token_authz_ver=int(user_snap["authz_ver"]),
            token_policy_epoch=int(user_snap["policy_epoch"]),
            token_iat_ms=int(user_snap["token_iat_ms"]),
            token_exp=issued_at + max_lifetime,
            delegated_agent_id=user_snap.get("delegated_agent_id"),
        )

    # 先按数据库单一事实源核验双版本与停用，再核验 Redis 吊销
    if db is not None:
        ctx = await _load(db)
    else:
        async with AsyncSessionLocal() as session:
            ctx = await _load(session)

    await assert_token_not_revoked_or_stale(ctx.user_id, token_iat_ms=ctx.token_iat_ms)

    if expected_purpose == "preview":
        remaining_cap = max_lifetime - elapsed
        sliding_ttl = min(max(60, int(settings.STREAM_TICKET_SLIDING_TTL_SECONDS)), remaining_cap)
        await redis_cli.expire(redis_key, sliding_ttl)

    from rag_kb.core.logging.context import get_trace_context
    trace_ctx = get_trace_context()
    trace_ctx.user_id = ctx.user_id
    trace_ctx.tenant_id = ctx.tenant_id
    return ctx


def get_jwt_public_key(token: Optional[str] = None) -> Any:
    """获取 RS256 验签公钥 (支持 JWKS 远程发现与本地公钥挂载)"""
    global _PUBLIC_KEY_CACHE, _JWKS_CLIENT

    if settings.JWT_JWKS_URL:
        if _JWKS_CLIENT is None:
            _JWKS_CLIENT = jwt.PyJWKClient(settings.JWT_JWKS_URL)
        if token:
            try:
                signing_key = _JWKS_CLIENT.get_signing_key_from_jwt(token)
                return signing_key.key
            except Exception as e:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail=f"【鉴权失败】从 JWKS 获取公钥失败: {str(e)}",
                    headers={"WWW-Authenticate": "Bearer"},
                )

    if _PUBLIC_KEY_CACHE:
        return _PUBLIC_KEY_CACHE

    if settings.JWT_PUBLIC_KEY and settings.JWT_PUBLIC_KEY.strip():
        _PUBLIC_KEY_CACHE = settings.JWT_PUBLIC_KEY.strip()
        return _PUBLIC_KEY_CACHE

    if settings.JWT_PUBLIC_KEY_PATH:
        key_path = Path(settings.JWT_PUBLIC_KEY_PATH)
        if not key_path.is_absolute():
            from rag_kb.core.config import WORKSPACE_ROOT
            key_path = WORKSPACE_ROOT / key_path
        if key_path.is_file():
            _PUBLIC_KEY_CACHE = key_path.read_text(encoding="utf-8").strip()
            return _PUBLIC_KEY_CACHE

    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="【鉴权中心配置致命错误】未找到有效的 JWT RS256 验签公钥！",
    )


def verify_jwt_setup_on_startup() -> None:
    """启动时强校验 JWKS 可达或本地验签公钥有效 (Fail-Fast)"""
    if settings.JWT_JWKS_URL:
        try:
            client = jwt.PyJWKClient(settings.JWT_JWKS_URL)
            jwk_set = client.get_jwk_set()
            if not jwk_set or not getattr(jwk_set, "keys", None):
                raise RuntimeError(f"JWKS 端点返回密钥列表为空: {settings.JWT_JWKS_URL}")
            return
        except Exception as e:
            raise RuntimeError(
                f"【启动鉴权自检失败】无法连接或解析远程 JWKS 公钥端点 ({settings.JWT_JWKS_URL}): {e} (Fail-Fast)"
            )

    try:
        pub_key = get_jwt_public_key()
        if not pub_key or "BEGIN PUBLIC KEY" not in str(pub_key):
            raise RuntimeError("公钥内容格式非法，缺少 BEGIN PUBLIC KEY 标识")
    except Exception as e:
        raise RuntimeError(f"【启动鉴权自检失败】未配置有效的 JWT RS256 验签公钥: {e} (Fail-Fast)")


async def authenticate_bearer_header(
    authorization: Optional[str] = None,
    db: Optional[AsyncSession] = None,
) -> EffectiveAuthzContext:
    """
    服务端严格校验 cv=2 RS256 Bearer JWT，实时核对数据库双版本与自然到期，并加载 EffectiveAuthzContext。
    """
    if not authorization or not authorization.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="【鉴权失败】缺少必要的 Authorization 请求头凭据",
            headers={"WWW-Authenticate": "Bearer"},
        )

    parts = authorization.strip().split(" ")
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="【鉴权失败】Authorization 格式必须为 Bearer <token>",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = parts[1]
    public_key = get_jwt_public_key(token)

    try:
        payload = jwt.decode(
            token,
            public_key,
            algorithms=["RS256"],
            audience=settings.JWT_AUDIENCE,
            issuer=settings.JWT_ISSUER,
            options={"require": ["exp", "iat", "iat_ms", "sub", "iss", "aud"]},
        )
    except jwt.PyJWTError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"【鉴权失败】JWT 令牌验签失败或已过期: {str(e)}",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # cv=2 必填声明校验（拒绝 cv=1 旧版令牌）
    required_claims = [
        "sub",
        "username",
        "tenant_id",
        "roles",
        "perms",
        "dept_path",
        "dept_paths",
        "scope_by_action",
        "position_code",
        "clearance_level",
        "authz_ver",
        "policy_epoch",
        "is_to_c",
        "cv",
        "typ",
        "iat",
        "iat_ms",
        "exp",
    ]
    for claim in required_claims:
        if claim not in payload or payload[claim] is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"【鉴权失败】JWT 载荷缺少必填字段: {claim}",
                headers={"WWW-Authenticate": "Bearer"},
            )

    typ = payload["typ"]
    if typ != "access":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"【鉴权失败】不支持的令牌类型: {typ}",
            headers={"WWW-Authenticate": "Bearer"},
        )

    cv = payload["cv"]
    if cv != 2:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"【鉴权失败】不受支持的契约版本 cv={cv}（仅接受 cv=2）",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if bool(payload["is_to_c"]):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="【访问拒绝】本系统当前版本仅面向 To B 内部员工，拒绝 To C 访问",
        )

    if not isinstance(payload["roles"], list) or not isinstance(payload["perms"], list):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="【鉴权失败】JWT roles/perms 字段类型必须为数组",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not isinstance(payload["dept_paths"], list):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="【鉴权失败】JWT dept_paths 字段类型必须为数组",
            headers={"WWW-Authenticate": "Bearer"},
        )
    scope_by_action_claim = payload["scope_by_action"]
    if not isinstance(scope_by_action_claim, dict):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="【鉴权失败】JWT scope_by_action 字段类型必须为对象",
            headers={"WWW-Authenticate": "Bearer"},
        )
    for act_k, scope_v in scope_by_action_claim.items():
        if str(scope_v) not in VALID_DATA_SCOPES:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"【鉴权失败】scope_by_action[{act_k}] 包含非白名单数据范围: {scope_v}",
                headers={"WWW-Authenticate": "Bearer"},
            )

    try:
        int(payload["iat"])
        token_iat_ms = int(payload["iat_ms"])
        token_exp = int(payload["exp"])
        token_authz_ver = int(payload["authz_ver"])
        token_policy_epoch = int(payload["policy_epoch"])
        int(payload["clearance_level"])
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="【鉴权失败】JWT 数值/时间戳声明格式非法",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user_id_str = str(payload["sub"]).strip()
    tenant_id_str = str(payload["tenant_id"]).strip()
    if not user_id_str or not tenant_id_str:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="【鉴权失败】JWT sub/tenant_id 不能为空",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 提取 RS256 签名保护的受托 Agent 身份声明
    delegated_agent_id: Optional[str] = None
    act_claim = payload.get("act")
    if isinstance(act_claim, dict) and act_claim.get("sub"):
        delegated_agent_id = str(act_claim["sub"]).strip() or None
    elif payload.get("agent_id"):
        delegated_agent_id = str(payload["agent_id"]).strip() or None
    elif payload.get("azp"):
        delegated_agent_id = str(payload["azp"]).strip() or None
    elif payload.get("client_id"):
        delegated_agent_id = str(payload["client_id"]).strip() or None

    raw_idp_roles = payload.get("idp_roles")
    idp_roles = list(raw_idp_roles) if isinstance(raw_idp_roles, list) else list(payload["roles"])

    # 1. PostgreSQL 单一事实源双版本校验与自然到期重算（优先于 Redis，即使 Redis 未同步也能拦截旧授权）
    if db is not None:
        ctx = await load_effective_authz_context(
            db=db,
            user_id=user_id_str,
            tenant_id=tenant_id_str,
            username=str(payload["username"]),
            idp_roles=idp_roles,
            token_authz_ver=token_authz_ver,
            token_policy_epoch=token_policy_epoch,
            token_iat_ms=token_iat_ms,
            token_exp=token_exp,
            delegated_agent_id=delegated_agent_id,
        )
    else:
        async with AsyncSessionLocal() as session:
            ctx = await load_effective_authz_context(
                db=session,
                user_id=user_id_str,
                tenant_id=tenant_id_str,
                username=str(payload["username"]),
                idp_roles=idp_roles,
                token_authz_ver=token_authz_ver,
                token_policy_epoch=token_policy_epoch,
                token_iat_ms=token_iat_ms,
                token_exp=token_exp,
                delegated_agent_id=delegated_agent_id,
            )

    # 2. Redis 秒级吊销与毫秒边界校验
    await assert_token_not_revoked_or_stale(ctx.user_id, token_iat_ms=token_iat_ms)

    from rag_kb.core.logging.context import get_trace_context
    trace_ctx = get_trace_context()
    trace_ctx.user_id = ctx.user_id
    trace_ctx.tenant_id = ctx.tenant_id

    return ctx


async def get_current_user(
    authorization: Optional[str] = Header(None, alias="Authorization"),
) -> EffectiveAuthzContext:
    return await authenticate_bearer_header(authorization=authorization, db=None)


async def get_current_user_with_db(
    authorization: Optional[str] = Header(None, alias="Authorization"),
    db: AsyncSession = Depends(get_db),
) -> EffectiveAuthzContext:
    return await authenticate_bearer_header(authorization=authorization, db=db)


def require_permission(perm: Any) -> Callable:
    """权限点拦截依赖生成器（基于数据库实时重算后的 EffectiveAuthzContext.perms）"""
    perm_val = perm.value if isinstance(perm, Permissions) else str(perm)

    async def _perm_dependency(user: EffectiveAuthzContext = Depends(get_current_user_with_db)) -> EffectiveAuthzContext:
        if not user.has_permission(perm_val):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"【权限不足】当前操作需要 [{perm_val}] 权限点，您的有效角色缺乏此权限",
            )
        return user

    return _perm_dependency
