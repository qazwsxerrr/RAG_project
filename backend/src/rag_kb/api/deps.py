"""src/rag_kb/api/deps.py —— 跨路由共用的 FastAPI 依赖与企业级 RBAC 鉴权防越权中心。

严格对齐 Phase-6 跨服务 JWT 契约与三条不变量：
1. 强制无状态 Bearer RS256 签名校验（对接 Java 鉴权中心，严禁 HS256 与本地假签）；
2. 服务端强制推导 department_scope，带 '/' 边界校验，空范围坚决 Fail-Closed；
3. 对象级防越权拦截 assert_doc_visible（防止依据 doc_id/chunk_id 横向穿透 BOLA）。
"""

import json
import secrets
import time
from pathlib import Path
from typing import Optional, List, Any, Callable, Literal
from pydantic import BaseModel, Field, computed_field
import jwt
from fastapi import Header, HTTPException, status, Depends
from rag_kb.core.config import settings
from rag_kb.core.constants import Permissions
from rag_kb.core.redis_client import get_redis_client
from rag_kb.db.session import get_db

# 全局公钥缓存
_PUBLIC_KEY_CACHE: Optional[str] = None
_JWKS_CLIENT: Optional[jwt.PyJWKClient] = None

REVOKED_USER_KEY_PREFIX = "auth:revoked:"
PERM_CHANGED_USER_KEY_PREFIX = "auth:perm_changed_at:"
STREAM_TICKET_KEY_PREFIX = "auth:stream_ticket:"
SESSION_AUTH_ERROR_DETAILS = {
    "auth_revoked": "账号权限已变更或停用，数据流已断开",
    "auth_expired": "访问凭据已过期，请续期后继续",
}


class UserContext(BaseModel):
    """当前操作用户安全上下文 (Phase-6 契约版本 CV=1)"""
    user_id: str
    tenant_id: str = "default_tenant"
    username: str
    dept_path: str
    data_scope: str  # 仅限 'ALL' | 'DEPT_TREE'
    perms: List[str] = Field(default_factory=list)
    roles: List[str] = Field(default_factory=list)
    is_to_c: bool = False
    cv: int = 1
    token_iat_ms: int = 0
    token_exp: int = 0

    @computed_field
    @property
    def allowed_department_prefixes(self) -> List[str]:
        """根据 data_scope 派生允许访问的部门树前缀"""
        if self.data_scope == "ALL":
            return ["/"]
        if self.data_scope == "DEPT_TREE":
            return [self.dept_path, "/public"]
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"【鉴权失败】非法数据权限范围枚举: {self.data_scope}"
        )

    def has_permission(self, permission: str) -> bool:
        """判定是否具有指定权限点 (支持 '*' 通配符)"""
        return Permissions.ALL.value in self.perms or permission in self.perms


async def revoke_user_access(user_id: str, ttl_seconds: Optional[int] = None) -> None:
    """向真实 Redis 写入秒级停用吊销标记（立即拦截存量 JWT 与活跃 SSE 长连接）"""
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
        await pipe.execute()


async def restore_user_access(user_id: str) -> int:
    """
    解除指定员工的 Redis 吊销拦截标记，并强制推进 auth:perm_changed_at:{user_id} 毫秒级时间戳，
    确保账号恢复后，停用前签发且尚未自然过期的旧 JWT 与流式短票不能死而复生，强制重新换票。
    """
    clean_uid = (user_id or "").strip()
    if not clean_uid:
        return 0
    redis_cli = get_redis_client()
    ttl = max(600, int(settings.AUTH_REVOCATION_TTL_SECONDS))
    now_ms = int(time.time() * 1000)
    async with redis_cli.pipeline(transaction=True) as pipe:
        pipe.delete(f"{REVOKED_USER_KEY_PREFIX}{clean_uid}")
        pipe.set(
            f"{PERM_CHANGED_USER_KEY_PREFIX}{clean_uid}",
            str(now_ms),
            ex=ttl,
        )
        results = await pipe.execute()
    return int(results[0] or 0)


def _is_iat_ms_stale(raw_perm_changed_val: Any, token_iat_ms: Optional[int]) -> bool:
    """严格按 13 位毫秒级时间戳判定凭据是否早于或等于最近一次权限/状态变更边界"""
    if token_iat_ms is None:
        return True
    changed_ms = int(raw_perm_changed_val)
    return int(token_iat_ms) <= changed_ms


async def get_session_auth_error(
    user_id: str,
    token_iat_ms: Optional[int] = None,
    token_exp: Optional[int] = None,
) -> Optional[Literal["auth_revoked", "auth_expired"]]:
    """
    供活跃 SSE 长连接定期核验凭证状态：
    1. 账号是否被停用/吊销 (auth:revoked:{user_id})；
    2. 当前凭据毫秒级签发时间是否早于或等于最近一次权限变更/恢复边界 (auth:perm_changed_at:{user_id})；
    3. 原 JWT 或流式短票本身是否已到期。
    """
    clean_uid = (user_id or "").strip()
    if not clean_uid or token_iat_ms is None or token_iat_ms <= 0:
        return "auth_revoked"
    redis_cli = get_redis_client()
    revoked_val, perm_changed_val = await redis_cli.mget(
        f"{REVOKED_USER_KEY_PREFIX}{clean_uid}",
        f"{PERM_CHANGED_USER_KEY_PREFIX}{clean_uid}",
    )
    if revoked_val is not None:
        return "auth_revoked"
    if perm_changed_val is not None and _is_iat_ms_stale(perm_changed_val, token_iat_ms=token_iat_ms):
        return "auth_revoked"
    if token_exp is not None and int(time.time()) >= int(token_exp):
        return "auth_expired"
    return None


async def assert_token_not_revoked_or_stale(
    user_id: str,
    token_iat_ms: Optional[int] = None,
) -> None:
    """
    单次 Redis MGET 原子校验：
    1. 账号是否被停用/吊销 (auth:revoked:{user_id})；
    2. 令牌毫秒级签发时间是否早于或等于最近一次权限/部门变更时间 (auth:perm_changed_at:{user_id})。
    """
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


async def issue_stream_ticket(user: UserContext, doc_id: str, purpose: str) -> tuple[str, int]:
    """
    签发绑定 user_id + doc_id + purpose 的短期受限只读票据（默认 TTL 60 秒）。
    专供浏览器原生大文件 Range 预览与 EventSource 使用，彻底杜绝通用业务 JWT 暴露于 URL。
    """
    if purpose not in ("preview", "events"):
        raise ValueError(f"不支持的短票用途: {purpose}")
    await assert_token_not_revoked_or_stale(user.user_id, token_iat_ms=user.token_iat_ms)
    ticket = secrets.token_urlsafe(32)
    ttl = max(15, min(300, int(settings.STREAM_TICKET_TTL_SECONDS)))
    redis_cli = get_redis_client()
    # 创建时间仅控制票据寿命，权限快照的版本保留在 user.token_iat_ms 中。
    record = {
        "purpose": purpose,
        "doc_id": str(doc_id),
        "issued_at": int(time.time()),
        "user": user.model_dump(),
    }
    await redis_cli.set(
        f"{STREAM_TICKET_KEY_PREFIX}{ticket}",
        json.dumps(record, ensure_ascii=False),
        ex=ttl,
    )
    return ticket, ttl


async def verify_stream_ticket(ticket: str, expected_doc_id: str, expected_purpose: str) -> UserContext:
    """
    校验单资源受限短票（严格比对 doc_id、purpose、实时吊销状态与最大寿命上限）。
    对持续阅读中的 PDF preview 短票执行受控滑动续期，既保证闲置泄露票据 60s 作废，又保障长时分片阅读不断流。
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
    if data.get("doc_id") != str(expected_doc_id) or data.get("purpose") != expected_purpose:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="【越权访问拦截】该短票仅绑定指定文档与单一用途，严禁跨资源复用",
        )
    user = UserContext(**data["user"])
    if data.get("issued_at") is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="【鉴权失败】流式短票缺少必填时间戳字段",
        )
    issued_at = int(data["issued_at"])
    await assert_token_not_revoked_or_stale(user.user_id, token_iat_ms=user.token_iat_ms)

    now_sec = int(time.time())
    max_lifetime = max(60, int(settings.STREAM_TICKET_MAX_LIFETIME_SECONDS))
    elapsed = max(0, now_sec - issued_at)
    if elapsed >= max_lifetime:
        await redis_cli.delete(redis_key)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="【鉴权失败】单次预览会话已达最大有效时长，请重新打开文档",
        )
    # SSE 持续使用来源 JWT 的授权版本，短票创建时间仅决定会话到期时间。
    user = user.model_copy(
        update={
            "token_exp": issued_at + max_lifetime,
        }
    )

    if expected_purpose == "preview":
        remaining_cap = max_lifetime - elapsed
        sliding_ttl = min(max(60, int(settings.STREAM_TICKET_SLIDING_TTL_SECONDS)), remaining_cap)
        await redis_cli.expire(redis_key, sliding_ttl)

    from rag_kb.core.logging.context import get_trace_context
    trace_ctx = get_trace_context()
    trace_ctx.user_id = user.user_id
    trace_ctx.tenant_id = user.tenant_id
    return user


def is_path_under_prefix(path: str, prefix: str) -> bool:
    """带 '/' 边界的绝对部门前缀从属校验，杜绝 '/技术部' 误匹配 '/技术部2'"""
    if not path or not prefix:
        return False
    if prefix == "/":
        return True
    norm_path = "/" + path.strip("/")
    norm_prefix = "/" + prefix.strip("/")
    return norm_path == norm_prefix or norm_path.startswith(norm_prefix + "/")


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
        detail="【鉴权中心配置致命错误】未找到有效的 JWT RS256 验签公钥！"
    )


def verify_jwt_setup_on_startup() -> None:
    """
    Phase-6 §3.3 铁律 6：启动时强校验 JWKS 可达或本地验签公钥有效；
    若缺失或不可达，直接抛出 RuntimeError 拒绝启动，严禁缓存为空后放行。
    """
    if settings.JWT_JWKS_URL:
        try:
            client = jwt.PyJWKClient(settings.JWT_JWKS_URL)
            jwk_set = client.get_jwk_set()
            if not jwk_set or not getattr(jwk_set, "keys", None):
                raise RuntimeError(f"JWKS 端点返回密钥列表为空: {settings.JWT_JWKS_URL}")
            return
        except Exception as e:
            raise RuntimeError(f"【启动鉴权自检失败】无法连接或解析远程 JWKS 公钥端点 ({settings.JWT_JWKS_URL}): {e} (Fail-Fast)")

    try:
        pub_key = get_jwt_public_key()
        if not pub_key or "BEGIN PUBLIC KEY" not in str(pub_key):
            raise RuntimeError("公钥内容格式非法，缺少 BEGIN PUBLIC KEY 标识")
    except Exception as e:
        raise RuntimeError(f"【启动鉴权自检失败】未配置有效的 JWT RS256 验签公钥: {e} (Fail-Fast)")


async def get_current_user(authorization: Optional[str] = Header(None, alias="Authorization")) -> UserContext:
    """
    服务端严格校验 JWT Bearer RS256 签名并强制推导用户安全上下文
    缺失凭据、验签失败或非法字段坚决触发 401 Unauthorized
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

    # 必填 Claim 强校验 (§3.3 铁律)
    required_claims = ["sub", "username", "tenant_id", "dept_path", "data_scope", "perms", "roles", "is_to_c", "cv", "typ", "iat", "iat_ms", "exp"]
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
    if cv != 1:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"【鉴权失败】不受支持的契约版本 cv={cv}",
            headers={"WWW-Authenticate": "Bearer"},
        )

    is_to_c = bool(payload["is_to_c"])
    if is_to_c:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="【访问拒绝】本系统当前版本仅面向 To B 内部员工，拒绝 To C 访问"
        )

    data_scope = str(payload["data_scope"])
    if data_scope not in ("ALL", "DEPT_TREE"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"【鉴权失败】非白名单数据范围: {data_scope}",
            headers={"WWW-Authenticate": "Bearer"},
        )

    dept_path = str(payload["dept_path"]).strip()
    if not dept_path:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="【鉴权失败】用户未绑定任何合法部门路径 (dept_path is empty)",
            headers={"WWW-Authenticate": "Bearer"},
        )

    roles = payload.get("roles", [])
    if not isinstance(roles, list):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="【鉴权失败】JWT roles 字段类型必须为数组",
            headers={"WWW-Authenticate": "Bearer"},
        )

    perms = payload.get("perms", [])
    if not isinstance(perms, list):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="【鉴权失败】JWT perms 字段类型必须为数组",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user_id_str = str(payload["sub"])
    try:
        int(payload["iat"])
        token_iat_ms = int(payload["iat_ms"])
        token_exp = int(payload["exp"])
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="【鉴权失败】JWT 时间戳声明 (iat/iat_ms/exp) 格式非法",
            headers={"WWW-Authenticate": "Bearer"},
        )
    await assert_token_not_revoked_or_stale(user_id_str, token_iat_ms=token_iat_ms)

    # Phase-7 §1.2: 将验签通过的真实员工与租户身份回写至全链路追踪上下文，确保审计日志严格穿透
    from rag_kb.core.logging.context import get_trace_context
    trace_ctx = get_trace_context()
    trace_ctx.user_id = user_id_str
    trace_ctx.tenant_id = str(payload["tenant_id"])

    return UserContext(
        user_id=user_id_str,
        tenant_id=str(payload["tenant_id"]),
        username=str(payload["username"]),
        dept_path=dept_path,
        data_scope=data_scope,
        perms=perms,
        roles=roles,
        is_to_c=is_to_c,
        cv=cv,
        token_iat_ms=token_iat_ms,
        token_exp=token_exp,
    )


def require_permission(perm: str) -> Callable:
    """权限点拦截依赖生成器 (支持通配符 '*' 与角色权限并集)"""
    perm_val = perm.value if isinstance(perm, Permissions) else str(perm)
    async def _perm_dependency(user: UserContext = Depends(get_current_user)) -> UserContext:
        if not user.has_permission(perm_val):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"【权限不足】当前操作需要 [{perm_val}] 权限点，您的角色缺乏此权限"
            )
        return user
    return _perm_dependency


def assert_doc_visible(user: UserContext, doc: Any) -> None:
    """
    Phase-6 §5.4 对象级越权判定 (BOLA 防御)
    校验目标文档的 department_path 是否严格包含于用户的合法可见部门前缀集合中
    """
    if doc is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="【资源不存在】未找到指定的文档或切片资源"
        )

    doc_dept_path = getattr(doc, "department_path", None)
    if not doc_dept_path or not str(doc_dept_path).strip():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="【安全拦截】文档所属部门信息缺失或异常，无法访问"
        )

    allowed_prefixes = user.allowed_department_prefixes
    matched = any(is_path_under_prefix(str(doc_dept_path), prefix) for prefix in allowed_prefixes)
    if not matched:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="【越权访问拦截】您当前无权访问或操作其他部门的文档资料"
        )


def derive_secure_department_scope(
    user: UserContext,
    requested_scope: Optional[List[str]] = None
) -> List[str]:
    """
    Phase-6 §5.3 集合级越权判定（检索域）
    1. 前端未传 scope：返回服务端从 Token 派生的合法前缀；
    2. 前端传了 scope：逐项做带 '/' 边界的前缀匹配，一旦越权坚决 403；
    3. data_scope=ALL 允许任意收窄或扩展；
    4. 派生结果为空时强制报 403 拒绝 (Fail-Closed)，坚决杜绝退化为全表扫描。
    """
    allowed_prefixes = user.allowed_department_prefixes

    if requested_scope is None or len(requested_scope) == 0:
        if not allowed_prefixes:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="【越权拦截】当前账号未配置可用部门权限范围，无法检索"
            )
        return allowed_prefixes

    if user.data_scope == "ALL":
        clean_req = [p.strip() for p in requested_scope if p and p.strip()]
        if not clean_req:
            return ["/"]
        return clean_req

    clean_requested = [p.strip() for p in requested_scope if p and p.strip()]
    if not clean_requested:
        if not allowed_prefixes:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="【越权拦截】当前账号未配置可用部门权限范围，无法检索"
            )
        return allowed_prefixes

    for clean_path in clean_requested:
        matched = any(is_path_under_prefix(clean_path, prefix) for prefix in allowed_prefixes)
        if not matched:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"【越权访问拦截】您当前无权查阅该部门资料: {clean_path}"
            )

    return clean_requested
