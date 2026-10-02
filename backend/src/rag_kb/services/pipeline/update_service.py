import uuid
import logging
from typing import List, Dict, Any, Optional
from sqlalchemy import select, func, update, delete
from sqlalchemy.ext.asyncio import AsyncSession

from rag_kb.db.models import Document, DocumentChunk, DocumentReviewItem
from rag_kb.services.pipeline.chunker import RawChunkData

logger = logging.getLogger(__name__)

class BlueGreenUpdateService:
    """蓝绿安全入库与版本无损原子切换服务 (Phase 2 - 节点 11)

    企业级生产准则:
    1. 严禁先删除后解析: 绝不允许在未确认新数据完整写入前抹除旧数据；
    2. 全量暂存校验: 新版本切片全量入库后，执行切片总数与向量完整性物理自校验；
    3. 事务原子切换: 校验通过后，在单一 DB 事务中将新文档置为 active，旧文档置为 archived；
    4. 异常全量回滚: 任意一步报错立即回滚，存量知识库毫发无损。
    """

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
        target_version: int = 1,
        old_doc_id: Optional[uuid.UUID] = None,
        pending_reviews: Optional[List[Dict[str, Any]]] = None
    ) -> Document:
        """执行蓝绿事务入库与指针原子切换"""

        # -------------------------------------------------------------
        # 1. 前置入参完整性自检 (Fail-Fast)
        # -------------------------------------------------------------
        total_chunks = len(chunks)
        if total_chunks == 0:
            raise RuntimeError("【入库红线阻断】待入库切片数量为 0，拒绝提交空数据！")

        if not (total_chunks == len(dense_embeddings) == len(sparse_vectors) == len(tsv_strings)):
            raise RuntimeError(
                f"【入库特征不一致】切片数 ({total_chunks})、稠密向量数 ({len(dense_embeddings)})、"
                f"稀疏向量数 ({len(sparse_vectors)}) 与分词数 ({len(tsv_strings)}) 不匹配！"
            )

        # 校验向量维度与非空
        for idx in range(total_chunks):
            if len(dense_embeddings[idx]) != 1024:
                raise RuntimeError(f"【向量异常】第 {idx} 个切片的稠密向量非 1024 维！")
            if not sparse_vectors[idx]:
                raise RuntimeError(f"【向量异常】第 {idx} 个切片的稀疏向量字典为空！")
            if not tsv_strings[idx].strip():
                raise RuntimeError(f"【分词异常】第 {idx} 个切片的 TSVector 分词序列为空！")

        logger.info(f"【蓝绿入库启动】doc_id={doc_id}, 切片总数={total_chunks}, 目标版本=v{target_version}")

        try:
            # -------------------------------------------------------------
            # 2. 写入/暂存新版本文档主表记录 (状态设为 indexing_v{target_version})
            # -------------------------------------------------------------
            doc_stmt = select(Document).where(Document.id == doc_id)
            doc_res = await session.execute(doc_stmt)
            doc = doc_res.scalar_one_or_none()

            temp_status = f"indexing_v{target_version}" if target_version > 1 else "indexing"

            if doc:
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
                # 清理若有历史重试遗留的残余切片与审核项
                await session.execute(delete(DocumentChunk).where(DocumentChunk.document_id == doc_id))
                await session.execute(delete(DocumentReviewItem).where(DocumentReviewItem.document_id == doc_id))
            else:
                doc = Document(
                    id=doc_id,
                    title=file_name,
                    file_hash=file_hash,
                    raw_oss_url=raw_oss_url,
                    enhanced_md_oss_url=enhanced_md_oss_url,
                    department_id=department_id,
                    department_path=department_path,
                    category=category,
                    tags=tags,
                    title_embedding=title_embedding,
                    version=target_version,
                    overall_status=temp_status,
                    is_active=False
                )
                session.add(doc)

            await session.flush()

            # -------------------------------------------------------------
            # 3. 批量生成并插入新版本切片数据 (rag_document_chunks)
            # -------------------------------------------------------------
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
                    tsv_content=func.to_tsvector('simple', tsv_strings[idx]),
                    page_idx=c.page_idx,
                    bbox=c.bbox,
                    breadcrumb=c.breadcrumb,
                    asset_url=c.asset_url,
                    department_path=department_path
                )
                session.add(chunk_record)

            # -------------------------------------------------------------
            # 3.1 批量持久化图元审核项记录 (rag_document_reviews)
            # -------------------------------------------------------------
            if pending_reviews:
                for rev in pending_reviews:
                    if not isinstance(rev, dict):
                        continue
                    item_key = rev.get("item_id") or rev.get("item_key") or f"rev_{uuid.uuid4().hex[:8]}"
                    review_rec = DocumentReviewItem(
                        id=uuid.uuid4(),
                        document_id=doc_id,
                        item_key=item_key,
                        type=rev.get("type", "image"),
                        title=rev.get("title", ""),
                        page_idx=rev.get("page_idx", 0),
                        display_page=rev.get("display_page", 1),
                        line_number=rev.get("line_number"),
                        asset_url=rev.get("asset_url", "") or "",
                        raw_content=rev.get("raw_content"),
                        vlm_description=rev.get("vlm_description", ""),
                        user_description=rev.get("user_description"),
                        remark=rev.get("remark"),
                        status=rev.get("status", "pending")
                    )
                    session.add(review_rec)

            await session.flush()

            # -------------------------------------------------------------
            # 4. 物理自校验: 确认数据库切片写入数量与向量非空
            # -------------------------------------------------------------
            count_stmt = select(func.count()).select_from(DocumentChunk).where(DocumentChunk.document_id == doc_id)
            count_res = await session.execute(count_stmt)
            written_count = count_res.scalar()

            if written_count != total_chunks:
                raise RuntimeError(
                    f"【入库自校验失败】期望写入 {total_chunks} 个切片，实际查询到 {written_count} 个切片！"
                )

            # -------------------------------------------------------------
            # 5. 原子切换指针: 新文档激活，旧文档下线 (Blue-Green Switch)
            # -------------------------------------------------------------
            doc.overall_status = "active"
            doc.is_active = True

            if old_doc_id and old_doc_id != doc_id:
                old_stmt = select(Document).where(Document.id == old_doc_id)
                old_res = await session.execute(old_stmt)
                old_doc = old_res.scalar_one_or_none()
                if old_doc:
                    old_doc.overall_status = "archived"
                    old_doc.is_active = False
                    logger.info(
                        f"【蓝绿无损切换完成】旧版文档 《{old_doc.title}》 (v{old_doc.version}, id={old_doc_id}) "
                        f"已标记为 archived，新版文档 (v{target_version}, id={doc_id}) 已正式生效 (active)！"
                    )

            # 提交数据库事务
            await session.commit()
            logger.info(f"【入库事务成功提交】文档 id={doc_id}, 状态=active, 版本=v{target_version}")
            return doc

        except Exception as err:
            logger.error(f"【入库异常回滚】入库过程中断，立即执行全量事务回滚: {err}", exc_info=True)
            await session.rollback()
            raise

update_service = BlueGreenUpdateService()
