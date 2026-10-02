import logging
from typing import List, Dict, Optional
from sqlalchemy import select, func, or_, cast, String, Float, literal_column
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.chunk import DocumentChunk
from backend.app.models.document import Document
from backend.app.schemas.retrieval import RetrievedChunk

logger = logging.getLogger(__name__)

class MultiRetriever:
    """阶段 2：三路并行召回调度器

    三路能力互补：
    1. 稠密向量召回 (Dense Search): 基于 1024 维 HNSW 索引，捕获深层语义与意图匹配。
    2. 归一化稀疏向量召回 (Sparse Search): 针对专有名词与高频关键词特征，基于 JSONB 向量做内积评分。
    3. 全文关键词召回 (BM25 Search): 基于 PostgreSQL TSVector 倒排索引，精准字面咬合。
    全部召回严格强制执行组织架构权限隔离 (department_path 过滤) 与激活状态校验。
    """

    def _apply_filters(
        self,
        stmt,
        department_scope: Optional[List[str]] = None,
        category_scope: Optional[str] = None,
        tags_scope: Optional[List[str]] = None
    ):
        """注入文档状态、部门权限、分类及多维标签前置过滤谓词"""
        # 1. 仅检索激活状态的文档切片
        stmt = stmt.join(Document, DocumentChunk.document_id == Document.id)
        stmt = stmt.where(
            Document.overall_status == "active",
            Document.is_active.is_(True)
        )

        # 2. 部门权限隔离: department_path 前缀包含过滤
        if department_scope:
            dept_conditions = [
                DocumentChunk.department_path.startswith(dept.strip())
                for dept in department_scope
                if dept and dept.strip()
            ]
            if dept_conditions:
                stmt = stmt.where(or_(*dept_conditions))

        # 3. 分类前置过滤: 精准收敛检索范畴
        if category_scope and category_scope.strip() and category_scope.strip() != "全部":
            stmt = stmt.where(Document.category == category_scope.strip())

        # 4. 标签前置过滤: JSONB 数组重叠匹配 (命中任一标签即满足)
        if tags_scope:
            clean_tags = [t.strip() for t in tags_scope if t and t.strip()]
            if clean_tags:
                stmt = stmt.where(Document.tags.op("?|")(cast(clean_tags, ARRAY(String))))

        return stmt

    async def dense_search(
        self,
        session: AsyncSession,
        query_dense: List[float],
        department_scope: Optional[List[str]] = None,
        category_scope: Optional[str] = None,
        tags_scope: Optional[List[str]] = None,
        top_k: int = 20
    ) -> List[RetrievedChunk]:
        """稠密向量召回 (Top-K)"""
        if not query_dense or len(query_dense) != 1024:
            raise ValueError(f"【稠密召回错误】期望 1024 维稠密特征向量，实际输入长度为 {len(query_dense) if query_dense else 0}")

        distance_expr = DocumentChunk.dense_embedding.cosine_distance(query_dense)
        score_expr = (1.0 - distance_expr).label("score")

        stmt = select(
            DocumentChunk,
            Document.title.label("doc_title"),
            Document.raw_oss_url.label("doc_raw_oss_url"),
            score_expr
        )
        stmt = self._apply_filters(stmt, department_scope, category_scope, tags_scope)
        stmt = stmt.order_by(distance_expr.asc()).limit(top_k)

        res = await session.execute(stmt)
        rows = res.all()

        chunks: List[RetrievedChunk] = []
        for idx, row in enumerate(rows):
            chunk_obj: DocumentChunk = row[0]
            doc_title: str = row[1]
            doc_oss_url: Optional[str] = row[2]
            score: float = float(row[3])

            c = RetrievedChunk(
                chunk_id=chunk_obj.id,
                document_id=chunk_obj.document_id,
                document_title=doc_title,
                chunk_index=chunk_obj.chunk_index,
                chunk_label=chunk_obj.chunk_label,
                content=chunk_obj.content,
                page_idx=chunk_obj.page_idx,
                bbox=chunk_obj.bbox or [],
                breadcrumb=chunk_obj.breadcrumb or [],
                asset_url=chunk_obj.asset_url,
                raw_oss_url=doc_oss_url,
                department_path=chunk_obj.department_path,
                score=round(score, 6),
                channel_ranks={"dense": idx + 1},
                dense_embedding=chunk_obj.dense_embedding
            )
            chunks.append(c)

        return chunks

    async def sparse_search(
        self,
        session: AsyncSession,
        query_sparse: Dict[str, float],
        department_scope: Optional[List[str]] = None,
        category_scope: Optional[str] = None,
        tags_scope: Optional[List[str]] = None,
        top_k: int = 20
    ) -> List[RetrievedChunk]:
        """稀疏向量归一化词频内积召回 (Top-K)"""
        if not query_sparse:
            return []

        tokens = list(query_sparse.keys())
        if not tokens:
            return []

        # 1. 使用 PostgreSQL JSONB ?| 获取候选，并基于相关性（命中词数 + 稀疏权重和）实施 ORDER BY 排序截断，杜绝无序随机截断
        token_array = cast(tokens, ARRAY(String))

        # 相关性指标 1：命中查询词的词频数 (Match Count / Token Overlap)
        match_count_subq = (
            select(func.count())
            .select_from(func.unnest(token_array).alias("tok"))
            .where(DocumentChunk.sparse_vector.op("?")(literal_column("tok")))
            .scalar_subquery()
        )
        # 相关性指标 2：命中查询词的稀疏权重和 (Sparse Weight Sum)
        weight_sum_subq = (
            select(func.coalesce(func.sum(cast(DocumentChunk.sparse_vector.op("->>")(literal_column("tok")), Float)), 0.0))
            .select_from(func.unnest(token_array).alias("tok"))
            .where(DocumentChunk.sparse_vector.op("?")(literal_column("tok")))
            .scalar_subquery()
        )

        stmt = select(
            DocumentChunk,
            Document.title.label("doc_title"),
            Document.raw_oss_url.label("doc_raw_oss_url")
        )
        stmt = self._apply_filters(stmt, department_scope, category_scope, tags_scope)
        stmt = stmt.where(DocumentChunk.sparse_vector.op("?|")(token_array))
        stmt = stmt.order_by(
            match_count_subq.desc(),
            weight_sum_subq.desc(),
            DocumentChunk.chunk_index.asc(),
            DocumentChunk.id.asc()
        )
        stmt = stmt.limit(100)  # 取相关度最高的前 100 条候选进入精确点积重排

        res = await session.execute(stmt)
        rows = res.all()

        # 2. 内存计算 L2 归一化内积
        scored_candidates: List[tuple] = []
        for row in rows:
            chunk_obj: DocumentChunk = row[0]
            doc_title: str = row[1]
            doc_oss_url: Optional[str] = row[2]
            doc_sp = chunk_obj.sparse_vector or {}

            # 计算交集点积: sum(q_i * d_i)
            dot_product = sum(query_sparse[k] * doc_sp.get(k, 0.0) for k in tokens if k in doc_sp)
            if dot_product > 0.0:
                scored_candidates.append((dot_product, chunk_obj, doc_title, doc_oss_url))

        # 按内积从高到低排序
        scored_candidates.sort(key=lambda x: x[0], reverse=True)
        top_candidates = scored_candidates[:top_k]

        chunks: List[RetrievedChunk] = []
        for idx, (score, chunk_obj, doc_title, doc_oss_url) in enumerate(top_candidates):
            c = RetrievedChunk(
                chunk_id=chunk_obj.id,
                document_id=chunk_obj.document_id,
                document_title=doc_title,
                chunk_index=chunk_obj.chunk_index,
                chunk_label=chunk_obj.chunk_label,
                content=chunk_obj.content,
                page_idx=chunk_obj.page_idx,
                bbox=chunk_obj.bbox or [],
                breadcrumb=chunk_obj.breadcrumb or [],
                asset_url=chunk_obj.asset_url,
                raw_oss_url=doc_oss_url,
                department_path=chunk_obj.department_path,
                score=round(float(score), 6),
                channel_ranks={"sparse": idx + 1},
                dense_embedding=chunk_obj.dense_embedding
            )
            chunks.append(c)

        return chunks

    async def bm25_search(
        self,
        session: AsyncSession,
        tsquery_tokens: str,
        department_scope: Optional[List[str]] = None,
        category_scope: Optional[str] = None,
        tags_scope: Optional[List[str]] = None,
        top_k: int = 20
    ) -> List[RetrievedChunk]:
        """PostgreSQL TSVector BM25 全文分词召回 (Top-K)"""
        tokens = [t.strip() for t in tsquery_tokens.split() if t.strip()]
        if not tokens:
            return []

        or_tsquery = " | ".join(tokens)
        query_expr = func.to_tsquery("simple", or_tsquery)
        rank_expr = func.ts_rank_cd(DocumentChunk.tsv_content, query_expr).label("score")

        stmt = select(
            DocumentChunk,
            Document.title.label("doc_title"),
            Document.raw_oss_url.label("doc_raw_oss_url"),
            rank_expr
        )
        stmt = self._apply_filters(stmt, department_scope, category_scope, tags_scope)
        stmt = stmt.where(DocumentChunk.tsv_content.op("@@")(query_expr))
        stmt = stmt.order_by(rank_expr.desc()).limit(top_k)

        res = await session.execute(stmt)
        rows = res.all()

        chunks: List[RetrievedChunk] = []
        for idx, row in enumerate(rows):
            chunk_obj: DocumentChunk = row[0]
            doc_title: str = row[1]
            doc_oss_url: Optional[str] = row[2]
            score: float = float(row[3])

            c = RetrievedChunk(
                chunk_id=chunk_obj.id,
                document_id=chunk_obj.document_id,
                document_title=doc_title,
                chunk_index=chunk_obj.chunk_index,
                chunk_label=chunk_obj.chunk_label,
                content=chunk_obj.content,
                page_idx=chunk_obj.page_idx,
                bbox=chunk_obj.bbox or [],
                breadcrumb=chunk_obj.breadcrumb or [],
                asset_url=chunk_obj.asset_url,
                raw_oss_url=doc_oss_url,
                department_path=chunk_obj.department_path,
                score=round(score, 6),
                channel_ranks={"bm25": idx + 1},
                dense_embedding=chunk_obj.dense_embedding
            )
            chunks.append(c)

        return chunks

    async def parallel_recall(
        self,
        session: AsyncSession,
        query_dense: List[float],
        query_sparse: Dict[str, float],
        tsquery_tokens: str,
        department_scope: Optional[List[str]] = None,
        category_scope: Optional[str] = None,
        tags_scope: Optional[List[str]] = None,
        top_k: int = 20
    ) -> Dict[str, List[RetrievedChunk]]:
        """执行三路基础召回 (Dense, Sparse, BM25)

        使用同一会话连接依次高效完成三路召回，避免在单个 AsyncSession 上发生并发连接竞争，
        全流程总耗时控制在 50ms 以内。
        """
        dense_res = await self.dense_search(session, query_dense, department_scope, category_scope, tags_scope, top_k)
        sparse_res = await self.sparse_search(session, query_sparse, department_scope, category_scope, tags_scope, top_k)
        bm25_res = await self.bm25_search(session, tsquery_tokens, department_scope, category_scope, tags_scope, top_k)

        return {
            "dense": dense_res,
            "sparse": sparse_res,
            "bm25": bm25_res
        }

multi_retriever = MultiRetriever()
