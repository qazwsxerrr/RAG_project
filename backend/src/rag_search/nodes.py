"""src/rag_search/nodes.py —— 检索 LangGraph 的全部节点函数实现。

从 prepare 到 dense / sparse / bm25 / hyde 四路并行召回，再到 fuse → rerank → mmr，
每个节点都是检索流程的执行体。
在各阶段埋设 StreamWriter 思考流事件帧（ThinkingStep），支持前端 ThinkingProcess 思考看板实时动画展示。
"""

import time
import logging
from typing import Any, Dict, List, Optional
import httpx
from langgraph.types import StreamWriter
from langchain_core.runnables import RunnableConfig

from rag_kb.core.config import settings
from rag_kb.db.session import AsyncSessionLocal
from rag_kb.utils.embedding import embedding_service
from rag_kb.utils import tokenize
from rag_search import edges, fusion, rerank as reranker_module, mmr
from rag_search.channels import dense, sparse, bm25, hyde
from rag_search.state import RetrievalState

logger = logging.getLogger(__name__)

def _emit_thinking(
    writer: Optional[StreamWriter],
    name: str,
    status: str,
    duration_ms: int = 0,
    count_change: Optional[str] = None,
    summary: Optional[str] = None
):
    """向前端流式派发单个思考阶段状态微标帧"""
    if writer is None:
        return
    step_payload = {
        "name": name,
        "status": status,
        "duration_ms": duration_ms,
        "count_change": count_change,
        "summary": summary
    }
    try:
        writer({"type": "thinking_step", "step": step_payload})
    except Exception:
        pass

async def _resolve_db_session(config: Optional[RunnableConfig]):
    """从 RunnableConfig 解析注入的 AsyncSession，或新建连接"""
    if config:
        cfg = config.get("configurable", {})
        existing = cfg.get("db_session")
        if existing is not None:
            return existing, False
    return AsyncSessionLocal(), True

# ==================== 阶段 1：查询预处理 ====================

async def prepare_node(
    state: RetrievalState,
    writer: StreamWriter,
    config: RunnableConfig
) -> Dict[str, Any]:
    """阶段 1: 多轮会话指代消除改写与特征提取"""
    _emit_thinking(
        writer,
        name="查询预处理",
        status="执行中...",
        summary="正在多轮会话指代消除并提取高维稠密与稀疏特征向量..."
    )
    t0 = time.perf_counter()

    query = state["query"].strip()
    history = state.get("history", [])

    # 1. 多轮指代消除
    rewritten_query = query
    if history and len(history) > 0 and settings.active_llm_api_key:
        valid_history = [m for m in history if m.get("content") and m.get("content") != query]
        has_assistant = any(m.get("role") == "assistant" for m in valid_history)
        if has_assistant:
            conv_text = "\n".join([f"{m.get('role')}: {m.get('content')}" for m in valid_history[-6:]])
            system_prompt = (
                "你是一个专业的智能检索查询预处理器。请根据多轮对话历史，对用户的最新提问进行指代消除与主语补全，"
                "生成自包含的独立检索句。仅输出改写后的一句话，严禁多余废话。"
            )
            user_prompt = f"【对话历史】：\n{conv_text}\n\n【最新提问】：\n{query}\n\n请输出独立检索查询："
            endpoint = f"{settings.active_llm_base_url.rstrip('/')}/chat/completions"
            headers = {
                "Authorization": f"Bearer {settings.active_llm_api_key}",
                "Content-Type": "application/json"
            }
            payload = {
                "model": settings.active_llm_model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                "temperature": 0.1,
                "max_tokens": 150
            }
            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    resp = await client.post(endpoint, headers=headers, json=payload)
                    if resp.status_code == 200:
                        data = resp.json()
                        rw = data["choices"][0]["message"]["content"].strip().strip('"\'')
                        if rw:
                            rewritten_query = rw
            except Exception as e:
                logger.warning(f"【指代消除跳过】LLM 改写异常: {e}")

    # 2. 提取特征
    dense_vec = await embedding_service.embed_query(rewritten_query)
    sparse_vec = tokenize.extract_sparse_vector(rewritten_query)
    tsquery_tokens = tokenize.cut_for_index(rewritten_query)

    cost_ms = int((time.perf_counter() - t0) * 1000)
    summary_desc = (
        f"完成多轮指代消除 (改写为: '{rewritten_query[:25]}...')，提取特征"
        if rewritten_query != query
        else "查询语义完整自包含，提取 1024 维特征向量与倒排分词"
    )
    _emit_thinking(
        writer,
        name="查询预处理",
        status="已执行",
        duration_ms=cost_ms,
        count_change="1 句",
        summary=summary_desc
    )

    return {
        "rewritten_query": rewritten_query,
        "dense_vec": dense_vec,
        "sparse_vec": sparse_vec,
        "tsquery_tokens": tsquery_tokens,
        "trace": [{
            "node": edges.NODE_PREPARE,
            "duration_ms": cost_ms,
            "status": "success",
            "rewritten_query": rewritten_query
        }]
    }

# ==================== 阶段 2：四路并行召回 ====================

async def dense_node(
    state: RetrievalState,
    writer: StreamWriter,
    config: RunnableConfig
) -> Dict[str, Any]:
    """阶段 2a: HNSW 稠密语义召回"""
    _emit_thinking(writer, name="稠密召回", status="执行中...", summary="并发检索 1024 维 HNSW 向量索引...")
    t0 = time.perf_counter()

    db_session, should_close = await _resolve_db_session(config)
    try:
        chunks = await dense.search(
            session=db_session,
            query_dense=state["dense_vec"],
            department_scope=state.get("department_scope"),
            category_scope=state.get("category_scope"),
            tags_scope=state.get("tags_scope"),
            top_k=settings.RETRIEVAL_RECALL_TOP_N
        )
    finally:
        if should_close:
            await db_session.close()

    cost_ms = int((time.perf_counter() - t0) * 1000)
    _emit_thinking(
        writer,
        name="稠密召回",
        status="已执行",
        duration_ms=cost_ms,
        count_change=f"{len(chunks)} 条",
        summary=f"HNSW 1024 维语义向量召回 Top-{len(chunks)}"
    )

    return {
        "candidates": chunks,
        "trace": [{
            "node": edges.NODE_DENSE,
            "duration_ms": cost_ms,
            "count": len(chunks),
            "status": "success"
        }]
    }

async def sparse_node(
    state: RetrievalState,
    writer: StreamWriter,
    config: RunnableConfig
) -> Dict[str, Any]:
    """阶段 2b: 归一化稀疏向量内积召回"""
    _emit_thinking(writer, name="稀疏召回", status="执行中...", summary="计算 JSONB 稀疏特征与 L2 内积打分...")
    t0 = time.perf_counter()

    db_session, should_close = await _resolve_db_session(config)
    try:
        chunks = await sparse.search(
            session=db_session,
            query_sparse=state["sparse_vec"],
            department_scope=state.get("department_scope"),
            category_scope=state.get("category_scope"),
            tags_scope=state.get("tags_scope"),
            top_k=settings.RETRIEVAL_RECALL_TOP_N
        )
    finally:
        if should_close:
            await db_session.close()

    cost_ms = int((time.perf_counter() - t0) * 1000)
    _emit_thinking(
        writer,
        name="稀疏召回",
        status="已执行",
        duration_ms=cost_ms,
        count_change=f"{len(chunks)} 条",
        summary=f"L2 归一化稀疏内积召回 Top-{len(chunks)}"
    )

    return {
        "candidates": chunks,
        "trace": [{
            "node": edges.NODE_SPARSE,
            "duration_ms": cost_ms,
            "count": len(chunks),
            "status": "success"
        }]
    }

async def bm25_node(
    state: RetrievalState,
    writer: StreamWriter,
    config: RunnableConfig
) -> Dict[str, Any]:
    """阶段 2c: 全文倒排索引召回"""
    _emit_thinking(writer, name="关键词召回", status="执行中...", summary="检索 PostgreSQL TSVector GIN 倒排索引...")
    t0 = time.perf_counter()

    db_session, should_close = await _resolve_db_session(config)
    try:
        chunks = await bm25.search(
            session=db_session,
            tsquery_tokens=state["tsquery_tokens"],
            department_scope=state.get("department_scope"),
            category_scope=state.get("category_scope"),
            tags_scope=state.get("tags_scope"),
            top_k=settings.RETRIEVAL_RECALL_TOP_N
        )
    finally:
        if should_close:
            await db_session.close()

    cost_ms = int((time.perf_counter() - t0) * 1000)
    _emit_thinking(
        writer,
        name="关键词召回",
        status="已执行",
        duration_ms=cost_ms,
        count_change=f"{len(chunks)} 条",
        summary=f"PostgreSQL TSVector 倒排索引召回 Top-{len(chunks)}"
    )

    return {
        "candidates": chunks,
        "trace": [{
            "node": edges.NODE_BM25,
            "duration_ms": cost_ms,
            "count": len(chunks),
            "status": "success"
        }]
    }

async def hyde_node(
    state: RetrievalState,
    writer: StreamWriter,
    config: RunnableConfig
) -> Dict[str, Any]:
    """阶段 3: 假设性搜索 (HyDE) 增强"""
    if not state.get("enable_hyde", True):
        # 未开启 HyDE 假想增强时不向流中推入该步骤，保持时间线纯粹
        return {"candidates": [], "trace": [{"node": edges.NODE_HYDE, "status": "skipped"}]}

    _emit_thinking(writer, name="假设性搜索", status="执行中...", summary="大模型正在构思撰写假想技术解答并二次召回...")
    t0 = time.perf_counter()

    db_session, should_close = await _resolve_db_session(config)
    try:
        chunks = await hyde.search(
            session=db_session,
            query=state["rewritten_query"],
            department_scope=state.get("department_scope"),
            category_scope=state.get("category_scope"),
            tags_scope=state.get("tags_scope"),
            top_k=settings.HYDE_TOP_N
        )
    finally:
        if should_close:
            await db_session.close()

    cost_ms = int((time.perf_counter() - t0) * 1000)
    status_label = "已执行" if chunks else "已熔断/跳过"
    summary_text = (
        f"专家假想技术文档二次召回 Top-{len(chunks)}"
        if chunks
        else f"假想问答超时熔断保护，跳过本路"
    )
    _emit_thinking(
        writer,
        name="假设性搜索",
        status=status_label,
        duration_ms=cost_ms,
        count_change=f"{len(chunks)} 条",
        summary=summary_text
    )

    return {
        "candidates": chunks,
        "trace": [{
            "node": edges.NODE_HYDE,
            "duration_ms": cost_ms,
            "count": len(chunks),
            "status": "success" if chunks else "fallback"
        }]
    }

# ==================== 阶段 4：RRF 融合 ====================

async def fuse_node(
    state: RetrievalState,
    writer: StreamWriter
) -> Dict[str, Any]:
    """阶段 4: 加权倒数排名动态融合与物理去重"""
    _emit_thinking(
        writer,
        name="RRF 融合",
        status="执行中...",
        summary=f"场景 '{state.get('scene_type', 'general')}' 动态加权倒数排名融合去重中..."
    )
    t0 = time.perf_counter()

    raw_candidates = state.get("candidates", [])
    total_candidates = len(raw_candidates)

    fused = fusion.fuse(
        candidates=raw_candidates,
        scene_type=state.get("scene_type", "general"),
        k=settings.RRF_K,
        top_n=settings.FUSE_TOP_N
    )

    cost_ms = int((time.perf_counter() - t0) * 1000)
    _emit_thinking(
        writer,
        name="RRF 融合",
        status="已执行",
        duration_ms=cost_ms,
        count_change=f"{total_candidates} -> {len(fused)} 条",
        summary=f"场景 '{state.get('scene_type', 'general')}' 动态加权倒数排名去重融合"
    )

    return {
        "fused_chunks": fused,
        "trace": [{
            "node": edges.NODE_FUSE,
            "duration_ms": cost_ms,
            "input_count": total_candidates,
            "output_count": len(fused),
            "status": "success"
        }]
    }

# ==================== 阶段 5：交叉精排 ====================

async def rerank_node(
    state: RetrievalState,
    writer: StreamWriter
) -> Dict[str, Any]:
    """阶段 5: Cross-Encoder 深度精排与丢弃审计"""
    _emit_thinking(writer, name="重排", status="执行中...", summary="正在使用 BGE-Reranker-v2 交叉注意力深度打分...")
    t0 = time.perf_counter()

    fused = state.get("fused_chunks", [])
    reranked, dropped = await reranker_module.rerank(
        query=state["rewritten_query"],
        chunks=fused,
        top_n=settings.RERANK_TOP_N
    )

    cost_ms = int((time.perf_counter() - t0) * 1000)
    _emit_thinking(
        writer,
        name="重排",
        status="已执行",
        duration_ms=cost_ms,
        count_change=f"{len(fused)} -> {len(reranked)} 条",
        summary=f"Cross-Encoder 交叉注意力打分，检出丢弃审计 {len(dropped)} 条"
    )

    return {
        "reranked_chunks": reranked,
        "dropped_chunks": dropped,
        "trace": [{
            "node": edges.NODE_RERANK,
            "duration_ms": cost_ms,
            "input_count": len(fused),
            "output_count": len(reranked),
            "dropped_count": len(dropped),
            "status": "success"
        }]
    }

# ==================== 阶段 6：MMR 去重与溯源卡片 ====================

async def mmr_node(
    state: RetrievalState,
    writer: StreamWriter
) -> Dict[str, Any]:
    """阶段 6: 最大边际相关性去重与角标溯源卡片构建"""
    _emit_thinking(writer, name="MMR 去重", status="执行中...", summary="正在使用最大边际相关性贪心筛选互补黄金切片...")
    t0 = time.perf_counter()

    reranked = state.get("reranked_chunks", [])
    final_chunks = mmr.mmr_select(
        query_dense=state["dense_vec"],
        chunks=reranked,
        top_k=settings.FINAL_TOP_K,
        lambda_param=settings.MMR_LAMBDA,
        strip_vector=True
    )

    cost_ms = int((time.perf_counter() - t0) * 1000)

    # 构造前端专属的精确溯源卡片列表
    citations: List[Dict[str, Any]] = []
    for idx, c in enumerate(final_chunks):
        target_page = (c.get("page_idx") or 0) + 1
        doc_id = str(c["document_id"])
        chunk_id = str(c["chunk_id"])
        asset_url = c.get("asset_url")

        cit = {
            "citation_id": idx + 1,
            "chunk_id": chunk_id,
            "document_id": doc_id,
            "document_title": c.get("document_title", ""),
            "chunk_label": c.get("chunk_label", ""),
            "page_idx": c.get("page_idx", 0),
            "display_page": target_page,
            "bbox": c.get("bbox", []),
            "snippet": c.get("content", "")[:220].strip(),
            "asset_url": asset_url,
            "raw_oss_url": c.get("raw_oss_url"),
            "preview_url": f"/api/v1/documents/{doc_id}/preview",
            "page_render_url": f"/api/v1/documents/{doc_id}/pages/{target_page}",
            "asset_proxy_url": f"/api/v1/documents/chunks/{chunk_id}/asset" if asset_url else None,
            "score": c.get("score", 0.0)
        }
        citations.append(cit)

    # 主动向 SSE 流广播 citations 卡片事件
    if writer is not None:
        try:
            writer({"type": "citations", "citations": citations})
        except Exception:
            pass

    _emit_thinking(
        writer,
        name="MMR 去重",
        status="已执行",
        duration_ms=cost_ms,
        count_change=f"{len(reranked)} -> {len(final_chunks)} 条",
        summary=f"最大边际相关性贪心选择 (λ={settings.MMR_LAMBDA})，提炼 {len(final_chunks)} 条互补黄金切片"
    )

    return {
        "results": final_chunks,
        "citations": citations,
        "trace": [{
            "node": edges.NODE_MMR,
            "duration_ms": cost_ms,
            "input_count": len(reranked),
            "output_count": len(final_chunks),
            "status": "success"
        }]
    }
