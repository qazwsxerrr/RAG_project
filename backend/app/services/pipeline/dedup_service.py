import logging
from typing import Dict, Any, Optional, Tuple, List
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.services.retrieval.embedding_service import embedding_service

logger = logging.getLogger(__name__)

class DeduplicationService:
    """文档标题特征向量防重检测服务 (Phase 2 - 节点 11 前置判定)

    将新文档的 文件名 + 各级标题 组装并计算 1024 维特征向量，
    通过 PostgreSQL pgvector HNSW 余弦相似度进行快速检索。
    若相似度 >= 0.88，触发 duplicate_warning 预警，等待人工确认或自动升级。
    """

    SIMILARITY_THRESHOLD = 0.88

    def extract_top_headings(self, markdown_text: str) -> Tuple[str, str]:
        """从 Markdown 源码中快速抽取首个一级标题和二级标题"""
        h1, h2 = "", ""
        for line in markdown_text.splitlines():
            line_s = line.strip()
            if not h1 and line_s.startswith("# ") and not line_s.startswith("## "):
                h1 = line_s[2:].strip()
            elif not h2 and line_s.startswith("## ") and not line_s.startswith("### "):
                h2 = line_s[3:].strip()
            if h1 and h2:
                break
        return h1, h2

    async def check_duplicate(
        self,
        session: AsyncSession,
        doc_id: str,
        file_name: str,
        markdown_text: str = "",
        h1: str = "",
        h2: str = ""
    ) -> Dict[str, Any]:
        """执行标题向量防重检测"""
        if not h1 and not h2 and markdown_text:
            h1, h2 = self.extract_top_headings(markdown_text)

        # 1. 计算 1024 维标题组合特征向量
        title_vec = await embedding_service.embed_title(file_name, h1, h2)
        title_vec_str = f"[{','.join(str(x) for x in title_vec)}]"

        # 2. 在 PostgreSQL 16 中通过 Cosine 距离执行 ANN 检索
        query = text("""
            SELECT id, title, file_hash, version, overall_status,
                   1 - (title_embedding <=> CAST(:vec AS vector)) AS similarity
            FROM rag_documents
            WHERE title_embedding IS NOT NULL
              AND id != CAST(:doc_id AS uuid)
              AND overall_status IN ('active', 'indexing', 'parsed_ready_for_chunking', 'awaiting_review')
            ORDER BY title_embedding <=> CAST(:vec AS vector)
            LIMIT 1;
        """)

        result = await session.execute(query, {"vec": title_vec_str, "doc_id": doc_id})
        row = result.fetchone()

        if row and row.similarity is not None:
            sim = float(row.similarity)
            matched_id = str(row.id)
            matched_title = row.title
            matched_version = row.version

            logger.info(f"【防重比对】doc_id={doc_id} 最高匹配相似度: {sim:.4f} (匹配文档: {matched_title})")

            if sim >= self.SIMILARITY_THRESHOLD:
                return {
                    "is_duplicate": True,
                    "similarity": round(sim, 4),
                    "matched_doc_id": matched_id,
                    "matched_title": matched_title,
                    "matched_version": matched_version,
                    "title_embedding": title_vec,
                    "message": f"检测到高度相似文档《{matched_title}》(相似度 {sim:.2%})，请选择是覆盖更新还是作为独立新文档入库"
                }

            return {
                "is_duplicate": False,
                "similarity": round(sim, 4),
                "matched_doc_id": matched_id,
                "matched_title": matched_title,
                "matched_version": matched_version,
                "title_embedding": title_vec,
                "message": "文档特征未命中重复预警，可直接入库"
            }

        return {
            "is_duplicate": False,
            "similarity": 0.0,
            "matched_doc_id": None,
            "matched_title": None,
            "matched_version": 0,
            "title_embedding": title_vec,
            "message": "知识库暂无存量文档或特征未命中"
        }

dedup_service = DeduplicationService()
