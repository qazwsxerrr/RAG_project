"""src/rag_kb/services/pipeline/update_service.py —— 蓝绿安全入库与防越权版本切换服务。

新增多维权限护栏：
1. 写入并强校验 tenant_id、kb_id、classification_level、created_by；
2. 蓝绿覆盖下线旧文档时，强制校验同 (tenant_id, kb_id) 且上传者密级 >= 旧文档生效密级，
   严禁低密级上传者覆盖下线高密级文档！
"""

import uuid
import logging
from typing import List, Dict, Any, Optional
from sqlalchemy import select, func, delete
from sqlalchemy.ext.asyncio import AsyncSession

from rag_kb.db.models import Document, DocumentChunk, DocumentReviewItem, KnowledgeBase
from rag_kb.services.pipeline.chunker import RawChunkData

logger = logging.getLogger(__name__)


class BlueGreenUpdateService:
    """蓝绿安全入库与版本无损原子切换服务 (节点 11)"""

    async def ingest_document_with_chunks(
        self,
        session: AsyncSession,
        doc_id: uuid.UUID,
        file_name: str,
        file_hash: str,
        raw_oss_url: str,
        enhanced_md_oss_url: Optional[str],
        department_id: uuid.UUID,
        department_path: str,
        category: str,
        tags: List[str],
        title_embedding: List[float],
        chunks: List[RawChunkData],
        dense_embeddings: List[List[float]],
        sparse_vectors: List[Dict[str, float]],
        tsv_strings: List[str],
        tenant_id: str = "default_tenant",
        kb_id: Optional[uuid.UUID] = None,
        classification_level: Optional[int] = None,
        created_by: Optional[str] = None,
        uploader_clearance: Optional[int] = None,
        target_version: int = 1,
        old_doc_id: Optional[uuid.UUID] = None,
        pending_reviews: Optional[List[Dict[str, Any]]] = None,
        review_asset_oss_map: Optional[Dict[str, str]] = None,
    ) -> Document:
        """执行蓝绿事务入库与指针原子切换"""
        total_chunks = len(chunks)
        if total_chunks == 0:
            raise RuntimeError("【入库红线阻断】待入库切片数量为 0，拒绝提交空数据！")

        if not (total_chunks == len(dense_embeddings) == len(sparse_vectors) == len(tsv_strings)):
            raise RuntimeError(
                f"【入库特征不一致】切片数 ({total_chunks})、稠密向量数 ({len(dense_embeddings)})、"
                f"稀疏向量数 ({len(sparse_vectors)}) 与分词数 ({len(tsv_strings)}) 不匹配！"
            )

        for idx in range(total_chunks):
            if len(dense_embeddings[idx]) != 1024:
                raise RuntimeError(f"【向量异常】第 {idx} 个切片的稠密向量非 1024 维！")
            if not sparse_vectors[idx]:
                raise RuntimeError(f"【向量异常】第 {idx} 个切片的稀疏向量字典为空！")
            if not tsv_strings[idx].strip():
                raise RuntimeError(f"【分词异常】第 {idx} 个切片的 TSVector 分词序列为空！")

        if kb_id is None:
            raise RuntimeError("【入库红线阻断】缺少归属知识库标识 kb_id！")

        kb_obj = (
            await session.execute(
                select(KnowledgeBase).where(KnowledgeBase.tenant_id == tenant_id, KnowledgeBase.id == kb_id)
            )
        ).scalar_one_or_none()
        if kb_obj is None:
            raise RuntimeError(f"【入库红线阻断】知识库 {kb_id} 不存在或不属于租户 {tenant_id}！")

        eff_doc_level = int(classification_level) if classification_level is not None else int(kb_obj.default_doc_level)
        if eff_doc_level < int(kb_obj.min_classification_level) or eff_doc_level > int(kb_obj.max_doc_level):
            raise RuntimeError(
                f"【密级红线阻断】文档密级 L{eff_doc_level} 超出知识库允许区间 [L{kb_obj.min_classification_level}, L{kb_obj.max_doc_level}]"
            )
        if uploader_clearance is not None and eff_doc_level > int(uploader_clearance):
            raise PermissionError(
                f"【密级红线阻断】上传者密级 L{uploader_clearance} 不能写入 L{eff_doc_level} 文档"
            )

        logger.info(
            f"【蓝绿入库启动】doc_id={doc_id}, kb_id={kb_id}, level=L{eff_doc_level}, "
            f"切片总数={total_chunks}, 目标版本=v{target_version}"
        )

        try:
            # 覆盖切换前防越权校验（Fail-Closed）：无法确认上传者密级或低密级上传者严禁覆盖存量文档
            old_doc: Optional[Document] = None
            if old_doc_id and old_doc_id != doc_id:
                old_res = await session.execute(
                    select(Document).where(
                        Document.id == old_doc_id,
                        Document.tenant_id == tenant_id,
                        Document.kb_id == kb_id,
                    )
                )
                old_doc = old_res.scalar_one_or_none()
                if old_doc is not None:
                    old_effective_level = max(int(kb_obj.min_classification_level), int(old_doc.classification_level))
                    if uploader_clearance is None or int(uploader_clearance) < old_effective_level:
                        raise PermissionError(
                            f"【越权覆盖拦截】存量文档密级为 L{old_effective_level}，上传者有效密级"
                            f"{'未知' if uploader_clearance is None else f'为 L{uploader_clearance}'}，严禁 overwrite 覆盖！"
                        )

            doc_res = await session.execute(
                select(Document).where(Document.id == doc_id).with_for_update()
            )
            doc = doc_res.scalar_one_or_none()
            if doc is None:
                raise RuntimeError(
                    f"【入库终止】排队文档 {doc_id} 在最终入库前已不存在（可能已被删除），拒绝重新创建文档"
                )
            if doc.overall_status == "cancelled":
                raise RuntimeError(f"【入库终止】文档 {doc_id} 已被取消，拒绝继续入库")
            if doc.tenant_id != tenant_id or doc.kb_id != kb_id:
                raise RuntimeError(
                    f"【入库终止】文档 {doc_id} 当前归属 ({doc.tenant_id}/{doc.kb_id}) 与写入目标 ({tenant_id}/{kb_id}) 不一致"
                )

            # 对已持久化的文档，以数据库当前密级为准，严禁后台入库任务将管理员在线调高的密级覆写回旧值
            if doc.classification_level is not None:
                eff_doc_level = int(doc.classification_level)
                if eff_doc_level < int(kb_obj.min_classification_level) or eff_doc_level > int(kb_obj.max_doc_level):
                    raise RuntimeError(
                        f"【密级红线阻断】文档当前数据库密级 L{eff_doc_level} 超出知识库允许区间 [L{kb_obj.min_classification_level}, L{kb_obj.max_doc_level}]"
                    )

            temp_status = f"indexing_v{target_version}" if target_version > 1 else "indexing"

            doc.tenant_id = tenant_id
            doc.kb_id = kb_id
            doc.classification_level = eff_doc_level
            if created_by and not doc.created_by:
                doc.created_by = created_by
            doc.title = file_name
            doc.file_hash = file_hash
            doc.raw_oss_url = raw_oss_url
            doc.enhanced_md_oss_url = enhanced_md_oss_url
            doc.department_id = department_id
            doc.department_path = department_path
            doc.category = category
            doc.tags = tags
            doc.title_embedding = title_embedding
            doc.version = target_version
            doc.overall_status = temp_status
            doc.is_active = False
            await session.execute(delete(DocumentChunk).where(DocumentChunk.document_id == doc_id))
            await session.execute(delete(DocumentReviewItem).where(DocumentReviewItem.document_id == doc_id))

            await session.flush()

            for idx, c in enumerate(chunks):
                chunk_record = DocumentChunk(
                    id=uuid.uuid4(),
                    document_id=doc_id,
                    chunk_index=c.chunk_index,
                    chunk_label=c.chunk_label,
                    content=c.content,
                    is_code=c.is_code,
                    is_table=c.is_table,
                    table_html=c.table_html,
                    dense_embedding=dense_embeddings[idx],
                    sparse_vector=sparse_vectors[idx],
                    tsv_content=func.to_tsvector("simple", tsv_strings[idx]),
                    page_idx=c.page_idx,
                    bbox=c.bbox,
                    breadcrumb=c.breadcrumb,
                    asset_url=c.asset_url,
                    department_path=department_path,
                )
                session.add(chunk_record)

            if pending_reviews:
                oss_map = review_asset_oss_map or {}
                for rev in pending_reviews:
                    if not isinstance(rev, dict):
                        continue
                    item_key = rev["item_id"]
                    review_rec = DocumentReviewItem(
                        id=uuid.uuid4(),
                        document_id=doc_id,
                        item_key=item_key,
                        type=rev.get("type", "image"),
                        title=rev.get("title", ""),
                        page_idx=rev.get("page_idx", 0),
                        display_page=rev.get("display_page", 1),
                        line_number=rev.get("line_number"),
                        asset_url=oss_map.get(item_key) or "",
                        raw_content=rev.get("raw_content"),
                        vlm_description=rev.get("vlm_description", ""),
                        user_description=rev.get("user_description"),
                        remark=rev.get("remark"),
                        status=rev.get("status", "pending"),
                    )
                    session.add(review_rec)

            await session.flush()

            count_res = await session.execute(
                select(func.count()).select_from(DocumentChunk).where(DocumentChunk.document_id == doc_id)
            )
            written_count = count_res.scalar()
            if written_count != total_chunks:
                raise RuntimeError(
                    f"【入库自校验失败】期望写入 {total_chunks} 个切片，实际查询到 {written_count} 个切片！"
                )

            doc.overall_status = "active"
            doc.is_active = True

            if old_doc is not None:
                old_doc.overall_status = "archived"
                old_doc.is_active = False
                logger.info(
                    f"【蓝绿无损切换完成】旧版文档 (v{old_doc.version}, id={old_doc_id}) "
                    f"已标记为 archived，新版文档 (v{target_version}, id={doc_id}) 已正式生效 (active)！"
                )

            await session.commit()
            logger.info(f"【入库事务成功提交】文档 id={doc_id}, kb_id={kb_id}, level=L{eff_doc_level}, 状态=active")
            return doc

        except Exception as err:
            logger.error(f"【入库异常回滚】入库过程中断，立即执行全量事务回滚: {err}", exc_info=True)
            await session.rollback()
            raise


update_service = BlueGreenUpdateService()
