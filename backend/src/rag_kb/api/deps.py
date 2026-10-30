"""src/rag_kb/api/deps.py —— 跨路由共用的 FastAPI 依赖与企业级 RBAC 鉴权防越权中心。

提供：
- UserContext: 统一用户安全上下文
- create_access_token: 签发标准 JWT 访问凭据 (供调试与 Java Web 规范对齐)
- get_current_user: 严格鉴权依赖注入（强制校验 Bearer JWT，401 拦截）
- get_current_user_optional: 可选鉴权依赖注入（平滑兼容未带凭据的公共场景）
- derive_secure_department_scope: 服务端强制数据范围推导与 BOLA / IDOR 越权拦截 (403 Forbidden)
- get_db: 数据库异步会话依赖
- get_tenant_id: 提取租户 ID 依赖
"""

import time
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field
import jwt
from fastapi import Header, HTTPException, status, Depends
from rag_kb.core.config import settings
from rag_kb.db.session import get_db

DEFAULT_DEPT_PATHS: Dict[str, str] = {
    "技术部": "/总公司/研发中心/技术部",
    "产品部": "/总公司/研发中心/产品部",
    "运营部": "/总公司/运营中心/运营部",
    "人事行政部": "/总公司/职能中心/人事行政部",
    "财务部": "/总公司/职能中心/财务部",
    "总经办": "/总公司/总经办",
}

class UserContext(BaseModel):
    """当前操作用户安全上下文"""
    user_id: str
    tenant_id: str = "default_tenant"
    department_name: str = "公共部门"
    department_path: str = "/public"
    allowed_department_prefixes: List[str] = Field(default_factory=lambda: ["/public"])
    roles: List[str] = Field(default_factory=list)
    is_admin: bool = False
    is_to_c: bool = False

    def __init__(self, **data: Any):
        super().__init__(**data)
        if "admin" in self.roles or "super_admin" in self.roles or self.is_admin:
            self.is_admin = True
            if "/" not in self.allowed_department_prefixes:
                self.allowed_department_prefixes = ["/"] + [p for p in self.allowed_department_prefixes if p != "/"]

def create_access_token(
    user_id: str,
    tenant_id: str = "default_tenant",
    department_name: str = "技术部",
    roles: Optional[List[str]] = None,
    department_path: Optional[str] = None,
    is_to_c: bool = False,
    expires_minutes: Optional[int] = None
) -> str:
    """
    签发标准无状态 JWT Token (开发期与 Java Web 规范严格对齐)
    """
    effective_roles = roles or ["employee"]
    eff_dept_path = department_path or DEFAULT_DEPT_PATHS.get(department_name, f"/总公司/{department_name}")
    exp_minutes = expires_minutes or getattr(settings, "JWT_ACCESS_TOKEN_EXPIRE_MINUTES", 1440)
    now = int(time.time())

    payload = {
        "sub": user_id,
        "tenant_id": tenant_id,
        "department_name": department_name,
        "department_path": eff_dept_path,
        "roles": effective_roles,
        "is_to_c": is_to_c,
        "iat": now,
        "exp": now + exp_minutes * 60
    }
    secret = getattr(settings, "JWT_SECRET_KEY", settings.SECRET_KEY)
    algorithm = getattr(settings, "JWT_ALGORITHM", "HS256")
    return jwt.encode(payload, secret, algorithm=algorithm)

async def get_current_user(authorization: Optional[str] = Header(None, alias="Authorization")) -> UserContext:
    """
    服务端严格解析 JWT 并强制推导用户权限上下文
    未携带凭据或凭据失效时坚决触发 401 Unauthorized
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
    secret = getattr(settings, "JWT_SECRET_KEY", settings.SECRET_KEY)
    algorithm = getattr(settings, "JWT_ALGORITHM", "HS256")

    try:
        payload = jwt.decode(token, secret, algorithms=[algorithm])
    except jwt.PyJWTError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"【鉴权失败】JWT 令牌无效或已过期: {str(e)}",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user_id = str(payload.get("sub") or "anonymous")
    tenant_id = str(payload.get("tenant_id", settings.DEFAULT_TENANT_ID))
    department_name = str(payload.get("department_name", "公共部门"))
    department_path = str(payload.get("department_path") or DEFAULT_DEPT_PATHS.get(department_name, "/public"))
    roles = payload.get("roles", [])
    if isinstance(roles, str):
        roles = [roles]
    is_to_c = bool(payload.get("is_to_c", False))
    is_admin = ("admin" in roles or "super_admin" in roles)

    if is_admin:
        allowed_prefixes = ["/"]
    else:
        allowed_prefixes = [department_path, "/public"]

    return UserContext(
        user_id=user_id,
        tenant_id=tenant_id,
        department_name=department_name,
        department_path=department_path,
        allowed_department_prefixes=allowed_prefixes,
        roles=roles,
        is_admin=is_admin,
        is_to_c=is_to_c
    )

async def get_current_user_optional(authorization: Optional[str] = Header(None, alias="Authorization")) -> Optional[UserContext]:
    """
    可选的当前用户注入：
    - 若未传 Authorization Header，返回 None；
    - 若传了非法 Authorization，严格触发 401 Unauthorized。
    """
    if not authorization or not authorization.strip():
        return None
    return await get_current_user(authorization=authorization)

def derive_secure_department_scope(
    user: UserContext,
    requested_scope: Optional[List[str]] = None
) -> List[str]:
    """
    防 BOLA / IDOR 越权拦截核心逻辑：
    1. 前端未指定 scope：服务端强制派生为用户的可信前缀 (allowed_department_prefixes)；
    2. 前端显式指定了 scope：
       - 若为管理员，允许跨部门全范围穿透；
       - 若为普通用户，必须是其所属部门树的子前缀，否则坚决抛出 403 Forbidden 拦截。
    """
    if requested_scope is None:
        return user.allowed_department_prefixes

    if user.is_admin:
        return requested_scope

    # 严格校验 requested_scope 中每一个路径是否在合法前缀树下
    for req_path in requested_scope:
        norm_path = req_path.rstrip("/")
        match = any(
            norm_path == prefix.rstrip("/") or norm_path.startswith(prefix.rstrip("/") + "/")
            for prefix in user.allowed_department_prefixes
        )
        if not match:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"【越权访问拦截】您无权访问或检索非授权部门目录: {req_path} (BOLA/IDOR Blocked)"
            )

    return requested_scope

async def get_tenant_id(x_tenant_id: Optional[str] = Header(None, alias="X-Tenant-Id")) -> str:
    """提取租户 ID 依赖"""
    if x_tenant_id and x_tenant_id.strip():
        return x_tenant_id.strip()
    return settings.DEFAULT_TENANT_ID
