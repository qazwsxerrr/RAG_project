"""src/rag_kb/db/session.py —— 数据库连接、会话管理与多租户部门树/默认知识库幂等播种。

严格连接真实 PostgreSQL 16 + pgvector，绝不降级。
"""

import uuid
from typing import AsyncGenerator
from contextlib import asynccontextmanager
from sqlalchemy import text, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from rag_kb.core.config import settings
from rag_kb.db.base import Base
from rag_kb.db.models import Department, KnowledgeBase, Position, TenantPolicyEpoch

# 真实连接 PostgreSQL 16 数据库（高并发连接池扩容：适配 4 路并行召回与异步入库消费）
engine = create_async_engine(
    settings.async_database_url,
    echo=False,
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
    pool_timeout=settings.DB_POOL_TIMEOUT,
    pool_pre_ping=True,
    pool_recycle=300,
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI 依赖注入：获取数据库会话"""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()


@asynccontextmanager
async def session_scope() -> AsyncGenerator[AsyncSession, None]:
    """提供带有自动 commit/rollback 边界的异步上下文管理器"""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


INITIAL_DEPARTMENTS = [
    {"name": "公共知识库", "path": "/public", "parent_path": None, "sort_order": 0},
    {"name": "总公司", "path": "/总公司", "parent_path": None, "sort_order": 10},
    {"name": "研发中心", "path": "/总公司/研发中心", "parent_path": "/总公司", "sort_order": 20},
    {"name": "技术部", "path": "/总公司/研发中心/技术部", "parent_path": "/总公司/研发中心", "sort_order": 21},
    {"name": "产品部", "path": "/总公司/研发中心/产品部", "parent_path": "/总公司/研发中心", "sort_order": 22},
    {"name": "运营中心", "path": "/总公司/运营中心", "parent_path": "/总公司", "sort_order": 30},
    {"name": "运营部", "path": "/总公司/运营中心/运营部", "parent_path": "/总公司/运营中心", "sort_order": 31},
    {"name": "职能中心", "path": "/总公司/职能中心", "parent_path": "/总公司", "sort_order": 40},
    {"name": "人事行政部", "path": "/总公司/职能中心/人事行政部", "parent_path": "/总公司/职能中心", "sort_order": 41},
    {"name": "财务部", "path": "/总公司/职能中心/财务部", "parent_path": "/总公司/职能中心", "sort_order": 42},
    {"name": "总裁办", "path": "/总公司/总裁办", "parent_path": "/总公司", "sort_order": 50},
]

INITIAL_POSITIONS = [
    ("INTERN_EXT", "实习 / 外部协作", 10, 1, 1, 1, "可看 L1 全员公开制度"),
    ("STAFF", "普通员工", 20, 2, 1, 2, "可看 L1 + 所属多部门及获授知识库内 L1-L2 文档"),
    ("SUPERVISOR", "部门主管", 30, 2, 2, 3, "默认 L2，可在后台动态调整"),
    ("MANAGER", "部门经理", 40, 3, 3, 3, "可看有权部门/知识库内 L1-L3 文档"),
    ("DIRECTOR", "总监", 50, 3, 3, 3, "覆盖中心及下属多部门 L1-L3 文档"),
    ("EXECUTIVE", "公司高管", 60, 4, 4, 4, "满足 L1-L4 密级门槛，仍遵守知识库范围与受限库 ACL"),
]


async def seed_tenant_defaults(session: AsyncSession, tenant_id: str) -> None:
    """为指定租户幂等播种策略版本、6 档标准职级、部门树及默认知识库"""
    epoch_row = await session.get(TenantPolicyEpoch, tenant_id)
    if not epoch_row:
        session.add(TenantPolicyEpoch(tenant_id=tenant_id, policy_epoch=1))

    for code, name, rank_order, default_level, min_level, max_level, desc in INITIAL_POSITIONS:
        pos = await session.get(Position, (tenant_id, code))
        if not pos:
            session.add(
                Position(
                    tenant_id=tenant_id,
                    position_code=code,
                    position_name=name,
                    rank_order=rank_order,
                    default_clearance_level=default_level,
                    min_allowed_clearance=min_level,
                    max_allowed_clearance=max_level,
                    description=desc,
                )
            )

    res = await session.execute(select(Department).where(Department.tenant_id == tenant_id))
    dept_by_path = {d.path: d for d in res.scalars().all()}

    for d_info in INITIAL_DEPARTMENTS:
        path = d_info["path"]
        if path in dept_by_path:
            continue
        parent = dept_by_path.get(d_info["parent_path"]) if d_info["parent_path"] else None
        dept = Department(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            parent_id=parent.id if parent else None,
            name=d_info["name"],
            path=path,
            sort_order=d_info["sort_order"],
            status="ACTIVE",
        )
        session.add(dept)
        dept_by_path[path] = dept

    await session.flush()

    kb_res = await session.execute(select(KnowledgeBase).where(KnowledgeBase.tenant_id == tenant_id))
    existing_kbs = kb_res.scalars().all()
    existing_kb_paths = {(kb.owner_dept_path, kb.visibility) for kb in existing_kbs}
    existing_kb_names = {kb.name for kb in existing_kbs}

    pub_dept = dept_by_path.get("/public")
    if pub_dept and ("/public", "PUBLIC") not in existing_kb_paths and "公司全员公共知识库" not in existing_kb_names:
        session.add(
            KnowledgeBase(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                name="公司全员公共知识库",
                description="全员公开 L1 知识库（员工手册、公开制度与公告）",
                owner_department_id=pub_dept.id,
                owner_dept_path="/public",
                visibility="PUBLIC",
                min_classification_level=1,
                default_doc_level=1,
                max_doc_level=1,
                status="ACTIVE",
                created_by="system_seed",
            )
        )
        existing_kb_names.add("公司全员公共知识库")

    for path, dept in dept_by_path.items():
        if path in ("/public", "/"):
            continue
        kb_name = f"{dept.name}知识库"
        if (path, "DEPARTMENT") in existing_kb_paths or kb_name in existing_kb_names:
            continue
        session.add(
            KnowledgeBase(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                name=kb_name,
                description=f"部门默认知识库 ({path})",
                owner_department_id=dept.id,
                owner_dept_path=path,
                visibility="DEPARTMENT",
                min_classification_level=2,
                default_doc_level=2,
                max_doc_level=4,
                status="ACTIVE",
                created_by="system_seed",
            )
        )
        existing_kb_names.add(kb_name)


async def init_db():
    """初始化数据库扩展、数据表结构、HNSW/GIN 混合索引及默认租户多维权限基座"""
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector;"))
        await conn.run_sync(Base.metadata.create_all)

        await conn.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_docs_title_embedding
            ON rag_documents USING hnsw (title_embedding vector_cosine_ops);
        """))
        await conn.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_chunks_dense
            ON rag_document_chunks USING hnsw (dense_embedding vector_cosine_ops)
            WITH (m = 16, ef_construction = 64);
        """))
        await conn.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_chunks_gin_tsv
            ON rag_document_chunks USING gin(tsv_content);
        """))

    async with AsyncSessionLocal() as session:
        await seed_tenant_defaults(session, settings.DEFAULT_TENANT_ID)
        await session.commit()
