"""src/rag_kb/nodes/table_apply.py —— 表格与描述回填 Markdown 节点 TableApplyNode。

流水线第八环：
将经过人机审核确认（或模型初版）的图片、流程图、代码块与结构化表格摘要原位回填至 Markdown 占位符，
产出 full.enhanced.md 并将全文与元数据归档上云至阿里云 OSS。
"""

import asyncio
import logging
import re
from collections import defaultdict
from pathlib import Path
from typing import Dict, Any, List, Optional
from langchain_core.runnables import RunnableConfig
from langgraph.types import StreamWriter
from rag_kb.core.config import settings
from rag_kb.core.constants import IngestStatus
from rag_kb.graph.state import IngestState
from rag_kb.nodes.base import BaseNode
from rag_kb.services.parser.code_flowchart_service import ParsedCodeBlock, ParsedFlowchartBlock
from rag_kb.services.parser.table_service import ParsedTableBlock
from rag_kb.utils.oss import oss_service

logger = logging.getLogger(__name__)

class TableApplyNode(BaseNode):
    """节点 8: 表格与描述回填 Markdown (生成 full.enhanced.md)"""

    def __init__(self):
        super().__init__(step_index=8, name="表格与描述回填")

    async def process(
        self,
        state: IngestState,
        config: Optional[RunnableConfig] = None,
        writer: Optional[StreamWriter] = None
    ) -> IngestState:
        doc_id = state.get("doc_id")
        parsed_dir = state.get("parsed_dir")
        doc_dir = Path(parsed_dir) if parsed_dir else (Path(settings.DATA_PARSED_DIR) / doc_id)
        full_md_path = doc_dir / "full.md"
        enhanced_md_path = doc_dir / "full.enhanced.md"

        if writer and callable(writer):
            writer({
                "event": "step_progress",
                "step": 8,
                "status": "running",
                "detail": "正在回填多模态增强版 Markdown 与图文元数据..."
            })

        # 获取原始 Markdown 文本
        content = state.get("md_content") or ""
        if not content and full_md_path.exists():
            with open(full_md_path, "r", encoding="utf-8") as f:
                content = f.read()

        if not content:
            raise RuntimeError(f"【回填错误】未能获取原始 Markdown 内容 (doc_id={doc_id})")

        # 统一换行符为 Unix 风格 \n
        content = content.replace("\r\n", "\n")

        pending_reviews = state.get("pending_reviews", [])
        reviews_map = {r.get("item_id"): r for r in pending_reviews if isinstance(r, dict)}

        # 1. 回填配图与流程图多模态增强块
        content_list: List[Dict[str, Any]] = state.get("content_list", [])
        image_elements = [el for el in content_list if el.get("type") == "image"]
        for idx, img in enumerate(image_elements):
            page_idx = img.get("page_idx", 0)
            display_page = page_idx + 1
            oss_url = img.get("oss_url", "")
            alt_text = img.get("alt", f"图片{idx + 1}")
            original_path = img.get("img_path", "")

            item_key = f"img_rev_{idx}"
            matching_review = reviews_map.get(item_key)
            final_desc = (
                matching_review.get("user_description")
                if matching_review and matching_review.get("user_description")
                else (img.get("vlm_description") or "")
            )
            is_flow = img.get("is_flowchart", False)
            b_type = "flowchart" if is_flow else "image"

            img_block = f"""
<div class="rag-multimodal-block" data-type="{b_type}" data-page="{display_page}" data-asset="{oss_url}">
<!-- VLM_VERIFIED_DESCRIPTION_START -->
{final_desc}
<!-- VLM_VERIFIED_DESCRIPTION_END -->
![{alt_text}]({oss_url})
</div>
"""
            replaced = False
            if original_path:
                target_md = f"![{alt_text}]({original_path})"
                if target_md in content:
                    content = content.replace(target_md, img_block, 1)
                    replaced = True
                else:
                    pattern = rf'!\[.*?\]\({re.escape(original_path)}\)'
                    if re.search(pattern, content):
                        content = re.sub(pattern, img_block, content, count=1)
                        replaced = True
                    else:
                        img_filename = Path(original_path).name
                        pattern_name = rf'!\[.*?\]\([^)]*{re.escape(img_filename)}\)'
                        if re.search(pattern_name, content):
                            content = re.sub(pattern_name, img_block, content, count=1)
                            replaced = True

            if not replaced:
                logger.warning(f"【配图回填警告】未能定位配图位置 [doc_id={doc_id}, path={original_path}], 保持原地未替换！")

        # 2. 回填表格的多模态增强块
        processed_tables: List[ParsedTableBlock] = state.get("processed_tables", [])
        grouped_tables = defaultdict(list)
        for tbl in processed_tables:
            if not tbl.headers and not tbl.rows and not tbl.raw_content.strip():
                continue
            parent_id = tbl.table_id.split("_p")[0]
            grouped_tables[parent_id].append(tbl)

        for parent_id, parts in grouped_tables.items():
            parts.sort(key=lambda x: x.part_index)
            sub_chunks = []
            for tbl in parts:
                item_key = f"tbl_rev_{tbl.table_id}"
                matching_review = reviews_map.get(item_key)
                final_text = (
                    matching_review.get("user_description")
                    if matching_review and matching_review.get("user_description")
                    else tbl.semantic_summary
                )

                raw_tbl_html = tbl.html_content or ""
                chunk_html = f"""
<div class="rag-table-chunk rag-multimodal-block" data-type="table" data-table-id="{tbl.table_id}" data-page="{tbl.display_page}" data-asset="{tbl.image_url or ''}">
<!-- VLM_VERIFIED_DESCRIPTION_START -->
{final_text}
<!-- VLM_VERIFIED_DESCRIPTION_END -->
<!-- RAW_TABLE_START -->
{raw_tbl_html}
<!-- RAW_TABLE_END -->
</div>"""
                sub_chunks.append(chunk_html.strip())

            combined_blocks = "\n\n".join(sub_chunks)
            replaced = False

            # 第一优先级: 母表原始 raw_content 匹配
            parent_raw = parts[0].raw_content
            if parent_raw and parent_raw in content:
                content = content.replace(parent_raw, combined_blocks, 1)
                replaced = True

            # 第二优先级: HTML table 标签匹配
            if not replaced and "<table" in content.lower():
                target_cell = (
                    parts[0].headers[0]
                    if parts[0].headers
                    else (parts[0].rows[0][0] if parts[0].rows and parts[0].rows[0] else None)
                )
                if target_cell:
                    pattern = re.compile(
                        rf'<table[^>]*>(?:(?!<table).)*?{re.escape(target_cell)}.*?</table>',
                        re.DOTALL | re.IGNORECASE
                    )
                    m = pattern.search(content)
                    if m:
                        content = content[:m.start()] + combined_blocks + content[m.end():]
                        replaced = True

            # 第三优先级: Markdown 表格匹配
            if not replaced and parts[0].markdown_content and parts[0].markdown_content in content:
                content = content.replace(parts[0].markdown_content, combined_blocks, 1)
                replaced = True

            if not replaced:
                logger.warning(f"【表格回填警告】未能定位表格位置 [parent_id={parent_id}], 保持原地未替换！")

        # 3. 回填 Mermaid 流程图
        processed_mermaid_flowcharts: List[ParsedFlowchartBlock] = state.get("processed_mermaid_flowcharts", [])
        for flow in processed_mermaid_flowcharts:
            item_key = f"flow_rev_{flow.chart_id}"
            matching_review = reviews_map.get(item_key)
            final_desc = (
                matching_review.get("user_description")
                if matching_review and matching_review.get("user_description")
                else flow.semantic_summary
            )

            flow_block = f"""
<div class="rag-multimodal-block" data-type="flowchart" data-page="{flow.display_page}" data-asset="">
<!-- VLM_VERIFIED_DESCRIPTION_START -->
{final_desc}
<!-- VLM_VERIFIED_DESCRIPTION_END -->
<!-- RAW_CODE_START -->
{flow.raw_content}
<!-- RAW_CODE_END -->
</div>
"""
            if flow.raw_content in content:
                content = content.replace(flow.raw_content, flow_block, 1)
            elif flow.raw_content.strip() in content:
                content = content.replace(flow.raw_content.strip(), flow_block.strip(), 1)
            else:
                logger.warning(f"【流程图回填警告】未能定位流程图位置 [chart_id={flow.chart_id}], 保持原地未替换！")

        # 4. 回填代码块
        processed_code_blocks: List[ParsedCodeBlock] = state.get("processed_code_blocks", [])
        for code_blk in processed_code_blocks:
            item_key = f"code_rev_{code_blk.block_id}"
            matching_review = reviews_map.get(item_key)
            final_desc = (
                matching_review.get("user_description")
                if matching_review and matching_review.get("user_description")
                else code_blk.semantic_summary
            )

            code_block = f"""
<div class="rag-multimodal-block" data-type="code" data-page="{code_blk.display_page}" data-language="{code_blk.language}">
<!-- VLM_VERIFIED_DESCRIPTION_START -->
{final_desc}
<!-- VLM_VERIFIED_DESCRIPTION_END -->
<!-- RAW_CODE_START -->
{code_blk.raw_code}
<!-- RAW_CODE_END -->
</div>
"""
            if code_blk.raw_code in content:
                content = content.replace(code_blk.raw_code, code_block, 1)
            elif code_blk.raw_code.strip() in content:
                content = content.replace(code_blk.raw_code.strip(), code_block.strip(), 1)
            else:
                logger.warning(f"【代码块回填警告】未能定位代码块位置 [block_id={code_blk.block_id}], 保持原地未替换！")

        # 写入本地 full.enhanced.md
        with open(enhanced_md_path, "w", encoding="utf-8") as f:
            f.write(content)

        state["enhanced_md_content"] = content
        state["enhanced_md_path"] = str(enhanced_md_path)

        # 5. 上传增强版 Markdown 及结构元数据至阿里云 OSS
        enhanced_bytes = content.encode("utf-8")
        enhanced_oss_path = f"rag_storage/parsed/{doc_id}/full.enhanced.md"
        enhanced_oss_url = await asyncio.to_thread(
            oss_service.upload_file,
            enhanced_bytes,
            enhanced_oss_path,
            content_type="text/markdown; charset=utf-8"
        )
        state["enhanced_md_oss_url"] = enhanced_oss_url

        # 上传原始 full.md
        if full_md_path.exists():
            with open(full_md_path, "rb") as f:
                full_md_bytes = f.read()
            full_md_oss_path = f"rag_storage/parsed/{doc_id}/full.md"
            await asyncio.to_thread(
                oss_service.upload_file,
                full_md_bytes,
                full_md_oss_path,
                content_type="text/markdown; charset=utf-8"
            )

        # 上传版面结构元数据 content_list.json
        cl_path = doc_dir / f"{doc_id}_content_list.json"
        if cl_path.exists():
            with open(cl_path, "rb") as f:
                cl_bytes = f.read()
            cl_oss_path = f"rag_storage/parsed/{doc_id}/{doc_id}_content_list.json"
            await asyncio.to_thread(
                oss_service.upload_file,
                cl_bytes,
                cl_oss_path,
                content_type="application/json; charset=utf-8"
            )

        # 上传 layout.json
        layout_path = doc_dir / "layout.json"
        if layout_path.exists():
            with open(layout_path, "rb") as f:
                layout_bytes = f.read()
            layout_oss_path = f"rag_storage/parsed/{doc_id}/layout.json"
            await asyncio.to_thread(
                oss_service.upload_file,
                layout_bytes,
                layout_oss_path,
                content_type="application/json; charset=utf-8"
            )

        state["overall_status"] = IngestStatus.TABLE_APPLY.value

        if writer and callable(writer):
            writer({
                "event": "step_progress",
                "step": 8,
                "status": "completed",
                "detail": "成功生成增强版 Markdown 并归档上云至阿里云 OSS"
            })

        return state

table_apply_node = TableApplyNode()
