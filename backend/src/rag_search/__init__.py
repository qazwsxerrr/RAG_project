"""src/rag_search/__init__.py —— rag_search 包的入口，集中导出检索与生成的公共符号。

提供 get_retrieval_graph、stream_answer、RetrievalState 等核心入口（纯检索与非流式推理已统一收口至 FastMCP 服务层）。
"""

from rag_search.state import RetrievalState, get_default_state
from rag_search.build import get_retrieval_graph
from rag_search.answer import stream_answer, build_generation_prompts

__all__ = [
    "RetrievalState",
    "get_default_state",
    "get_retrieval_graph",
    "stream_answer",
    "build_generation_prompts",
]
