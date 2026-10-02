"""src/rag_kb/core/config.py —— 全局配置中心：把 .env 收敛成强类型 Settings 并提供单例。

用 pydantic-settings 读取 .env 与环境变量，并据此拼出 database_url、pg_conninfo 两条连接串，
再通过带缓存的 get_settings() 向全项目提供唯一的配置入口。位于 core 基础层，不依赖项目内其他模块。
"""

import os
from functools import lru_cache
from pathlib import Path
from typing import List, Optional
try:
    from pydantic_settings import BaseSettings, SettingsConfigDict
except ImportError:
    from pydantic import BaseModel as BaseSettings  # type: ignore
    SettingsConfigDict = dict  # type: ignore

# 寻找 .env 文件路径（优先当前包根目录，次优先父级项目根目录）
CURRENT_DIR = Path(__file__).resolve().parent
PACKAGE_ROOT = CURRENT_DIR.parent.parent.parent  # knowledge/code
WORKSPACE_ROOT = PACKAGE_ROOT.parent.parent      # RAG_project

ENV_PATH = PACKAGE_ROOT / ".env"
if not ENV_PATH.exists():
    ENV_PATH = PACKAGE_ROOT.parent / ".env"
if not ENV_PATH.exists():
    ENV_PATH = WORKSPACE_ROOT / ".env"

# 确保国内云端服务不被代理误拦截
DOMESTIC_DOMAINS = [
    "aliyuncs.com",
    "mineru.net",
    "openxlab.org.cn",
    "shlab.tech",
    "siliconflow.cn",
    "siliconflow.com",
    "localhost",
    "127.0.0.1"
]
for env_var in ["NO_PROXY", "no_proxy"]:
    current = os.environ.get(env_var, "")
    current_list = [d.strip() for d in current.split(",") if d.strip()]
    for dom in DOMESTIC_DOMAINS:
        if dom not in current_list:
            current_list.append(dom)
    os.environ[env_var] = ",".join(current_list)

class Settings(BaseSettings):
    # ==================== 应用基础配置 ====================
    APP_NAME: str = "rag-kb"
    APP_ENV: str = "development"
    DEBUG: bool = True
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    API_V1_PREFIX: str = "/api/v1"
    SECRET_KEY: str = "rag-enterprise-secret-key-change-in-production"
    DATA_PARSED_DIR: str = str(WORKSPACE_ROOT / "data" / "parsed")
    DEFAULT_TENANT_ID: str = "default_tenant"

    CORS_ORIGINS: List[str] = [
        "http://localhost:5173",
        "http://localhost:3000",
        "http://127.0.0.1:5173",
        "https://rag.sakivo.com"
    ]

    # ==================== 数据库配置 ====================
    # 严格直连真实 PostgreSQL 16 + pgvector，严禁本地 SQLite
    POSTGRES_USER: str = "postgres"
    POSTGRES_PASSWORD: str = "postgres_password"
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 5432
    POSTGRES_DB: str = "rag_enterprise"
    DATABASE_URL: Optional[str] = None

    @property
    def async_database_url(self) -> str:
        """SQLAlchemy 异步连接串 (postgresql+asyncpg)"""
        if self.DATABASE_URL and self.DATABASE_URL.startswith("postgresql+asyncpg"):
            return self.DATABASE_URL
        if self.DATABASE_URL and self.DATABASE_URL.startswith("postgresql://"):
            return self.DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)
        return f"postgresql+asyncpg://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"

    @property
    def pg_conninfo(self) -> str:
        """标准 libpq / LangGraph PostgresSaver 连接串 (postgresql://)"""
        return f"postgresql://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"

    # ==================== 阿里云 OSS 配置 ====================
    # 严格接入真实 OSS 云端存储，私有读，严禁本地静态回退
    OSS_ACCESS_KEY_ID: str = ""
    OSS_ACCESS_KEY_SECRET: str = ""
    OSS_ENDPOINT: str = "https://oss-cn-shenzhen.aliyuncs.com"
    OSS_BUCKET_NAME: str = ""
    OSS_REGION: str = "oss-cn-shenzhen"
    OSS_KEY_PREFIX: str = "rag_storage"
    OSS_SIGNED_URL_EXPIRES: int = 7200

    # ==================== MinerU 云端解析 API ====================
    MINERU_API_KEY: str = ""
    MINERU_API_URL: str = "https://mineru.net/api/v4"
    MINERU_MODEL_VERSION: str = "vlm"
    MINERU_TIMEOUT_SECONDS: int = 1200
    MINERU_POLL_INTERVAL: int = 5
    MINERU_PARSER_MODE: str = "auto"
    MINERU_AGENT_BASE_URL: str = "https://mineru.net/api/v1/agent"

    # ==================== 通用 LLM & VLM 配置 (OpenAI 协议) ====================
    API_KEY: str = ""
    BASE_URL: str = ""
    MODEL_NAME: str = "grok-4.5"

    @property
    def active_api_key(self) -> str:
        return self.API_KEY.strip()

    @property
    def active_base_url(self) -> str:
        return self.BASE_URL.strip()

    @property
    def active_model_name(self) -> str:
        return self.MODEL_NAME.strip() or "grok-4.5"

    @property
    def active_llm_api_key(self) -> str:
        return self.active_api_key

    @property
    def active_llm_base_url(self) -> str:
        return self.active_base_url

    @property
    def active_llm_model(self) -> str:
        return self.active_model_name

    @property
    def active_vlm_api_key(self) -> str:
        return self.active_api_key

    @property
    def active_vlm_base_url(self) -> str:
        return self.active_base_url

    @property
    def active_vlm_model(self) -> str:
        return self.active_model_name

    # ==================== 向量模型配置 (Dense & Sparse) ====================
    EMBEDDING_API_KEY: str = ""
    EMBEDDING_BASE_URL: str = "https://api.siliconflow.cn/v1"
    EMBEDDING_MODEL: str = "BAAI/bge-m3"
    EMBEDDING_DIM: int = 1024

    # ==================== 检索与重排配置 ====================
    RERANK_ENABLED: bool = True
    RERANK_API_KEY: str = ""
    RERANK_BASE_URL: str = "https://api.siliconflow.cn/v1"
    RERANK_MODEL: str = "BAAI/bge-reranker-v2-m3"
    RERANK_TOP_N: int = 20

    RETRIEVAL_RECALL_TOP_N: int = 20
    HYDE_TOP_N: int = 15
    HYDE_TIMEOUT_SECONDS: float = 90.0
    FUSE_TOP_N: int = 29
    RRF_K: int = 60
    FINAL_TOP_K: int = 5
    MMR_LAMBDA: float = 0.7
    CHAT_HISTORY_TURNS: int = 5

    model_config = SettingsConfigDict(
        env_file=str(ENV_PATH) if ENV_PATH.exists() else None,
        env_file_encoding="utf-8",
        extra="ignore"
    )

@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """获取强类型 Settings 单例"""
    return Settings()

settings: Settings = get_settings()
