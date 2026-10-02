"""src/rag_kb/nodes/splitter.py —— 两阶段混合切分节点 SplitterNode。

流水线第九环：
对 full.enhanced.md 执行两阶段混合切分（文档块与段落级滑动窗口递归切分），
并继承多模态图表元数据、父级标题链 (Breadcrumb) 与物理页码坐标。
"""

import logging
from typing import Optional
from langchain_core.runnables import RunnableConfig
from langgraph.types import StreamWriter
from rag_kb.core.constants import IngestStatus
from rag_kb.graph.state import IngestState
from rag_kb.nodes.base import BaseNode
from rag_kb.services.pipeline.chunker import chunker

logger = logging.getLogger(__name__)

class SplitterNode(BaseNode):
    """节点 9: Markdown 两阶段混合切分"""

    def __init__(self):
        super().__init__(step_index=9, name="两阶段混合切分")

    async def process(
        self,
        state: IngestState,
        config: Optional[RunnableConfig] = None,
        writer: Optional[StreamWriter] = None
    ) -> IngestState:
        enhanced_md_content = state.get("enhanced_md_content")
        if not enhanced_md_content:
            raise ValueError(f"【切分错误】未获取到增强版 Markdown 内容 (doc_id={state.get('doc_id')})")

        department = state.get("department", "技术部")
        content_list = state.get("content_list", [])

        if writer and callable(writer):
            writer({
                "event": "step_progress",
                "step": 9,
                "status": "running",
                "detail": "正在执行两阶段语义与代码切分..."
            })

        # 调用切分器进行结构化切分
        raw_chunks = chunker.split_enhanced_markdown(
            markdown_text=enhanced_md_content,
            department=department,
            content_list=content_list
        )

        state["raw_chunks"] = raw_chunks
        state["overall_status"] = IngestStatus.SPLITTER.value

        if writer and callable(writer):
            writer({
                "event": "step_progress",
                "step": 9,
                "status": "completed",
                "detail": f"完成切分，产出 {len(raw_chunks)} 个切片",
                "chunk_count": len(raw_chunks)
            })

        return state

splitter_node = SplitterNode()
