"""src/rag_kb/db/session.py —— 数据库连接与会话管理。

模块导入时按配置创建 engine 与 SessionLocal，对外提供事务边界上下文管理器 session_scope()
（自动 commit / rollback / close）和幂等的建表入口 init_db()（建 vector 扩展 + create_all + HNSW/GIN 索引 + 部门树播种）。
严格连接真实 PostgreSQL 16 + pgvector，绝不降级。
"""

import uuid
from typing import AsyncGenerator
from contextlib import asynccontextmanager
from sqlalchemy import text, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from rag_kb.core.config import settings
from rag_kb.db.base import Base
from rag_kb.db.models import Department

# 真实连接 PostgreSQL 16 数据库
engine = create_async_engine(
    settings.async_database_url,
    echo=False,
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
    {"name": "技术部", "path": "/总公司/研发中心/技术部"},
    {"name": "产品部", "path": "/总公司/研发中心/产品部"},
    {"name": "运营部", "path": "/总公司/运营中心/运营部"},
    {"name": "人事行政部", "path": "/总公司/职能中心/人事行政部"},
    {"name": "财务部", "path": "/总公司/职能中心/财务部"},
]

async def init_db():
    """初始化数据库扩展、数据表结构、HNSW/GIN 混合索引及默认企业部门树"""
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector;"))
        await conn.run_sync(Base.metadata.create_all)

        # 创建 HNSW 向量索引与 GIN 全文检索索引
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

    # 播种初始组织架构部门数据
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Department))
        existing_depts = result.scalars().all()
        if not existing_depts:
            for d_info in INITIAL_DEPARTMENTS:
                dept = Department(
                    id=uuid.uuid4(),
                    name=d_info["name"],
                    path=d_info["path"]
                )
                session.add(dept)
            await session.commit()
