"""src/rag_kb/nodes/mineru.py —— MinerU 解析节点 MineruNode。

流水线第二环，调用 MinerU 适配器执行深度版面分析：
产出 full.md (排版结构)、layout.json (版面树)、{doc_id}_content_list.json (图元列表与物理坐标) 与 images/。
"""

import asyncio
from pathlib import Path
from typing import Optional
from langchain_core.runnables import RunnableConfig
from langgraph.types import StreamWriter
from rag_kb.core.config import settings
from rag_kb.core.constants import IngestStatus
from rag_kb.graph.state import IngestState
from rag_kb.nodes.base import BaseNode
from rag_kb.services.parser.mineru_adapter import mineru_adapter

class MineruNode(BaseNode):
    """节点 2: MinerU 解析"""

    def __init__(self):
        super().__init__(step_index=2, name="MinerU解析")

    async def process(
        self,
        state: IngestState,
        config: Optional[RunnableConfig] = None,
        writer: Optional[StreamWriter] = None
    ) -> IngestState:
        doc_id = state.get("doc_id")
        file_name = state.get("file_name", "document.pdf")
        temp_file_path = state.get("temp_file_path")
        file_bytes = state.get("file_bytes")
        raw_oss_url = state.get("raw_oss_url")

        # 保证本地存在物理文件供解析引擎读取
        if not temp_file_path or not Path(temp_file_path).exists():
            if not file_bytes:
                raise ValueError(f"【MinerU 错误】未提供有效的文件路径或二进制内容 (doc_id={doc_id})")
            target_dir = Path(settings.DATA_PARSED_DIR) / doc_id
            target_dir.mkdir(parents=True, exist_ok=True)
            local_path = target_dir / file_name
            with open(local_path, "wb") as f:
                f.write(file_bytes)
            temp_file_path = str(local_path)
            state["temp_file_path"] = temp_file_path

        if writer and callable(writer):
            writer({
                "event": "step_progress",
                "step": 2,
                "status": "running",
                "detail": "正在调用 MinerU 解析引擎进行高精版面分析..."
            })

        # 异步线程执行解析
        parse_res = await asyncio.to_thread(
            mineru_adapter.parse_document,
            doc_id=doc_id,
            file_path=temp_file_path,
            file_name=file_name,
            file_url=raw_oss_url
        )

        state["parse_res"] = parse_res
        state["parsed_dir"] = parse_res.get("doc_dir")
        state["overall_status"] = IngestStatus.MINERU.value

        return state

mineru_node = MineruNode()
