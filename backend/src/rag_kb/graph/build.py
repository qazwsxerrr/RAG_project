"""src/rag_kb/graph/build.py —— 入库 LangGraph 的组装与出口层。

用 StateGraph(IngestState) 注册 nodes.py 的 11 个节点包装函数，
按 edges.py 的条件边连线，挂上 Checkpointer，支持 interrupt 挂起与跨进程/跨请求恢复。
提供 compile_ingest_graph() 与 get_ingest_graph()。
"""

import logging
from typing import Optional
from psycopg_pool import AsyncConnectionPool
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.memory import MemorySaver

from rag_kb.core.config import settings
from rag_kb.graph.state import IngestState
from rag_kb.graph.edges import (
    NODE_INGEST,
    NODE_MINERU,
    NODE_LOADER,
    NODE_UPLOAD,
    NODE_ENRICH,
    NODE_TABLE,
    NODE_REVIEW,
    NODE_TABLE_APPLY,
    NODE_SPLITTER,
    NODE_EMBEDDER,
    NODE_STORE,
    route_after_ingest,
    route_after_mineru,
    route_after_loader,
    route_after_upload,
    route_after_enrich,
    route_after_table,
    route_after_review,
    route_after_table_apply,
    route_after_splitter,
    route_after_embedder,
)
from rag_kb.graph.nodes import (
    node_ingest,
    node_mineru,
    node_loader,
    node_upload,
    node_enrich,
    node_table,
    node_review,
    node_table_apply,
    node_splitter,
    node_embedder,
    node_store,
)

logger = logging.getLogger(__name__)

# 全局单例连接池与 Checkpointer 缓存
_pg_pool: Optional[AsyncConnectionPool] = None
_default_saver: Optional[BaseCheckpointSaver] = None

async def get_postgres_checkpointer() -> AsyncPostgresSaver:
    """获取连接到真实 PostgreSQL 16 的异步 Checkpointer"""
    global _pg_pool, _default_saver
    if _default_saver is None or not isinstance(_default_saver, AsyncPostgresSaver):
        if _pg_pool is None:
            _pg_pool = AsyncConnectionPool(
                conninfo=settings.pg_conninfo,
                max_size=10,
                open=False
            )
            await _pg_pool.open()
        saver = AsyncPostgresSaver(_pg_pool)
        await saver.setup()
        _default_saver = saver
    return _default_saver

def create_ingest_graph_builder() -> StateGraph:
    """构建入库流水线 StateGraph 拓扑图"""
    builder = StateGraph(IngestState)

    # 1. 注册 11 个业务节点
    builder.add_node(NODE_INGEST, node_ingest)
    builder.add_node(NODE_MINERU, node_mineru)
    builder.add_node(NODE_LOADER, node_loader)
    builder.add_node(NODE_UPLOAD, node_upload)
    builder.add_node(NODE_ENRICH, node_enrich)
    builder.add_node(NODE_TABLE, node_table)
    builder.add_node(NODE_REVIEW, node_review)
    builder.add_node(NODE_TABLE_APPLY, node_table_apply)
    builder.add_node(NODE_SPLITTER, node_splitter)
    builder.add_node(NODE_EMBEDDER, node_embedder)
    builder.add_node(NODE_STORE, node_store)

    # 2. 连接拓扑条件边
    builder.add_edge(START, NODE_INGEST)
    builder.add_conditional_edges(NODE_INGEST, route_after_ingest)
    builder.add_conditional_edges(NODE_MINERU, route_after_mineru)
    builder.add_conditional_edges(NODE_LOADER, route_after_loader)
    builder.add_conditional_edges(NODE_UPLOAD, route_after_upload)
    builder.add_conditional_edges(NODE_ENRICH, route_after_enrich)
    builder.add_conditional_edges(NODE_TABLE, route_after_table)
    builder.add_conditional_edges(NODE_REVIEW, route_after_review)
    builder.add_conditional_edges(NODE_TABLE_APPLY, route_after_table_apply)
    builder.add_conditional_edges(NODE_SPLITTER, route_after_splitter)
    builder.add_conditional_edges(NODE_EMBEDDER, route_after_embedder)
    builder.add_edge(NODE_STORE, END)

    return builder

def compile_ingest_graph(checkpointer: Optional[BaseCheckpointSaver] = None):
    """编译入库图。若 checkpointer 为 None，默认挂载 MemorySaver 方便单次执行，亦可传入 PostgresSaver"""
    builder = create_ingest_graph_builder()
    actual_checkpointer = checkpointer if checkpointer is not None else MemorySaver()
    return builder.compile(checkpointer=actual_checkpointer)

# 默认全局编译图实例（挂载内存 Checkpointer）
ingest_graph = compile_ingest_graph()

def get_ingest_graph(checkpointer: Optional[BaseCheckpointSaver] = None):
    """获取入库图实例"""
    if checkpointer is not None:
        return compile_ingest_graph(checkpointer)
    return ingest_graph
