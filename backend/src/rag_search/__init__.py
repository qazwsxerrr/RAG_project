"""src/rag_search/__init__.py —— rag_search 包的入口，集中导出检索与生成的公共符号。

提供 run_search、get_retrieval_graph、stream_answer、generate_answer、RetrievalState 等对外核心入口。
"""

from rag_search.state import RetrievalState, get_default_state
from rag_search.build import get_retrieval_graph, run_search
from rag_search.answer import stream_answer, generate_answer, build_generation_prompts

__all__ = [
    "RetrievalState",
    "get_default_state",
    "get_retrieval_graph",
    "run_search",
    "stream_answer",
    "generate_answer",
    "build_generation_prompts"
]
