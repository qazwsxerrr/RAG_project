"""src/rag_kb/api/auth.py —— 开发期与调试专用轻量凭据签发与身份核验端点。

与 Java Web 鉴权中心签发规范 100% 对齐。
提供：
- POST /api/v1/auth/dev-token: 一键生成携带合法签名的 JWT 访问凭据 (测试用)
- GET /api/v1/auth/me: 获取当前登录用户的身份上下文与数据权限范围
"""

from typing import List, Optional
from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field
from rag_kb.api.deps import (
    UserContext,
    create_access_token,
    get_current_user,
    DEFAULT_DEPT_PATHS
)

router = APIRouter(prefix="/auth", tags=["统一鉴权与身份认证"])

class DevTokenRequest(BaseModel):
    user_id: str = Field(default="dev_user_01", description="用户工号或唯一ID")
    tenant_id: str = Field(default="default_tenant", description="租户ID")
    department_name: str = Field(default="技术部", description="所属部门名称")
    department_path: Optional[str] = Field(default=None, description="可选的部门树路径")
    roles: List[str] = Field(default_factory=lambda: ["developer"], description="角色列表，如 developer / accountant / admin")
    is_to_c: bool = Field(default=False, description="是否为 C 端公共外部访问")
    expires_minutes: int = Field(default=1440, description="有效时长(分钟)")

class DevTokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in_seconds: int
    user: UserContext

@router.post("/dev-token", response_model=DevTokenResponse, status_code=status.HTTP_200_OK)
async def generate_dev_token(payload: DevTokenRequest):
    """
    开发与调试专用：一键生成携带合法签名的 JWT 访问凭据
    与未来 Java Web 鉴权中心颁发的 Token 契约保持完全一致。
    """
    token = create_access_token(
        user_id=payload.user_id,
        tenant_id=payload.tenant_id,
        department_name=payload.department_name,
        department_path=payload.department_path,
        roles=payload.roles,
        is_to_c=payload.is_to_c,
        expires_minutes=payload.expires_minutes
    )
    user_ctx = await get_current_user(authorization=f"Bearer {token}")
    return DevTokenResponse(
        access_token=token,
        token_type="bearer",
        expires_in_seconds=payload.expires_minutes * 60,
        user=user_ctx
    )

@router.get("/me", response_model=UserContext)
async def get_my_profile(user: UserContext = Depends(get_current_user)):
    """获取当前登录用户的身份上下文与数据权限范围"""
    return user
