"""src/rag_kb/core/config.py —— 全局配置中心：把 .env 收敛成强类型 Settings 并提供单例。

用 pydantic-settings 读取 .env 与环境变量，并据此拼出 database_url、pg_conninfo 两条连接串，
再通过带缓存的 get_settings() 向全项目提供唯一的配置入口。位于 core 基础层，不依赖项目内其他模块。
"""

import os
import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field
try:
    from pydantic_settings import BaseSettings, SettingsConfigDict
except ImportError:
    from pydantic import BaseModel as BaseSettings  # type: ignore
    SettingsConfigDict = dict  # type: ignore

config_logger = logging.getLogger("rag_kb.config")

# 寻找 .env 文件路径（优先当前包根目录，次优先父级项目根目录）
CURRENT_DIR = Path(__file__).resolve().parent
PACKAGE_ROOT = CURRENT_DIR.parent.parent.parent  # backend
WORKSPACE_ROOT = PACKAGE_ROOT.parent              # RAG_project

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
    PORT: int = 8001
    API_V1_PREFIX: str = "/api/v1"
    
    # 跨服务 JWT 鉴权配置 (Phase-6: RS256 + 强校验，严禁本地对称签名密钥)
    JWT_ALGORITHM: str = "RS256"
    JWT_PUBLIC_KEY: Optional[str] = None
    JWT_PUBLIC_KEY_PATH: Optional[str] = None
    JWT_JWKS_URL: Optional[str] = "http://127.0.0.1:8080/.well-known/jwks.json"
    JWT_ISSUER: str = "rag-admin"
    JWT_AUDIENCE: str = "rag-engine"
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    DATA_PARSED_DIR: str = str(WORKSPACE_ROOT / "data" / "parsed")
    DEFAULT_TENANT_ID: str = "default_tenant"

    # 模型密钥鉴权中心凭据托管 (Phase-6 §7: 内部租约换端点)
    AUTH_CENTER_BASE_URL: str = "http://127.0.0.1:8080"
    MODEL_KEY_SERVICE_ID: str = "rag-engine"
    MODEL_KEY_SERVICE_SECRET: str = "rag-engine-service-secret"

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

    # ==================== 通用 LLM & VLM 配置 (OpenAI 协议，API Key 统一由 Java 鉴权中心托管) ====================
    BASE_URL: str = "https://kedaya.ai/v1"
    MODEL_NAME: str = "grok-4.5"
    LLM_API_BACKEND: str = "responses"

    @property
    def active_llm_api_backend(self) -> str:
        backend = (self.LLM_API_BACKEND or "responses").strip().lower()
        if backend in ("responses", "response"):
            return "responses"
        return "chat_completions"

    @property
    def active_base_url(self) -> str:
        return (self.BASE_URL or "https://kedaya.ai/v1").strip()

    @property
    def active_model_name(self) -> str:
        return self.MODEL_NAME.strip() or "grok-4.5"

    @property
    def active_llm_base_url(self) -> str:
        return self.active_base_url

    @property
    def active_llm_model(self) -> str:
        return self.active_model_name

    @property
    def active_vlm_base_url(self) -> str:
        return self.active_base_url

    @property
    def active_vlm_model(self) -> str:
        return self.active_model_name

    # ==================== 向量模型配置 (Dense & Sparse，API Key 统一由 Java 鉴权中心托管) ====================
    EMBEDDING_BASE_URL: str = "https://api.siliconflow.cn/v1"
    EMBEDDING_MODEL: str = "BAAI/bge-m3"
    EMBEDDING_DIM: int = 1024

    # ==================== 检索与重排配置 (API Key 统一由 Java 鉴权中心托管) ====================
    RERANK_ENABLED: bool = True
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

    # ==================== Phase-7: 多模型热切换与智能监控预警配置 ====================
    LLM_FALLBACKS_JSON: Optional[str] = None
    ALERT_WEBHOOK_URL: Optional[str] = None
    ALERT_ERROR_THRESHOLD: float = 5.0
    CIRCUIT_BREAKER_COOLDOWN_SECONDS: int = 300
    SLIDING_WINDOW_SIZE: int = 100

    @property
    def resilient_llm_config(self) -> "ResilientLLMConfig":
        return load_resilient_llm_config(self)

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

# ==================== Phase-7 模型节点配置与候选链数据模型 ====================

class ModelNodeConfig(BaseModel):
    """单模型服务节点配置模型"""
    name: str                           # 节点唯一标识，如 "primary-default", "fallback-qwen"
    model: str                          # 实际模型名称
    base_url: str                       # 供应商接口 Base URL
    api_key: str = ""                   # API 密钥 (对外输出与日志中自动脱敏，支持运行时凭据注入)
    provider: Optional[str] = None      # 供应商标识，如 "siliconflow", "kedaya"
    backend: str = "responses"          # "responses" 或 "chat_completions"
    weight: int = 100                   # 优先级权重 (越大优先级越高)
    is_active: bool = True              # 是否启用

    def masked_api_key(self) -> str:
        """敏感密钥脱敏展示"""
        if not self.api_key or len(self.api_key) <= 8:
            return f"[{self.provider or 'managed'}]-lease-active"
        return f"{self.api_key[:4]}****{self.api_key[-4:]}"

class ResilientLLMConfig(BaseModel):
    """具备弹性候选池与监控预警的 LLM 集群配置"""
    primary: ModelNodeConfig
    fallbacks: List[ModelNodeConfig] = Field(default_factory=list)
    alert_webhook_url: Optional[str] = None          # 告警 Webhook 地址
    error_threshold_percentage: float = 5.0          # 报警错误率阈值 (5%)
    sliding_window_size: int = 100                  # SLA 统计滑动窗口大小
    circuit_breaker_cooldown_seconds: int = 300     # 熔断冷却时间 (默认 5 分钟)

def load_resilient_llm_config(cfg: Settings) -> ResilientLLMConfig:
    """从当前 settings 与环境变量动态组装高可用模型链 (Phase-6 §7.5: 节点仅声明 provider，运行时通过租约注入凭据)"""
    primary_provider = "kedaya" if "kedaya" in cfg.active_llm_base_url.lower() else "siliconflow"
    primary = ModelNodeConfig(
        name="primary-default",
        model=cfg.active_llm_model,
        base_url=cfg.active_llm_base_url,
        api_key="",
        provider=primary_provider,
        backend=cfg.active_llm_api_backend,
        weight=100
    )

    fallbacks: List[ModelNodeConfig] = []
    # 优先解析环境变量中的结构化候选池配置 (JSON 格式)
    raw_fallbacks = cfg.LLM_FALLBACKS_JSON or ""
    if raw_fallbacks.strip():
        try:
            items = json.loads(raw_fallbacks)
            for item in items:
                if "provider" not in item or not item["provider"]:
                    b_url = str(item.get("base_url", "")).lower()
                    item["provider"] = "kedaya" if "kedaya" in b_url else "siliconflow"
                fallbacks.append(ModelNodeConfig(**item))
        except Exception as e:
            config_logger.warning(f"解析 LLM_FALLBACKS_JSON 失败: {e}")

    # 若未配置独立 JSON，节点声明 provider，凭据在运行时从鉴权中心租约注入 (Phase-6 §7.5)
    if not fallbacks:
        sf_base_url = (cfg.RERANK_BASE_URL or cfg.EMBEDDING_BASE_URL or "https://api.siliconflow.cn/v1").strip()
        fallbacks.append(ModelNodeConfig(
            name="fallback-siliconflow-deepseek",
            model="deepseek-ai/DeepSeek-V3",
            base_url=sf_base_url,
            api_key="",
            provider="siliconflow",
            backend="chat_completions",
            weight=90
        ))
        fallbacks.append(ModelNodeConfig(
            name="fallback-siliconflow-qwen",
            model="Qwen/Qwen2.5-72B-Instruct",
            base_url=sf_base_url,
            api_key="",
            provider="siliconflow",
            backend="chat_completions",
            weight=80
        ))

    # Phase-6 §9 风险缓解：启动断言备用模型池与主模型节点均具备合法 provider 声明
    for node in [primary] + fallbacks:
        if not node.provider or not node.provider.strip():
            raise RuntimeError(f"【配置断言失败】模型节点 [{node.name}] 未声明合法的 provider，拒绝装配！")

    return ResilientLLMConfig(
        primary=primary,
        fallbacks=fallbacks,
        alert_webhook_url=cfg.ALERT_WEBHOOK_URL,
        error_threshold_percentage=cfg.ALERT_ERROR_THRESHOLD,
        sliding_window_size=cfg.SLIDING_WINDOW_SIZE,
        circuit_breaker_cooldown_seconds=cfg.CIRCUIT_BREAKER_COOLDOWN_SECONDS
    )

