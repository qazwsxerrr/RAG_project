import os
from pathlib import Path
from typing import List
try:
    from pydantic_settings import BaseSettings, SettingsConfigDict
except ImportError:
    from pydantic import BaseModel as BaseSettings  # type: ignore
    SettingsConfigDict = dict  # type: ignore

# 寻找项目根目录中的 .env 文件
BASE_DIR = Path(__file__).resolve().parent.parent.parent.parent
ENV_PATH = BASE_DIR / ".env"

# 确保国内云端服务（阿里云 OSS、MinerU 解析与 CDN、硅基流动等）不被本地网络代理误拦截导致 SSL EOF 协议中断
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
    # 应用基础配置
    APP_NAME: str = "Enterprise Multimodal RAG System"
    APP_ENV: str = "development"
    DEBUG: bool = True
    API_V1_PREFIX: str = "/api/v1"
    SECRET_KEY: str = "rag-enterprise-secret-key-change-in-production"
    DATA_PARSED_DIR: str = str(BASE_DIR / "data" / "parsed")
    CORS_ORIGINS: List[str] = [
        "http://localhost:5173",
        "http://localhost:3000",
        "http://127.0.0.1:5173",
    ]

    # 阿里云 OSS (Phase 1 严格接入，无本地回退)
    OSS_ACCESS_KEY_ID: str = ""
    OSS_ACCESS_KEY_SECRET: str = ""
    OSS_ENDPOINT: str = "https://oss-cn-hangzhou.aliyuncs.com"
    OSS_BUCKET_NAME: str = ""

    # MinerU 官方云端解析 API (Phase 1 高精版面解析)
    MINERU_API_KEY: str = ""
    MINERU_API_URL: str = "https://mineru.net/api/v4"
    MINERU_MODEL_VERSION: str = "vlm"
    MINERU_TIMEOUT_SECONDS: int = 1200

    # MinerU 解析通道与降级策略 ("auto" | "v4" | "agent")
    MINERU_PARSER_MODE: str = "auto"
    MINERU_V4_QUEUE_TIMEOUT_SECONDS: int = 30
    MINERU_AGENT_BASE_URL: str = "https://mineru.net/api/v1/agent"

    # 通用大模型与多模态统一配置 (OpenAI 兼容规范接口)
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
    def active_vlm_api_key(self) -> str:
        return self.active_api_key

    @property
    def active_vlm_base_url(self) -> str:
        return self.active_base_url

    @property
    def active_vlm_model(self) -> str:
        return self.active_model_name

    @property
    def active_llm_api_key(self) -> str:
        return self.active_api_key

    @property
    def active_llm_base_url(self) -> str:
        return self.active_base_url

    @property
    def active_llm_model(self) -> str:
        return self.active_model_name

    # 向量化模型配置 (Phase 2: 稠密与稀疏向量三路混合索引)
    EMBEDDING_API_KEY: str = ""
    EMBEDDING_BASE_URL: str = "https://api.siliconflow.cn/v1"
    EMBEDDING_MODEL: str = "BAAI/bge-m3"

    # 重排精排模型配置 (Phase 3: 交叉编码重排)
    RERANK_API_KEY: str = ""
    RERANK_BASE_URL: str = "https://api.siliconflow.cn/v1"
    RERANK_MODEL: str = "BAAI/bge-reranker-v2-m3"

    # HyDE 假设性推理召回超时配置 (秒，支持 grok 等大型推理模型完整输出)
    HYDE_TIMEOUT_SECONDS: float = 90.0

    # 数据库配置 (严格仅使用 PostgreSQL 16 + pgvector)
    POSTGRES_USER: str = "postgres"
    POSTGRES_PASSWORD: str = "postgres_password"
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 5432
    POSTGRES_DB: str = "rag_enterprise"
    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres_password@localhost:5432/rag_enterprise"

    model_config = SettingsConfigDict(
        env_file=str(ENV_PATH) if ENV_PATH.exists() else None,
        env_file_encoding="utf-8",
        extra="ignore"
    )

settings = Settings()

def reload_settings() -> Settings:
    """动态热重载 .env 配置文件并更新全局 settings 单例"""
    global settings
    import dotenv
    if ENV_PATH.exists():
        dotenv.load_dotenv(ENV_PATH, override=True)
    new_settings = Settings()
    for field_name in Settings.model_fields.keys():
        setattr(settings, field_name, getattr(new_settings, field_name))
    return settings

