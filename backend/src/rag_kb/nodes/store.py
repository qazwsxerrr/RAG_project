"""src/rag_kb/nodes/store.py —— 标题向量防重与蓝绿安全入库节点 StoreNode。

流水线第十一环（终环）：
1. 校验部门归属（不存在则动态在知识库根下创建）；
2. 基于 1024 维标题向量执行 HNSW 余弦相似度防重检测；
3. 执行蓝绿事务入库：全量暂存切片、特征维度自检、原子切换活动版本，
4. 保证流水线幂等与数据一致性。
"""

import uuid
import logging
from typing import Optional
from sqlalchemy import select
from langchain_core.runnables import RunnableConfig
from langgraph.types import StreamWriter
from rag_kb.core.constants import IngestStatus
from rag_kb.db.models import Department
from rag_kb.db.session import AsyncSessionLocal
from rag_kb.graph.state import IngestState
from rag_kb.nodes.base import BaseNode
from rag_kb.services.pipeline.dedup_service import dedup_service
from rag_kb.services.pipeline.update_service import update_service

logger = logging.getLogger(__name__)

class StoreNode(BaseNode):
    """节点 11: 标题向量防重与蓝绿安全入库"""

    def __init__(self):
        super().__init__(step_index=11, name="向量索引入库")

    async def process(
        self,
        state: IngestState,
        config: Optional[RunnableConfig] = None,
        writer: Optional[StreamWriter] = None
    ) -> IngestState:
        doc_id = state.get("doc_id")
        file_name = state.get("file_name", "document.pdf")
        enhanced_md_content = state.get("enhanced_md_content") or ""
        department_name = state.get("department", "技术部")
        category = state.get("category", "技术规范")
        tags = state.get("tags", [])
        raw_chunks = state.get("raw_chunks", [])
        dense_embeddings = state.get("dense_embeddings", [])
        sparse_vectors = state.get("sparse_vectors", [])
        tsv_strings = state.get("tsv_strings", [])
        force_overwrite = state.get("force_overwrite", False)
        skip_review = state.get("skip_review", False)

        if not raw_chunks:
            raise ValueError(f"【入库错误】待入库切片数量为 0 (doc_id={doc_id})")

        if writer and callable(writer):
            writer({
                "event": "step_progress",
                "step": 11,
                "status": "running",
                "detail": "正在执行标题向量防重检测与数据库蓝绿安全入库..."
            })

        async with AsyncSessionLocal() as session:
            # 1. 匹配或动态创建部门
            dept_stmt = select(Department).where(Department.name == department_name)
            dept_res = await session.execute(dept_stmt)
            dept_obj = dept_res.scalar_one_or_none()
            if not dept_obj:
                dept_obj = Department(
                    id=uuid.uuid4(),
                    name=department_name,
                    path=f"/总公司/动态部门/{department_name}"
                )
                session.add(dept_obj)
                await session.flush()

            department_id = dept_obj.id
            department_path = dept_obj.path

            # 2. 标题向量防重检测
            dedup_res = await dedup_service.check_duplicate(
                session=session,
                doc_id=doc_id,
                file_name=file_name,
                markdown_text=enhanced_md_content
            )
            state["dedup_res"] = dedup_res
            title_embedding = dedup_res.get("title_embedding")

            is_dup = dedup_res.get("is_duplicate", False)
            if is_dup and not force_overwrite and not skip_review:
                logger.warning(f"【防重预警】文档《{file_name}》命中相似文档，已记录预警信息")
                if writer and callable(writer):
                    writer({
                        "event": "duplicate_warning",
                        "doc_id": doc_id,
                        "similarity": dedup_res.get("similarity"),
                        "matched_doc_id": dedup_res.get("matched_doc_id"),
                        "matched_title": dedup_res.get("matched_title"),
                        "message": dedup_res.get("message")
                    })

            target_version = (dedup_res.get("matched_version", 1) + 1) if (is_dup and force_overwrite) else 1
            old_doc_id = (
                uuid.UUID(dedup_res["matched_doc_id"])
                if (is_dup and force_overwrite and dedup_res.get("matched_doc_id"))
                else None
            )

            # 3. 蓝绿事务入库
            await update_service.ingest_document_with_chunks(
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
                target_version=target_version,
                old_doc_id=old_doc_id,
                pending_reviews=state.get("pending_reviews", [])
            )

        state["overall_status"] = IngestStatus.COMPLETED.value

        if writer and callable(writer):
            writer({
                "event": "pipeline_completed",
                "doc_id": doc_id,
                "file_name": file_name,
                "status": "completed",
                "chunk_count": len(raw_chunks),
                "version": target_version,
                "message": "入库流水线 11 节点全部成功执行完毕！"
            })

        return state

store_node = StoreNode()
