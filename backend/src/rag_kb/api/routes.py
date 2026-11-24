"""src/rag_kb/api/routes.py —— 文档入库、多维权限管控、单文档改密与流式预览 HTTP 端点层。

全面迁移至 rag_kb.core.authz 单一策略决策中心 (PDP)：
- 物理清除全部 8 处单部门前缀与 "/" 放行硬编码；
- 上传支持多部门库与跨部门 ALLOW UPLOAD 共享库及受控 L1-L4 密级；
- 新增 PATCH /api/v1/documents/{doc_id}/classification 单文档独立改密接口（含降密双权校验、租户版本递增与同事务安全审计）；
- PDF 分片预览短票、单页高清渲染（含命中 _PDF_DOCUMENT_CACHE 时）与切图代理均执行对象级实时复核 + L3/L4 交付前强制留痕。
"""

import io
import re
import uuid
import json
import zlib
import struct
import hashlib
import asyncio
import logging
from pathlib import Path
from urllib.parse import quote
from typing import List, Optional, AsyncGenerator, Dict, Any, Tuple

import numpy as np
import pypdfium2 as pdfium
from fastapi import APIRouter, UploadFile, File, Form, HTTPException, Header, Query, Depends, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select, delete, func, text
from sqlalchemy.ext.asyncio import AsyncSession

from rag_kb.api.deps import (
    EffectiveAuthzContext,
    get_current_user_with_db,
    require_permission,
    issue_stream_ticket,
    verify_stream_ticket,
    get_session_auth_error,
    publish_tenant_policy_epoch_to_redis,
    SESSION_AUTH_ERROR_DETAILS,
)
from rag_kb.api.schemas import (
    DocumentUploadResponse,
    DocumentItemSchema,
    DocumentClassificationUpdateRequest,
    DocumentClassificationUpdateResponse,
    ChunkItemSchema,
    PipelineSnapshotResponse,
    ReviewSubmitRequest,
    DuplicateResolveRequest,
    DuplicateResolveResponse,
    RetryPipelineRequest,
    StreamTicketRequest,
    StreamTicketResponse,
)
from rag_kb.api.sse import pipeline_manager, format_sse
from rag_kb.core.authz import (
    apply_document_authz_join_and_filter,
    assert_doc_access,
    assert_pipeline_state_access,
    audit_high_classification_read,
    invalidate_authz_cache,
    list_accessible_kbs,
    resolve_authorized_target_kb,
    validate_requested_kb_ids,
)
from rag_kb.core.config import settings
from rag_kb.core.constants import IngestStatus, KbAction, Permissions
from rag_kb.core.logging.audit import (
    AuditWriteError,
    record_audit_log,
    record_denial_audit,
    record_security_audit_in_tx,
)
from rag_kb.core.logging.context import get_request_id
from rag_kb.core.rate_limit import enforce_user_rate_limit
from rag_kb.db.models import Document, DocumentChunk, DocumentReviewItem, KnowledgeBase
from rag_kb.db.session import get_db, AsyncSessionLocal
from rag_kb.graph.edges import can_safely_jump_to_step
from rag_kb.graph.state import get_default_state
from rag_kb.services.pipeline.dedup_service import render_duplicate_warning_for_viewer
from rag_kb.services.pipeline.ingest_queue import ingest_queue_service, cleanup_doc_temp_files
from rag_kb.utils.oss import oss_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/documents", tags=["文档管理与入库流水线"])
router_chunks = APIRouter(prefix="/chunks", tags=["切片资产"])

ALLOWED_EXTENSIONS = {".pdf", ".docx", ".pptx", ".md", ".txt"}
PRIVATE_NO_STORE_CACHE = "private, no-store"
_RANGE_HEADER_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


def _parse_http_byte_range(range_header: Optional[str], total_size: int) -> Optional[Tuple[int, int]]:
    if not range_header or total_size <= 0:
        return None
    m = _RANGE_HEADER_RE.match(range_header.strip())
    if not m:
        return None
    start_str, end_str = m.group(1), m.group(2)
    if not start_str and not end_str:
        return None
    if not start_str:
        suffix_len = int(end_str)
        if suffix_len <= 0:
            return None
        start = max(0, total_size - suffix_len)
        end = total_size - 1
    else:
        start = int(start_str)
        end = int(end_str) if end_str else (total_size - 1)
        if start >= total_size or start > end:
            raise HTTPException(
                status_code=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE,
                detail="Requested Range Not Satisfiable",
                headers={"Content-Range": f"bytes */{total_size}"},
            )
        end = min(end, total_size - 1)
    return start, end


async def _authenticate_bearer_or_ticket(
    doc_id: str,
    purpose: str,
    authorization: Optional[str],
    ticket: Optional[str],
    db: Optional[AsyncSession] = None,
) -> EffectiveAuthzContext:
    """支持标准 Authorization Bearer 头或单文档单用途 cv=2 短票 (?ticket=)，每次调用重验双版本与自然到期"""
    if authorization and authorization.strip():
        user = await get_current_user_with_db(authorization=authorization, db=db)
    elif ticket and ticket.strip():
        user = await verify_stream_ticket(ticket.strip(), expected_doc_id=doc_id, expected_purpose=purpose, db=db)
    else:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="【鉴权失败】缺少 Authorization 请求头或有效的单资源访问短票",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not user.has_permission(Permissions.KB_RETRIEVE.value):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"【权限不足】当前操作需要 [{Permissions.KB_RETRIEVE.value}] 权限点",
        )
    return user


async def _stream_read_upload_with_limit(
    upload_file: UploadFile,
    dest_path: Path,
    max_size_bytes: int,
) -> tuple[int, str]:
    sha = hashlib.sha256()
    total_bytes = 0
    chunk_size = 1024 * 1024

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


# ==================== 1. 文档上传与受理 ====================

@router.post("/upload", response_model=DocumentUploadResponse, status_code=status.HTTP_202_ACCEPTED)
async def upload_document(
    request: Request,
    file: UploadFile = File(...),
    kb_id: Optional[str] = Form(None),
    classification_level: Optional[int] = Form(None),
    category: str = Form("技术规范"),
    tags: str = Form("[]"),
    custom_file_name: Optional[str] = Form(None),
    skip_review: bool = Form(False),
    db: AsyncSession = Depends(get_db),
    user: EffectiveAuthzContext = Depends(require_permission(Permissions.DOC_UPLOAD)),
):
    """企业级多维权限异步文档上传受理接口（支持多部门库与跨部门 ALLOW UPLOAD 共享库及 L1-L4 独立密级）"""
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
            detail=f"不支持的文件格式: {ext}。仅允许上传: {', '.join(ALLOWED_EXTENSIONS)}",
        )

    parsed_tags = []
    try:
        parsed_tags = json.loads(tags)
        if not isinstance(parsed_tags, list):
            parsed_tags = [str(tags)]
    except Exception:
        parsed_tags = [t.strip() for t in tags.split(",") if t.strip()]

    target_kb, validated_level = await resolve_authorized_target_kb(
        db=db,
        ctx=user,
        kb_id=kb_id,
        classification_level=classification_level,
    )

    doc_id = str(uuid.uuid4())
    temp_dir = Path(settings.DATA_PARSED_DIR) / "tmp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_file_path = temp_dir / f"{doc_id}_{actual_file_name}"

    max_bytes = max(1, int(settings.MAX_UPLOAD_SIZE_MB)) * 1024 * 1024
    file_size, file_hash = await _stream_read_upload_with_limit(file, temp_file_path, max_bytes)

    from rag_kb.services.mcp.server import execute_core_document_ingest_from_temp_file

    return await execute_core_document_ingest_from_temp_file(
        db=db,
        user=user,
        doc_id=doc_id,
        actual_file_name=actual_file_name,
        temp_file_path=temp_file_path,
        file_size=file_size,
        file_hash=file_hash,
        target_kb=target_kb,
        classification_level=validated_level,
        category=category,
        parsed_tags=parsed_tags,
        skip_review=skip_review,
        queue_service=ingest_queue_service,
    )


# ==================== 2. 文档列表、单文档在线改密与删除 ====================

@router.get("", response_model=List[DocumentItemSchema])
async def list_documents(
    kb_id: Optional[str] = Query(None, description="按知识库 ID 收窄过滤"),
    classification_level: Optional[int] = Query(None, ge=1, le=4, description="按文档密级过滤"),
    db: AsyncSession = Depends(get_db),
    user: EffectiveAuthzContext = Depends(require_permission(Permissions.KB_RETRIEVE)),
):
    """获取当前员工有权 READ 的文档列表（基于单一策略决策中心 SQL 前置过滤 + 逐库动作能力标记）"""
    authorized_kb_ids = await validate_requested_kb_ids(db, user, [kb_id] if kb_id else None, action=KbAction.READ.value)

    # 加载逐库动作矩阵，用于前端精确展示单篇文档的可删除/可改密操作按钮
    accessible_kbs = await list_accessible_kbs(db, user, action=KbAction.READ.value)
    kb_caps = {str(item.kb.id): item for item in accessible_kbs}

    stmt = select(Document, KnowledgeBase).order_by(Document.created_at.desc())
    stmt = apply_document_authz_join_and_filter(
        stmt=stmt,
        ctx=user,
        action=KbAction.READ.value,
        kb_ids=authorized_kb_ids,
    )
    if classification_level is not None:
        stmt = stmt.where(
            func.greatest(KnowledgeBase.min_classification_level, Document.classification_level) == classification_level
        )

    rows = (await db.execute(stmt)).all()

    chunk_counts: Dict[uuid.UUID, int] = {}
    if rows:
        doc_ids = [d.id for d, _ in rows]
        cnt_stmt = (
            select(DocumentChunk.document_id, func.count(DocumentChunk.id))
            .where(DocumentChunk.document_id.in_(doc_ids))
            .group_by(DocumentChunk.document_id)
        )
        chunk_counts = {row[0]: int(row[1]) for row in (await db.execute(cnt_stmt)).all()}

    results: List[DocumentItemSchema] = []
    seen_ids = set()

    for d, kb in rows:
        doc_id_str = str(d.id)
        seen_ids.add(doc_id_str)
        kb_id_str = str(kb.id)
        cap = kb_caps.get(kb_id_str)
        eff_level = max(int(kb.min_classification_level), int(d.classification_level))

        if d.department_path in ("/public", "/"):
            dept_name = "公共知识库"
        elif d.department_path:
            dept_name = d.department_path.strip("/").split("/")[-1] or "公共知识库"
        else:
            dept_name = kb.name

        results.append(
            DocumentItemSchema(
                doc_id=doc_id_str,
                file_name=d.title,
                kb_id=kb_id_str,
                kb_name=kb.name,
                classification_level=int(d.classification_level),
                effective_classification_level=eff_level,
                can_delete=bool(cap.can_delete) if cap else False,
                can_manage=bool(cap.can_manage and user.has_permission(Permissions.SECURITY_CLASSIFY)) if cap else False,
                department=dept_name,
                category=d.category,
                tags=d.tags or [],
                status=d.overall_status,
                step_index=d.current_step_index or 11,
                chunk_count=chunk_counts.get(d.id, 0),
                created_at=d.created_at,
                updated_at=d.updated_at,
            )
        )

    # 合并内存中活跃的流水线实例（逐条通过 assert_pipeline_state_access 校验）
    for mem_doc_id, state in list(pipeline_manager._active_states.items()):
        if mem_doc_id in seen_ids:
            continue
        if authorized_kb_ids and str(state.get("kb_id") or "") not in {str(k) for k in authorized_kb_ids}:
            continue
        try:
            kb, eff_level = await assert_pipeline_state_access(db, user, state, KbAction.READ.value, audit_denial=False)
        except HTTPException:
            continue
        if classification_level is not None and eff_level != classification_level:
            continue
        mem_dept = state.get("department", kb.owner_dept_path)
        cap = kb_caps.get(str(kb.id))
        results.append(
            DocumentItemSchema(
                doc_id=mem_doc_id,
                file_name=state.get("file_name", "unknown"),
                kb_id=str(kb.id),
                kb_name=kb.name,
                classification_level=int(state.get("classification_level") or kb.default_doc_level),
                effective_classification_level=eff_level,
                can_delete=bool(cap.can_delete) if cap else False,
                can_manage=bool(cap.can_manage and user.has_permission(Permissions.SECURITY_CLASSIFY)) if cap else False,
                department=mem_dept.split("/")[-1] if "/" in mem_dept else mem_dept,
                category=state.get("category", "技术规范"),
                tags=state.get("tags", []),
                status=state.get("overall_status", "pending"),
                step_index=state.get("current_step_index", 1),
                created_at=None,
                updated_at=None,
            )
        )
        seen_ids.add(mem_doc_id)

    return results


@router.patch("/{doc_id}/classification", response_model=DocumentClassificationUpdateResponse)
async def update_document_classification(
    doc_id: str,
    req: DocumentClassificationUpdateRequest,
    db: AsyncSession = Depends(get_db),
    user: EffectiveAuthzContext = Depends(require_permission(Permissions.SECURITY_CLASSIFY)),
):
    """
    单文档独立改密接口（严格遵循方案 §3.3.4）：
    1. 校验真实所属库的 MANAGE 权限、security:classify 权限、操作者可访问原密级；
    2. 目标密级必须落入目标知识库允许区间 [min, max] 且不超过操作者自身有效密级；
    3. 若为降密 (target < current)，额外强制要求 security:declassify 权限点；
    4. 同事务更新文档密级、递增租户 policy_epoch 并落盘安全审计（任一步失败整体回滚）；
    5. 事务提交后发布最新 policy_epoch 至 Redis，立即驱逐旧版本 JWT 与预览短票！
    """
    try:
        doc_uuid = uuid.UUID(doc_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的文档 UUID 格式")

    doc = (
        await db.execute(
            select(Document).where(Document.id == doc_uuid).with_for_update()
        )
    ).scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="未找到该文档")

    # 要求对真实所属库具备 MANAGE 且当前操作者能访问原密级（防止盲改本人不可读的高密文档）
    kb, prev_effective_level = await assert_doc_access(db, user, doc, KbAction.MANAGE.value)

    target_level = int(req.classification_level)
    prev_level = int(doc.classification_level)

    if target_level < int(kb.min_classification_level) or target_level > int(kb.max_doc_level):
        await record_denial_audit(
            action="DOCUMENT_CLASSIFICATION_DENIED",
            resource_id=doc_id,
            error_msg=f"目标密级 L{target_level} 超出知识库区间 [L{kb.min_classification_level}, L{kb.max_doc_level}]",
            details={"kb_id": str(kb.id), "target_level": target_level},
            tenant_id=user.tenant_id,
            user_id=user.user_id,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"【密级边界拦截】目标密级 L{target_level} 超出所属知识库允许区间 [L{kb.min_classification_level}, L{kb.max_doc_level}]",
        )

    if target_level > int(user.effective_clearance):
        await record_denial_audit(
            action="DOCUMENT_CLASSIFICATION_DENIED",
            resource_id=doc_id,
            error_msg=f"目标密级 L{target_level} 高于操作者有效密级 L{user.effective_clearance}",
            details={"kb_id": str(kb.id), "target_level": target_level, "user_clearance": user.effective_clearance},
            tenant_id=user.tenant_id,
            user_id=user.user_id,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"【密级边界拦截】不能将文档设置为高于操作者自身密级 (L{user.effective_clearance}) 的密级 L{target_level}",
        )

    if target_level < prev_level and not user.has_permission(Permissions.SECURITY_DECLASSIFY):
        await record_denial_audit(
            action="DOCUMENT_DECLASSIFY_DENIED",
            resource_id=doc_id,
            error_msg="缺少 security:declassify 降密权限",
            details={"kb_id": str(kb.id), "previous_level": prev_level, "target_level": target_level},
            tenant_id=user.tenant_id,
            user_id=user.user_id,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="【降密拦截】将文档密级调低需要额外的 [security:declassify] 权限点",
        )

    try:
        doc.classification_level = target_level
        epoch_res = await db.execute(
            text(
                "INSERT INTO sys_tenant_policy_epochs (tenant_id, policy_epoch, updated_at) VALUES (:tid, 2, NOW()) "
                "ON CONFLICT (tenant_id) DO UPDATE "
                "SET policy_epoch = sys_tenant_policy_epochs.policy_epoch + 1, updated_at = NOW() "
                "RETURNING policy_epoch"
            ),
            {"tid": user.tenant_id},
        )
        new_epoch = int(epoch_res.scalar_one())

        new_effective_level = max(int(kb.min_classification_level), target_level)
        await record_security_audit_in_tx(
            db=db,
            action="DOCUMENT_CLASSIFICATION_UPDATE",
            resource_id=doc_id,
            status="SUCCESS",
            details={
                "operator": user.username,
                "kb_id": str(kb.id),
                "kb_name": kb.name,
                "previous_level": prev_level,
                "previous_effective_level": prev_effective_level,
                "target_level": target_level,
                "new_effective_level": new_effective_level,
                "reason": req.reason,
                "policy_epoch": new_epoch,
            },
            tenant_id=user.tenant_id,
            user_id=user.user_id,
        )
        await db.commit()
    except AuditWriteError as ae:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(ae))
    except Exception:
        await db.rollback()
        raise

    # 同步更新内存态流水线缓存并向 Redis 广播新租户策略纪元
    mem_state = pipeline_manager.get_state(doc_id)
    if mem_state is not None:
        mem_state["classification_level"] = target_level
    invalidate_authz_cache(tenant_id=user.tenant_id)
    await publish_tenant_policy_epoch_to_redis(user.tenant_id, new_epoch)

    return DocumentClassificationUpdateResponse(
        doc_id=doc_id,
        kb_id=str(kb.id),
        previous_level=prev_level,
        classification_level=target_level,
        effective_classification_level=new_effective_level,
        policy_epoch=new_epoch,
        message=f"文档密级已从 L{prev_level} 更新为 L{target_level}，策略版本已推进至 v{new_epoch}",
    )


@router.delete("/{doc_id}")
async def delete_document(
    doc_id: str,
    db: AsyncSession = Depends(get_db),
    user: EffectiveAuthzContext = Depends(require_permission(Permissions.DOC_DELETE)),
):
    """物理删除文档及其全部切片（严格校验 DELETE 动作范围 + KB ACL + 密级）"""
    try:
        doc_uuid = uuid.UUID(doc_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的文档 UUID 格式")

    doc = await db.get(Document, doc_uuid)
    if not doc:
        raise HTTPException(status_code=404, detail="未找到该文档或已被删除")
    kb, eff_level = await assert_doc_access(db, user, doc, KbAction.DELETE.value)

    await db.execute(delete(DocumentChunk).where(DocumentChunk.document_id == doc_uuid))
    await db.execute(delete(Document).where(Document.id == doc_uuid))
    await record_security_audit_in_tx(
        db=db,
        action="DOCUMENT_DELETE",
        resource_id=doc_id,
        status="SUCCESS",
        details={
            "operator": user.username,
            "kb_id": str(kb.id),
            "kb_name": kb.name,
            "effective_doc_level": eff_level,
        },
        tenant_id=user.tenant_id,
        user_id=user.user_id,
    )
    await db.commit()

    mem_temp_path = None
    if doc_id in pipeline_manager._active_states:
        mem_temp_path = pipeline_manager._active_states[doc_id].get("temp_file_path")
    pipeline_manager.remove_pipeline(doc_id)
    cleanup_doc_temp_files(doc_id, mem_temp_path)

    return {"status": "success", "message": f"文档 {doc_id} 及其切片已彻底删除"}


# ==================== 3. 切片详情查询 ====================

@router.get("/{doc_id}/chunks", response_model=List[ChunkItemSchema])
async def get_document_chunks(
    doc_id: str,
    kb_id: Optional[str] = Query(None, description="可选收窄核对的知识库 ID"),
    db: AsyncSession = Depends(get_db),
    user: EffectiveAuthzContext = Depends(require_permission(Permissions.KB_RETRIEVE)),
):
    """获取指定文档已入库的全部切片详情（强制对象级 READ 鉴权 + L3/L4 交付前强制留痕）"""
    try:
        doc_uuid = uuid.UUID(doc_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的文档 UUID 格式")

    expected_kb_uuid: Optional[uuid.UUID] = None
    if kb_id and kb_id.strip():
        try:
            expected_kb_uuid = uuid.UUID(kb_id.strip())
        except ValueError:
            raise HTTPException(status_code=400, detail="无效的 kb_id UUID 格式")

    doc = await db.get(Document, doc_uuid)
    if not doc:
        raise HTTPException(status_code=404, detail="未找到该文档")
    kb, eff_level = await assert_doc_access(db, user, doc, KbAction.READ.value, expected_kb_id=expected_kb_uuid)
    await audit_high_classification_read(
        ctx=user,
        action="HIGH_CLASSIFICATION_CHUNKS_READ",
        doc_id=doc_id,
        kb_id=str(kb.id),
        effective_level=eff_level,
        extra_details={"document_title": doc.title},
    )

    stmt = select(DocumentChunk).where(DocumentChunk.document_id == doc_uuid).order_by(DocumentChunk.chunk_index.asc())
    chunks = (await db.execute(stmt)).scalars().all()

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
            sparse_token_count=len(c.sparse_vector) if c.sparse_vector else 0,
        )
        for c in chunks
    ]


# ==================== 4. 状态快照与 SSE 流式推流 ====================

@router.get("/{doc_id}/status", response_model=PipelineSnapshotResponse)
async def get_pipeline_status(
    doc_id: str,
    db: AsyncSession = Depends(get_db),
    user: EffectiveAuthzContext = Depends(require_permission(Permissions.KB_RETRIEVE)),
):
    from rag_kb.services.mcp.server import build_core_document_status_snapshot
    return await build_core_document_status_snapshot(db=db, user=user, doc_id=doc_id)


@router.post("/{doc_id}/stream-ticket", response_model=StreamTicketResponse)
async def create_document_stream_ticket(
    doc_id: str,
    req: StreamTicketRequest,
    db: AsyncSession = Depends(get_db),
    user: EffectiveAuthzContext = Depends(require_permission(Permissions.KB_RETRIEVE)),
):
    """签发绑定当前用户与单篇文档的 cv=2 短期受限只读流式票据"""
    try:
        doc_uuid = uuid.UUID(doc_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的文档 UUID 格式")

    state = pipeline_manager.get_state(doc_id)
    if req.purpose == "events" and not state and doc_id not in pipeline_manager._event_history:
        raise HTTPException(status_code=404, detail=f"未找到文档 ID 为 {doc_id} 的活跃流水线")
    if state:
        await assert_pipeline_state_access(db, user, state, KbAction.READ.value)
    else:
        doc = await db.get(Document, doc_uuid)
        if not doc:
            raise HTTPException(status_code=404, detail=f"未找到文档 {doc_id}")
        await assert_doc_access(db, user, doc, KbAction.READ.value)

    ticket, ttl = await issue_stream_ticket(user=user, doc_id=doc_id, purpose=req.purpose)
    return StreamTicketResponse(
        ticket=ticket,
        doc_id=doc_id,
        purpose=req.purpose,
        expires_in=ttl,
    )


@router.get("/{doc_id}/events")
async def stream_pipeline_events(
    doc_id: str,
    request: Request,
    last_event_id: Optional[str] = Header(None, alias="Last-Event-ID"),
    last_event_id_query: Optional[int] = Query(None, alias="last_event_id", ge=0),
    authorization: Optional[str] = Header(None, alias="Authorization"),
    ticket: Optional[str] = Query(None, description="单文档短期受限 SSE 票据"),
    db: AsyncSession = Depends(get_db),
):
    """SSE 长连接推流接口（强制验签、对象权限复核与推送前实时双版本/到期检测）"""
    user = await _authenticate_bearer_or_ticket(
        doc_id=doc_id,
        purpose="events",
        authorization=authorization,
        ticket=ticket,
        db=db,
    )

    try:
        doc_uuid = uuid.UUID(doc_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的文档 UUID 格式")

    state = pipeline_manager.get_state(doc_id)
    if state:
        await assert_pipeline_state_access(db, user, state, KbAction.READ.value)
    else:
        db_doc = await db.get(Document, doc_uuid)
        if db_doc:
            await assert_doc_access(db, user, db_doc, KbAction.READ.value)

    if not state and doc_id not in pipeline_manager._event_history:
        raise HTTPException(status_code=404, detail=f"未找到文档 ID 为 {doc_id} 的活跃流水线")

    event_queue = pipeline_manager.subscribe(doc_id)

    since_id = 0
    if last_event_id and last_event_id.isdigit():
        since_id = int(last_event_id)
    elif last_event_id_query is not None:
        since_id = int(last_event_id_query)

    async def _prepare_sse_payload(event_name: str, raw_data: Any) -> Any:
        if event_name == "duplicate_warning" and isinstance(raw_data, dict):
            async with AsyncSessionLocal() as viewer_db:
                return await render_duplicate_warning_for_viewer(viewer_db, user, raw_data)
        return raw_data

    async def event_generator() -> AsyncGenerator[str, None]:
        try:
            auth_error = await get_session_auth_error(
                user, token_iat_ms=user.token_iat_ms, token_exp=user.token_exp, watched_doc_ids=[doc_uuid]
            )
            if auth_error:
                yield format_sse(auth_error, {"doc_id": doc_id, "detail": SESSION_AUTH_ERROR_DETAILS[auth_error]})
                return

            if since_id == 0:
                snapshot = pipeline_manager.to_snapshot(doc_id)
                if snapshot:
                    init_data = json.dumps(snapshot.model_dump(), default=str, ensure_ascii=False)
                    yield f"id: 0\nevent: initial_state\ndata: {init_data}\n\n"
            else:
                for past_evt in pipeline_manager.get_history(doc_id):
                    past_id = int(past_evt["id"])
                    if past_id > since_id:
                        past_payload = await _prepare_sse_payload(past_evt["event"], past_evt["data"])
                        yield format_sse(past_evt["event"], past_payload, past_id)

            curr_status = state.get("overall_status") if state else None
            if curr_status in ["completed", "failed"]:
                return

            idle_ticks = 0
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(event_queue.get(), timeout=2.0)
                    idle_ticks = 0
                except asyncio.TimeoutError:
                    event = None
                    idle_ticks += 1

                if await request.is_disconnected():
                    break

                auth_error = await get_session_auth_error(
                    user, token_iat_ms=user.token_iat_ms, token_exp=user.token_exp, watched_doc_ids=[doc_uuid]
                )
                if auth_error:
                    yield format_sse(auth_error, {"doc_id": doc_id, "detail": SESSION_AUTH_ERROR_DETAILS[auth_error]})
                    break
                if event is None:
                    if idle_ticks >= 5:
                        idle_ticks = 0
                        yield ": keepalive\n\n"
                    continue

                event_name = event["event"]
                event_payload = await _prepare_sse_payload(event_name, event["data"])
                yield format_sse(event_name, event_payload, event["id"])

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
            "Cache-Control": PRIVATE_NO_STORE_CACHE,
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ==================== 5. 人工审核提交与恢复 ====================

@router.post("/{doc_id}/review")
async def submit_pipeline_review(
    doc_id: str,
    request: ReviewSubmitRequest,
    user: EffectiveAuthzContext = Depends(require_permission(Permissions.DOC_REVIEW)),
    db: AsyncSession = Depends(get_db),
):
    """提交人工审核结果并唤醒流水线（校验 REVIEW 动作范围 + KB ACL + 密级）"""
    try:
        doc_uuid = uuid.UUID(doc_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的文档 UUID 格式")

    state = pipeline_manager.get_state(doc_id)
    if state:
        await assert_pipeline_state_access(db, user, state, KbAction.REVIEW.value)
    else:
        doc = await db.get(Document, doc_uuid)
        if doc:
            await assert_doc_access(db, user, doc, KbAction.REVIEW.value)
        else:
            raise HTTPException(status_code=404, detail=f"未找到文档 {doc_id} 的活跃流水线")

    if state and state.get("overall_status") != IngestStatus.REVIEW.value:
        raise HTTPException(status_code=400, detail="当前流水线并未处于待人工审核挂起状态")

    updated_reviews: List[Dict[str, Any]] = []
    if state:
        update_map = {item.item_id: item for item in request.items}
        for rev in state.get("pending_reviews", []):
            item_id = rev.get("item_id")
            if item_id in update_map:
                up = update_map[item_id]
                rev["user_description"] = up.user_description
                rev["remark"] = up.remark
                rev["status"] = "modified" if up.user_description != rev.get("vlm_description") else "approved"
        updated_reviews = list(state.get("pending_reviews", []))
        # 返回 200 前立即将内存状态切出 pending_review，消除提交后快照轮询回弹与并发重复提交窗口
        state["overall_status"] = "running"
        state["current_step_index"] = 8
        steps = state.get("steps", [])
        if isinstance(steps, list) and len(steps) >= 7 and isinstance(steps[6], dict):
            steps[6]["status"] = "completed"

    resume_payload = [item.model_dump() for item in request.items]
    ingest_queue_service.schedule_background_resume(
        ingest_queue_service.resume_after_review(
            doc_id,
            resume_payload,
            user_id=user.user_id,
            tenant_id=user.tenant_id,
            request_id=get_request_id(),
        )
    )

    await pipeline_manager.broadcast(doc_id, "review_completed", {
        "doc_id": doc_id,
        "reviewed_items_count": len(request.items),
        "pending_reviews": updated_reviews,
    })

    await record_audit_log(
        action="DOCUMENT_REVIEW",
        resource_id=doc_id,
        status="SUCCESS",
        details={
            "reviewed_items_count": len(request.items),
            "operator": user.username,
            "action": "resume",
        },
        tenant_id=user.tenant_id,
        user_id=user.user_id,
    )

    return {
        "status": "success",
        "message": "人工审核已确认，流水线已继续执行",
        "doc_id": doc_id,
    }


# ==================== 6. 资产安全流式反向代理 ====================

@router.get("/{doc_id}/review-items/{item_id}/asset")
async def get_review_item_asset(
    doc_id: str,
    item_id: str,
    user: EffectiveAuthzContext = Depends(require_permission(Permissions.KB_RETRIEVE)),
    db: AsyncSession = Depends(get_db),
):
    """人工审核多模态资产私有 OSS 安全流式反向代理"""
    try:
        doc_uuid = uuid.UUID(doc_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的文档 UUID 格式")

    state = pipeline_manager.get_state(doc_id)
    if state:
        kb, eff_level = await assert_pipeline_state_access(db, user, state, KbAction.READ.value)
    else:
        doc = await db.get(Document, doc_uuid)
        if not doc:
            raise HTTPException(status_code=404, detail=f"未找到文档 {doc_id}")
        kb, eff_level = await assert_doc_access(db, user, doc, KbAction.READ.value)

    await audit_high_classification_read(
        ctx=user,
        action="HIGH_CLASSIFICATION_REVIEW_ASSET_READ",
        doc_id=doc_id,
        kb_id=str(kb.id),
        effective_level=eff_level,
        extra_details={"item_id": item_id},
    )

    target_url = None
    if state:
        oss_map = state.get("review_asset_oss_map") or {}
        target_url = oss_map.get(item_id)
    else:
        rev_item = (await db.execute(
            select(DocumentReviewItem).where(DocumentReviewItem.document_id == doc_uuid, DocumentReviewItem.item_key == item_id)
        )).scalar_one_or_none()
        if rev_item:
            target_url = rev_item.asset_url

    if not target_url or not str(target_url).strip():
        raise HTTPException(status_code=404, detail="未找到该待审核图元或该项无关联切图")

    try:
        stream_gen, content_length, clean_key = await asyncio.to_thread(oss_service.get_object_stream, target_url)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"拉取 OSS 资产流失败: {e}")

    ext = Path(clean_key).suffix.lower()
    media_type = "image/png" if ext == ".png" else "image/jpeg"
    headers = {"Cache-Control": PRIVATE_NO_STORE_CACHE, "Content-Disposition": "inline"}
    if content_length:
        headers["Content-Length"] = str(content_length)

    return StreamingResponse(stream_gen, media_type=media_type, headers=headers)


@router_chunks.get("/{chunk_id}/asset")
async def get_chunk_asset(
    chunk_id: str,
    db: AsyncSession = Depends(get_db),
    user: EffectiveAuthzContext = Depends(require_permission(Permissions.KB_RETRIEVE)),
):
    """切片关联的多模态切图安全流式反向代理（先查 chunk.document_id 再按真实主文档与所属库裁决）"""
    try:
        c_uuid = uuid.UUID(chunk_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的切片 UUID 格式")

    chunk = (await db.execute(select(DocumentChunk).where(DocumentChunk.id == c_uuid))).scalar_one_or_none()
    if not chunk or not chunk.asset_url:
        raise HTTPException(status_code=404, detail="切片无关联图元切图")

    doc = await db.get(Document, chunk.document_id)
    if not doc:
        raise HTTPException(status_code=404, detail="切片所属主文档不存在")
    kb, eff_level = await assert_doc_access(db, user, doc, KbAction.READ.value)

    await audit_high_classification_read(
        ctx=user,
        action="HIGH_CLASSIFICATION_CHUNK_ASSET_READ",
        doc_id=str(doc.id),
        kb_id=str(kb.id),
        effective_level=eff_level,
        extra_details={"chunk_id": chunk_id},
    )

    try:
        stream_gen, content_length, clean_key = await asyncio.to_thread(oss_service.get_object_stream, chunk.asset_url)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"拉取 OSS 资产流失败: {e}")

    ext = Path(clean_key).suffix.lower()
    media_type = "image/png" if ext == ".png" else "image/jpeg"
    headers = {"Cache-Control": PRIVATE_NO_STORE_CACHE, "Content-Disposition": "inline"}
    if content_length:
        headers["Content-Length"] = str(content_length)

    return StreamingResponse(stream_gen, media_type=media_type, headers=headers)


# ==================== 6.1 原文预览与单页高清渲染 ====================

_PDF_DOCUMENT_CACHE: Dict[str, bytes] = {}
_PDF_RENDER_SEM: Optional[asyncio.Semaphore] = None


def _get_pdf_render_sem() -> asyncio.Semaphore:
    global _PDF_RENDER_SEM
    if _PDF_RENDER_SEM is None:
        _PDF_RENDER_SEM = asyncio.Semaphore(max(1, int(settings.PDF_RENDER_CONCURRENCY)))
    return _PDF_RENDER_SEM


async def _fetch_pdf_bytes(raw_oss_url: str) -> bytes:
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


def _encode_numpy_to_png(arr: np.ndarray) -> bytes:
    if arr.ndim != 3 or arr.shape[2] not in (3, 4):
        raise ValueError(f"不支持的像素通道维度: {arr.shape}")
    height, width, channels = arr.shape
    color_type = 2 if channels == 3 else 6
    scanlines = np.concatenate(
        [np.zeros((height, 1), dtype=np.uint8), arr.reshape(height, width * channels)],
        axis=1,
    ).tobytes()

    def _png_chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(scanlines, level=6))
        + _png_chunk(b"IEND", b"")
    )


def _render_pdf_page_sync(pdf_bytes: bytes, page_num: int, scale: float) -> tuple[bytes, int]:
    pdf = pdfium.PdfDocument(pdf_bytes)
    try:
        total_pages = len(pdf)
        if page_num < 1 or page_num > total_pages:
            raise IndexError(f"页码超出范围: 1 - {total_pages}")
        page = pdf[page_num - 1]
        rgb_array = page.render(scale=scale, rev_byteorder=True).to_numpy()
        return _encode_numpy_to_png(rgb_array), total_pages
    finally:
        try:
            pdf.close()
        except Exception:
            pass


@router.get("/{doc_id}/preview")
async def preview_document(
    doc_id: str,
    range_header: Optional[str] = Header(None, alias="Range"),
    authorization: Optional[str] = Header(None, alias="Authorization"),
    ticket: Optional[str] = Query(None, description="单文档短期受限预览票据"),
    db: AsyncSession = Depends(get_db),
):
    """文档安全流式预览接口（每次 Range 请求均重验双版本、自然到期、对象密级 + L3/L4 交付前强制留痕）"""
    user = await _authenticate_bearer_or_ticket(
        doc_id=doc_id,
        purpose="preview",
        authorization=authorization,
        ticket=ticket,
        db=db,
    )
    try:
        doc_uuid = uuid.UUID(doc_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的文档 UUID 格式")

    doc = (await db.execute(select(Document).where(Document.id == doc_uuid))).scalar_one_or_none()
    if not doc or not doc.raw_oss_url:
        raise HTTPException(status_code=404, detail="未找到文档或未关联 OSS 原文")
    kb, eff_level = await assert_doc_access(db, user, doc, KbAction.READ.value)

    await audit_high_classification_read(
        ctx=user,
        action="HIGH_CLASSIFICATION_PREVIEW_READ",
        doc_id=doc_id,
        kb_id=str(kb.id),
        effective_level=eff_level,
        extra_details={"range": range_header, "document_title": doc.title},
    )

    ext = Path(doc.title).suffix.lower()
    if ext == ".pdf":
        media_type = "application/pdf"
    elif ext in [".md", ".markdown", ".txt"]:
        media_type = "text/plain; charset=utf-8"
    else:
        media_type = "application/octet-stream"

    encoded_filename = quote(doc.title)
    if range_header:
        try:
            bucket = oss_service.get_client()
            clean_key = oss_service.normalize_key(doc.raw_oss_url)
            head_meta = await asyncio.to_thread(bucket.head_object, clean_key)
            total_len = int(head_meta.content_length or 0)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"拉取 OSS 元数据失败: {e}")

        parsed_range = _parse_http_byte_range(range_header, total_len)
        if parsed_range is not None:
            start, end = parsed_range
            try:
                part_stream, _, _ = await asyncio.to_thread(
                    oss_service.get_object_stream, doc.raw_oss_url, 65536, (start, end)
                )
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"拉取 OSS 分片失败: {e}")
            part_headers = {
                "Content-Disposition": f"inline; filename*=utf-8''{encoded_filename}",
                "Accept-Ranges": "bytes",
                "Content-Range": f"bytes {start}-{end}/{total_len}",
                "Content-Length": str(end - start + 1),
                "Cache-Control": PRIVATE_NO_STORE_CACHE,
            }
            return StreamingResponse(
                part_stream,
                status_code=status.HTTP_206_PARTIAL_CONTENT,
                media_type=media_type,
                headers=part_headers,
            )

    try:
        stream_gen, content_length, _ = await asyncio.to_thread(oss_service.get_object_stream, doc.raw_oss_url)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"拉取 OSS 失败: {e}")

    headers = {
        "Content-Disposition": f"inline; filename*=utf-8''{encoded_filename}",
        "Accept-Ranges": "bytes",
        "Cache-Control": PRIVATE_NO_STORE_CACHE,
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
    user: EffectiveAuthzContext = Depends(require_permission(Permissions.KB_RETRIEVE)),
):
    """文档单页超高精渲染接口（即使命中 _PDF_DOCUMENT_CACHE 也必须先复核授权并完成高密留痕）"""
    try:
        doc_uuid = uuid.UUID(doc_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的文档 UUID 格式")

    doc = (await db.execute(select(Document).where(Document.id == doc_uuid))).scalar_one_or_none()
    if not doc or not doc.raw_oss_url:
        raise HTTPException(status_code=404, detail="文档未找到或未关联 OSS 原文")

    # 强制先复核对象授权与高密审计，再访问 _PDF_DOCUMENT_CACHE
    kb, eff_level = await assert_doc_access(db, user, doc, KbAction.READ.value)
    await audit_high_classification_read(
        ctx=user,
        action="HIGH_CLASSIFICATION_PAGE_RENDER_READ",
        doc_id=doc_id,
        kb_id=str(kb.id),
        effective_level=eff_level,
        extra_details={"page_num": page_num, "document_title": doc.title},
    )

    if not doc.title.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="单页图片预览仅支持 PDF 格式文档")

    pdf_bytes = await _fetch_pdf_bytes(doc.raw_oss_url)
    async with _get_pdf_render_sem():
        try:
            png_bytes, total_pages = await asyncio.to_thread(_render_pdf_page_sync, pdf_bytes, page_num, scale)
        except IndexError as idx_err:
            raise HTTPException(status_code=404, detail=str(idx_err))
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"渲染 PDF 单页失败: {e}")

    headers = {
        "Cache-Control": PRIVATE_NO_STORE_CACHE,
        "X-Total-Pages": str(total_pages),
        "X-Current-Page": str(page_num),
        "Access-Control-Expose-Headers": "X-Total-Pages, X-Current-Page",
    }
    return StreamingResponse(io.BytesIO(png_bytes), media_type="image/png", headers=headers)


# ==================== 7. 防重冲突与重试 ====================

@router.post("/{doc_id}/resolve_duplicate", response_model=DuplicateResolveResponse)
async def resolve_duplicate(
    doc_id: str,
    request: DuplicateResolveRequest,
    db: AsyncSession = Depends(get_db),
    user: EffectiveAuthzContext = Depends(require_permission(Permissions.DOC_UPLOAD)),
):
    """防重冲突解决决策接口（禁止低密级用户 overwrite 覆盖高密级存量文档）"""
    try:
        doc_uuid = uuid.UUID(doc_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的文档 UUID 格式")

    state = pipeline_manager.get_state(doc_id)
    if state:
        kb, _ = await assert_pipeline_state_access(db, user, state, KbAction.UPLOAD.value)
    else:
        doc = await db.get(Document, doc_uuid)
        if doc:
            await assert_doc_access(db, user, doc, KbAction.UPLOAD.value)
            raise HTTPException(status_code=400, detail=f"文档 {doc_id} 内存中无挂起的活跃流水线")
        raise HTTPException(status_code=404, detail=f"未找到文档 {doc_id} 的活跃流水线")

    if request.action == "overwrite":
        dedup_res = state.get("dedup_res") or {}
        if dedup_res.get("is_higher_classification") or dedup_res.get("can_overwrite") is False:
            await record_denial_audit(
                action="DOCUMENT_OVERWRITE_HIGHER_CLASSIFICATION_DENIED",
                resource_id=doc_id,
                error_msg="禁止低密级上传者覆盖高密级存量文档",
                details={"kb_id": str(kb.id), "user_clearance": user.effective_clearance},
                tenant_id=user.tenant_id,
                user_id=user.user_id,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="【越权覆盖拦截】命中文档的密级高于您的访问等级，严禁覆盖更新，请选择独立新建或取消",
            )
        # 二次查库核对 matched_doc_id 真实密级
        matched_id = request.matched_doc_id or dedup_res.get("matched_doc_id")
        if matched_id:
            try:
                old_doc = await db.get(Document, uuid.UUID(str(matched_id)))
            except ValueError:
                old_doc = None
            if old_doc is not None:
                old_eff_lvl = max(int(kb.min_classification_level), int(old_doc.classification_level))
                if int(user.effective_clearance) < old_eff_lvl or old_doc.kb_id != kb.id or old_doc.tenant_id != user.tenant_id:
                    await record_denial_audit(
                        action="DOCUMENT_OVERWRITE_HIGHER_CLASSIFICATION_DENIED",
                        resource_id=str(old_doc.id),
                        error_msg="禁止低密级或跨库覆盖存量高密文档",
                        details={"kb_id": str(kb.id), "old_eff_lvl": old_eff_lvl, "user_clearance": user.effective_clearance},
                        tenant_id=user.tenant_id,
                        user_id=user.user_id,
                    )
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="【越权覆盖拦截】您当前密级不足以覆盖该存量高密文档",
                    )
        state["force_overwrite"] = True
        state["uploader_clearance"] = user.effective_clearance
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
            "message": "用户取消重复文档入库",
        })
        return DuplicateResolveResponse(doc_id=doc_id, status="cancelled", message="已取消入库")

    ingest_queue_service.schedule_background_resume(
        ingest_queue_service.resume_after_duplicate_decision(
            doc_id,
            state,
            user_id=user.user_id,
            tenant_id=user.tenant_id,
            request_id=get_request_id(),
        )
    )
    return DuplicateResolveResponse(
        doc_id=doc_id,
        status="resumed",
        message=f"已按动作 {request.action} 恢复入库流水线",
    )


@router.post("/{doc_id}/retry")
async def retry_pipeline(
    doc_id: str,
    request: Optional[RetryPipelineRequest] = None,
    db: AsyncSession = Depends(get_db),
    user: EffectiveAuthzContext = Depends(require_permission(Permissions.DOC_UPLOAD)),
):
    """流水线失败引导重试（校验 UPLOAD 动作范围 + KB ACL + 密级，并透传 kb_id 与密级）"""
    try:
        doc_uuid = uuid.UUID(doc_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的文档 UUID 格式")

    doc = await db.get(Document, doc_uuid)
    state = pipeline_manager.get_state(doc_id)
    if state:
        kb, eff_level = await assert_pipeline_state_access(db, user, state, KbAction.UPLOAD.value)
    else:
        if doc:
            kb, eff_level = await assert_doc_access(db, user, doc, KbAction.UPLOAD.value)
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
                tenant_id=doc.tenant_id,
                kb_id=str(doc.kb_id),
                kb_name=kb.name,
                classification_level=int(doc.classification_level),
                created_by=doc.created_by or user.user_id,
                uploader_clearance=user.effective_clearance,
            )
            state["raw_oss_url"] = doc.raw_oss_url
            state["file_hash"] = doc.file_hash
            state["enhanced_md_oss_url"] = doc.enhanced_md_oss_url
            pipeline_manager.register_pipeline(doc_id, state)
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
    state["uploader_clearance"] = user.effective_clearance
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
        department=state.get("department") or kb.owner_dept_path,
        category=state.get("category") or (doc.category if doc else "技术规范"),
        tags=state.get("tags") or (doc.tags if doc else []),
        skip_review=bool(state.get("skip_review", False)),
        temp_file_path=state.get("temp_file_path"),
        from_step=effective_from_step,
        user_id=user.user_id,
        tenant_id=user.tenant_id,
        request_id=get_request_id(),
        kb_id=str(kb.id),
        classification_level=int(state.get("classification_level") or eff_level),
        uploader_clearance=user.effective_clearance,
    )

    retry_hint = (
        f"已重新提交处理，将从第 {effective_from_step} 步继续执行"
        if effective_from_step > 1
        else "已重新提交处理，将从第 1 步重新解析"
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
            "kb_id": str(kb.id),
            "requested_from_step": requested_step,
            "effective_from_step": effective_from_step,
            "operator": user.username,
            "stream_msg_id": stream_msg_id,
        },
        tenant_id=user.tenant_id,
        user_id=user.user_id,
    )

    return {
        "doc_id": doc_id,
        "task_id": stream_msg_id,
        "status": "queued",
        "from_step": effective_from_step,
        "message": retry_hint,
    }
