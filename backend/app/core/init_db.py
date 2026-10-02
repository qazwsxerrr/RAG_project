import uuid
import asyncio
from sqlalchemy import text, select
from backend.app.core.database import engine, AsyncSessionLocal, Base
from backend.app.models import Department, Document, DocumentReviewItem, DocumentChunk

INITIAL_DEPARTMENTS = [
    {"name": "技术部", "path": "/总公司/研发中心/技术部"},
    {"name": "产品部", "path": "/总公司/研发中心/产品部"},
    {"name": "运营部", "path": "/总公司/运营中心/运营部"},
    {"name": "人事行政部", "path": "/总公司/职能中心/人事行政部"},
    {"name": "财务部", "path": "/总公司/职能中心/财务部"},
]

async def init_database():
    """初始化 PostgreSQL 16 物理表、pgvector 向量扩展与混合索引"""
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
            print(" [DB] 已成功初始化默认企业组织架构部门树")

if __name__ == "__main__":
    asyncio.run(init_database())
