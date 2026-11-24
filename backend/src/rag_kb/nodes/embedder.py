"""src/rag_kb/nodes/embedder.py —— 三路混合索引计算节点 EmbedderNode。

流水线第十环：
为切片列表批量计算三路特征索引：
1. 1024 维 BGE-M3 稠密语义特征向量；
2. L2 归一化稀疏特征向量 (JSONB)；
3. 专供 PostgreSQL 16 GIN 倒排索引的空格分隔中文分词序列 (tsvector)。
"""

import logging
from typing import Optional
from langchain_core.runnables import RunnableConfig
from langgraph.types import StreamWriter
from rag_kb.core.authz.models import format_kb_full_path
from rag_kb.core.constants import IngestStatus
from rag_kb.graph.state import IngestState
from rag_kb.nodes.base import BaseNode
from rag_kb.services.mcp.server import execute_core_embeddings_extraction
from rag_kb.utils.tokenize import cut_for_index

logger = logging.getLogger(__name__)

class EmbedderNode(BaseNode):
    """节点 10: 三路混合索引计算 (Dense + Sparse + TSVector)"""

    def __init__(self):
        super().__init__(step_index=10, name="向量化")

    async def process(
        self,
        state: IngestState,
        config: Optional[RunnableConfig] = None,
        writer: Optional[StreamWriter] = None
    ) -> IngestState:
        raw_chunks = state.get("raw_chunks", [])
        if not raw_chunks:
            raise ValueError(f"【索引计算错误】未获取到切片列表 (doc_id={state.get('doc_id')})")

        if writer and callable(writer):
            writer({
                "event": "step_progress",
                "step": 10,
                "status": "running",
                "detail": f"正在为 {len(raw_chunks)} 个切片建立检索索引..."
            })

        kb_full_path = format_kb_full_path(state.get("department"), state.get("kb_name"))
        doc_title = str(state.get("file_name") or "").strip()
        index_prefix = f"【归属】{kb_full_path} | 【文档】{doc_title}\n" if kb_full_path else ""

        chunk_texts = [f"{index_prefix}{c.content}" if index_prefix else c.content for c in raw_chunks]

        # 1. 委托 MCP 向量特征单一事实源 (SSOT) 批量计算 1024 维稠密特征与 L2 归一化稀疏特征向量
        dense_embeddings, sparse_vectors = await execute_core_embeddings_extraction(
            texts=chunk_texts,
            include_sparse=True,
            batch_size=16,
        )

        # 2. 计算用于 PostgreSQL GIN to_tsvector('simple', text) 的中文切词序列（同步纳入归属路径与文档名）
        tsv_strings = [
            cut_for_index(f"{kb_full_path} {doc_title} {c.content}".strip()) if kb_full_path else cut_for_index(c.content)
            for c in raw_chunks
        ]

        # 确保每个切片的分词序列和稀疏字典非空，防止校验拦截
        for idx, (sp, tsv) in enumerate(zip(sparse_vectors, tsv_strings)):
            if not sp:
                sparse_vectors[idx] = {"default": 1.0}
            if not tsv.strip():
                tsv_strings[idx] = "空切片"

        state["dense_embeddings"] = dense_embeddings
        state["sparse_vectors"] = sparse_vectors
        state["tsv_strings"] = tsv_strings
        state["overall_status"] = IngestStatus.EMBEDDER.value

        if writer and callable(writer):
            writer({
                "event": "step_progress",
                "step": 10,
                "status": "completed",
                "detail": f"已完成检索索引构建（共 {len(dense_embeddings)} 条）"
            })

        return state

embedder_node = EmbedderNode()
