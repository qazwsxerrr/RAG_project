"""src/rag_search/build.py —— 检索 LangGraph 的组装层，提供编译后的图单例与便捷入口 run_search。

把 nodes 的 8 个节点连成 START → prepare → {四路并行} → fuse → rerank → mmr → END，
run_search 构造初始状态、流式跑图并返回最终状态。
图无状态、可被多请求高并发调用；get_retrieval_graph 用 lru_cache 做进程内编译单例。
"""

from functools import lru_cache
from typing import Any, Dict, List, Optional
from langgraph.graph import StateGraph, START, END
from rag_search import edges, nodes
from rag_search.state import RetrievalState, get_default_state

@lru_cache(maxsize=1)
def get_retrieval_graph():
    """构建并编译检索流程 LangGraph 单例"""
    builder = StateGraph(RetrievalState)

    # 注册 8 个主要节点
    builder.add_node(edges.NODE_PREPARE, nodes.prepare_node)
    builder.add_node(edges.NODE_DENSE, nodes.dense_node)
    builder.add_node(edges.NODE_SPARSE, nodes.sparse_node)
    builder.add_node(edges.NODE_BM25, nodes.bm25_node)
    builder.add_node(edges.NODE_HYDE, nodes.hyde_node)
    builder.add_node(edges.NODE_FUSE, nodes.fuse_node)
    builder.add_node(edges.NODE_RERANK, nodes.rerank_node)
    builder.add_node(edges.NODE_MMR, nodes.mmr_node)

    # 拓扑连边
    builder.add_edge(START, edges.NODE_PREPARE)

    # 阶段 1 -> 四路并行扇出 (Fan-out)
    builder.add_edge(edges.NODE_PREPARE, edges.NODE_DENSE)
    builder.add_edge(edges.NODE_PREPARE, edges.NODE_SPARSE)
    builder.add_edge(edges.NODE_PREPARE, edges.NODE_BM25)
    builder.add_edge(edges.NODE_PREPARE, edges.NODE_HYDE)

    # 四路召回汇聚扇入 (Fan-in) -> 阶段 4: RRF 融合
    builder.add_edge(edges.NODE_DENSE, edges.NODE_FUSE)
    builder.add_edge(edges.NODE_SPARSE, edges.NODE_FUSE)
    builder.add_edge(edges.NODE_BM25, edges.NODE_FUSE)
    builder.add_edge(edges.NODE_HYDE, edges.NODE_FUSE)

    # 阶段 4 -> 5 -> 6 -> END
    builder.add_edge(edges.NODE_FUSE, edges.NODE_RERANK)
    builder.add_edge(edges.NODE_RERANK, edges.NODE_MMR)
    builder.add_edge(edges.NODE_MMR, END)

    return builder.compile()

async def run_search(
    query: str,
    conversation_id: Optional[str] = None,
    history: Optional[List[Dict[str, str]]] = None,
    department_scope: Optional[List[str]] = None,
    category_scope: Optional[str] = None,
    tags_scope: Optional[List[str]] = None,
    scene_type: str = "general",
    enable_hyde: bool = True,
    config: Optional[dict] = None
) -> RetrievalState:
    """便捷统一检索入口：构造初始状态、执行 LangGraph 并返回最终状态"""
    initial_state = get_default_state(
        query=query,
        conversation_id=conversation_id,
        history=history,
        department_scope=department_scope,
        category_scope=category_scope,
        tags_scope=tags_scope,
        scene_type=scene_type,
        enable_hyde=enable_hyde
    )

    graph = get_retrieval_graph()
    final_state = await graph.ainvoke(initial_state, config=config)
    return final_state
