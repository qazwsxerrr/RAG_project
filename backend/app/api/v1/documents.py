import uuid
import json
import asyncio
import io
from pathlib import Path
from typing import List, Optional, Dict
import pypdfium2 as pdfium
from urllib.parse import quote
from fastapi import APIRouter, UploadFile, File, Form, HTTPException, BackgroundTasks, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from backend.app.core.database import get_db
from backend.app.core.oss import AliyunOSSService
from backend.app.models.document import Document, Department
from backend.app.models.chunk import DocumentChunk
from pydantic import BaseModel, Field
from backend.app.schemas.document import (
    DocumentUploadResponse,
    DocumentItemSchema,
    DepartmentItemSchema,
    ChunkItemSchema,
    DuplicateResolveRequest,
    DuplicateResolveResponse
)
from backend.app.services.pipeline.state_machine import pipeline_manager, CheckpointManager
from backend.app.services.pipeline.steps import pipeline_executor
from backend.app.services.pipeline.update_service import update_service
from backend.app.core.config import settings

router = APIRouter(prefix="/documents", tags=["文档入库与管理"])

# 初始部门树，支持动态打字新增 (满足实拍照 Img 01 需求)
DEPARTMENTS_STORE = [
    {"id": 1, "name": "技术部", "path": "/总公司/研发中心/技术部"},
    {"id": 2, "name": "产品部", "path": "/总公司/研发中心/产品部"},
    {"id": 3, "name": "运营部", "path": "/总公司/运营中心/运营部"},
    {"id": 4, "name": "人事行政部", "path": "/总公司/职能中心/人事行政部"},
    {"id": 5, "name": "财务部", "path": "/总公司/职能中心/财务部"},
]

ALLOWED_EXTENSIONS = {".pdf", ".docx", ".pptx", ".md", ".txt"}

@router.get("/departments", response_model=List[DepartmentItemSchema])
async def list_departments():
    """获取所有可用部门列表（支持前端下拉选择与动态匹配）"""
    return DEPARTMENTS_STORE

@router.post("/upload", response_model=DocumentUploadResponse)
async def upload_document(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    department: str = Form("技术部"),
    category: str = Form("技术规范"),
    tags: str = Form("[]"),
    custom_file_name: Optional[str] = Form(None),
    skip_review: bool = Form(False)
):
    """
    文档上传接入接口 (Phase 1 - 节点 1)
    - 校验格式白名单 (.pdf, .docx, .pptx, .md, .txt)
    - 支持部门动态输入新增 (Img 01)
    - 注册入库流水线并异步启动 11 节点处理
    """
    actual_file_name = custom_file_name.strip() if custom_file_name else file.filename
    ext = Path(actual_file_name).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件格式: {ext}。仅允许上传: {', '.join(ALLOWED_EXTENSIONS)}"
        )

    # 动态部门检测与自动挂载新增
    existing_dept = next((d for d in DEPARTMENTS_STORE if d["name"] == department), None)
    if not existing_dept:
        new_id = len(DEPARTMENTS_STORE) + 1
        new_dept = {
            "id": new_id,
            "name": department,
            "path": f"/总公司/动态部门/{department}"
        }
        DEPARTMENTS_STORE.append(new_dept)

    # 解析 tags JSON 或逗号分隔字符串
    parsed_tags = []
    try:
        parsed_tags = json.loads(tags)
        if not isinstance(parsed_tags, list):
            parsed_tags = [str(tags)]
    except Exception:
        parsed_tags = [t.strip() for t in tags.split(",") if t.strip()]

    # 生成全局唯一 doc_id
    doc_id = str(uuid.uuid4())
    file_bytes = await file.read()
    file_size = len(file_bytes)

    # 保存临时文件供解析引擎使用
    temp_dir = Path(settings.DATA_PARSED_DIR) / "tmp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_file_path = temp_dir / f"{doc_id}_{actual_file_name}"
    with open(temp_file_path, "wb") as f:
        f.write(file_bytes)

    # 初始化流水线实例
    instance = pipeline_manager.create_instance(
        doc_id=doc_id,
        file_name=actual_file_name,
        department=department,
        category=category,
        tags=parsed_tags
    )

    # 启动后台异步处理流水线（采用独立 Task 运行，避免阻塞上传 HTTP 响应返回）
    async def _safe_run():
        try:
            await pipeline_executor.run_pipeline(
                instance=instance,
                temp_file_path=str(temp_file_path),
                file_bytes=file_bytes,
                skip_review=skip_review
            )
        except Exception as err:
            import logging
            logging.getLogger(__name__).error(f"流水线异步任务失败 [doc_id={doc_id}]: {err}", exc_info=True)

    asyncio.create_task(_safe_run())

    # 计算 sha256 与真实 OSS 存储地址
    from backend.app.core.oss import oss_service
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

@router.get("", response_model=List[DocumentItemSchema])
async def list_documents(db: AsyncSession = Depends(get_db)):
    """获取所有已入库/正在入库的文档列表 (联合查询 PostgreSQL 与内存活跃流水线)"""
    stmt = select(Document).order_by(Document.created_at.desc())
    res = await db.execute(stmt)
    db_docs = res.scalars().all()

    results = []
    seen_ids = set()

    # 1. 遍历持久化文档
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

    # 2. 合并当前内存中正在运行但尚未完成入库的流水线实例
    instances = pipeline_manager.list_all()
    for inst in instances:
        if inst.doc_id not in seen_ids:
            results.append(DocumentItemSchema(
                doc_id=inst.doc_id,
                file_name=inst.file_name,
                department=inst.department,
                category=inst.category,
                tags=inst.tags,
                status=inst.overall_status,
                step_index=inst.current_step_index,
                created_at=inst.created_at,
                updated_at=inst.updated_at
            ))
            seen_ids.add(inst.doc_id)

    return results

@router.delete("/{doc_id}")
async def delete_document(doc_id: str, db: AsyncSession = Depends(get_db)):
    """删除文档记录及其切片与审核数据"""
    try:
        doc_uuid = uuid.UUID(doc_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的文档 UUID 格式")

    from backend.app.models.chunk import DocumentChunk
    from backend.app.models.document import DocumentReviewItem
    from sqlalchemy import delete
    await db.execute(delete(DocumentReviewItem).where(DocumentReviewItem.document_id == doc_uuid))
    await db.execute(delete(DocumentChunk).where(DocumentChunk.document_id == doc_uuid))
    await db.execute(delete(Document).where(Document.id == doc_uuid))
    await db.commit()

    pipeline_manager.remove_instance(doc_id)

    return {"status": "success", "message": "文档已成功删除", "doc_id": doc_id}

@router.get("/{doc_id}/chunks", response_model=List[ChunkItemSchema])
async def get_document_chunks(doc_id: str, db: AsyncSession = Depends(get_db)):
    """获取指定文档已切分入库的全部切片详情 (支持溯源定位与内容核对)"""
    try:
        doc_uuid = uuid.UUID(doc_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的文档 UUID 格式")

    # 优先从数据库查询已入库切片
    stmt = (
        select(DocumentChunk)
        .where(DocumentChunk.document_id == doc_uuid)
        .order_by(DocumentChunk.chunk_index.asc())
    )
    res = await db.execute(stmt)
    chunks = res.scalars().all()

    if chunks:
        return [
            ChunkItemSchema(
                id=str(c.id),
                document_id=str(c.document_id),
                chunk_index=c.chunk_index,
                chunk_label=c.chunk_label,
                content=c.content,
                is_code=c.is_code,
                is_table=c.is_table,
                page_idx=c.page_idx,
                display_page=c.page_idx + 1,
                breadcrumb=c.breadcrumb or [],
                asset_url=c.asset_url,
                table_html=c.table_html,
                sparse_token_count=len(c.sparse_vector) if isinstance(c.sparse_vector, dict) else 0
            )
            for c in chunks
        ]

    # 若未入库但在流水线内存上下文，返回上下文切片
    inst = pipeline_manager.get_instance(doc_id)
    if inst and "raw_chunks" in inst.context:
        raw_chunks = inst.context["raw_chunks"]
        return [
            ChunkItemSchema(
                id=None,
                document_id=doc_id,
                chunk_index=rc.chunk_index,
                chunk_label=rc.chunk_label,
                content=rc.content,
                is_code=rc.is_code,
                is_table=rc.is_table,
                page_idx=rc.page_idx,
                display_page=rc.page_idx + 1,
                breadcrumb=rc.breadcrumb or [],
                asset_url=rc.asset_url,
                table_id=rc.table_id,
                table_html=rc.table_html,
                sparse_token_count=0
            )
            for rc in raw_chunks
        ]

    raise HTTPException(status_code=404, detail="未找到该文档的切片数据")

@router.post("/{doc_id}/resolve_duplicate", response_model=DuplicateResolveResponse)
async def resolve_document_duplicate(
    doc_id: str,
    req: DuplicateResolveRequest,
    db: AsyncSession = Depends(get_db)
):
    """解决防重预警冲突 (用户决策覆盖升级还是作为独立新文档)"""
    inst = pipeline_manager.get_instance(doc_id)
    if not inst:
        raise HTTPException(status_code=404, detail=f"未找到运行中的文档流水线实例: {doc_id}")

    if inst.overall_status != "duplicate_warning":
        raise HTTPException(
            status_code=400,
            detail=f"当前文档状态为 {inst.overall_status}，非 duplicate_warning 状态，无需处理"
        )

    if req.action == "cancel":
        inst.overall_status = "cancelled"
        inst.update_step_status(11, "failed", error="用户主动取消重复文档入库")
        await inst.broadcast_event("pipeline_cancelled", {"doc_id": doc_id, "message": "用户主动取消入库"})
        return DuplicateResolveResponse(
            doc_id=doc_id,
            status="cancelled",
            message="已成功取消文档入库",
            version=1
        )

    # 准备恢复执行 Step 11 入库
    raw_chunks = inst.context.get("raw_chunks", [])
    dense_embeddings = inst.context.get("dense_embeddings", [])
    sparse_vectors = inst.context.get("sparse_vectors", [])
    tsv_strings = inst.context.get("tsv_strings", [])
    dedup_res = inst.context.get("dedup_res", {})

    if not raw_chunks or not dense_embeddings:
        raise HTTPException(status_code=500, detail="流水线上下文切片或向量数据丢失，无法恢复入库")

    # 确定部门
    dept_stmt = select(Department).where(Department.name == inst.department)
    dept_res = await db.execute(dept_stmt)
    dept_obj = dept_res.scalar_one_or_none()
    if not dept_obj:
        dept_obj = Department(
            id=uuid.uuid4(),
            name=inst.department,
            path=f"/总公司/动态部门/{inst.department}"
        )
        db.add(dept_obj)
        await db.flush()

    if req.action == "overwrite":
        # 覆盖升级: 版本递增，旧版本下线
        old_id_str = req.matched_doc_id or dedup_res.get("matched_doc_id")
        old_doc_id = uuid.UUID(old_id_str) if old_id_str else None
        target_version = (dedup_res.get("matched_version", 1)) + 1

        doc = await update_service.ingest_document_with_chunks(
            session=db,
            doc_id=uuid.UUID(doc_id),
            file_name=inst.file_name,
            file_hash=inst.context.get("sha256", ""),
            raw_oss_url=inst.context.get("raw_oss_url", ""),
            enhanced_md_oss_url=inst.context.get("enhanced_md_oss_url"),
            department_id=dept_obj.id,
            department_path=dept_obj.path,
            category=inst.category,
            tags=inst.tags,
            title_embedding=dedup_res.get("title_embedding", []),
            chunks=raw_chunks,
            dense_embeddings=dense_embeddings,
            sparse_vectors=sparse_vectors,
            tsv_strings=tsv_strings,
            target_version=target_version,
            old_doc_id=old_doc_id
        )

        inst.overall_status = "completed"
        inst.update_step_status(11, "completed")
        await inst.broadcast_event("pipeline_completed", {
            "doc_id": doc_id,
            "status": "completed",
            "version": target_version,
            "message": f"文档已成功覆盖升级至版本 v{target_version}"
        })

        return DuplicateResolveResponse(
            doc_id=doc_id,
            status="completed",
            message=f"已成功覆盖升级原文档，当前版本为 v{target_version}",
            version=target_version
        )

    elif req.action == "create_new":
        # 独立新建: 不下线旧文档
        doc = await update_service.ingest_document_with_chunks(
            session=db,
            doc_id=uuid.UUID(doc_id),
            file_name=inst.file_name,
            file_hash=inst.context.get("sha256", ""),
            raw_oss_url=inst.context.get("raw_oss_url", ""),
            enhanced_md_oss_url=inst.context.get("enhanced_md_oss_url"),
            department_id=dept_obj.id,
            department_path=dept_obj.path,
            category=inst.category,
            tags=inst.tags,
            title_embedding=dedup_res.get("title_embedding", []),
            chunks=raw_chunks,
            dense_embeddings=dense_embeddings,
            sparse_vectors=sparse_vectors,
            tsv_strings=tsv_strings,
            target_version=1,
            old_doc_id=None
        )

        inst.overall_status = "completed"
        inst.update_step_status(11, "completed")
        await inst.broadcast_event("pipeline_completed", {
            "doc_id": doc_id,
            "status": "completed",
            "version": 1,
            "message": "文档已作为全新独立文档成功入库"
        })

        return DuplicateResolveResponse(
            doc_id=doc_id,
            status="completed",
            message="已作为独立新文档入库",
            version=1
        )
    else:
        raise HTTPException(status_code=400, detail=f"不支持的决策动作: {req.action}")


@router.get("/{doc_id}/preview")
async def preview_document(
    doc_id: str,
    db: AsyncSession = Depends(get_db)
):
    """
    文档安全流式预览接口 (支持 PDF/MD 原生直接内联预览，免下载零落盘)
    - 验证文档 ID 与数据库元数据
    - 从真实阿里云 OSS 内存管道拉取字节流
    - 设置 Content-Disposition: inline 与 Content-Type: application/pdf
    - 严格遵循 AGENTS.md 准则，零本地临时落盘
    """
    try:
        doc_uuid = uuid.UUID(doc_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的文档 UUID 格式")

    res = await db.execute(select(Document).where(Document.id == doc_uuid))
    doc = res.scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail=f"未找到 ID 为 {doc_id} 的文档")

    if not doc.raw_oss_url:
        raise HTTPException(status_code=404, detail="该文档未关联有效的 OSS 原文路径")

    oss_service = AliyunOSSService()
    try:
        stream_gen, content_length, clean_key = oss_service.get_object_stream(doc.raw_oss_url)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"拉取 OSS 原文件流失败: {str(e)}")

    ext = Path(doc.title).suffix.lower()
    if ext == ".pdf":
        media_type = "application/pdf"
    elif ext in [".md", ".markdown", ".txt"]:
        media_type = "text/plain; charset=utf-8"
    elif ext in [".jpg", ".jpeg"]:
        media_type = "image/jpeg"
    elif ext == ".png":
        media_type = "image/png"
    else:
        media_type = "application/octet-stream"

    encoded_filename = quote(doc.title)
    headers = {
        "Content-Disposition": f"inline; filename*=utf-8''{encoded_filename}",
        "Accept-Ranges": "bytes",
        "Cache-Control": "public, max-age=3600",
    }
    if content_length:
        headers["Content-Length"] = str(content_length)

    return StreamingResponse(
        content=stream_gen,
        media_type=media_type,
        headers=headers
    )


_PDF_DOCUMENT_CACHE: Dict[str, bytes] = {}

async def _fetch_pdf_bytes(raw_oss_url: str) -> bytes:
    """从 OSS 内存流式拉取 PDF 原始字节并缓存至内存，避免重复拉取开销"""
    if raw_oss_url in _PDF_DOCUMENT_CACHE:
        return _PDF_DOCUMENT_CACHE[raw_oss_url]

    oss_service = AliyunOSSService()
    stream_gen, _, _ = oss_service.get_object_stream(raw_oss_url)
    chunks = []
    for chunk in stream_gen:
        chunks.append(chunk)
    pdf_bytes = b"".join(chunks)

    # 简单 LRU 控制缓存最多 10 份文档
    if len(_PDF_DOCUMENT_CACHE) > 10:
        _PDF_DOCUMENT_CACHE.pop(next(iter(_PDF_DOCUMENT_CACHE)))
    _PDF_DOCUMENT_CACHE[raw_oss_url] = pdf_bytes
    return pdf_bytes


@router.get("/{doc_id}/pages/{page_num}")
async def get_document_page_image(
    doc_id: str,
    page_num: int,
    scale: float = 2.0,
    db: AsyncSession = Depends(get_db)
):
    """
    文档单页超高精渲染接口 (返回 PNG 原生纯净图片流)
    - 解决不同浏览器 iframe 对 #page=N 锚点支持不一致（导致总是跳回第 1 页封面）的问题
    - 服务端直接使用 pypdfium2 高保真渲染对应页，100% 准确绝无偏差
    - 零落盘内存管道直出，符合 AGENTS.md 准则
    """
    try:
        doc_uuid = uuid.UUID(doc_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的文档 UUID 格式")

    res = await db.execute(select(Document).where(Document.id == doc_uuid))
    doc = res.scalar_one_or_none()
    if not doc or not doc.raw_oss_url:
        raise HTTPException(status_code=404, detail="文档未找到或未关联 OSS 原文")

    ext = Path(doc.title).suffix.lower()
    if ext != ".pdf":
        raise HTTPException(status_code=400, detail="单页渲染功能仅支持 PDF 文档")

    try:
        pdf_bytes = await _fetch_pdf_bytes(doc.raw_oss_url)
        pdf = pdfium.PdfDocument(pdf_bytes)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"加载 PDF 文档失败: {str(e)}")

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
        raise HTTPException(status_code=500, detail=f"渲染 PDF 单页失败: {str(e)}")

    headers = {
        "Cache-Control": "public, max-age=86400",
        "X-Total-Pages": str(total_pages),
        "X-Current-Page": str(page_num)
    }
    return StreamingResponse(buf, media_type="image/png", headers=headers)


@router.get("/chunks/{chunk_id}/asset")
async def get_chunk_asset(
    chunk_id: str,
    db: AsyncSession = Depends(get_db)
):
    """
    切片关联富媒体资产 (表格截屏/图表) 安全流式代理
    避免前端直连阿里云 OSS 导致私有桶 403 AccessDenied
    """
    try:
        cid = uuid.UUID(chunk_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的切片 UUID 格式")

    res = await db.execute(select(DocumentChunk).where(DocumentChunk.id == cid))
    chunk = res.scalar_one_or_none()
    if not chunk or not chunk.asset_url:
        raise HTTPException(status_code=404, detail="未找到该切片或该切片无关联媒体资产")

    oss_service = AliyunOSSService()
    try:
        stream_gen, content_length, _ = oss_service.get_object_stream(chunk.asset_url)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"拉取 OSS 资产流失败: {str(e)}")

    ext = Path(chunk.asset_url).suffix.lower()
    if ext in [".jpg", ".jpeg"]:
        media_type = "image/jpeg"
    elif ext == ".png":
        media_type = "image/png"
    elif ext == ".webp":
        media_type = "image/webp"
    else:
        media_type = "application/octet-stream"

    headers = {
        "Cache-Control": "public, max-age=86400",
        "Content-Disposition": "inline"
    }
    if content_length:
        headers["Content-Length"] = str(content_length)

    return StreamingResponse(stream_gen, media_type=media_type, headers=headers)


class PipelineRetryRequest(BaseModel):
    from_step: Optional[int] = Field(None, description="恢复执行的起始步骤编号 (如 9 代表从切分步骤续跑)")


@router.post("/{doc_id}/retry")
async def retry_pipeline(
    doc_id: str,
    payload: Optional[PipelineRetryRequest] = None
):
    """
    流水线断点重试接口：
    - 自动热重载 .env 与重置模型 Client（改了 API_KEY 无需重启后端服务）；
    - 支持内存实例重试与 Checkpoint 自动反序列化复活；
    - 支持从 Checkpoint 1 (Step 5 VLM / Step 6 表格) 或 Checkpoint 3 (Step 9 切分) 分层精准续跑。
    """
    # 1. 动态热重载 .env 配置与模型连接客户端 (保证运维换 Key 零重启即时生效)
    from backend.app.core.config import reload_settings
    from backend.app.services.parser.vlm_service import vlm_service
    reload_settings()
    vlm_service.reset_client()

    instance = pipeline_manager.get_instance(doc_id)
    if not instance:
        instance = CheckpointManager.restore_instance(doc_id)
        if not instance:
            raise HTTPException(status_code=404, detail=f"未找到文档 {doc_id} 的运行时实例或检查点快照")
        pipeline_manager._instances[doc_id] = instance

    # 确定续跑起始节点:
    # 优先采用显式传入的 from_step;
    # 若未指定, 优先使用实例失败的 failed_step, 其次使用 current_step_index, 兜底为 9
    target_step = None
    if payload and payload.from_step:
        target_step = payload.from_step
    elif getattr(instance, "failed_step", None):
        target_step = instance.failed_step
    elif getattr(instance, "current_step_index", None):
        target_step = instance.current_step_index
    else:
        target_step = 9

    async def _safe_resume():
        try:
            await pipeline_executor.resume_pipeline(instance=instance, from_step=target_step)
        except Exception as err:
            import logging
            logger = logging.getLogger(__name__)
            logger.error(f"流水线断点重试执行失败 [doc_id={doc_id}]: {err}", exc_info=True)

    asyncio.create_task(_safe_resume())

    return {
        "doc_id": doc_id,
        "status": "resumed",
        "from_step": target_step,
        "message": f"文档流水线已成功从第 {target_step} 步启动断点续跑"
    }
