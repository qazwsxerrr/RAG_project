from pathlib import Path
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from backend.app.core.config import settings
from backend.app.core.database import AsyncSessionLocal
from backend.app.api.v1.documents import router as documents_router
from backend.app.api.v1.pipeline import router as pipeline_router
from backend.app.api.v1.retrieval import router as retrieval_router

app = FastAPI(
    title=settings.APP_NAME,
    version="1.0.0",
    description="企业级多模态 RAG 架构后端 API 服务",
    docs_url="/docs",
    redoc_url="/redoc"
)

# 配置跨域 CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册静态文件目录（供访问解析出的工作区本地中间产物）
parsed_dir = Path(settings.DATA_PARSED_DIR)
parsed_dir.mkdir(parents=True, exist_ok=True)
app.mount("/static/parsed", StaticFiles(directory=str(parsed_dir)), name="parsed")

# 注册 API 路由
app.include_router(documents_router, prefix=settings.API_V1_PREFIX)
app.include_router(pipeline_router, prefix=settings.API_V1_PREFIX)
app.include_router(retrieval_router, prefix=settings.API_V1_PREFIX)

@app.get("/health", tags=["基础监控"])
@app.get(f"{settings.API_V1_PREFIX}/health", tags=["基础监控"])
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
        "app_name": settings.APP_NAME,
        "environment": settings.APP_ENV,
        "debug": settings.DEBUG,
        "model_name": settings.MODEL_NAME,
        "embedding_model": settings.EMBEDDING_MODEL,
        "db_status": db_status,
        "oss_status": "online" if oss_configured else "unconfigured"
    }

@app.get("/", tags=["基础监控"])
async def root():
    return {
        "message": "Welcome to Enterprise Multimodal RAG API",
        "docs_url": "/docs",
        "version": "1.0.0"
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend.app.main:app", host="0.0.0.0", port=8000, reload=True)
