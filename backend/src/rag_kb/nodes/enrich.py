"""src/rag_kb/nodes/enrich.py —— 图片多模态语义描述节点 EnrichNode。

流水线第五环：
并发调用视觉多模态大模型 (VLM)，识别架构图/流程图与普通插图，
生成深度业务级语义摘要，将其注入 content_list 中的配图图元中。
"""

import asyncio
import logging
from typing import Dict, Any, Optional
from langchain_core.runnables import RunnableConfig
from langgraph.types import StreamWriter
from rag_kb.core.constants import IngestStatus
from rag_kb.graph.state import IngestState
from rag_kb.nodes.base import BaseNode
from rag_kb.services.parser.code_flowchart_service import code_flowchart_service
from rag_kb.services.parser.vlm_service import vlm_service
from rag_kb.utils.oss import oss_service

logger = logging.getLogger(__name__)

class EnrichNode(BaseNode):
    """节点 5: 图片多模态语义描述 (VLM)"""

    def __init__(self):
        super().__init__(step_index=5, name="多模态配图增强")

    async def process(
        self,
        state: IngestState,
        config: Optional[RunnableConfig] = None,
        writer: Optional[StreamWriter] = None
    ) -> IngestState:
        doc_id = state.get("doc_id")
        file_name = state.get("file_name", "document.pdf")
        content_list = state.get("content_list", [])

        image_elements = [el for el in content_list if el.get("type") == "image"]
        img_sem = asyncio.Semaphore(4)

        if writer and callable(writer):
            writer({
                "event": "step_progress",
                "step": 5,
                "status": "running",
                "detail": f"正在并发调用 VLM 分析 {len(image_elements)} 张配图语义..."
            })

        async def _process_image(idx: int, img_el: Dict[str, Any]):
            raw_url = img_el.get("oss_url", "")
            if not raw_url:
                img_el["vlm_description"] = img_el.get("alt", "") or "文档配图"
                return

            signed_img_url = (
                oss_service.sign_url(raw_url, expires=3600)
                if ("aliyuncs.com" in raw_url and "OSSAccessKeyId" not in raw_url)
                else raw_url
            )
            is_flowchart = code_flowchart_service.is_flowchart_image(img_el)
            img_el["is_flowchart"] = is_flowchart

            ctx = {
                "doc_name": file_name,
                "heading": img_el.get("heading", ""),
                "surrounding_text": img_el.get("alt", "") or file_name,
                "element_type": "flowchart" if is_flowchart else "image"
            }

            async with img_sem:
                logger.info(f"▶ [VLM] 正在分析第 {idx + 1}/{len(image_elements)} 张配图 (页码: {img_el.get('page_idx', 0) + 1})...")
                if is_flowchart:
                    desc = await asyncio.to_thread(vlm_service.describe_flowchart, signed_img_url, ctx)
                else:
                    desc = await asyncio.to_thread(vlm_service.describe_image, signed_img_url, ctx)
                img_el["vlm_description"] = desc

        if image_elements:
            await asyncio.gather(*[_process_image(i, el) for i, el in enumerate(image_elements)])
        else:
            logger.info(f"▶ [VLM] 文档 (doc_id={doc_id}) 无独立配图切图资产，自动跳过视觉理解。")

        state["content_list"] = content_list
        state["overall_status"] = IngestStatus.ENRICH.value

        if writer and callable(writer):
            writer({
                "event": "step_progress",
                "step": 5,
                "status": "completed",
                "detail": f"完成 {len(image_elements)} 张配图多模态描述"
            })

        return state

enrich_node = EnrichNode()
