"""
生产容器与既有架构兼容层入口 (Legacy Container Entrypoint Adapter)
用于让既有容器 `uvicorn backend.app.main:app` 无缝桥接并运行最新的 LangGraph 后端引擎
"""
import sys
from pathlib import Path

# 将 backend/src 路径加入 sys.path，保证无论在容器内外均能准确寻址 rag_kb 和 rag_search
backend_src_dir = Path(__file__).resolve().parent.parent / "src"
if str(backend_src_dir) not in sys.path:
    sys.path.insert(0, str(backend_src_dir))

# 导出基于 LangGraph 的核心业务后端 FastAPI 实例
from rag_kb.api.main import app

__all__ = ["app"]
