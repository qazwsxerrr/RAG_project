"""src/rag_kb/nodes/ingest.py —— 接入节点 IngestNode。

流水线 START 之后的第 1 个业务节点：
校验文件内容或来源地址，计算 SHA-256 校验和，
并把原件上传至阿里云 OSS 归档目录，记录 raw_oss_url。
"""

import asyncio
from pathlib import Path
from typing import Optional
from langchain_core.runnables import RunnableConfig
from langgraph.types import StreamWriter
from rag_kb.core.constants import IngestStatus
from rag_kb.graph.state import IngestState
from rag_kb.nodes.base import BaseNode
from rag_kb.utils.oss import oss_service

class IngestNode(BaseNode):
    """节点 1: 接入 (Ingestion)"""

    def __init__(self):
        super().__init__(step_index=1, name="接入")

    async def process(
        self,
        state: IngestState,
        config: Optional[RunnableConfig] = None,
        writer: Optional[StreamWriter] = None
    ) -> IngestState:
        file_bytes = state.get("file_bytes")
        file_name = state.get("file_name", "document.pdf")
        temp_file_path = state.get("temp_file_path")

        # 优先从内存 bytes 获取，若为空则从临时文件路径读取
        if not file_bytes and temp_file_path and Path(temp_file_path).exists():
            with open(temp_file_path, "rb") as f:
                file_bytes = f.read()
            state["file_bytes"] = file_bytes

        if not file_bytes:
            raise ValueError(f"【接入错误】未获取到文件二进制内容: {file_name}")

        sha256 = oss_service.calculate_sha256(file_bytes)
        state["file_hash"] = sha256

        department = state.get("department", "技术部")
        clean_dept = department.strip("/").replace("/", "_")
        target_oss_key = f"rag_storage/raw/{clean_dept}/{sha256[:8]}_{file_name}"

        # 异步线程池上传原件到 OSS
        raw_oss_url = await asyncio.to_thread(
            oss_service.upload_file,
            file_bytes,
            target_oss_key
        )
        state["raw_oss_url"] = raw_oss_url
        state["overall_status"] = IngestStatus.INGEST.value

        return state

ingest_node = IngestNode()
