"""src/rag_kb/api/deps.py —— 跨路由共用的 FastAPI 依赖与企业级 RBAC 鉴权防越权中心。

严格对齐 Phase-6 跨服务 JWT 契约与三条不变量：
1. 强制无状态 Bearer RS256 签名校验（对接 Java 鉴权中心，严禁 HS256 与本地假签）；
2. 服务端强制推导 department_scope，带 '/' 边界校验，空范围坚决 Fail-Closed；
3. 对象级防越权拦截 assert_doc_visible（防止依据 doc_id/chunk_id 横向穿透 BOLA）。
"""

from pathlib import Path
from typing import Optional, List, Any, Callable
from pydantic import BaseModel, Field, computed_field
import jwt
from fastapi import Header, Query, HTTPException, status, Depends
from rag_kb.core.config import settings
from rag_kb.db.session import get_db

# 全局公钥缓存
_PUBLIC_KEY_CACHE: Optional[str] = None
_JWKS_CLIENT: Optional[jwt.PyJWKClient] = None

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
        return "*" in self.perms or permission in self.perms


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
            issuer=settings.JWT_ISSUER
        )
    except jwt.PyJWTError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"【鉴权失败】JWT 令牌验签失败或已过期: {str(e)}",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 必填 Claim 强校验 (§3.3 铁律)
    required_claims = ["sub", "username", "tenant_id", "dept_path", "data_scope", "perms", "roles", "is_to_c", "cv", "typ"]
    for claim in required_claims:
        if claim not in payload:
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
    if isinstance(roles, str):
        roles = [roles]

    perms = payload.get("perms", [])
    if isinstance(perms, str):
        perms = [perms]

    return UserContext(
        user_id=str(payload["sub"]),
        tenant_id=str(payload["tenant_id"]),
        username=str(payload["username"]),
        dept_path=dept_path,
        data_scope=data_scope,
        perms=perms,
        roles=roles,
        is_to_c=is_to_c,
        cv=cv
    )


def require_permission(perm: str) -> Callable:
    """权限点拦截依赖生成器 (支持通配符 '*' 与角色权限并集)"""
    async def _perm_dependency(user: UserContext = Depends(get_current_user)) -> UserContext:
        if not user.has_permission(perm):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"【权限不足】当前操作需要 [{perm}] 权限点，您的角色缺乏此权限"
            )
        return user
    return _perm_dependency


async def get_current_user_with_query_token(
    authorization: Optional[str] = Header(None, alias="Authorization"),
    token: Optional[str] = Query(None, description="仅供浏览器原生媒体/流式标签受控透传短期 JWT")
) -> UserContext:
    """
    受控支持 Header Bearer 或 Query ?token= 的验签依赖 (仅用于只读媒体流端点 /preview, /pages, /asset, /events)
    """
    eff_auth = authorization if (authorization and authorization.strip()) else (f"Bearer {token.strip()}" if token and token.strip() else None)
    return await get_current_user(authorization=eff_auth)


def require_permission_with_query_token(perm: str) -> Callable:
    """支持受控 Query Token 的权限点拦截依赖生成器"""
    async def _perm_query_dependency(user: UserContext = Depends(get_current_user_with_query_token)) -> UserContext:
        if not user.has_permission(perm):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"【权限不足】当前操作需要 [{perm}] 权限点，您的角色缺乏此权限"
            )
        return user
    return _perm_query_dependency


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
            detail="【安全拦截】文档所属部门路径缺失或异常，拒绝判定放行 (Fail-Closed)"
        )

    allowed_prefixes = user.allowed_department_prefixes
    matched = any(is_path_under_prefix(str(doc_dept_path), prefix) for prefix in allowed_prefixes)
    if not matched:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"【越权访问拦截】您无权访问或操作属于其他部门的文档资源 (BOLA Blocked)"
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
                detail="【越权拦截】用户可用部门前缀为空，拒绝检索 (Fail-Closed)"
            )
        return allowed_prefixes

    if user.data_scope == "ALL":
        clean_req = [p.strip() for p in requested_scope if p and p.strip()]
        if not clean_req:
            return ["/"]
        return clean_req

    for req_path in requested_scope:
        if not req_path or not req_path.strip():
            continue
        clean_path = req_path.strip()
        matched = any(is_path_under_prefix(clean_path, prefix) for prefix in allowed_prefixes)
        if not matched:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"【越权访问拦截】您无权访问或检索非授权部门目录: {clean_path} (BOLA Blocked)"
            )

    return requested_scope
