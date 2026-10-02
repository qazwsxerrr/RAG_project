"""src/rag_kb/api/deps.py —— 跨路由共用的 FastAPI 依赖。

提供：
- get_tenant_id: 从 Header 或配置获取当前租户
- get_db: 数据库异步会话生成器
"""

from typing import Optional
from fastapi import Header
from rag_kb.core.config import settings
from rag_kb.db.session import get_db

async def get_tenant_id(x_tenant_id: Optional[str] = Header(None, alias="X-Tenant-Id")) -> str:
    """提取租户 ID 依赖"""
    if x_tenant_id and x_tenant_id.strip():
        return x_tenant_id.strip()
    return settings.DEFAULT_TENANT_ID
