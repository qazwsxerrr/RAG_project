"""src/rag_kb/api/auth.py —— 当前登录员工实时权限上下文回显端点。

仅暴露 GET /api/v1/auth/me 回显实时核验后的 cv=2 EffectiveAuthzContext；
账号应急停用与恢复已统一收口至 /api/v1/admin/users/{user_id}/revoke。
"""

from fastapi import APIRouter, Depends

from rag_kb.api.deps import (
    EffectiveAuthzContext,
    get_current_user_with_db,
)

router = APIRouter(prefix="/auth", tags=["统一鉴权与身份认证"])


@router.get("/me", response_model=EffectiveAuthzContext)
async def get_my_profile(user: EffectiveAuthzContext = Depends(get_current_user_with_db)):
    """获取当前登录用户的实时多维权限上下文 (cv=2)"""
    return user
