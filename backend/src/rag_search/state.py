"""src/rag_search/state.py —— 定义检索 LangGraph 的状态契约 RetrievalState 及默认状态工厂。

声明一次检索流程中流转的全部字段，get_default_state() 负责构造显式初始化好的初始状态，供 build 建图使用。
四路召回并行写入，所以 candidates / warnings / trace 三个字段带 operator.add 归约器，其余字段都只有单一写入者。
"""

import copy
import operator
from typing import Annotated, Any, Dict, List, Optional, TypedDict

class RetrievalState(TypedDict):
    """LangGraph 检索图全局流转状态"""
    # 外部输入
    query: str
    conversation_id: Optional[str]
    history: List[Dict[str, str]]
    department_scope: Optional[List[str]]
    category_scope: Optional[str]
    tags_scope: Optional[List[str]]
    scene_type: str
    enable_hyde: bool

    # 阶段 1：预处理中间产物
    rewritten_query: str
    dense_vec: List[float]
    sparse_vec: Dict[str, float]
    tsquery_tokens: str

    # 阶段 2 & 3：四路召回候选（带 operator.add 归约器，支持图节点并行扇出与合并）
    candidates: Annotated[List[Dict[str, Any]], operator.add]

    # 阶段 4：RRF 融合产物
    fused_chunks: List[Dict[str, Any]]

    # 阶段 5：重排产物与丢弃诊断
    reranked_chunks: List[Dict[str, Any]]
    dropped_chunks: List[Dict[str, Any]]

    # 阶段 6：MMR 最终切片与溯源卡片
    results: List[Dict[str, Any]]
    citations: List[Dict[str, Any]]

    # 过程监控与异常降级追踪（带 operator.add 归约器）
    warnings: Annotated[List[str], operator.add]
    trace: Annotated[List[Dict[str, Any]], operator.add]

def get_default_state(
    query: str,
    conversation_id: Optional[str] = None,
    history: Optional[List[Dict[str, str]]] = None,
    department_scope: Optional[List[str]] = None,
    category_scope: Optional[str] = None,
    tags_scope: Optional[List[str]] = None,
    scene_type: str = "general",
    enable_hyde: bool = True,
) -> RetrievalState:
    """构造初始检索图状态字典"""
    state: RetrievalState = {
        "query": query.strip(),
        "conversation_id": conversation_id,
        "history": copy.deepcopy(history) if history else [],
        "department_scope": list(department_scope) if department_scope else None,
        "category_scope": category_scope,
        "tags_scope": list(tags_scope) if tags_scope else None,
        "scene_type": scene_type or "general",
        "enable_hyde": enable_hyde,

        "rewritten_query": query.strip(),
        "dense_vec": [],
        "sparse_vec": {},
        "tsquery_tokens": "",

        "candidates": [],
        "fused_chunks": [],
        "reranked_chunks": [],
        "dropped_chunks": [],
        "results": [],
        "citations": [],

        "warnings": [],
        "trace": []
    }
    return state
