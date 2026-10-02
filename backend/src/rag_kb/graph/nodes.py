"""src/rag_kb/graph/nodes.py —— graph 层与 nodes 业务层之间的适配层。

包装 11 个业务节点实例为 LangGraph 节点函数供 StateGraph 注册。
每个函数严格接收 (state, config, writer) 参数，
统一与 LangGraph 1.2+ 的 StreamWriter 机制配合，派发实时流式进度事件。
"""

from typing import Optional
from langchain_core.runnables import RunnableConfig
from langgraph.types import StreamWriter
from rag_kb.graph.state import IngestState

from rag_kb.nodes.ingest import ingest_node
from rag_kb.nodes.mineru import mineru_node
from rag_kb.nodes.loader import loader_node
from rag_kb.nodes.upload import upload_node
from rag_kb.nodes.enrich import enrich_node
from rag_kb.nodes.table import table_node
from rag_kb.nodes.review import review_node
from rag_kb.nodes.table_apply import table_apply_node
from rag_kb.nodes.splitter import splitter_node
from rag_kb.nodes.embedder import embedder_node
from rag_kb.nodes.store import store_node

async def node_ingest(state: IngestState, config: RunnableConfig = None, writer: StreamWriter = None) -> IngestState:
    return await ingest_node(state, config=config, writer=writer)

async def node_mineru(state: IngestState, config: RunnableConfig = None, writer: StreamWriter = None) -> IngestState:
    return await mineru_node(state, config=config, writer=writer)

async def node_loader(state: IngestState, config: RunnableConfig = None, writer: StreamWriter = None) -> IngestState:
    return await loader_node(state, config=config, writer=writer)

async def node_upload(state: IngestState, config: RunnableConfig = None, writer: StreamWriter = None) -> IngestState:
    return await upload_node(state, config=config, writer=writer)

async def node_enrich(state: IngestState, config: RunnableConfig = None, writer: StreamWriter = None) -> IngestState:
    return await enrich_node(state, config=config, writer=writer)

async def node_table(state: IngestState, config: RunnableConfig = None, writer: StreamWriter = None) -> IngestState:
    return await table_node(state, config=config, writer=writer)

async def node_review(state: IngestState, config: RunnableConfig = None, writer: StreamWriter = None) -> IngestState:
    return await review_node(state, config=config, writer=writer)

async def node_table_apply(state: IngestState, config: RunnableConfig = None, writer: StreamWriter = None) -> IngestState:
    return await table_apply_node(state, config=config, writer=writer)

async def node_splitter(state: IngestState, config: RunnableConfig = None, writer: StreamWriter = None) -> IngestState:
    return await splitter_node(state, config=config, writer=writer)

async def node_embedder(state: IngestState, config: RunnableConfig = None, writer: StreamWriter = None) -> IngestState:
    return await embedder_node(state, config=config, writer=writer)

async def node_store(state: IngestState, config: RunnableConfig = None, writer: StreamWriter = None) -> IngestState:
    return await store_node(state, config=config, writer=writer)
