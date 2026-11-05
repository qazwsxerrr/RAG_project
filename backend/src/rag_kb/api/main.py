"""src/rag_kb/api/main.py —— FastAPI 应用入口，负责应用装配与生命周期。

功能：
1. 配置 CORS 跨域与静态切片目录挂载；
2. 注册 /api/v1 下全部业务路由 (documents, departments, chunks, retrieval, sessions)；
3. lifespan 异步生命周期管理（启动时幂等校验数据库与初始化表结构）；
4. 提供 /health 与 /api/v1/health 双端健康检查。
"""

from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from rag_kb.core.config import settings
from rag_kb.db.session import AsyncSessionLocal, init_db
from rag_kb.api.routes import router as documents_router, router_chunks as chunks_router
from rag_kb.api.departments import router as departments_router
from rag_kb.api.retrieval import router as retrieval_router
from rag_kb.api.sessions import router as sessions_router
from rag_kb.api.auth import router as auth_router
from rag_kb.api.system import router as system_router

@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理器"""
    # 1. 初始化全局企业级链路日志体系并绑定主事件循环 (装配 TraceLogFilter 与跨线程安全审计)
    from rag_kb.core.logging import setup_logging, bind_main_event_loop
    setup_logging()
    bind_main_event_loop()

    # 2. Phase-6 §3.3 铁律 6: 启动时强校验 JWT RS256 验签公钥或 JWKS 可达性，缺失立即拒绝启动
    from rag_kb.api.deps import verify_jwt_setup_on_startup
    verify_jwt_setup_on_startup()

    # 3. 启动时保证数据库表与 pgvector 扩展已就绪 (Phase-6: 严格 Fail-Fast，异常坚决抛出中断)
    await init_db()

    # 4. Phase-7 §4.3: 启动时执行一次大模型熔断状态机冷启动历史对齐 (hydrate_once)
    from rag_kb.api.system import hydrate_llm_health_tracker_from_db
    await hydrate_llm_health_tracker_from_db()

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

    # 1. 全链路追踪与统一异常掩码中间件 (首层拦截)
    from rag_kb.core.logging import TraceLoggingMiddleware
    app.add_middleware(TraceLoggingMiddleware)

    # 2. 跨域 CORS 中间件
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Phase-6 §1.5 & §6.2: 物理移除 /static/parsed 静态免鉴权目录直挂，杜绝绕过 API 鉴权泄露切图

    # 3. 注册 API 业务路由 (优先展示鉴权与检索核心路由)
    prefix = settings.API_V1_PREFIX
    app.include_router(auth_router, prefix=prefix)
    app.include_router(retrieval_router, prefix=prefix)
    app.include_router(departments_router, prefix=prefix)
    app.include_router(documents_router, prefix=prefix)
    app.include_router(chunks_router, prefix=prefix)
    app.include_router(sessions_router, prefix=prefix)
    app.include_router(system_router, prefix=prefix)

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
    uvicorn.run("rag_kb.api.main:app", host=settings.HOST, port=settings.PORT, reload=settings.DEBUG)
