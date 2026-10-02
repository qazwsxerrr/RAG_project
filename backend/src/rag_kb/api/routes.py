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
import asyncio
import logging
from pathlib import Path
from typing import List, Optional, AsyncGenerator, Dict, Any
from fastapi import APIRouter, UploadFile, File, Form, HTTPException, Header, Query, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession
from langgraph.types import Command

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

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/documents", tags=["文档管理与入库流水线"])
router_chunks = APIRouter(prefix="/chunks", tags=["切片资产"])

ALLOWED_EXTENSIONS = {".pdf", ".docx", ".pptx", ".md", ".txt"}

def _sse_business_payload(event: dict) -> str:
    payload = event.get("data")
    if event.get("event") and isinstance(payload, dict):
        return json.dumps(payload, default=str, ensure_ascii=False)
    return json.dumps(event, default=str, ensure_ascii=False)

# ==================== 1. 文档上传与受理 ====================

@router.post("/upload", response_model=DocumentUploadResponse)
async def upload_document(
    file: UploadFile = File(...),
    department: str = Form("技术部"),
    category: str = Form("技术规范"),
    tags: str = Form("[]"),
    custom_file_name: Optional[str] = Form(None),
    skip_review: bool = Form(False)
):
    """
    文档上传受理接口 (Phase 1 接入)
    - 校验格式白名单 (.pdf, .docx, .pptx, .md, .txt)
    - 生成唯一 doc_id 并保存临时文件
    - 注册 LangGraph 实例并在后台异步通过 astream(stream_mode="custom") 执行
    - 快速返回响应
    """
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

    doc_id = str(uuid.uuid4())
    file_bytes = await file.read()
    file_size = len(file_bytes)

    temp_dir = Path(settings.DATA_PARSED_DIR) / "tmp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_file_path = temp_dir / f"{doc_id}_{actual_file_name}"
    with open(temp_file_path, "wb") as f:
        f.write(file_bytes)

    # 构造并注册初始状态
    initial_state = get_default_state(
        doc_id=doc_id,
        file_name=actual_file_name,
        file_bytes=file_bytes,
        temp_file_path=str(temp_file_path),
        department=department,
        category=category,
        tags=parsed_tags,
        skip_review=skip_review
    )
    pipeline_manager.register_pipeline(doc_id, initial_state)

    # 后台异步启动 LangGraph 流式执行
    async def _safe_run_pipeline():
        config = {"configurable": {"thread_id": doc_id}}
        graph = get_ingest_graph()
        try:
            async for custom_chunk in graph.astream(initial_state, config=config, stream_mode="custom"):
                evt_name = custom_chunk.get("event", "message")
                await pipeline_manager.broadcast(doc_id, evt_name, custom_chunk)
        except Exception as err:
            logger.error(f"流水线异步任务失败 [doc_id={doc_id}]: {err}", exc_info=True)
            await pipeline_manager.broadcast(doc_id, "pipeline_failed", {
                "doc_id": doc_id,
                "error": str(err)
            })

    asyncio.create_task(_safe_run_pipeline())

    file_hash = oss_service.calculate_sha256(file_bytes)
    clean_endpoint = oss_service.endpoint.replace("https://", "").replace("http://", "").rstrip("/")
    real_oss_url = f"https://{oss_service.bucket_name}.{clean_endpoint}/rag_storage/raw/{department}/{file_hash[:8]}_{actual_file_name}"

    return DocumentUploadResponse(
        doc_id=doc_id,
        file_name=actual_file_name,
        file_size=file_size,
        file_hash=file_hash,
        department=department,
        category=category,
        tags=parsed_tags,
        status="ingested",
        oss_url=real_oss_url
    )

# ==================== 2. 文档列表与删除 ====================

@router.get("", response_model=List[DocumentItemSchema])
async def list_documents(db: AsyncSession = Depends(get_db)):
    """获取所有已入库与正在入库的文档列表"""
    stmt = select(Document).order_by(Document.created_at.desc())
    res = await db.execute(stmt)
    db_docs = res.scalars().all()

    results = []
    seen_ids = set()

    for d in db_docs:
        seen_ids.add(str(d.id))
        dept_name = d.department_path.split("/")[-1] if d.department_path else "技术部"
        results.append(DocumentItemSchema(
            doc_id=str(d.id),
            file_name=d.title,
            department=dept_name,
            category=d.category,
            tags=d.tags or [],
            status=d.overall_status,
            step_index=d.current_step_index or 11,
            created_at=d.created_at,
            updated_at=d.updated_at
        ))

    # 合并内存中活跃的流水线实例
    for doc_id, state in pipeline_manager._active_states.items():
        if doc_id not in seen_ids:
            results.append(DocumentItemSchema(
                doc_id=doc_id,
                file_name=state.get("file_name", "unknown"),
                department=state.get("department", "技术部"),
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
async def delete_document(doc_id: str, db: AsyncSession = Depends(get_db)):
    """物理删除文档及其全部切片"""
    try:
        doc_uuid = uuid.UUID(doc_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的文档 UUID 格式")

    await db.execute(delete(DocumentChunk).where(DocumentChunk.document_id == doc_uuid))
    await db.execute(delete(Document).where(Document.id == doc_uuid))
    await db.commit()

    if doc_id in pipeline_manager._active_states:
        del pipeline_manager._active_states[doc_id]

    return {"status": "success", "message": f"文档 {doc_id} 及其切片已彻底删除"}

# ==================== 3. 切片详情查询 ====================

@router.get("/{doc_id}/chunks", response_model=List[ChunkItemSchema])
async def get_document_chunks(doc_id: str, db: AsyncSession = Depends(get_db)):
    """获取指定文档已入库的全部切片详情"""
    try:
        doc_uuid = uuid.UUID(doc_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的文档 UUID 格式")

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
async def get_pipeline_status(doc_id: str, db: AsyncSession = Depends(get_db)):
    """获取流水线静态快照（用于页面加载与断线恢复）"""
    snapshot = pipeline_manager.to_snapshot(doc_id)
    if snapshot:
        # 如果内存快照已经有审核项，或流水线仍处于进行中/等待中，直接返回内存快照
        if snapshot.pending_reviews or snapshot.overall_status != "completed":
            return snapshot
        # 若流水线在内存中已完成但 pending_reviews 为空，尝试从数据库补充持久化的图元明细
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

    steps = [
        PipelineStepStatus(
            step_index=i + 1,
            name=name,
            status="completed" if doc.overall_status == "active" else "waiting",
            duration_ms=0
        )
        for i, name in enumerate(STEP_NAMES)
    ]

    # 从数据库反查该文档持久化的多模态图元审核记录
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

    return PipelineSnapshotResponse(
        doc_id=doc_id,
        file_name=doc.title,
        overall_status="completed" if doc.overall_status == "active" else doc.overall_status,
        current_step_index=11,
        current_step_name=STEP_NAMES[10],
        steps=steps,
        requires_review=False,
        pending_reviews=pending_review_items
    )

@router.get("/{doc_id}/events")
async def stream_pipeline_events(
    doc_id: str,
    last_event_id: Optional[str] = Header(None, alias="Last-Event-ID"),
    from_event_id: Optional[int] = Query(None, description="支持 Query 参数重传历史事件")
):
    """
    SSE 长连接推流接口（实时同步 11 节点流转、审核挂起信号）
    支持标准 Last-Event-ID 机制进行断网重连与历史事件回放
    """
    state = pipeline_manager.get_state(doc_id)
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
async def submit_pipeline_review(doc_id: str, request: ReviewSubmitRequest):
    """
    提交人工审核结果并唤醒 LangGraph 流水线恢复执行 (ReviewDialog 确认并继续)
    通过 LangGraph 原生 Command(resume=...) 注入审核确认数据，无损继续后续步骤
    """
    state = pipeline_manager.get_state(doc_id)
    if not state:
        raise HTTPException(status_code=404, detail=f"未找到文档 {doc_id} 的活跃流水线")

    if state.get("overall_status") != IngestStatus.REVIEW.value:
        raise HTTPException(status_code=400, detail="当前流水线并未处于待人工审核挂起状态")

    # 更新状态中的用户修改
    update_map = {item.item_id: item for item in request.items}
    for rev in state.get("pending_reviews", []):
        item_id = rev.get("item_id")
        if item_id in update_map:
            up = update_map[item_id]
            rev["user_description"] = up.user_description
            rev["remark"] = up.remark
            rev["status"] = "modified" if up.user_description != rev.get("vlm_description") else "approved"

    # 后台异步注入 Command(resume=...) 唤醒流水线并继续接收推流
    async def _safe_resume():
        config = {"configurable": {"thread_id": doc_id}}
        graph = get_ingest_graph()
        try:
            await pipeline_manager.broadcast(doc_id, "pipeline_resumed", {
                "doc_id": doc_id,
                "message": "人工审核已确认，流水线继续执行下一步"
            })
            resume_payload = [item.model_dump() for item in request.items]
            async for custom_chunk in graph.astream(Command(resume=resume_payload), config=config, stream_mode="custom"):
                evt_name = custom_chunk.get("event", "message")
                await pipeline_manager.broadcast(doc_id, evt_name, custom_chunk)
        except Exception as err:
            logger.error(f"流水线唤醒后执行失败 [doc_id={doc_id}]: {err}", exc_info=True)
            await pipeline_manager.broadcast(doc_id, "pipeline_failed", {
                "doc_id": doc_id,
                "error": str(err)
            })

    asyncio.create_task(_safe_resume())

    await pipeline_manager.broadcast(doc_id, "review_completed", {
        "doc_id": doc_id,
        "reviewed_items_count": len(request.items)
    })

    return {
        "status": "success",
        "message": "人工审核已确认，流水线已继续执行",
        "doc_id": doc_id
    }

# ==================== 6. 资产安全流式反向代理 ====================

@router.get("/{doc_id}/review-items/{item_id}/asset")
async def get_review_item_asset(doc_id: str, item_id: str):
    """人工审核多模态资产 (配图/表格切图) 私有 OSS 安全流式反向代理"""
    state = pipeline_manager.get_state(doc_id)
    if not state:
        raise HTTPException(status_code=404, detail=f"未找到文档 {doc_id} 的活跃流水线")

    target_url = None
    for r in state.get("pending_reviews", []):
        if r.get("item_id") == item_id:
            target_url = r.get("raw_oss_url") or r.get("asset_url")
            break

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
async def get_chunk_asset(chunk_id: str, db: AsyncSession = Depends(get_db)):
    """切片关联的多模态切图安全流式反向代理"""
    try:
        c_uuid = uuid.UUID(chunk_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的切片 UUID 格式")

    stmt = select(DocumentChunk).where(DocumentChunk.id == c_uuid)
    res = await db.execute(stmt)
    chunk = res.scalar_one_or_none()
    if not chunk or not chunk.asset_url:
        raise HTTPException(status_code=404, detail="切片无关联图元切图")

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
async def get_document_chunk_asset_alias(chunk_id: str, db: AsyncSession = Depends(get_db)):
    """切片切图别名路由 (/api/v1/documents/chunks/{chunk_id}/asset)"""
    return await get_chunk_asset(chunk_id, db=db)

# ==================== 6.1 原文预览与单页高清渲染 ====================

import io
import pypdfium2 as pdfium
from urllib.parse import quote

_PDF_DOCUMENT_CACHE: Dict[str, bytes] = {}

async def _fetch_pdf_bytes(raw_oss_url: str) -> bytes:
    """从 OSS 内存拉取 PDF 二进制并做 LRU 内存缓存"""
    if raw_oss_url in _PDF_DOCUMENT_CACHE:
        return _PDF_DOCUMENT_CACHE[raw_oss_url]

    stream_gen, _, _ = oss_service.get_object_stream(raw_oss_url)
    chunks = []
    for chunk in stream_gen:
        chunks.append(chunk)
    pdf_bytes = b"".join(chunks)

    if len(_PDF_DOCUMENT_CACHE) > 10:
        _PDF_DOCUMENT_CACHE.pop(next(iter(_PDF_DOCUMENT_CACHE)))
    _PDF_DOCUMENT_CACHE[raw_oss_url] = pdf_bytes
    return pdf_bytes

@router.get("/{doc_id}/preview")
async def preview_document(doc_id: str, db: AsyncSession = Depends(get_db)):
    """文档安全流式预览接口 (支持 PDF/MD 原生内联预览，零磁盘落盘)"""
    try:
        doc_uuid = uuid.UUID(doc_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的文档 UUID 格式")

    res = await db.execute(select(Document).where(Document.id == doc_uuid))
    doc = res.scalar_one_or_none()
    if not doc or not doc.raw_oss_url:
        raise HTTPException(status_code=404, detail="未找到文档或未关联 OSS 原文")

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
    db: AsyncSession = Depends(get_db)
):
    """
    文档单页超高精渲染接口 (返回 PNG 原生纯净图片流)
    解决不同浏览器 iframe 对 #page=N 锚点支持不一致问题，服务端使用 pypdfium2 高精度渲染
    """
    try:
        doc_uuid = uuid.UUID(doc_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的文档 UUID 格式")

    res = await db.execute(select(Document).where(Document.id == doc_uuid))
    doc = res.scalar_one_or_none()
    if not doc or not doc.raw_oss_url:
        raise HTTPException(status_code=404, detail="文档未找到或未关联 OSS 原文")

    if not doc.title.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="单页超高精渲染仅支持 PDF 文档")

    pdf_bytes = await _fetch_pdf_bytes(doc.raw_oss_url)
    try:
        pdf = pdfium.PdfDocument(pdf_bytes)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"加载 PDF 文档失败: {e}")

    total_pages = len(pdf)
    if page_num < 1 or page_num > total_pages:
        raise HTTPException(status_code=404, detail=f"页码超出范围: 1 - {total_pages}")

    try:
        page = pdf[page_num - 1]
        pil_img = page.render(scale=scale).to_pil().convert("RGB")
        buf = io.BytesIO()
        pil_img.save(buf, format="PNG", optimize=True)
        buf.seek(0)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"渲染 PDF 单页失败: {e}")

    headers = {
        "Cache-Control": "public, max-age=86400",
        "X-Total-Pages": str(total_pages),
        "X-Current-Page": str(page_num)
    }
    return StreamingResponse(buf, media_type="image/png", headers=headers)

# ==================== 7. 防重冲突与重试 ====================

@router.post("/{doc_id}/resolve_duplicate", response_model=DuplicateResolveResponse)
async def resolve_duplicate(doc_id: str, request: DuplicateResolveRequest):
    """防重冲突解决决策接口"""
    state = pipeline_manager.get_state(doc_id)
    if not state:
        raise HTTPException(status_code=404, detail=f"未找到文档 {doc_id} 的活跃流水线")

    if request.action == "overwrite":
        state["force_overwrite"] = True
    elif request.action == "create_new":
        state["force_overwrite"] = False
        state["skip_review"] = True
    elif request.action == "cancel":
        state["overall_status"] = IngestStatus.FAILED.value
        await pipeline_manager.broadcast(doc_id, "pipeline_cancelled", {
            "doc_id": doc_id,
            "message": "用户取消重复文档入库"
        })
        return DuplicateResolveResponse(doc_id=doc_id, status="cancelled", message="已取消入库")

    # 唤醒继续入库
    config = {"configurable": {"thread_id": doc_id}}
    graph = get_ingest_graph()

    async def _resume_store():
        try:
            async for custom_chunk in graph.astream(state, config=config, stream_mode="custom"):
                evt_name = custom_chunk.get("event", "message")
                await pipeline_manager.broadcast(doc_id, evt_name, custom_chunk)
        except Exception as e:
            logger.error(f"防重恢复执行失败 [doc_id={doc_id}]: {e}", exc_info=True)

    asyncio.create_task(_resume_store())
    return DuplicateResolveResponse(
        doc_id=doc_id,
        status="resumed",
        message=f"已按动作 {request.action} 恢复入库流水线"
    )

@router.post("/{doc_id}/retry")
async def retry_pipeline(doc_id: str, request: Optional[RetryPipelineRequest] = None):
    """流水线断点重试"""
    state = pipeline_manager.get_state(doc_id)
    if not state:
        raise HTTPException(status_code=404, detail=f"未找到文档 {doc_id} 的历史现场")

    state["overall_status"] = IngestStatus.INGEST.value
    state["error"] = None
    state["failed_step"] = None

    config = {"configurable": {"thread_id": doc_id}}
    graph = get_ingest_graph()

    async def _safe_retry():
        try:
            async for custom_chunk in graph.astream(state, config=config, stream_mode="custom"):
                evt_name = custom_chunk.get("event", "message")
                await pipeline_manager.broadcast(doc_id, evt_name, custom_chunk)
        except Exception as e:
            logger.error(f"流水线重试异常 [doc_id={doc_id}]: {e}", exc_info=True)

    asyncio.create_task(_safe_retry())
    return {
        "doc_id": doc_id,
        "status": "retrying",
        "from_step": request.from_step if request else 1,
        "message": "流水线已开始重试执行"
    }
