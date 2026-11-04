"""src/rag_kb/api/auth.py —— 用户身份信息回显端点。

Phase-6 严格规范：
- 物理删除免认证签发端点 /dev-token 与相关 DTO，拒绝任何本地伪造身份入口；
- 仅提供 GET /api/v1/auth/me 回显通过验签的当前用户上下文信息。
"""

from fastapi import APIRouter, Depends
from rag_kb.api.deps import UserContext, get_current_user

router = APIRouter(prefix="/auth", tags=["统一鉴权与身份认证"])

@router.get("/me", response_model=UserContext)
async def get_my_profile(user: UserContext = Depends(get_current_user)):
    """获取当前登录用户的身份上下文与数据权限范围 (基于 Java 签发的真实 JWT)"""
    return user
