"""src/rag_kb/api/routes.py —— 文档入库与流水线管控 HTTP 端点层。

前缀 /api/v1/documents，与前端 Vue 3 完全无缝对齐：
- POST /documents/upload: 接收文件上传，注册并启动后台 LangGraph 流水线
- GET /documents: 获取所有持久化及运行中文档列表
- DELETE /documents/{doc_id}: 物理删除文档及其切片
- GET /documents/{doc_id}/chunks: 查询指定文档切片详情
- GET /documents/{doc_id}/status: 获取流水线静态状态快照
- GET /documents/{doc_id}/events: SSE 长连接实时同步 11 节点执行流
- POST /documents/{doc_id}/review: 提交人工审核确认并通过 Command(resume) 唤醒 LangGraph
- GET /documents/{doc_id}/review-items/{item_id}/asset: 私有 OSS 切图反向代理
- GET /chunks/{chunk_id}/asset: 切片图元反向代理
- POST /documents/{doc_id}/resolve_duplicate: 防重冲突决策处理
- POST /documents/{doc_id}/retry: 流水线重试
"""

import uuid
import json
import hashlib
import asyncio
import logging
from pathlib import Path
from typing import List, Optional, AsyncGenerator, Dict, Any
from fastapi import APIRouter, UploadFile, File, Form, HTTPException, Header, Query, Depends, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession
from langgraph.types import Command

from rag_kb.api.deps import (
    UserContext,
    get_current_user,
    require_permission,
    require_permission_with_query_token,
    assert_doc_visible,
    is_path_under_prefix
)
from rag_kb.core.config import settings
from rag_kb.core.constants import IngestStatus, STEP_NAMES
from rag_kb.db.session import get_db, AsyncSessionLocal
from rag_kb.db.models import Document, DocumentChunk, Department, DocumentReviewItem
from rag_kb.graph.state import get_default_state, IngestState
from rag_kb.graph.build import get_ingest_graph
from rag_kb.utils.oss import oss_service
from rag_kb.api.schemas import (
    DocumentUploadResponse,
    DocumentItemSchema,
    ChunkItemSchema,
    PipelineSnapshotResponse,
    PipelineStepStatus,
    ReviewSubmitRequest,
    DuplicateResolveRequest,
    DuplicateResolveResponse,
    RetryPipelineRequest,
    ReviewItem
)
from rag_kb.api.sse import pipeline_manager, format_sse
from rag_kb.core.logging.audit import record_audit_log
from rag_kb.core.rate_limit import (
    enforce_user_rate_limit,
    upload_dedup_lock,
)
from rag_kb.graph.edges import can_safely_jump_to_step
from rag_kb.services.pipeline.ingest_queue import ingest_queue_service, cleanup_doc_temp_files

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/documents", tags=["文档管理与入库流水线"])
router_chunks = APIRouter(prefix="/chunks", tags=["切片资产"])

ALLOWED_EXTENSIONS = {".pdf", ".docx", ".pptx", ".md", ".txt"}

def _sse_business_payload(event: dict) -> str:
    payload = event.get("data")
    if event.get("event") and isinstance(payload, dict):
        return json.dumps(payload, default=str, ensure_ascii=False)
    return json.dumps(event, default=str, ensure_ascii=False)

async def _resolve_or_create_department(db: AsyncSession, department_path: str) -> Department:
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


async def _stream_read_upload_with_limit(
    upload_file: UploadFile,
    dest_path: Path,
    max_size_bytes: int,
) -> tuple[int, str]:
    """流式分块读取上传文件并同步计算 SHA-256，超出单文件上限立即 413 熔断，杜绝超大文件撑爆内存"""
    sha = hashlib.sha256()
    total_bytes = 0
    chunk_size = 1024 * 1024  # 1MB 分块

    try:
        with open(dest_path, "wb") as out_f:
            while True:
                chunk = await upload_file.read(chunk_size)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > max_size_bytes:
                    raise HTTPException(
                        status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                        detail=f"文件体积超出允许上限（最大 {settings.MAX_UPLOAD_SIZE_MB}MB），请拆分后重新上传",
                    )
                sha.update(chunk)
                out_f.write(chunk)
    except Exception:
        if dest_path.exists():
            try:
                dest_path.unlink()
            except OSError:
                pass
        raise

    if total_bytes == 0:
        if dest_path.exists():
            try:
                dest_path.unlink()
            except OSError:
                pass
        raise HTTPException(status_code=400, detail="不允许上传 0 字节的空文件")

    return total_bytes, sha.hexdigest()


# ==================== 1. 文档上传与受理 (Phase-8 先存后返 202 + Redis 队列异步削峰) ====================

@router.post("/upload", response_model=DocumentUploadResponse, status_code=status.HTTP_202_ACCEPTED)
async def upload_document(
    request: Request,
    file: UploadFile = File(...),
    department: Optional[str] = Form(None),
    category: str = Form("技术规范"),
    tags: str = Form("[]"),
    custom_file_name: Optional[str] = Form(None),
    skip_review: bool = Form(False),
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(require_permission("doc:upload"))
):
    """
    企业级高并发异步文档上传受理接口 (Phase-6 RBAC + Phase-8 Redis 限流与队列削峰)：
    1. Redis Lua 原子滑动窗口频率限流（超限返回 429 + Retry-After）；
    2. 严格校验 doc:upload 权限点与目标归属部门范围（BOLA 防越权）；
    3. 流式分块落盘校验体积与 SHA-256 + Redis 分布式防抖幂等锁；
    4. 真实上传阿里云 OSS 原件，写入 rag_documents(overall_status='queued', is_active=False)；
    5. XADD 投递至 Redis Streams 消费队列，立即响应 HTTP 202 Accepted 成功受理提示。
    """
    # 0. Redis 高并发上传频率限制
    await enforce_user_rate_limit(
        request=request,
        tenant_id=user.tenant_id,
        user_id=user.user_id,
        scope="doc_upload",
        limit=settings.RATE_LIMIT_UPLOAD_PER_MINUTE,
    )

    actual_file_name = custom_file_name.strip() if custom_file_name else file.filename
    ext = Path(actual_file_name).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件格式: {ext}。仅允许上传: {', '.join(ALLOWED_EXTENSIONS)}"
        )

    parsed_tags = []
    try:
        parsed_tags = json.loads(tags)
        if not isinstance(parsed_tags, list):
            parsed_tags = [str(tags)]
    except Exception:
        parsed_tags = [t.strip() for t in tags.split(",") if t.strip()]

    # 解析并严格校验目标归属部门路径 (防止跨部门伪造与根路径 "/" 空部门名脏数据)
    default_dept = "/public" if user.dept_path == "/" else user.dept_path
    raw_dept = department.strip() if (department and department.strip()) else default_dept
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
            status_code=403,
            detail=f"【越权访问拦截】您无权将文档上传至非授权部门目录: {department_path} (BOLA Blocked)"
        )

    doc_id = str(uuid.uuid4())
    temp_dir = Path(settings.DATA_PARSED_DIR) / "tmp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_file_path = temp_dir / f"{doc_id}_{actual_file_name}"

    max_bytes = max(1, int(settings.MAX_UPLOAD_SIZE_MB)) * 1024 * 1024
    file_size, file_hash = await _stream_read_upload_with_limit(file, temp_file_path, max_bytes)

    # Redis 分布式短时幂等锁：拦截前端连点重复提交同一文件
    try:
        async with upload_dedup_lock(department_path=department_path, file_sha256=file_hash):
            clean_dept_key = department_path.strip("/").replace("/", "_") or "public"
            target_oss_key = f"rag_storage/raw/{clean_dept_key}/{file_hash[:8]}_{actual_file_name}"

            try:
                # 1. 真实上传至阿里云 OSS（按需读取文件后立即由局部作用域释放，不驻留 State）
                def _upload_local_to_oss() -> str:
                    uploaded_key = oss_service.upload_file(str(temp_file_path), target_oss_key)
                    if str(uploaded_key).startswith("http"):
                        return str(uploaded_key)
                    clean_ep = oss_service.endpoint.replace("https://", "").replace("http://", "").rstrip("/")
                    return f"https://{oss_service.bucket_name}.{clean_ep}/{str(uploaded_key).lstrip('/')}"

                real_oss_url = await asyncio.to_thread(_upload_local_to_oss)

                # 2. 构造无二进制内存占用的轻量状态并写入数据库初始排队态 (overall_status='queued', is_active=False)
                initial_state = get_default_state(
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

                dept_obj = await _resolve_or_create_department(db, department_path)
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

                # 3. 推入真实 Redis Streams 异步消费队列（削峰填谷）
                stream_msg_id = await ingest_queue_service.enqueue_ingest_task(
                    doc_id=doc_id,
                    file_name=actual_file_name,
                    raw_oss_url=real_oss_url,
                    file_hash=file_hash,
                    department=department_path,
                    category=category,
                    tags=parsed_tags,
                    skip_review=skip_review,
                    temp_file_path=str(temp_file_path),
                )

                await db.commit()
                pipeline_manager.register_pipeline(doc_id, initial_state)
            except Exception:
                raise
    except Exception:
        # 捕获所有上传阶段异常（包括 HTTPException 与未预期的 OSS/DB 异常），彻底清理临时文件与半成品状态
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
        }
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
        oss_url=real_oss_url
    )

# ==================== 2. 文档列表与删除 ====================

@router.get("", response_model=List[DocumentItemSchema])
async def list_documents(
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(require_permission("kb:retrieve"))
):
    """获取所有已入库与正在入库的文档列表 (强制按当前员工权限前缀隔离)"""
    stmt = select(Document).order_by(Document.created_at.desc())

    # 非全局管理员强制施加部门树隔离
    if "/" not in user.allowed_department_prefixes:
        from sqlalchemy import or_
        dept_conds = [
            or_(
                Document.department_path == p.rstrip("/"),
                Document.department_path.startswith(p.rstrip("/") + "/")
            )
            for p in user.allowed_department_prefixes
        ]
        stmt = stmt.where(or_(*dept_conds))

    res = await db.execute(stmt)
    db_docs = res.scalars().all()

    # 统计当前可见文档各自的切片数
    chunk_counts: Dict[uuid.UUID, int] = {}
    if db_docs:
        from sqlalchemy import func
        doc_ids = [d.id for d in db_docs]
        cnt_stmt = (
            select(DocumentChunk.document_id, func.count(DocumentChunk.id))
            .where(DocumentChunk.document_id.in_(doc_ids))
            .group_by(DocumentChunk.document_id)
        )
        cnt_res = await db.execute(cnt_stmt)
        chunk_counts = {row[0]: int(row[1]) for row in cnt_res.all()}

    results = []
    seen_ids = set()

    for d in db_docs:
        seen_ids.add(str(d.id))
        if d.department_path in ("/public", "/"):
            dept_name = "公共知识库"
        elif d.department_path:
            dept_name = d.department_path.strip("/").split("/")[-1] or "公共知识库"
        else:
            dept_name = "技术部"
        results.append(DocumentItemSchema(
            doc_id=str(d.id),
            file_name=d.title,
            department=dept_name,
            category=d.category,
            tags=d.tags or [],
            status=d.overall_status,
            step_index=d.current_step_index or 11,
            chunk_count=chunk_counts.get(d.id, 0),
            created_at=d.created_at,
            updated_at=d.updated_at
        ))

    # 合并内存中活跃的流水线实例 (同样受部门树前缀过滤)
    for doc_id, state in pipeline_manager._active_states.items():
        if doc_id not in seen_ids:
            mem_dept = state.get("department", "")
            if "/" in user.allowed_department_prefixes or any(is_path_under_prefix(mem_dept, p) for p in user.allowed_department_prefixes):
                results.append(DocumentItemSchema(
                    doc_id=doc_id,
                    file_name=state.get("file_name", "unknown"),
                    department=mem_dept.split("/")[-1] if "/" in mem_dept else mem_dept,
                    category=state.get("category", "技术规范"),
                    tags=state.get("tags", []),
                    status=state.get("overall_status", "pending"),
                    step_index=state.get("current_step_index", 1),
                    created_at=None,
                    updated_at=None
                ))
                seen_ids.add(doc_id)

    return results

@router.delete("/{doc_id}")
async def delete_document(
    doc_id: str,
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(require_permission("doc:delete"))
):
    """物理删除文档及其全部切片 (Phase-6: 校验 doc:delete 且强制 assert_doc_visible 防越权)"""
    try:
        doc_uuid = uuid.UUID(doc_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的文档 UUID 格式")

    doc = await db.get(Document, doc_uuid)
    if not doc:
        raise HTTPException(status_code=404, detail="未找到该文档或已被删除")
    assert_doc_visible(user, doc)

    await db.execute(delete(DocumentChunk).where(DocumentChunk.document_id == doc_uuid))
    await db.execute(delete(Document).where(Document.id == doc_uuid))
    await db.commit()

    mem_temp_path = None
    if doc_id in pipeline_manager._active_states:
        mem_temp_path = pipeline_manager._active_states[doc_id].get("temp_file_path")
        del pipeline_manager._active_states[doc_id]
    cleanup_doc_temp_files(doc_id, mem_temp_path)

    await record_audit_log(
        action="DOCUMENT_DELETE",
        resource_id=doc_id,
        status="SUCCESS",
        details={"operator": user.username, "dept_path": user.dept_path}
    )

    return {"status": "success", "message": f"文档 {doc_id} 及其切片已彻底删除"}

# ==================== 3. 切片详情查询 ====================

@router.get("/{doc_id}/chunks", response_model=List[ChunkItemSchema])
async def get_document_chunks(
    doc_id: str,
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(require_permission("kb:retrieve"))
):
    """获取指定文档已入库的全部切片详情 (校验 kb:retrieve 并做 assert_doc_visible)"""
    try:
        doc_uuid = uuid.UUID(doc_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的文档 UUID 格式")

    doc = await db.get(Document, doc_uuid)
    if not doc:
        raise HTTPException(status_code=404, detail="未找到该文档")
    assert_doc_visible(user, doc)

    stmt = select(DocumentChunk).where(DocumentChunk.document_id == doc_uuid).order_by(DocumentChunk.chunk_index.asc())
    res = await db.execute(stmt)
    chunks = res.scalars().all()

    return [
        ChunkItemSchema(
            id=str(c.id),
            document_id=str(c.document_id),
            chunk_index=c.chunk_index,
            chunk_label=c.chunk_label,
            content=c.content,
            is_code=c.is_code,
            is_table=c.is_table,
            table_html=c.table_html,
            page_idx=c.page_idx,
            display_page=c.page_idx + 1,
            breadcrumb=c.breadcrumb or [],
            asset_url=f"/api/v1/chunks/{c.id}/asset" if getattr(c, "asset_url", None) else None,
            table_id=getattr(c, "table_id", None),
            sparse_token_count=len(c.sparse_vector) if c.sparse_vector else 0
        )
        for c in chunks
    ]

# ==================== 4. 状态快照与 SSE 流式推流 ====================

@router.get("/{doc_id}/status", response_model=PipelineSnapshotResponse)
async def get_pipeline_status(
    doc_id: str,
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(require_permission("kb:retrieve"))
):
    """获取流水线静态快照 (Phase-6: 校验权限并做 assert_doc_visible)"""
    snapshot = pipeline_manager.to_snapshot(doc_id)
    if snapshot:
        mem_state = pipeline_manager.get_state(doc_id) or {}
        mem_dept = mem_state.get("department", "")
        if "/" not in user.allowed_department_prefixes and not any(is_path_under_prefix(mem_dept, p) for p in user.allowed_department_prefixes):
            raise HTTPException(status_code=403, detail="【越权拦截】无权查看非本部门文档快照 (BOLA Blocked)")

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
                        asset_url=r.asset_url,
                        raw_oss_url=r.asset_url,
                        raw_content=r.raw_content,
                        vlm_description=r.vlm_description,
                        user_description=r.user_description,
                        remark=r.remark,
                        status=r.status
                    )
                    for r in db_reviews
                ]
        except Exception:
            pass
        return snapshot

    try:
        doc_uuid = uuid.UUID(doc_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="未找到该文档的流水线快照")

    stmt = select(Document).where(Document.id == doc_uuid)
    res = await db.execute(stmt)
    doc = res.scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail=f"未找到文档 {doc_id}")
    assert_doc_visible(user, doc)

    if doc.pipeline_steps and isinstance(doc.pipeline_steps, list) and len(doc.pipeline_steps) == len(STEP_NAMES):
        steps = [
            PipelineStepStatus(
                step_index=s.get("step_index", i + 1),
                name=s.get("name", STEP_NAMES[i]),
                status=s.get("status", "waiting"),
                duration_ms=int(s.get("duration_ms") or 0),
                error=s.get("error"),
                detail=s.get("detail"),
            )
            for i, s in enumerate(doc.pipeline_steps)
        ]
    else:
        steps = [
            PipelineStepStatus(
                step_index=i + 1,
                name=name,
                status="completed" if doc.overall_status == "active" else "waiting",
                duration_ms=0
            )
            for i, name in enumerate(STEP_NAMES)
        ]

    review_stmt = select(DocumentReviewItem).where(DocumentReviewItem.document_id == doc_uuid)
    review_res = await db.execute(review_stmt)
    db_reviews = review_res.scalars().all()

    pending_review_items = [
        ReviewItem(
            item_id=r.item_key,
            doc_id=str(r.document_id),
            type=r.type,
            title=r.title,
            page_idx=r.page_idx,
            display_page=r.display_page,
            line_number=r.line_number,
            asset_url=r.asset_url,
            raw_oss_url=r.asset_url,
            raw_content=r.raw_content,
            vlm_description=r.vlm_description,
            user_description=r.user_description,
            remark=r.remark,
            status=r.status
        )
        for r in db_reviews
    ]

    eff_step_idx = 11 if doc.overall_status == "active" else max(1, min(len(STEP_NAMES), int(doc.current_step_index or 1)))
    return PipelineSnapshotResponse(
        doc_id=doc_id,
        file_name=doc.title,
        overall_status="completed" if doc.overall_status == "active" else doc.overall_status,
        current_step_index=eff_step_idx,
        current_step_name=STEP_NAMES[eff_step_idx - 1],
        steps=steps,
        requires_review=(doc.overall_status == IngestStatus.REVIEW.value and len(pending_review_items) > 0),
        pending_reviews=pending_review_items
    )

@router.get("/{doc_id}/events")
async def stream_pipeline_events(
    doc_id: str,
    last_event_id: Optional[str] = Header(None, alias="Last-Event-ID"),
    from_event_id: Optional[int] = Query(None, description="支持 Query 参数重传历史事件"),
    authorization: Optional[str] = Header(None, alias="Authorization"),
    token: Optional[str] = Query(None)
):
    """
    SSE 长连接推流接口（实时同步 11 节点流转、审核挂起信号）
    支持标准 Last-Event-ID 机制进行断网重连与历史事件回放 (强制验签与防越权校验)
    """
    auth_header = authorization or (f"Bearer {token}" if token else None)
    user = await get_current_user(authorization=auth_header)
    if not user.has_permission("kb:retrieve"):
        raise HTTPException(status_code=403, detail="【权限不足】缺少 kb:retrieve 权限点")

    state = pipeline_manager.get_state(doc_id)
    if state:
        mem_dept = state.get("department", "")
        if "/" not in user.allowed_department_prefixes and not any(is_path_under_prefix(mem_dept, p) for p in user.allowed_department_prefixes):
            raise HTTPException(status_code=403, detail="【越权拦截】无权监听非本部门文档流 (BOLA Blocked)")
    else:
        try:
            doc_uuid = uuid.UUID(doc_id)
            async with AsyncSessionLocal() as session:
                db_doc = await session.get(Document, doc_uuid)
                if db_doc:
                    assert_doc_visible(user, db_doc)
        except ValueError:
            raise HTTPException(status_code=400, detail="无效的文档 UUID 格式")

    if not state and doc_id not in pipeline_manager._event_history:
        raise HTTPException(status_code=404, detail=f"未找到文档 ID 为 {doc_id} 的活跃流水线")

    event_queue = pipeline_manager.subscribe(doc_id)

    since_id = 0
    if last_event_id and last_event_id.isdigit():
        since_id = int(last_event_id)
    elif from_event_id is not None:
        since_id = from_event_id

    async def event_generator() -> AsyncGenerator[str, None]:
        # 首次连接下发初始快照
        if since_id == 0:
            snapshot = pipeline_manager.to_snapshot(doc_id)
            if snapshot:
                init_data = json.dumps(snapshot.model_dump(), default=str, ensure_ascii=False)
                yield f"id: 0\nevent: initial_state\ndata: {init_data}\n\n"
        else:
            # 回放历史断线事件
            for past_evt in pipeline_manager.get_history(doc_id):
                past_id = int(past_evt.get("id", 0))
                if past_id > since_id:
                    p_name = past_evt.get("event", "message")
                    p_data = _sse_business_payload(past_evt)
                    yield f"id: {past_id}\nevent: {p_name}\ndata: {p_data}\n\n"

        curr_status = state.get("overall_status") if state else None
        if curr_status in ["completed", "failed"]:
            return

        try:
            while True:
                event = await event_queue.get()
                event_id = event.get("id", "")
                event_name = event.get("event", "message")
                data_str = _sse_business_payload(event)
                yield f"id: {event_id}\nevent: {event_name}\ndata: {data_str}\n\n"

                if event_name in ["pipeline_completed", "pipeline_failed"]:
                    break
        except asyncio.CancelledError:
            pass
        finally:
            pipeline_manager.unsubscribe(doc_id, event_queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
    )

# ==================== 5. 人工审核提交与恢复 ====================

@router.post("/{doc_id}/review")
async def submit_pipeline_review(
    doc_id: str,
    request: ReviewSubmitRequest,
    user: UserContext = Depends(require_permission("doc:review")),
    db: AsyncSession = Depends(get_db)
):
    """
    提交人工审核结果并唤醒 LangGraph 流水线恢复执行 (Phase-6: 校验 doc:review 并做 assert_doc_visible)
    """
    try:
        doc_uuid = uuid.UUID(doc_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的文档 UUID 格式")

    state = pipeline_manager.get_state(doc_id)
    if state:
        mem_dept = state.get("department", "")
        if "/" not in user.allowed_department_prefixes and not any(is_path_under_prefix(mem_dept, p) for p in user.allowed_department_prefixes):
            raise HTTPException(status_code=403, detail="【越权拦截】无权审核非本部门文档 (BOLA Blocked)")
    else:
        doc = await db.get(Document, doc_uuid)
        if doc:
            assert_doc_visible(user, doc)
        else:
            raise HTTPException(status_code=404, detail=f"未找到文档 {doc_id} 的活跃流水线")

    if state and state.get("overall_status") != IngestStatus.REVIEW.value:
        raise HTTPException(status_code=400, detail="当前流水线并未处于待人工审核挂起状态")

    # 更新状态中的用户修改
    if state:
        update_map = {item.item_id: item for item in request.items}
        for rev in state.get("pending_reviews", []):
            item_id = rev.get("item_id")
            if item_id in update_map:
                up = update_map[item_id]
                rev["user_description"] = up.user_description
                rev["remark"] = up.remark
                rev["status"] = "modified" if up.user_description != rev.get("vlm_description") else "approved"

    # 后台受队列并发信号量保护注入 Command(resume=...) 唤醒流水线并完整同步 DB 终态
    resume_payload = [item.model_dump() for item in request.items]
    ingest_queue_service.schedule_background_resume(
        ingest_queue_service.resume_after_review(doc_id, resume_payload)
    )

    await pipeline_manager.broadcast(doc_id, "review_completed", {
        "doc_id": doc_id,
        "reviewed_items_count": len(request.items)
    })

    await record_audit_log(
        action="DOCUMENT_REVIEW",
        resource_id=doc_id,
        status="SUCCESS",
        details={
            "reviewed_items_count": len(request.items),
            "operator": user.username,
            "action": "resume"
        }
    )

    return {
        "status": "success",
        "message": "人工审核已确认，流水线已继续执行",
        "doc_id": doc_id
    }

# ==================== 6. 资产安全流式反向代理 ====================

@router.get("/{doc_id}/review-items/{item_id}/asset")
async def get_review_item_asset(
    doc_id: str,
    item_id: str,
    user: UserContext = Depends(require_permission_with_query_token("kb:retrieve")),
    db: AsyncSession = Depends(get_db)
):
    """人工审核多模态资产私有 OSS 安全流式反向代理 (挂载 kb:retrieve 鉴权与 assert_doc_visible)"""
    try:
        doc_uuid = uuid.UUID(doc_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的文档 UUID 格式")

    state = pipeline_manager.get_state(doc_id)
    if state:
        mem_dept = state.get("department", "")
        if "/" not in user.allowed_department_prefixes and not any(is_path_under_prefix(mem_dept, p) for p in user.allowed_department_prefixes):
            raise HTTPException(status_code=403, detail="【越权拦截】无权查看非本部门图元资产 (BOLA Blocked)")
    else:
        doc = await db.get(Document, doc_uuid)
        if doc:
            assert_doc_visible(user, doc)
        else:
            raise HTTPException(status_code=404, detail=f"未找到文档 {doc_id}")

    target_url = None
    if state:
        for r in state.get("pending_reviews", []):
            if r.get("item_id") == item_id:
                target_url = r.get("raw_oss_url") or r.get("asset_url")
                break
    else:
        rev_item = (await db.execute(
            select(DocumentReviewItem).where(DocumentReviewItem.document_id == doc_uuid, DocumentReviewItem.item_key == item_id)
        )).scalar_one_or_none()
        if rev_item:
            target_url = rev_item.asset_url

    if not target_url or not target_url.startswith("http"):
        raise HTTPException(status_code=404, detail="未找到该待审核图元或该项无关联切图")

    try:
        stream_gen, content_length, clean_key = oss_service.get_object_stream(target_url)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"拉取 OSS 资产流失败: {e}")

    ext = Path(clean_key).suffix.lower()
    media_type = "image/png" if ext == ".png" else "image/jpeg"
    headers = {"Cache-Control": "public, max-age=86400", "Content-Disposition": "inline"}
    if content_length:
        headers["Content-Length"] = str(content_length)

    return StreamingResponse(stream_gen, media_type=media_type, headers=headers)

@router_chunks.get("/{chunk_id}/asset")
async def get_chunk_asset(
    chunk_id: str,
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(require_permission_with_query_token("kb:retrieve"))
):
    """切片关联的多模态切图安全流式反向代理 (Phase-6: 校验关联主文档归属权限 BOLA)"""
    try:
        c_uuid = uuid.UUID(chunk_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的切片 UUID 格式")

    stmt = select(DocumentChunk).where(DocumentChunk.id == c_uuid)
    res = await db.execute(stmt)
    chunk = res.scalar_one_or_none()
    if not chunk or not chunk.asset_url:
        raise HTTPException(status_code=404, detail="切片无关联图元切图")

    doc = await db.get(Document, chunk.document_id)
    if not doc:
        raise HTTPException(status_code=404, detail="切片所属主文档不存在")
    assert_doc_visible(user, doc)

    target_url = chunk.asset_url
    try:
        stream_gen, content_length, clean_key = oss_service.get_object_stream(target_url)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"拉取 OSS 资产流失败: {e}")

    ext = Path(clean_key).suffix.lower()
    media_type = "image/png" if ext == ".png" else "image/jpeg"
    headers = {"Cache-Control": "public, max-age=86400", "Content-Disposition": "inline"}
    if content_length:
        headers["Content-Length"] = str(content_length)

    return StreamingResponse(stream_gen, media_type=media_type, headers=headers)

@router.get("/chunks/{chunk_id}/asset")
async def get_document_chunk_asset_alias(
    chunk_id: str,
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(require_permission_with_query_token("kb:retrieve"))
):
    """切片切图别名路由 (/api/v1/documents/chunks/{chunk_id}/asset)"""
    return await get_chunk_asset(chunk_id, db=db, user=user)

# ==================== 6.1 原文预览与单页高清渲染 ====================

import io
import pypdfium2 as pdfium
from urllib.parse import quote

_PDF_DOCUMENT_CACHE: Dict[str, bytes] = {}
_PDF_RENDER_SEM: Optional[asyncio.Semaphore] = None


def _get_pdf_render_sem() -> asyncio.Semaphore:
    global _PDF_RENDER_SEM
    if _PDF_RENDER_SEM is None:
        _PDF_RENDER_SEM = asyncio.Semaphore(max(1, int(settings.PDF_RENDER_CONCURRENCY)))
    return _PDF_RENDER_SEM


async def _fetch_pdf_bytes(raw_oss_url: str) -> bytes:
    """从 OSS 异步线程拉取 PDF 二进制并做受控 LRU 缓存"""
    if raw_oss_url in _PDF_DOCUMENT_CACHE:
        return _PDF_DOCUMENT_CACHE[raw_oss_url]

    def _download_sync() -> bytes:
        stream_gen, _, _ = oss_service.get_object_stream(raw_oss_url)
        return b"".join(stream_gen)

    pdf_bytes = await asyncio.to_thread(_download_sync)

    if len(_PDF_DOCUMENT_CACHE) >= 8:
        _PDF_DOCUMENT_CACHE.pop(next(iter(_PDF_DOCUMENT_CACHE)))
    _PDF_DOCUMENT_CACHE[raw_oss_url] = pdf_bytes
    return pdf_bytes


def _render_pdf_page_sync(pdf_bytes: bytes, page_num: int, scale: float) -> tuple[bytes, int]:
    """在独立工作线程执行 CPU 密集型 pypdfium2 渲染，严禁阻塞 FastAPI 主事件循环"""
    pdf = pdfium.PdfDocument(pdf_bytes)
    try:
        total_pages = len(pdf)
        if page_num < 1 or page_num > total_pages:
            raise IndexError(f"页码超出范围: 1 - {total_pages}")
        page = pdf[page_num - 1]
        pil_img = page.render(scale=scale).to_pil().convert("RGB")
        buf = io.BytesIO()
        pil_img.save(buf, format="PNG", optimize=True)
        return buf.getvalue(), total_pages
    finally:
        try:
            pdf.close()
        except Exception:
            pass

@router.get("/{doc_id}/preview")
async def preview_document(
    doc_id: str,
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(require_permission_with_query_token("kb:retrieve"))
):
    """文档安全流式预览接口 (Phase-6: 校验权限并做 assert_doc_visible)"""
    try:
        doc_uuid = uuid.UUID(doc_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的文档 UUID 格式")

    res = await db.execute(select(Document).where(Document.id == doc_uuid))
    doc = res.scalar_one_or_none()
    if not doc or not doc.raw_oss_url:
        raise HTTPException(status_code=404, detail="未找到文档或未关联 OSS 原文")
    assert_doc_visible(user, doc)

    try:
        stream_gen, content_length, clean_key = oss_service.get_object_stream(doc.raw_oss_url)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"拉取 OSS 失败: {e}")

    ext = Path(doc.title).suffix.lower()
    if ext == ".pdf":
        media_type = "application/pdf"
    elif ext in [".md", ".markdown", ".txt"]:
        media_type = "text/plain; charset=utf-8"
    else:
        media_type = "application/octet-stream"

    encoded_filename = quote(doc.title)
    headers = {
        "Content-Disposition": f"inline; filename*=utf-8''{encoded_filename}",
        "Accept-Ranges": "bytes",
        "Cache-Control": "public, max-age=3600"
    }
    if content_length:
        headers["Content-Length"] = str(content_length)

    return StreamingResponse(stream_gen, media_type=media_type, headers=headers)

@router.get("/{doc_id}/pages/{page_num}")
async def get_document_page_image(
    doc_id: str,
    page_num: int,
    scale: float = 2.0,
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(require_permission_with_query_token("kb:retrieve"))
):
    """
    文档单页超高精渲染接口 (Phase-6: 校验权限并做 assert_doc_visible)
    """
    try:
        doc_uuid = uuid.UUID(doc_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的文档 UUID 格式")

    res = await db.execute(select(Document).where(Document.id == doc_uuid))
    doc = res.scalar_one_or_none()
    if not doc or not doc.raw_oss_url:
        raise HTTPException(status_code=404, detail="文档未找到或未关联 OSS 原文")
    assert_doc_visible(user, doc)

    if not doc.title.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="单页超高精渲染仅支持 PDF 文档")

    pdf_bytes = await _fetch_pdf_bytes(doc.raw_oss_url)
    async with _get_pdf_render_sem():
        try:
            png_bytes, total_pages = await asyncio.to_thread(_render_pdf_page_sync, pdf_bytes, page_num, scale)
        except IndexError as idx_err:
            raise HTTPException(status_code=404, detail=str(idx_err))
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"渲染 PDF 单页失败: {e}")

    headers = {
        "Cache-Control": "public, max-age=86400",
        "X-Total-Pages": str(total_pages),
        "X-Current-Page": str(page_num)
    }
    return StreamingResponse(io.BytesIO(png_bytes), media_type="image/png", headers=headers)

# ==================== 7. 防重冲突与重试 ====================

@router.post("/{doc_id}/resolve_duplicate", response_model=DuplicateResolveResponse)
async def resolve_duplicate(
    doc_id: str,
    request: DuplicateResolveRequest,
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(require_permission("doc:upload"))
):
    """防重冲突解决决策接口 (Phase-6: 校验权限与 assert_doc_visible)"""
    try:
        doc_uuid = uuid.UUID(doc_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的文档 UUID 格式")

    state = pipeline_manager.get_state(doc_id)
    if state:
        mem_dept = state.get("department", "")
        if "/" not in user.allowed_department_prefixes and not any(is_path_under_prefix(mem_dept, p) for p in user.allowed_department_prefixes):
            raise HTTPException(status_code=403, detail="【越权拦截】无权操作非本部门文档 (BOLA Blocked)")
    else:
        doc = await db.get(Document, doc_uuid)
        if doc:
            assert_doc_visible(user, doc)
            raise HTTPException(status_code=400, detail=f"文档 {doc_id} 内存中无挂起的活跃流水线")
        else:
            raise HTTPException(status_code=404, detail=f"未找到文档 {doc_id} 的活跃流水线")

    if request.action == "overwrite":
        state["force_overwrite"] = True
    elif request.action == "create_new":
        state["force_overwrite"] = False
        state["skip_review"] = True
    elif request.action == "cancel":
        state["overall_status"] = "cancelled"
        doc = await db.get(Document, doc_uuid)
        if doc:
            doc.overall_status = "cancelled"
            doc.is_active = False
            await db.commit()
        cleanup_doc_temp_files(doc_id, state.get("temp_file_path"))
        await pipeline_manager.broadcast(doc_id, "pipeline_cancelled", {
            "doc_id": doc_id,
            "message": "用户取消重复文档入库"
        })
        return DuplicateResolveResponse(doc_id=doc_id, status="cancelled", message="已取消入库")

    # 受队列并发信号量保护唤醒继续完成第 11 步入库并同步 DB 终态
    ingest_queue_service.schedule_background_resume(
        ingest_queue_service.resume_after_duplicate_decision(doc_id, state)
    )
    return DuplicateResolveResponse(
        doc_id=doc_id,
        status="resumed",
        message=f"已按动作 {request.action} 恢复入库流水线"
    )

@router.post("/{doc_id}/retry")
async def retry_pipeline(
    doc_id: str,
    request: Optional[RetryPipelineRequest] = None,
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(require_permission("doc:upload"))
):
    """流水线失败引导重试 (Phase-6 权限校验 + Phase-8 重新推入 Redis Streams 消费队列受控执行)"""
    try:
        doc_uuid = uuid.UUID(doc_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的文档 UUID 格式")

    doc = await db.get(Document, doc_uuid)
    state = pipeline_manager.get_state(doc_id)
    if state:
        mem_dept = state.get("department", "")
        if "/" not in user.allowed_department_prefixes and not any(is_path_under_prefix(mem_dept, p) for p in user.allowed_department_prefixes):
            raise HTTPException(status_code=403, detail="【越权拦截】无权重试非本部门文档 (BOLA Blocked)")
    else:
        if doc:
            assert_doc_visible(user, doc)
            persisted_skip_review = bool(
                doc.pipeline_steps
                and isinstance(doc.pipeline_steps, list)
                and isinstance(doc.pipeline_steps[0], dict)
                and doc.pipeline_steps[0].get("skip_review", False)
            )
            state = get_default_state(
                doc_id=doc_id,
                file_name=doc.title,
                department=doc.department_path,
                category=doc.category,
                tags=doc.tags or [],
                skip_review=persisted_skip_review,
            )
            state["raw_oss_url"] = doc.raw_oss_url
            state["file_hash"] = doc.file_hash
            state["enhanced_md_oss_url"] = doc.enhanced_md_oss_url
            pipeline_manager.register_state(doc_id, state)
        else:
            raise HTTPException(status_code=404, detail=f"未找到文档 {doc_id} 的历史现场")

    requested_step = max(1, min(11, int(request.from_step))) if (request and request.from_step) else 1
    effective_from_step = (
        requested_step
        if (requested_step > 1 and can_safely_jump_to_step(state, requested_step))
        else 1
    )

    state["overall_status"] = IngestStatus.QUEUED.value
    state["current_step_index"] = effective_from_step
    state["error"] = None
    state["failed_step"] = None
    for s in state.get("steps", []):
        s_idx = s.get("step_index", 0)
        if s_idx >= effective_from_step or s.get("status") == "failed":
            s["status"] = "waiting"
            s["error"] = None
            s["duration_ms"] = 0

    if doc:
        doc.overall_status = IngestStatus.QUEUED.value
        doc.current_step_index = effective_from_step
        doc.pipeline_steps = state.get("steps", [])
        await db.commit()

    stream_msg_id = await ingest_queue_service.enqueue_ingest_task(
        doc_id=doc_id,
        file_name=state.get("file_name") or (doc.title if doc else "document.pdf"),
        raw_oss_url=state.get("raw_oss_url") or (doc.raw_oss_url if doc else ""),
        file_hash=state.get("file_hash") or (doc.file_hash if doc else ""),
        department=state.get("department") or (doc.department_path if doc else "/public"),
        category=state.get("category") or (doc.category if doc else "技术规范"),
        tags=state.get("tags") or (doc.tags if doc else []),
        skip_review=bool(state.get("skip_review", False)),
        temp_file_path=state.get("temp_file_path"),
        from_step=effective_from_step,
    )

    retry_hint = (
        f"已加入 Redis 异步队列，将直接从第 {effective_from_step} 节点断点续跑"
        if effective_from_step > 1
        else "已加入 Redis 异步队列，将从第 1 节点完整重新解析"
    )
    await pipeline_manager.broadcast(doc_id, "pipeline_queued", {
        "doc_id": doc_id,
        "stream_msg_id": stream_msg_id,
        "from_step": effective_from_step,
        "message": retry_hint,
    })

    await record_audit_log(
        action="DOCUMENT_RETRY",
        resource_id=doc_id,
        status="SUCCESS",
        details={
            "requested_from_step": requested_step,
            "effective_from_step": effective_from_step,
            "operator": user.username,
            "stream_msg_id": stream_msg_id,
        }
    )

    return {
        "doc_id": doc_id,
        "task_id": stream_msg_id,
        "status": "queued",
        "from_step": effective_from_step,
        "message": retry_hint,
    }
