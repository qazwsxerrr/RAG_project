"""src/rag_kb/api/main.py —— FastAPI 应用入口，负责应用装配与生命周期。

功能：
1. 配置 CORS 跨域与静态切片目录挂载；
2. 注册 /api/v1 下全部业务路由 (documents, departments, chunks, retrieval, sessions)；
3. lifespan 异步生命周期管理（启动时幂等校验数据库与初始化表结构）；
4. 提供 /health 与 /api/v1/health 双端健康检查。
"""

from pathlib import Path
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from rag_kb.core.config import settings
from rag_kb.db.session import AsyncSessionLocal, init_db
from rag_kb.api.routes import router as documents_router, router_chunks as chunks_router
from rag_kb.api.departments import router as departments_router
from rag_kb.api.retrieval import router as retrieval_router
from rag_kb.api.sessions import router as sessions_router

@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理器"""
    # 启动时保证数据库表与 pgvector 扩展已就绪
    try:
        await init_db()
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"启动时 init_db 提示: {e}")
    yield

def create_app() -> FastAPI:
    """FastAPI 工厂方法"""
    app = FastAPI(
        title="企业级多模态 RAG 知识库系统",
        version="2.0.0",
        description="基于 LangGraph 1.2+ 的企业级多模态 RAG 架构后端 API 服务",
        docs_url="/docs",
        redoc_url="/redoc",
        lifespan=lifespan
    )

    # 1. 跨域 CORS 中间件
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 2. 静态中间产物切图挂载
    parsed_dir = Path(settings.DATA_PARSED_DIR)
    parsed_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/static/parsed", StaticFiles(directory=str(parsed_dir)), name="parsed")

    # 3. 注册 API 业务路由
    prefix = settings.API_V1_PREFIX
    app.include_router(documents_router, prefix=prefix)
    app.include_router(chunks_router, prefix=prefix)
    app.include_router(departments_router, prefix=prefix)
    app.include_router(retrieval_router, prefix=prefix)
    app.include_router(sessions_router, prefix=prefix)

    # 4. 健康检查与根路由
    @app.get("/health", tags=["监控"])
    @app.get(f"{prefix}/health", tags=["监控"])
    async def health_check():
        db_status = "offline"
        try:
            async with AsyncSessionLocal() as session:
                await session.execute(text("SELECT 1"))
                db_status = "online"
        except Exception as e:
            db_status = f"error: {str(e)}"

        oss_configured = bool(settings.OSS_ACCESS_KEY_ID and settings.OSS_ENDPOINT and settings.OSS_BUCKET_NAME)

        return {
            "status": "ok" if db_status == "online" else "degraded",
            "app_name": "rag_kb_enterprise",
            "version": "2.0.0",
            "engine": "LangGraph 1.2+",
            "db_status": db_status,
            "oss_status": "online" if oss_configured else "unconfigured",
            "model_name": settings.active_model_name,
            "embedding_model": settings.EMBEDDING_MODEL
        }

    @app.get("/", tags=["监控"])
    async def root():
        return {
            "message": "Welcome to Enterprise Multimodal RAG API (LangGraph Edition)",
            "docs_url": "/docs",
            "version": "2.0.0"
        }

    return app

app = create_app()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("rag_kb.api.main:app", host="0.0.0.0", port=8000, reload=True)
