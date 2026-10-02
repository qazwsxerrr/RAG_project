"""src/rag_kb/nodes/table.py —— 表格缝合、代码块与流程图语义提炼节点 TableNode。

流水线第六环：
1. 识别并跨页缝合超长表格，调用大模型提炼 Markdown 结构化摘要；
2. 提取 Markdown 中的代码块与 Mermaid 流程图，提炼业务级架构语义；
3. 装配待人工核对审查项列表 pending_reviews（包含真实坐标、切图代理与初版语义）。
"""

import asyncio
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional
from langchain_core.runnables import RunnableConfig
from langgraph.types import StreamWriter
from rag_kb.api.schemas import ReviewItem
from rag_kb.core.config import settings
from rag_kb.core.constants import IngestStatus
from rag_kb.graph.state import IngestState
from rag_kb.nodes.base import BaseNode
from rag_kb.services.parser.code_flowchart_service import (
    code_flowchart_service,
    ParsedCodeBlock,
    ParsedFlowchartBlock
)
from rag_kb.services.parser.table_service import table_stitching_service, ParsedTableBlock
from rag_kb.services.parser.vlm_service import vlm_service
from rag_kb.utils.oss import oss_service

logger = logging.getLogger(__name__)

class TableNode(BaseNode):
    """节点 6: 表格跨页缝合与代码/流程图提炼"""

    def __init__(self):
        super().__init__(step_index=6, name="表格缝合与代码提炼")

    async def process(
        self,
        state: IngestState,
        config: Optional[RunnableConfig] = None,
        writer: Optional[StreamWriter] = None
    ) -> IngestState:
        doc_id = state.get("doc_id")
        file_name = state.get("file_name", "document.pdf")
        content_list = state.get("content_list", [])
        parsed_dir = state.get("parsed_dir")
        doc_dir = Path(parsed_dir) if parsed_dir else (Path(settings.DATA_PARSED_DIR) / doc_id)

        if writer and callable(writer):
            writer({
                "event": "step_progress",
                "step": 6,
                "status": "running",
                "detail": "正在执行表格跨页缝合与代码流程图提炼..."
            })

        # 1. 表格提取与跨页缝合
        raw_tables = [el for el in content_list if el.get("type") == "table"]
        processed_tables: List[ParsedTableBlock] = table_stitching_service.process_document_tables(
            doc_id=doc_id,
            raw_tables=raw_tables,
            doc_name=file_name
        )

        tbl_sem = asyncio.Semaphore(3)

        async def _process_table(idx: int, tbl: ParsedTableBlock):
            ctx = {
                "doc_name": file_name,
                "heading": f"第 {tbl.display_page} 页表格",
                "title": tbl.title or f"第 {tbl.display_page} 页表格",
            }
            tbl_img = tbl.image_url
            if tbl_img and "aliyuncs.com" in tbl_img and "OSSAccessKeyId" not in tbl_img:
                tbl_img = oss_service.sign_url(tbl_img, expires=3600)

            async with tbl_sem:
                logger.info(f"▶ [表格处理] 正在提取表格语义 (第 {idx + 1}/{len(processed_tables)} 个, 页码: {tbl.display_page})...")
                desc = await asyncio.to_thread(
                    vlm_service.describe_table,
                    tbl.html_content or tbl.raw_content,
                    ctx,
                    tbl_img
                )
                tbl.semantic_summary = desc

        if processed_tables:
            await asyncio.gather(*[_process_table(i, tbl) for i, tbl in enumerate(processed_tables)])

        # 2. 从 Markdown 提取代码块与 Mermaid 流程图
        full_md_path = doc_dir / "full.md"
        full_md_text = state.get("md_content") or ""
        if not full_md_text and full_md_path.exists():
            with open(full_md_path, "r", encoding="utf-8") as f:
                full_md_text = f.read()

        processed_code_blocks, processed_mermaid_flowcharts = code_flowchart_service.extract_blocks_from_markdown(
            doc_id=doc_id,
            markdown_text=full_md_text,
            content_list=content_list,
            doc_name=file_name
        )

        code_sem = asyncio.Semaphore(3)

        async def _process_code_block(idx: int, cb: ParsedCodeBlock):
            ctx = {
                "doc_name": file_name,
                "heading": f"第 {cb.display_page} 页代码块",
                "title": cb.title,
            }
            async with code_sem:
                logger.info(f"▶ [代码块提炼] 正在提炼代码语义 (第 {idx + 1}/{len(processed_code_blocks)} 个)...")
                llm_desc = await asyncio.to_thread(vlm_service.describe_code, cb.raw_code, cb.language, ctx)
                cb.semantic_summary = llm_desc

        async def _process_flowchart_block(idx: int, fb: ParsedFlowchartBlock):
            ctx = {
                "doc_name": file_name,
                "heading": f"第 {fb.display_page} 页流程图",
                "title": fb.title,
            }
            async with code_sem:
                logger.info(f"▶ [流程图提炼] 正在提炼 Mermaid 架构语义 (第 {idx + 1}/{len(processed_mermaid_flowcharts)} 个)...")
                llm_desc = await asyncio.to_thread(vlm_service.describe_flowchart_text, fb.raw_content, ctx)
                fb.semantic_summary = llm_desc

        model_tasks = []
        if processed_code_blocks:
            model_tasks.extend([_process_code_block(i, cb) for i, cb in enumerate(processed_code_blocks)])
        if processed_mermaid_flowcharts:
            model_tasks.extend([_process_flowchart_block(i, fb) for i, fb in enumerate(processed_mermaid_flowcharts)])

        if model_tasks:
            await asyncio.gather(*model_tasks)

        # 3. 装配 ReviewItem 审核项
        md_lines = full_md_text.splitlines() if full_md_text else []

        def resolve_image_physical_line(img_dict: Dict[str, Any]) -> Optional[int]:
            if not md_lines:
                return None
            img_path = img_dict.get("img_path", "")
            img_name = Path(img_path).name if img_path else ""
            if img_name:
                for l_no, line_content in enumerate(md_lines, start=1):
                    if img_name in line_content:
                        return l_no
            alt = img_dict.get("alt", "")
            if alt and len(alt.strip()) > 3:
                for l_no, line_content in enumerate(md_lines, start=1):
                    if alt in line_content:
                        return l_no
            return None

        def resolve_table_physical_line(tbl_block: ParsedTableBlock) -> Optional[int]:
            if not md_lines:
                return None
            if tbl_block.raw_content:
                first_line = tbl_block.raw_content.strip().splitlines()[0][:40]
                for l_no, line_content in enumerate(md_lines, start=1):
                    if first_line in line_content:
                        return l_no
            if tbl_block.headers:
                h0 = tbl_block.headers[0].strip()
                if h0:
                    for l_no, line_content in enumerate(md_lines, start=1):
                        if h0 in line_content and ("<table" in line_content or "<tr" in line_content or "<td" in line_content or "<th" in line_content or "|" in line_content):
                            return l_no
            if tbl_block.markdown_content:
                first_md = tbl_block.markdown_content.strip().splitlines()[0]
                for l_no, line_content in enumerate(md_lines, start=1):
                    if first_md in line_content:
                        return l_no
            return None

        review_items: List[ReviewItem] = []

        # 3.1 表格审核项
        for idx, tbl in enumerate(processed_tables):
            real_line = resolve_table_physical_line(tbl)
            table_title = (
                f"表格 {idx + 1} (第 {tbl.display_page} 页)"
                if tbl.total_parts == 1
                else f"表格 {idx + 1} (第 {tbl.display_page} 页 - 分片 {tbl.part_index}/{tbl.total_parts})"
            )
            table_text = tbl.semantic_summary or "表格语义描述"
            tbl_img = tbl.image_url or ""
            tbl_key = f"tbl_rev_{tbl.table_id}"
            tbl_proxy = f"/api/v1/documents/{doc_id}/review-items/{tbl_key}/asset" if tbl_img else ""
            review_items.append(ReviewItem(
                item_id=tbl_key,
                doc_id=doc_id,
                type="table",
                title=table_title,
                page_idx=tbl.page_idx,
                display_page=tbl.display_page,
                line_number=real_line,
                asset_url=tbl_proxy or tbl_img,
                raw_oss_url=tbl_img,
                asset_proxy_url=tbl_proxy,
                raw_content=tbl.html_content or tbl.raw_content,
                vlm_description=table_text,
                user_description=table_text,
                status="pending"
            ))

        # 3.2 配图审核项
        image_elements = [el for el in content_list if el.get("type") == "image"]
        for idx, img in enumerate(image_elements):
            real_line = resolve_image_physical_line(img)
            is_flow = img.get("is_flowchart", False)
            item_type = "flowchart" if is_flow else "image"
            item_title = (
                f"流程图 {idx + 1} (第 {img.get('page_idx', 0) + 1} 页)"
                if is_flow
                else f"配图 {idx + 1} (第 {img.get('page_idx', 0) + 1} 页)"
            )
            raw_img = img.get("oss_url", "")
            img_key = f"img_rev_{idx}"
            img_proxy = f"/api/v1/documents/{doc_id}/review-items/{img_key}/asset" if raw_img else ""
            vlm_desc = img.get("vlm_description") or "图片语义描述"
            review_items.append(ReviewItem(
                item_id=img_key,
                doc_id=doc_id,
                type=item_type,
                title=item_title,
                page_idx=img.get("page_idx", 0),
                display_page=img.get("page_idx", 0) + 1,
                line_number=real_line,
                asset_url=img_proxy or raw_img,
                raw_oss_url=raw_img,
                asset_proxy_url=img_proxy,
                raw_content=img.get("alt", ""),
                vlm_description=vlm_desc,
                user_description=vlm_desc,
                status="pending"
            ))

        # 3.3 Mermaid 流程图审核项
        for idx, flow in enumerate(processed_mermaid_flowcharts):
            review_items.append(ReviewItem(
                item_id=f"flow_rev_{flow.chart_id}",
                doc_id=doc_id,
                type="flowchart",
                title=flow.title or f"流程图 {idx + 1} (Mermaid) (第 {flow.display_page} 页)",
                page_idx=flow.page_idx,
                display_page=flow.display_page,
                line_number=flow.line_number,
                asset_url="",
                raw_content=flow.raw_content,
                vlm_description=flow.semantic_summary or "流程图架构语义",
                user_description=flow.semantic_summary or "流程图架构语义",
                status="pending"
            ))

        # 3.4 代码块审核项
        for idx, code_blk in enumerate(processed_code_blocks):
            review_items.append(ReviewItem(
                item_id=f"code_rev_{code_blk.block_id}",
                doc_id=doc_id,
                type="code",
                title=code_blk.title or f"代码块 {idx + 1} ({code_blk.language.upper()}) (第 {code_blk.display_page} 页)",
                page_idx=code_blk.page_idx,
                display_page=code_blk.display_page,
                line_number=code_blk.line_number,
                asset_url="",
                raw_content=code_blk.raw_code,
                vlm_description=code_blk.semantic_summary or "代码语义摘要",
                user_description=code_blk.semantic_summary or "代码语义摘要",
                language=code_blk.language,
                status="pending"
            ))

        state["processed_tables"] = processed_tables
        state["processed_code_blocks"] = processed_code_blocks
        state["processed_mermaid_flowcharts"] = processed_mermaid_flowcharts
        state["pending_reviews"] = [item.model_dump() for item in review_items]
        state["overall_status"] = IngestStatus.TABLE.value

        if writer and callable(writer):
            writer({
                "event": "step_progress",
                "step": 6,
                "status": "completed",
                "detail": f"完成表格与代码提炼，装配 {len(review_items)} 个待审核项",
                "pending_reviews": [item.model_dump() for item in review_items]
            })

        return state

table_node = TableNode()
