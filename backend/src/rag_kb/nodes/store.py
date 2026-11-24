"""src/rag_kb/nodes/store.py —— 标题向量防重与蓝绿安全入库节点 StoreNode（支持多租户、KB 与 L1-L4 密级）。"""

import uuid
import logging
from typing import Optional
from sqlalchemy import select
from langchain_core.runnables import RunnableConfig
from langgraph.types import StreamWriter

from rag_kb.core.constants import IngestStatus
from rag_kb.db.models import Department, Document, KnowledgeBase
from rag_kb.db.session import AsyncSessionLocal
from rag_kb.graph.state import IngestState
from rag_kb.nodes.base import BaseNode
from rag_kb.services.pipeline.dedup_service import (
    REDACTED_HIGH_CLASSIFICATION_TITLE,
    dedup_service,
)
from rag_kb.services.pipeline.update_service import update_service

logger = logging.getLogger(__name__)


class StoreNode(BaseNode):
    """节点 11: 同库标题向量防重与蓝绿安全入库"""

    def __init__(self):
        super().__init__(step_index=11, name="入库")

    async def process(
        self,
        state: IngestState,
        config: Optional[RunnableConfig] = None,
        writer: Optional[StreamWriter] = None,
    ) -> IngestState:
        doc_id = state.get("doc_id")
        tenant_id = state.get("tenant_id") or "default_tenant"
        raw_kb_id = state.get("kb_id")
        classification_level = state.get("classification_level")
        created_by = state.get("created_by")
        uploader_clearance = state.get("uploader_clearance")

        file_name = state.get("file_name", "document.pdf")
        enhanced_md_content = state.get("enhanced_md_content") or ""
        department_name = state.get("department", "/public")
        category = state.get("category", "技术规范")
        tags = state.get("tags", [])
        raw_chunks = state.get("raw_chunks", [])
        dense_embeddings = state.get("dense_embeddings", [])
        sparse_vectors = state.get("sparse_vectors", [])
        tsv_strings = state.get("tsv_strings", [])
        force_overwrite = bool(state.get("force_overwrite", False))
        skip_review = bool(state.get("skip_review", False))

        if not raw_chunks:
            raise ValueError(f"【入库错误】待入库切片数量为 0 (doc_id={doc_id})")

        if writer and callable(writer):
            writer({
                "event": "step_progress",
                "step": 11,
                "status": "running",
                "detail": "正在进行同库重复文档检测并写入知识库...",
            })

        async with AsyncSessionLocal() as session:
            # 1. Fail-Fast 核验排队文档与显式目标知识库仍存在且有效（严禁隐式改投其他知识库或重建已删文档）
            try:
                doc_uuid = uuid.UUID(str(doc_id))
            except (TypeError, ValueError) as e:
                raise RuntimeError(f"【入库失败】非法文档 UUID: {doc_id}") from e

            existing_doc = await session.get(Document, doc_uuid)
            if existing_doc is None:
                raise RuntimeError(
                    f"【入库终止】排队文档 {doc_id} 在解析期间已被删除，终止入库任务（严禁重建已删文档）"
                )
            if existing_doc.overall_status == "cancelled":
                raise RuntimeError(f"【入库终止】文档 {doc_id} 已被取消，终止入库任务")

            if raw_kb_id:
                try:
                    kb_uuid = uuid.UUID(str(raw_kb_id))
                except ValueError as e:
                    raise RuntimeError(f"【入库失败】非法目标知识库 UUID: {raw_kb_id}") from e
                if existing_doc.kb_id != kb_uuid or existing_doc.tenant_id != tenant_id:
                    raise RuntimeError(
                        f"【入库终止】文档 {doc_id} 归属知识库与任务目标不一致 "
                        f"(doc_kb={existing_doc.kb_id}, task_kb={kb_uuid})"
                    )
            else:
                kb_uuid = existing_doc.kb_id
                tenant_id = existing_doc.tenant_id

            kb_obj = (
                await session.execute(
                    select(KnowledgeBase).where(
                        KnowledgeBase.tenant_id == tenant_id,
                        KnowledgeBase.id == kb_uuid,
                    )
                )
            ).scalar_one_or_none()
            if kb_obj is None:
                raise RuntimeError(
                    f"【入库终止】目标知识库 {kb_uuid} (tenant={tenant_id}) 已不存在，终止任务（严禁改投其他知识库）"
                )
            if kb_obj.status != "ACTIVE":
                raise RuntimeError(
                    f"【入库终止】目标知识库 {kb_obj.name} ({kb_uuid}) 状态为 {kb_obj.status}，禁止写入"
                )

            department_path = kb_obj.owner_dept_path
            dept_obj: Optional[Department] = None
            if kb_obj.owner_department_id:
                dept_obj = await session.get(Department, kb_obj.owner_department_id)
            if dept_obj is None:
                dept_obj = (
                    await session.execute(
                        select(Department).where(
                            Department.tenant_id == tenant_id,
                            Department.path == department_path,
                        )
                    )
                ).scalar_one_or_none()
            if dept_obj is None:
                raise RuntimeError(
                    f"【入库终止】目标知识库 {kb_obj.id} 归属部门 [{department_path}] 不存在"
                )

            department_id = dept_obj.id
            eff_doc_level = (
                int(existing_doc.classification_level)
                if existing_doc.classification_level is not None
                else (
                    int(classification_level)
                    if classification_level is not None
                    else int(kb_obj.default_doc_level)
                )
            )

            # 2. 同 (tenant_id, kb_id) 标题向量防重检测（含高密标题侧信道脱敏）
            dedup_res = await dedup_service.check_duplicate(
                session=session,
                doc_id=doc_id,
                file_name=file_name,
                markdown_text=enhanced_md_content,
                tenant_id=tenant_id,
                kb_id=str(kb_obj.id),
                uploader_clearance=uploader_clearance,
            )
            # 共享流水线状态与共享 SSE 事件不保存真实命中标题，避免高密标题向同库低密订阅者侧信道泄露
            safe_dedup_state = {
                "is_duplicate": bool(dedup_res.get("is_duplicate", False)),
                "similarity": dedup_res.get("similarity"),
                "matched_doc_id": dedup_res.get("matched_doc_id"),
                "matched_title": REDACTED_HIGH_CLASSIFICATION_TITLE if dedup_res.get("matched_doc_id") else None,
                "matched_version": dedup_res.get("matched_version", 0),
                "matched_effective_level": dedup_res.get("matched_effective_level"),
                "is_higher_classification": bool(dedup_res.get("is_higher_classification", False)),
                "can_overwrite": bool(dedup_res.get("can_overwrite", False)),
            }
            state["dedup_res"] = safe_dedup_state
            title_embedding = dedup_res.get("title_embedding")

            is_dup = dedup_res.get("is_duplicate", False)
            can_overwrite = bool(dedup_res.get("can_overwrite", True))

            if is_dup and force_overwrite and not can_overwrite:
                raise PermissionError(
                    "【越权覆盖拦截】命中了高于您当前访问密级的存量文档，严禁执行 overwrite 覆盖更新！"
                )

            if is_dup and not force_overwrite and not skip_review:
                logger.warning(f"【防重预警】文档《{file_name}》命中同库相似文档，已记录预警信息")
                sim_val = float(dedup_res.get("similarity") or 0.0)
                if writer and callable(writer):
                    writer({
                        "event": "duplicate_warning",
                        "doc_id": doc_id,
                        "similarity": dedup_res.get("similarity"),
                        "matched_doc_id": dedup_res.get("matched_doc_id"),
                        "matched_title": REDACTED_HIGH_CLASSIFICATION_TITLE,
                        "can_overwrite": False,
                        "is_higher_classification": True,
                        "message": f"检测到知识库内存在高度相似文档 (相似度 {sim_val:.2%})，请确认处理方式。",
                    })

            target_version = (dedup_res.get("matched_version", 1) + 1) if (is_dup and force_overwrite) else 1
            old_doc_id = (
                uuid.UUID(dedup_res["matched_doc_id"])
                if (is_dup and force_overwrite and dedup_res.get("matched_doc_id"))
                else None
            )

            # 3. 蓝绿事务入库
            saved_doc = await update_service.ingest_document_with_chunks(
                session=session,
                doc_id=uuid.UUID(doc_id),
                file_name=file_name,
                file_hash=state.get("file_hash") or "",
                raw_oss_url=state.get("raw_oss_url") or "",
                enhanced_md_oss_url=state.get("enhanced_md_oss_url"),
                department_id=department_id,
                department_path=department_path,
                category=category,
                tags=tags,
                title_embedding=title_embedding,
                chunks=raw_chunks,
                dense_embeddings=dense_embeddings,
                sparse_vectors=sparse_vectors,
                tsv_strings=tsv_strings,
                tenant_id=tenant_id,
                kb_id=kb_obj.id,
                classification_level=eff_doc_level,
                created_by=created_by,
                uploader_clearance=uploader_clearance,
                target_version=target_version,
                old_doc_id=old_doc_id,
                pending_reviews=state.get("pending_reviews", []),
                review_asset_oss_map=state.get("review_asset_oss_map") or {},
            )
            if saved_doc is not None and saved_doc.classification_level is not None:
                eff_doc_level = int(saved_doc.classification_level)

        state["kb_id"] = str(kb_obj.id)
        state["kb_name"] = kb_obj.name
        state["classification_level"] = eff_doc_level
        state["overall_status"] = IngestStatus.COMPLETED.value

        if writer and callable(writer):
            writer({
                "event": "pipeline_completed",
                "doc_id": doc_id,
                "kb_id": str(kb_obj.id),
                "classification_level": eff_doc_level,
                "file_name": file_name,
                "status": "completed",
                "chunk_count": len(raw_chunks),
                "version": target_version,
                "message": "文档处理完成，已正式入库",
            })

        return state


store_node = StoreNode()
