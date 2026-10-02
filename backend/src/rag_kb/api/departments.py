"""src/rag_kb/api/departments.py —— 部门树与部门列表接口。

支持前端下拉选择、部门树展示与动态打字匹配新增。
"""

from typing import List
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from rag_kb.api.schemas import DepartmentItemSchema
from rag_kb.db.models import Department
from rag_kb.db.session import get_db

router = APIRouter(prefix="/documents/departments", tags=["部门管理"])

# 初始常规模板部门
DEFAULT_DEPARTMENTS = [
    {"id": 1, "name": "技术部", "path": "/总公司/研发中心/技术部"},
    {"id": 2, "name": "产品部", "path": "/总公司/研发中心/产品部"},
    {"id": 3, "name": "运营部", "path": "/总公司/运营中心/运营部"},
    {"id": 4, "name": "人事行政部", "path": "/总公司/职能中心/人事行政部"},
    {"id": 5, "name": "财务部", "path": "/总公司/职能中心/财务部"},
]

@router.get("", response_model=List[DepartmentItemSchema])
async def list_departments(db: AsyncSession = Depends(get_db)):
    """获取所有可用部门列表（合并模板部门与数据库真实部门）"""
    dept_map = {d["name"]: d for d in DEFAULT_DEPARTMENTS}

    try:
        stmt = select(Department).order_by(Department.created_at.asc())
        res = await db.execute(stmt)
        db_depts = res.scalars().all()
        for idx, d in enumerate(db_depts, start=len(DEFAULT_DEPARTMENTS) + 1):
            if d.name not in dept_map:
                dept_map[d.name] = {
                    "id": idx,
                    "name": d.name,
                    "path": d.path or f"/总公司/动态部门/{d.name}"
                }
    except Exception:
        pass

    return list(dept_map.values())
