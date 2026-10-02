"""src/rag_kb/nodes/loader.py —— Markdown 加载与几何图元对齐节点 LoaderNode。

流水线第三环：
读取 MinerU 解析产出的 full.md 与 content_list.json，
加载 Markdown 全文排版结构与所有几何图元（图片、表格、标题位置）并注入 IngestState。
"""

import json
from pathlib import Path
from typing import Optional
from langchain_core.runnables import RunnableConfig
from langgraph.types import StreamWriter
from rag_kb.core.config import settings
from rag_kb.core.constants import IngestStatus
from rag_kb.graph.state import IngestState
from rag_kb.nodes.base import BaseNode

class LoaderNode(BaseNode):
    """节点 3: MD加载与图元对齐"""

    def __init__(self):
        super().__init__(step_index=3, name="MD加载与图元对齐")

    async def process(
        self,
        state: IngestState,
        config: Optional[RunnableConfig] = None,
        writer: Optional[StreamWriter] = None
    ) -> IngestState:
        doc_id = state.get("doc_id")
        parsed_dir = state.get("parsed_dir")
        doc_dir = Path(parsed_dir) if parsed_dir else (Path(settings.DATA_PARSED_DIR) / doc_id)

        # 读取 full.md
        full_md_path = doc_dir / "full.md"
        if not full_md_path.exists():
            raise FileNotFoundError(f"【加载错误】未找到解析产出的 Markdown 文件: {full_md_path}")

        with open(full_md_path, "r", encoding="utf-8") as f:
            md_content = f.read()
        state["md_content"] = md_content

        # 读取图元列表
        parse_res = state.get("parse_res") or {}
        content_list = parse_res.get("content_list") or []
        if not content_list:
            cl_path = doc_dir / f"{doc_id}_content_list.json"
            if cl_path.exists():
                with open(cl_path, "r", encoding="utf-8") as f:
                    content_list = json.load(f)

        state["content_list"] = content_list
        state["overall_status"] = IngestStatus.LOADER.value

        if writer and callable(writer):
            writer({
                "event": "step_progress",
                "step": 3,
                "status": "completed",
                "detail": f"成功加载 Markdown 结构，共 {len(content_list)} 个几何图元"
            })

        return state

loader_node = LoaderNode()
