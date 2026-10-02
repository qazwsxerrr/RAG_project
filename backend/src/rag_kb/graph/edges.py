"""src/rag_kb/graph/edges.py —— 入库图的条件边（路由）层：定义节点名常量与条件路由函数。

提供 NODE_INGEST ~ NODE_STORE 节点名常量，
统一的失败判定 is_failed 和一组 route_after_* 路由函数：
任一步骤失败立即短路到 END，人机审核在无审核项或 skip_review 时自动跳过。
"""

from langgraph.graph import END
from rag_kb.core.constants import IngestStatus
from rag_kb.graph.state import IngestState

# 11 个标准节点名称常量
NODE_INGEST = "ingest"
NODE_MINERU = "mineru"
NODE_LOADER = "loader"
NODE_UPLOAD = "upload"
NODE_ENRICH = "enrich"
NODE_TABLE = "table"
NODE_REVIEW = "review"
NODE_TABLE_APPLY = "table_apply"
NODE_SPLITTER = "splitter"
NODE_EMBEDDER = "embedder"
NODE_STORE = "store"

def is_failed(state: IngestState) -> bool:
    """统一校验流水线是否处于失败状态"""
    return (
        state.get("overall_status") == IngestStatus.FAILED.value
        or state.get("error") is not None
    )

def route_after_ingest(state: IngestState) -> str:
    if is_failed(state):
        return END
    return NODE_MINERU

def route_after_mineru(state: IngestState) -> str:
    if is_failed(state):
        return END
    return NODE_LOADER

def route_after_loader(state: IngestState) -> str:
    if is_failed(state):
        return END
    return NODE_UPLOAD

def route_after_upload(state: IngestState) -> str:
    if is_failed(state):
        return END
    return NODE_ENRICH

def route_after_enrich(state: IngestState) -> str:
    if is_failed(state):
        return END
    return NODE_TABLE

def route_after_table(state: IngestState) -> str:
    if is_failed(state):
        return END
    # 有待审核项且未配置跳过审核时，进入 review 节点执行 interrupt 挂起
    if state.get("pending_reviews") and not state.get("skip_review", False):
        return NODE_REVIEW
    return NODE_TABLE_APPLY

def route_after_review(state: IngestState) -> str:
    if is_failed(state):
        return END
    return NODE_TABLE_APPLY

def route_after_table_apply(state: IngestState) -> str:
    if is_failed(state):
        return END
    return NODE_SPLITTER

def route_after_splitter(state: IngestState) -> str:
    if is_failed(state):
        return END
    return NODE_EMBEDDER

def route_after_embedder(state: IngestState) -> str:
    if is_failed(state):
        return END
    return NODE_STORE
