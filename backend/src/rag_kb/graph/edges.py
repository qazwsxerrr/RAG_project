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

STEP_INDEX_TO_NODE = {
    1: NODE_INGEST,
    2: NODE_MINERU,
    3: NODE_LOADER,
    4: NODE_UPLOAD,
    5: NODE_ENRICH,
    6: NODE_TABLE,
    7: NODE_REVIEW,
    8: NODE_TABLE_APPLY,
    9: NODE_SPLITTER,
    10: NODE_EMBEDDER,
    11: NODE_STORE,
}

def _resolve_content_list(state: IngestState) -> list:
    """优先读取 LoaderNode/UploadNode 维护的顶层 content_list，其次回退 parse_res.content_list"""
    top_cl = state.get("content_list")
    if isinstance(top_cl, list) and top_cl:
        return top_cl
    parse_res = state.get("parse_res") or {}
    fallback_cl = parse_res.get("content_list")
    return fallback_cl if isinstance(fallback_cl, list) else []

def _is_step_completed(state: IngestState, step_index: int) -> bool:
    steps = state.get("steps") or []
    idx = step_index - 1
    if 0 <= idx < len(steps) and isinstance(steps[idx], dict):
        return steps[idx].get("status") == "completed"
    return False

def can_safely_jump_to_step(state: IngestState, target_step: int) -> bool:
    """校验当前状态中是否完整保留了跳转至 target_step 所需的上游前置产物"""
    if target_step <= 1:
        return True
    if target_step == 2:
        return bool(state.get("raw_oss_url") or state.get("temp_file_path") or state.get("file_bytes"))
    if target_step == 3:
        return bool(state.get("parsed_dir") or state.get("parse_res"))
    if target_step == 4:
        return bool(state.get("md_content"))
    if target_step in (5, 6):
        if not state.get("md_content"):
            return False
        content_list = _resolve_content_list(state)
        # 若含本地配图或表格切图，必须已完成 Step 4 OSS 上传
        has_unacked_imgs = any(
            c.get("type") == "image" and c.get("img_path") and not c.get("oss_url")
            for c in content_list
        )
        has_unacked_tbl_imgs = any(
            c.get("type") == "table" and c.get("img_path") and not c.get("img_url")
            for c in content_list
        )
        if has_unacked_imgs or has_unacked_tbl_imgs:
            return False
        if target_step == 6:
            has_unenriched_imgs = any(
                c.get("type") == "image" and c.get("oss_url") and not c.get("vlm_description")
                for c in content_list
            )
            if has_unenriched_imgs:
                return False
        return True
    if target_step == 7:
        # 要求正文存在，且节点 6（表格缝合与审核项装配）必须已执行完成或已有非空审核列表
        if not state.get("md_content"):
            return False
        if not (_is_step_completed(state, 6) or bool(state.get("pending_reviews"))):
            return False
        return True
    if target_step == 8:
        # 要求正文非空，且节点 6 已完成（若含表格则 processed_tables 必须非空）
        if not state.get("md_content"):
            return False
        content_list = _resolve_content_list(state)
        has_tables = any(c.get("type") == "table" for c in content_list)
        if has_tables and not state.get("processed_tables"):
            return False
        return _is_step_completed(state, 6) or (not has_tables and bool(state.get("skip_review")))
    if target_step == 9:
        # 强契约要求 Step 8 产出的 enhanced_md_content，严禁回退未缝合的 md_content
        return bool(state.get("enhanced_md_content"))
    if target_step == 10:
        return bool(state.get("raw_chunks"))
    if target_step == 11:
        # 要求 raw_chunks、dense_embeddings、sparse_vectors、tsv_strings 均非空且长度完全一致
        rc = state.get("raw_chunks") or []
        de = state.get("dense_embeddings") or []
        sv = state.get("sparse_vectors") or []
        ts = state.get("tsv_strings") or []
        return bool(rc and len(rc) == len(de) == len(sv) == len(ts))
    return False

def route_from_start(state: IngestState) -> str:
    """支持断点指定节点续跑：仅当 resume_from_step 合法且上游产物齐备时精准跳转，否则安全从节点 1 启动"""
    target_step = state.get("resume_from_step")
    if isinstance(target_step, int) and 2 <= target_step <= 11:
        if can_safely_jump_to_step(state, target_step):
            return STEP_INDEX_TO_NODE[target_step]
    return NODE_INGEST

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
