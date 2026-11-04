"""src/rag_kb/api/departments.py —— 企业组织架构部门接口。

Phase-6 严格规范：
- 彻底物理删除内置硬编码 DEFAULT_DEPARTMENTS 模板部门与 except: pass 吞异常逻辑；
- 仅以 PostgreSQL sys_departments 作为单一真实来源，Fail-Fast 抛出真实异常；
- 挂载 kb:retrieve 鉴权依赖。
"""

from typing import List
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from rag_kb.api.schemas import DepartmentItemSchema
from rag_kb.api.deps import UserContext, require_permission
from rag_kb.db.models import Department
from rag_kb.db.session import get_db

router = APIRouter(prefix="/documents/departments", tags=["部门管理"])

@router.get("", response_model=List[DepartmentItemSchema])
async def list_departments(
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(require_permission("kb:retrieve"))
):
    """获取所有可用企业真实部门列表（只读数据库单一真实源，严禁伪造兜底）"""
    try:
        stmt = select(Department).order_by(Department.created_at.asc())
        res = await db.execute(stmt)
        db_depts = res.scalars().all()
        return [
            DepartmentItemSchema(
                id=idx,
                name=d.name,
                path=d.path
            )
            for idx, d in enumerate(db_depts, start=1)
        ]
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"【数据库异常】读取企业真实部门列表失败: {str(e)} (Fail-Fast)"
        )
