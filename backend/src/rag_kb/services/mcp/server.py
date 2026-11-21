"""src/rag_kb/services/mcp/server.py —— 企业级多模态 RAG 公共能力 FastMCP 服务中台与核心业务单一事实源 (SSOT)。

对外注册 5 大核心 FastMCP 工具，同时作为 FastAPI HTTP 路由层 (api/routes.py, api/retrieval.py) 与检索/向量节点的唯一业务委托中心：
1. rag_resilient_llm    : 统一容灾大模型推理（主备毫秒级热切换 + 10min Java 密钥租约 + 输入输出安全护栏）
2. rag_hybrid_search    : 8 阶段多模态混合检索（服务端强制部门推导 + 全局 FINAL_TOP_K 对齐 + 剥离 raw_oss_url + 强制 PII 脱敏 + 防间接提示注入扫描）
3. rag_get_embeddings   : BGE-M3 1024 维稠密语义向量 + L2 归一化稀疏词频特征提取（零 API Key 暴露）
4. rag_document_ingest  : 支持文本 / Base64 二进制文件内容的异步削峰入库（OSS 直传 + Redis Streams 队列，默认不跳过人工审核）
5. rag_document_status  : 文档 11 步流水线状态快照与专业文档 [FORMAT_INVALID] 准入质检结果查询（含 BOLA 对象级防越权）
"""

import uuid
import time
import base64
import hashlib
import asyncio
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from fastmcp import FastMCP
from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from rag_kb.api.deps import (
    UserContext,
    assert_doc_visible,
    derive_secure_department_scope,
    is_path_under_prefix,
)
from rag_kb.api.schemas import (
    DocumentUploadResponse,
    PipelineSnapshotResponse,
    PipelineStepStatus,
    ReviewItem,
)
from rag_kb.api.sse import pipeline_manager
from rag_kb.core.config import settings
from rag_kb.core.constants import IngestStatus, Permissions, STEP_NAMES
from rag_kb.core.llm import llm_gateway
from rag_kb.core.logging.audit import record_audit_log
from rag_kb.core.logging.context import get_request_id
from rag_kb.core.rate_limit import upload_dedup_lock
from rag_kb.core.security import (
    default_ac_automaton,
    detect_credential_leak,
    mask_all_pii,
    validate_prompt_security,
    PromptSecurityError,
)
from rag_kb.db.models import Department, Document, DocumentReviewItem
from rag_kb.db.session import AsyncSessionLocal
from rag_kb.graph.state import get_default_state as get_default_ingest_state
from rag_kb.services.mcp.auth_guard import get_current_mcp_caller, mcp_secure_tool
from rag_kb.services.mcp.schemas import (
    ALLOWED_INGEST_EXTENSIONS,
    MCP_ALLOWED_SCENES,
    MCP_MAX_EMBED_BATCH,
    MCP_MAX_SEARCH_TOP_K,
    sanitize_mcp_chunk,
    sanitize_mcp_citation,
)
from rag_kb.utils import tokenize
from rag_kb.utils.embedding import embedding_service
from rag_kb.utils.oss import oss_service
from rag_search.build import get_retrieval_graph
from rag_search.state import get_default_state as get_default_retrieval_state

logger = logging.getLogger("rag_kb.mcp.server")

mcp_server = FastMCP(
    name="EnterpriseRAGService",
    instructions=(
        "企业级多模态 RAG 知识库公共能力中台：提供统一容灾大模型推理 (rag_resilient_llm)、"
        "8阶段多模态混合检索 (rag_hybrid_search)、稠密+稀疏特征向量提取 (rag_get_embeddings)、"
        "异步削峰文档入库 (rag_document_ingest) 与流水线状态查询 (rag_document_status)。"
    ),
)


# ==============================================================================
# 内部单一事实源 (SSOT) 共享核心实现 (供同进程模块与 FastMCP 工具共同复用)
# ==============================================================================

async def resolve_or_create_department(db: AsyncSession, department_path: str) -> Department:
    """根据已校验的合法部门路径查找或建档 Department 记录，保障 rag_documents 外键完整"""
    clean_path = "/" + department_path.strip("/") if department_path != "/" else "/public"
    res = await db.execute(select(Department).where(Department.path == clean_path))
    dept_obj = res.scalar_one_or_none()
    if dept_obj:
        return dept_obj

    pure_name = "公共知识库" if clean_path == "/public" else clean_path.strip("/").split("/")[-1]
    res_by_name = await db.execute(select(Department).where(Department.name == pure_name))
    existing_by_name = res_by_name.scalar_one_or_none()
    if existing_by_name:
        return existing_by_name

    dept_obj = Department(
        id=uuid.uuid4(),
        name=pure_name,
        path=clean_path,
    )
    db.add(dept_obj)
    await db.flush()
    return dept_obj


async def resolve_authorized_department_path(
    db: AsyncSession,
    user: UserContext,
    raw_department: Optional[str],
) -> str:
    """统一解析并严格校验用户上传文档的目标归属部门路径（BOLA 防越权）"""
    default_dept = "/public" if user.dept_path == "/" else user.dept_path
    raw_dept = raw_department.strip() if (raw_department and raw_department.strip()) else default_dept
    if raw_dept == "/":
        raw_dept = "/public"

    if not raw_dept.startswith("/"):
        dept_res = await db.execute(select(Department).where(Department.name == raw_dept))
        matched_dept = dept_res.scalar_one_or_none()
        if matched_dept:
            department_path = matched_dept.path
        elif user.data_scope == "ALL":
            department_path = f"/总公司/{raw_dept}"
        else:
            base_dept = user.dept_path.rstrip("/")
            if base_dept.endswith("/" + raw_dept) or base_dept == "/" + raw_dept:
                department_path = base_dept
            else:
                department_path = f"{base_dept}/{raw_dept}"
    else:
        department_path = "/" + raw_dept.strip("/")

    if not any(is_path_under_prefix(department_path, prefix) for prefix in user.allowed_department_prefixes):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"【越权访问拦截】您当前无权将文档上传至该部门目录: {department_path}",
        )
    return department_path


async def execute_core_embeddings_extraction(
    texts: List[str],
    include_sparse: bool = True,
    batch_size: int = 16,
) -> Tuple[List[List[float]], List[Dict[str, float]]]:
    """
    向量特征提取单一事实源 (SSOT)：
    统一计算 BGE-M3 1024 维稠密语义向量（自动换取 Java 10min 密钥租约 + 瞬态重试）与 Jieba L2 归一化稀疏词频特征。
    供 rag_get_embeddings 工具、入库流水线 EmbedderNode 与检索预处理节点 prepare_node 共同复用。
    """
    if not texts or not isinstance(texts, list):
        raise ValueError("texts 必须为非空字符串列表")

    cleaned_texts: List[str] = []
    for idx, t in enumerate(texts):
        if not isinstance(t, str) or not t.strip():
            raise ValueError(f"第 {idx} 条文本为空，严禁对空文本计算特征向量")
        cleaned_texts.append(t.strip())

    dense_embeddings = await embedding_service.embed_texts(cleaned_texts, batch_size=batch_size)
    sparse_vectors = (
        [tokenize.extract_sparse_vector(t) for t in cleaned_texts]
        if include_sparse
        else []
    )
    return dense_embeddings, sparse_vectors


async def execute_core_hybrid_retrieval(
    query: str,
    effective_department_scope: List[str],
    conversation_id: Optional[str] = None,
    history: Optional[List[Dict[str, str]]] = None,
    category_scope: Optional[str] = None,
    tags_scope: Optional[List[str]] = None,
    scene_type: str = "general",
    enable_hyde: bool = True,
    top_k: Optional[int] = None,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]], int]:
    """
    8 阶段多模态混合检索单一事实源 (SSOT)：
    统一构造检索图初始状态、流式执行 8 阶段 LangGraph 检索图并捕获思考流阶段帧 (thinking_steps)、
    引文卡片 (citations) 与精选切片 (final_chunks)。
    供 rag_hybrid_search 工具与 api/retrieval.py 共同调用。
    """
    t0 = time.perf_counter()
    initial_state = get_default_retrieval_state(
        query=query.strip(),
        conversation_id=conversation_id,
        history=history,
        department_scope=effective_department_scope,
        category_scope=category_scope,
        tags_scope=tags_scope,
        scene_type=scene_type,
        enable_hyde=enable_hyde,
        top_k=top_k,
    )

    graph = get_retrieval_graph()
    latest_state: Dict[str, Any] = dict(initial_state)
    thinking_steps: List[Dict[str, Any]] = []
    citations_raw: List[Dict[str, Any]] = []

    async for mode, chunk in graph.astream(initial_state, stream_mode=["custom", "values"]):
        if mode == "custom" and isinstance(chunk, dict):
            if chunk.get("type") == "thinking_step" and "step" in chunk:
                thinking_steps.append(chunk["step"])
            elif chunk.get("type") == "citations" and "citations" in chunk:
                citations_raw = chunk["citations"]
        elif mode == "values" and isinstance(chunk, dict):
            latest_state = chunk

    retrieve_ms = int((time.perf_counter() - t0) * 1000)
    if not citations_raw:
        citations_raw = latest_state.get("citations", [])

    return latest_state, thinking_steps, citations_raw, retrieve_ms


async def execute_core_document_ingest_from_temp_file(
    db: AsyncSession,
    user: UserContext,
    doc_id: str,
    actual_file_name: str,
    temp_file_path: Path,
    file_size: int,
    file_hash: str,
    department_path: str,
    category: str,
    parsed_tags: List[str],
    skip_review: bool,
    queue_service: Any = None,
) -> DocumentUploadResponse:
    """
    文档异步削峰入库单一事实源 (SSOT)：
    统一执行 Redis 分布式防重幂等锁 -> 阿里云 OSS 真实直传 -> PostgreSQL queued 初始记录 -> Redis Streams 异步入队。
    """
    try:
        if queue_service is not None:
            active_queue = queue_service
        else:
            from rag_kb.services.pipeline.ingest_queue import ingest_queue_service
            active_queue = ingest_queue_service

        async with upload_dedup_lock(department_path=department_path, file_sha256=file_hash):
            clean_dept_key = department_path.strip("/").replace("/", "_") or "public"
            target_oss_key = f"rag_storage/raw/{clean_dept_key}/{file_hash[:8]}_{actual_file_name}"

            def _upload_local_to_oss() -> str:
                uploaded_key = oss_service.upload_file(str(temp_file_path), target_oss_key)
                if str(uploaded_key).startswith("http"):
                    return str(uploaded_key)
                clean_ep = oss_service.endpoint.replace("https://", "").replace("http://", "").rstrip("/")
                return f"https://{oss_service.bucket_name}.{clean_ep}/{str(uploaded_key).lstrip('/')}"

            real_oss_url = await asyncio.to_thread(_upload_local_to_oss)

            initial_state = get_default_ingest_state(
                doc_id=doc_id,
                file_name=actual_file_name,
                file_bytes=None,
                temp_file_path=str(temp_file_path),
                department=department_path,
                category=category,
                tags=parsed_tags,
                skip_review=skip_review,
            )
            initial_state["raw_oss_url"] = real_oss_url
            initial_state["file_hash"] = file_hash
            initial_state["overall_status"] = IngestStatus.QUEUED.value

            dept_obj = await resolve_or_create_department(db, department_path)
            queued_doc = Document(
                id=uuid.UUID(doc_id),
                title=actual_file_name,
                file_hash=file_hash,
                raw_oss_url=real_oss_url,
                department_id=dept_obj.id,
                department_path=department_path,
                category=category,
                tags=parsed_tags,
                overall_status=IngestStatus.QUEUED.value,
                current_step_index=1,
                pipeline_steps=initial_state["steps"],
                version=1,
                is_active=False,
            )
            db.add(queued_doc)
            await db.flush()

            stream_msg_id = await active_queue.enqueue_ingest_task(
                doc_id=doc_id,
                file_name=actual_file_name,
                raw_oss_url=real_oss_url,
                file_hash=file_hash,
                department=department_path,
                category=category,
                tags=parsed_tags,
                skip_review=skip_review,
                temp_file_path=str(temp_file_path),
                user_id=user.user_id,
                tenant_id=user.tenant_id,
                request_id=get_request_id(),
            )

            await db.commit()
            pipeline_manager.register_pipeline(doc_id, initial_state)
    except Exception:
        if temp_file_path.exists():
            try:
                temp_file_path.unlink()
            except OSError:
                pass
        pipeline_manager.remove_pipeline(doc_id)
        try:
            await db.rollback()
        except Exception:
            pass
        raise

    await record_audit_log(
        action="DOCUMENT_UPLOAD",
        resource_id=doc_id,
        status="SUCCESS",
        details={
            "file_name": actual_file_name,
            "file_size": file_size,
            "file_hash": file_hash,
            "department": department_path,
            "category": category,
            "stream_msg_id": stream_msg_id,
        },
    )

    return DocumentUploadResponse(
        doc_id=doc_id,
        task_id=stream_msg_id,
        file_name=actual_file_name,
        file_size=file_size,
        file_hash=file_hash,
        department=department_path,
        category=category,
        tags=parsed_tags,
        status=IngestStatus.QUEUED.value,
        message="文档已成功存入云端并加入后台异步解析队列，可实时查看进度；若解析异常可一键重试。",
        oss_url=real_oss_url,
    )


async def build_core_document_status_snapshot(
    db: AsyncSession,
    user: UserContext,
    doc_id: str,
) -> PipelineSnapshotResponse:
    """
    文档 11 步流水线状态快照单一事实源 (SSOT)：
    统一执行内存态 + PostgreSQL 回表合成，并强制执行 assert_doc_visible BOLA 对象级防越权校验。
    """
    snapshot = pipeline_manager.to_snapshot(doc_id)
    if snapshot:
        mem_state = pipeline_manager.get_state(doc_id) or {}
        mem_dept = mem_state.get("department", "")
        if "/" not in user.allowed_department_prefixes and not any(
            is_path_under_prefix(mem_dept, p) for p in user.allowed_department_prefixes
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="【越权访问拦截】您无权查看其他部门的文档处理进度",
            )

        if snapshot.pending_reviews or snapshot.overall_status != "completed":
            return snapshot
        try:
            doc_uuid = uuid.UUID(doc_id)
            review_stmt = select(DocumentReviewItem).where(DocumentReviewItem.document_id == doc_uuid)
            review_res = await db.execute(review_stmt)
            db_reviews = review_res.scalars().all()
            if db_reviews:
                snapshot.pending_reviews = [
                    ReviewItem(
                        item_id=r.item_key,
                        doc_id=str(r.document_id),
                        type=r.type,
                        title=r.title,
                        page_idx=r.page_idx,
                        display_page=r.display_page,
                        line_number=r.line_number,
                        asset_url=f"/api/v1/documents/{doc_id}/review-items/{r.item_key}/asset" if r.asset_url else "",
                        raw_oss_url=r.asset_url,
                        raw_content=r.raw_content,
                        vlm_description=r.vlm_description,
                        user_description=r.user_description,
                        remark=r.remark,
                        status=r.status,
                    )
                    for r in db_reviews
                ]
                if len(snapshot.steps) >= 7 and snapshot.steps[6].status == "skipped":
                    snapshot.steps[6].detail = "已跳过人工审核"
        except Exception:
            pass
        return snapshot

    try:
        doc_uuid = uuid.UUID(doc_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="未找到该文档的流水线快照")

    stmt = select(Document).where(Document.id == doc_uuid)
    res = await db.execute(stmt)
    doc = res.scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"未找到文档 {doc_id}")
    assert_doc_visible(user, doc)

    review_stmt = select(DocumentReviewItem).where(DocumentReviewItem.document_id == doc_uuid)
    review_res = await db.execute(review_stmt)
    db_reviews = review_res.scalars().all()
    was_human_reviewed = any(r.status in ("approved", "modified") for r in db_reviews)
    skipped_detail_text = "已跳过人工审核" if len(db_reviews) > 0 else "无图表项，无需审核"

    if doc.pipeline_steps and isinstance(doc.pipeline_steps, list) and len(doc.pipeline_steps) == len(STEP_NAMES):
        steps = []
        for i, s in enumerate(doc.pipeline_steps):
            raw_st = s.get("status", "waiting")
            detail_val = s.get("detail")
            if i == 6 and doc.overall_status == "active" and raw_st == "waiting" and not was_human_reviewed:
                raw_st = "skipped"
                detail_val = skipped_detail_text
            steps.append(
                PipelineStepStatus(
                    step_index=s.get("step_index", i + 1),
                    name=s.get("name", STEP_NAMES[i]),
                    status=raw_st,
                    duration_ms=int(s.get("duration_ms") or 0),
                    error=s.get("error"),
                    detail=detail_val,
                )
            )
    else:
        steps = []
        for i, name in enumerate(STEP_NAMES):
            if doc.overall_status == "active":
                st_val = "skipped" if (i == 6 and not was_human_reviewed) else "completed"
            else:
                st_val = "waiting"
            steps.append(
                PipelineStepStatus(
                    step_index=i + 1,
                    name=name,
                    status=st_val,
                    duration_ms=0,
                    detail=skipped_detail_text if st_val == "skipped" else None,
                )
            )

    pending_review_items = [
        ReviewItem(
            item_id=r.item_key,
            doc_id=str(r.document_id),
            type=r.type,
            title=r.title,
            page_idx=r.page_idx,
            display_page=r.display_page,
            line_number=r.line_number,
            asset_url=f"/api/v1/documents/{doc_id}/review-items/{r.item_key}/asset" if r.asset_url else "",
            raw_oss_url=r.asset_url,
            raw_content=r.raw_content,
            vlm_description=r.vlm_description,
            user_description=r.user_description,
            remark=r.remark,
            status=r.status,
        )
        for r in db_reviews
    ]

    eff_step_idx = (
        11
        if doc.overall_status == "active"
        else max(1, min(len(STEP_NAMES), int(doc.current_step_index or 1)))
    )
    return PipelineSnapshotResponse(
        doc_id=doc_id,
        file_name=doc.title,
        overall_status="completed" if doc.overall_status == "active" else doc.overall_status,
        current_step_index=eff_step_idx,
        current_step_name=STEP_NAMES[eff_step_idx - 1],
        steps=steps,
        requires_review=(doc.overall_status == IngestStatus.REVIEW.value and len(pending_review_items) > 0),
        pending_reviews=pending_review_items,
    )


# ==============================================================================
# 对外暴露的 5 大核心 FastMCP 标准工具
# ==============================================================================

@mcp_server.tool(name="rag_resilient_llm")
@mcp_secure_tool(required_perm=Permissions.KB_RETRIEVE.value)
async def rag_resilient_llm(
    prompt: str,
    system_prompt: Optional[str] = None,
    temperature: float = 0.2,
    max_tokens: int = 2048,
) -> Dict[str, Any]:
    """
    工具 1：企业级高可用容灾大模型推理。
    底层复用全局单例 llm_gateway (ResilientLLMGateway)，具备主备多模型毫秒级热切换、
    10min Java 密钥短期租约、SLA 滑动窗口监控预警，以及输入输出全链路合规审查。
    """
    caller = get_current_mcp_caller()
    if not prompt or not prompt.strip():
        raise ValueError("prompt 不能为空")

    max_budget = max(1, int(settings.MCP_MAX_TOKENS_LIMIT))
    if max_tokens < 1 or max_tokens > max_budget:
        raise ValueError(f"max_tokens ({max_tokens}) 超出允许范围 [1, {max_budget}]")

    # 1. 输入前置安全护栏（拦截 Prompt 越狱注入与 5 万敏感词库）
    is_safe, err_msg = validate_prompt_security(prompt)
    if not is_safe:
        raise PromptSecurityError(f"【安全合规拦截】{err_msg}")
    if system_prompt and system_prompt.strip():
        sys_safe, sys_err = validate_prompt_security(system_prompt)
        if not sys_safe:
            raise PromptSecurityError(f"【安全合规拦截】system_prompt 违规: {sys_err}")

    messages: List[Dict[str, Any]] = []
    if system_prompt and system_prompt.strip():
        messages.append({"role": "system", "content": system_prompt.strip()})
    messages.append({"role": "user", "content": prompt.strip()})

    # 2. 委托全局单例 llm_gateway 执行弹性推理
    extra_llm_kwargs: Dict[str, Any] = {}
    if settings.VLM_REASONING_EFFORT and settings.VLM_REASONING_EFFORT.strip():
        extra_llm_kwargs["reasoning_effort"] = settings.VLM_REASONING_EFFORT.strip()

    raw_reply = await llm_gateway.chat(
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        **extra_llm_kwargs,
    )

    # 3. 输出侧凭据防泄漏硬熔断 + 敏感词审查 + PII 隐私脱敏
    leaked_cred = detect_credential_leak(raw_reply)
    if leaked_cred:
        raise PromptSecurityError(f"【系统安全熔断】检测到输出包含高危系统凭据 ({leaked_cred})，已阻断返回")

    ac_hits = default_ac_automaton.search(raw_reply)
    if ac_hits:
        raise PromptSecurityError("【安全合规拦截】模型生成内容命中敏感词护栏，已阻断返回")

    sanitized_reply = mask_all_pii(raw_reply)

    return {
        "request_id": caller.request_id,
        "content": sanitized_reply,
    }


@mcp_server.tool(name="rag_hybrid_search")
@mcp_secure_tool(required_perm=Permissions.KB_RETRIEVE.value)
async def rag_hybrid_search(
    query: str,
    top_k: Optional[int] = None,
    department_scope: Optional[List[str]] = None,
    category_scope: Optional[str] = None,
    tags_scope: Optional[List[str]] = None,
    scene_type: str = "general",
    enable_hyde: bool = True,
) -> Dict[str, Any]:
    """
    工具 2：企业级 8 阶段多模态混合检索（Dense + Sparse + BM25 + HyDE + RRF + BGE Rerank + MMR）。
    默认复用全局 settings.FINAL_TOP_K 与 enable_hyde=True，共用 SCENE_WEIGHT_PROFILES 场景清单；
    内置服务端强制部门范围推导、输入越狱拦截、剥离原始 OSS URL、强制 PII 隐私脱敏与防间接提示注入扫描。
    """
    caller = get_current_mcp_caller()
    if not query or not query.strip():
        raise ValueError("检索 query 不能为空")

    effective_top_k = int(settings.FINAL_TOP_K) if top_k is None else int(top_k)
    if effective_top_k < 1 or effective_top_k > MCP_MAX_SEARCH_TOP_K:
        raise ValueError(f"top_k ({effective_top_k}) 必须在 1 ~ {MCP_MAX_SEARCH_TOP_K} 之间")

    if scene_type not in MCP_ALLOWED_SCENES:
        raise ValueError(f"不支持的 scene_type: {scene_type}，仅支持 {sorted(MCP_ALLOWED_SCENES)}")

    # 1. 输入前置安全护栏
    is_safe, err_msg = validate_prompt_security(query)
    if not is_safe:
        raise PromptSecurityError(f"【安全合规拦截】{err_msg}")

    # 2. 服务端从 JWT 身份强制推导合法部门前缀范围（跨部门请求立即抛出 403 -> PermissionError）
    effective_dept_scope = derive_secure_department_scope(caller.user, department_scope)

    final_state, _, citations_raw, retrieve_ms = await execute_core_hybrid_retrieval(
        query=query.strip(),
        effective_department_scope=effective_dept_scope,
        category_scope=category_scope,
        tags_scope=tags_scope,
        scene_type=scene_type,
        enable_hyde=enable_hyde,
        top_k=effective_top_k,
    )

    raw_chunks = (final_state.get("results") or [])[:effective_top_k]
    raw_citations = (citations_raw or [])[:effective_top_k]

    safe_chunks = [sanitize_mcp_chunk(c) for c in raw_chunks]
    safe_citations = [sanitize_mcp_citation(cit) for cit in raw_citations]

    return {
        "request_id": caller.request_id,
        "rewritten_query": final_state.get("rewritten_query", query.strip()),
        "effective_department_scope": effective_dept_scope,
        "total_retrieve_ms": retrieve_ms,
        "citations": safe_citations,
        "chunks": safe_chunks,
    }


@mcp_server.tool(name="rag_get_embeddings")
@mcp_secure_tool(required_perm=Permissions.KB_RETRIEVE.value)
async def rag_get_embeddings(
    texts: List[str],
    include_sparse: bool = True,
) -> Dict[str, Any]:
    """
    工具 3：批量提取 BGE-M3 1024 维稠密语义特征向量与 Jieba L2 归一化稀疏词频特征字典。
    底层自动向 Java 鉴权中心换取 10min 模型密钥租约并带瞬态退避重试，实现零密钥暴露调用。
    """
    caller = get_current_mcp_caller()
    if not texts or not isinstance(texts, list):
        raise ValueError("texts 必须为非空字符串列表")
    if len(texts) > MCP_MAX_EMBED_BATCH:
        raise ValueError(f"单次批量向量提取上限为 {MCP_MAX_EMBED_BATCH} 条，当前传入 {len(texts)} 条")

    dense_embeddings, sparse_vectors = await execute_core_embeddings_extraction(
        texts=texts,
        include_sparse=include_sparse,
        batch_size=16,
    )

    return {
        "request_id": caller.request_id,
        "model": settings.EMBEDDING_MODEL,
        "dimension": settings.EMBEDDING_DIM,
        "count": len(dense_embeddings),
        "dense_embeddings": dense_embeddings,
        "sparse_vectors": sparse_vectors,
    }


@mcp_server.tool(name="rag_document_ingest")
@mcp_secure_tool(required_perm=Permissions.DOC_UPLOAD.value)
async def rag_document_ingest(
    file_name: str,
    content: str,
    is_base64: bool = False,
    department: Optional[str] = None,
    category: str = "技术规范",
    tags: Optional[List[str]] = None,
    skip_review: bool = False,
) -> Dict[str, Any]:
    """
    工具 4：企业级异步削峰文档入库（支持纯文本或 Base64 编码的二进制文件内容）。
    默认 skip_review=False（与网页上传一致，不跳过人工审核）；
    复用体积上限熔断、Redis 防抖幂等锁、阿里云 OSS 真实直传与 Redis Streams 队列异步削峰，立即返回 queued。
    """
    caller = get_current_mcp_caller()
    clean_name = (file_name or "").strip()
    if not clean_name:
        raise ValueError("file_name 不能为空")

    ext = Path(clean_name).suffix.lower()
    if ext not in ALLOWED_INGEST_EXTENSIONS:
        raise ValueError(
            f"不支持的文件格式: {ext}。仅允许: {', '.join(sorted(ALLOWED_INGEST_EXTENSIONS))}"
        )

    if content is None or len(str(content)) == 0:
        raise ValueError("content 不能为空")

    raw_str = str(content)
    if is_base64:
        try:
            raw_bytes = base64.b64decode(raw_str, validate=True)
        except Exception as e:
            raise ValueError(f"Base64 解码失败: {e}") from e
    else:
        raw_bytes = raw_str.encode("utf-8")

    file_size = len(raw_bytes)
    if file_size == 0:
        raise ValueError("不允许入库 0 字节的空文档")

    max_bytes = max(1, int(settings.MAX_UPLOAD_SIZE_MB)) * 1024 * 1024
    if file_size > max_bytes:
        raise ValueError(f"文档体积超出允许上限（最大 {settings.MAX_UPLOAD_SIZE_MB}MB）")

    file_hash = hashlib.sha256(raw_bytes).hexdigest()
    doc_id = str(uuid.uuid4())
    temp_dir = Path(settings.DATA_PARSED_DIR) / "tmp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_file_path = temp_dir / f"{doc_id}_{clean_name}"

    async with AsyncSessionLocal() as db:
        department_path = await resolve_authorized_department_path(db, caller.user, department)
        temp_file_path.write_bytes(raw_bytes)

        upload_resp = await execute_core_document_ingest_from_temp_file(
            db=db,
            user=caller.user,
            doc_id=doc_id,
            actual_file_name=clean_name,
            temp_file_path=temp_file_path,
            file_size=file_size,
            file_hash=file_hash,
            department_path=department_path,
            category=(category or "技术规范").strip(),
            parsed_tags=[str(t).strip() for t in (tags or []) if str(t).strip()],
            skip_review=bool(skip_review),
        )

    return {
        "request_id": caller.request_id,
        "status": upload_resp.status,
        "doc_id": upload_resp.doc_id,
        "task_id": upload_resp.task_id,
        "file_name": upload_resp.file_name,
        "file_size": upload_resp.file_size,
        "file_hash": upload_resp.file_hash,
        "department": upload_resp.department,
        "category": upload_resp.category,
        "tags": upload_resp.tags,
        "message": upload_resp.message,
    }


@mcp_server.tool(name="rag_document_status")
@mcp_secure_tool(required_perm=Permissions.KB_RETRIEVE.value)
async def rag_document_status(
    doc_id: str,
) -> Dict[str, Any]:
    """
    工具 5：查询文档 11 步入库流水线进度与异构数据分级清洗准入结果。
    强制执行 assert_doc_visible 对象级 BOLA 防越权校验；若专业文档触发 [FORMAT_INVALID] 准入拦截，原样返回重整原因。
    """
    caller = get_current_mcp_caller()
    clean_id = (doc_id or "").strip()
    if not clean_id:
        raise ValueError("doc_id 不能为空")

    async with AsyncSessionLocal() as db:
        snapshot = await build_core_document_status_snapshot(db=db, user=caller.user, doc_id=clean_id)

    # 提取顶层错误原因（特别是第 3 步专业文档 [FORMAT_INVALID] 准入质检拦截提示）
    top_error: Optional[str] = None
    mem_state = pipeline_manager.get_state(clean_id)
    if mem_state and mem_state.get("error"):
        top_error = str(mem_state["error"])
    if not top_error:
        for st in snapshot.steps:
            if st.error:
                top_error = st.error
                break

    return {
        "request_id": caller.request_id,
        "doc_id": snapshot.doc_id,
        "file_name": snapshot.file_name,
        "overall_status": snapshot.overall_status,
        "current_step_index": snapshot.current_step_index,
        "current_step_name": snapshot.current_step_name,
        "requires_review": snapshot.requires_review,
        "error": top_error,
        "steps": [
            {
                "step_index": s.step_index,
                "name": s.name,
                "status": s.status,
                "duration_ms": s.duration_ms,
                "error": s.error,
                "detail": s.detail,
            }
            for s in snapshot.steps
        ],
    }


# ==============================================================================
# FastAPI 进程内挂载工厂
# ==============================================================================

_mcp_http_app = None


def get_mcp_http_app():
    """获取挂载至 FastAPI /mcp 路径的 StreamableHTTP ASGI 子应用单例"""
    global _mcp_http_app
    if _mcp_http_app is None:
        _mcp_http_app = mcp_server.http_app(path="/")
    return _mcp_http_app
