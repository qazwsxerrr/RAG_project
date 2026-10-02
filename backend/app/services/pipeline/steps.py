import asyncio
import time
import re
import uuid
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional
from sqlalchemy import select

logger = logging.getLogger(__name__)
import json
from backend.app.core.config import settings
from backend.app.core.oss import oss_service
from backend.app.core.database import AsyncSessionLocal
from backend.app.models.document import Department
from backend.app.services.parser.mineru_adapter import mineru_adapter
from backend.app.services.parser.vlm_service import vlm_service
from backend.app.services.parser.table_service import table_stitching_service, ParsedTableBlock
from backend.app.services.parser.code_flowchart_service import code_flowchart_service, ParsedCodeBlock, ParsedFlowchartBlock
from backend.app.services.pipeline.state_machine import PipelineInstance, CheckpointManager
from backend.app.schemas.pipeline import ReviewItem
from backend.app.services.pipeline.chunker import chunker
from backend.app.services.retrieval.embedding_service import embedding_service
from backend.app.services.retrieval.vector_utils import extract_sparse_vector, format_tsvector_tokens
from backend.app.services.pipeline.dedup_service import dedup_service
from backend.app.services.pipeline.update_service import update_service

class PipelineExecutor:
    """入库流水线异步执行引擎 (支持 1~11 步端到端自动执行与人机协同挂起)"""

    async def run_pipeline(
        self,
        instance: PipelineInstance,
        temp_file_path: str,
        file_bytes: bytes,
        skip_review: bool = False
    ):
        """异步执行流水线"""
        doc_id = instance.doc_id
        file_name = instance.file_name

        try:
            # -------------------------------------------------------------
            # 节点 1: 接入 (Ingestion)
            # -------------------------------------------------------------
            step_start = time.time()
            instance.update_step_status(1, "running")
            await instance.broadcast_event("step_progress", {"step": 1, "status": "running"})

            sha256 = oss_service.calculate_sha256(file_bytes)
            ext = Path(file_name).suffix.lower()
            target_oss_raw = f"rag_storage/raw/{instance.department}/{sha256[:8]}_{file_name}"
            raw_oss_url = await asyncio.to_thread(oss_service.upload_file, file_bytes, target_oss_raw)
            instance.context["raw_oss_url"] = raw_oss_url
            instance.context["sha256"] = sha256

            duration = int((time.time() - step_start) * 1000)
            instance.update_step_status(1, "completed", duration)
            await instance.broadcast_event("step_progress", {"step": 1, "status": "completed", "duration_ms": duration})

            # -------------------------------------------------------------
            # 节点 2: MinerU 解析
            # -------------------------------------------------------------
            step_start = time.time()
            instance.update_step_status(2, "running")
            await instance.broadcast_event("step_progress", {"step": 2, "status": "running"})

            raw_oss_url = instance.context.get("raw_oss_url")
            parse_res = await asyncio.to_thread(
                mineru_adapter.parse_document,
                doc_id=doc_id,
                file_path=temp_file_path,
                file_name=file_name,
                file_url=raw_oss_url
            )
            instance.context["parse_res"] = parse_res
            instance.context["parse_channel"] = parse_res.get("parse_channel", "v4")

            duration = int((time.time() - step_start) * 1000)
            instance.update_step_status(2, "completed", duration)
            await instance.broadcast_event("step_progress", {"step": 2, "status": "completed", "duration_ms": duration})

            # -------------------------------------------------------------
            # 节点 3: MD 加载与几何图元对齐
            # -------------------------------------------------------------
            step_start = time.time()
            instance.update_step_status(3, "running")
            await instance.broadcast_event("step_progress", {"step": 3, "status": "running"})

            content_list = parse_res.get("content_list", [])
            instance.context["content_list"] = content_list

            duration = int((time.time() - step_start) * 1000)
            instance.update_step_status(3, "completed", duration)
            await instance.broadcast_event("step_progress", {"step": 3, "status": "completed", "duration_ms": duration})
            CheckpointManager.save_checkpoint(instance)

            # -------------------------------------------------------------
            # 节点 4: 图片与表格切图上传 OSS
            # -------------------------------------------------------------
            step_start = time.time()
            instance.update_step_status(4, "running")
            await instance.broadcast_event("step_progress", {"step": 4, "status": "running"})

            await self._execute_step_4_images_and_tables(instance, parse_res, content_list)
            duration = int((time.time() - step_start) * 1000)
            instance.update_step_status(4, "completed", duration)
            await instance.broadcast_event("step_progress", {"step": 4, "status": "completed", "duration_ms": duration})
            CheckpointManager.save_checkpoint(instance)

            # -------------------------------------------------------------
            # 节点 5: 图片多模态语义描述 (VLM / Grok 4.5)
            # -------------------------------------------------------------
            await self._execute_step_5_images(instance, content_list, file_name)

            # -------------------------------------------------------------
            # 节点 6: 表格转文本与跨页缝合、代码块与流程图语义提炼
            # -------------------------------------------------------------
            await self._execute_step_6_tables_and_code(instance, parse_res, content_list, file_name)

            # -------------------------------------------------------------
            # 节点 7: 人工审核挂起 (ReviewDialog)
            # -------------------------------------------------------------
            review_ok = await self._execute_step_7_review(instance, parse_res, content_list, file_name, skip_review)
            if not review_ok:
                return

            # -------------------------------------------------------------
            # 节点 8: 表格与描述回填 Markdown (生成 full.enhanced.md)
            # -------------------------------------------------------------
            enhanced_md_content = await self._execute_step_8_backfill(instance, parse_res)

            # -------------------------------------------------------------
            # 节点 9: Markdown 两阶段混合切分
            # -------------------------------------------------------------
            raw_chunks = await self._execute_step_9_chunking(instance, enhanced_md_content, Path(parse_res["doc_dir"]))

            # -------------------------------------------------------------
            # 节点 10: 三路混合索引计算 (Dense + Normalized Sparse + TSVector)
            # -------------------------------------------------------------
            await self._execute_step_10_embedding(instance, raw_chunks)

            # -------------------------------------------------------------
            # 节点 11: 标题向量防重与蓝绿安全入库
            # -------------------------------------------------------------
            await self._execute_step_11_storage(instance, enhanced_md_content, skip_review)

        except Exception as e:
            instance.overall_status = "failed"
            instance.failed_step = instance.current_step_index
            instance.error_message = str(e)
            instance.root_cause_error = str(e)
            instance.update_step_status(instance.current_step_index, "failed", error=str(e))
            CheckpointManager.save_checkpoint(instance, failed_step=instance.current_step_index, error=str(e))
            await instance.broadcast_event("pipeline_failed", {
                "doc_id": doc_id,
                "step_index": instance.current_step_index,
                "error": str(e)
            })
            raise

    async def _execute_step_4_images_and_tables(
        self,
        instance: PipelineInstance,
        parse_res: Dict[str, Any],
        content_list: List[Dict[str, Any]]
    ):
        """执行节点 4: 图片与表格切图上传 OSS（幂等上传，已有 oss_url 绝不重复上传）"""
        doc_id = instance.doc_id
        doc_dir = Path(parse_res.get("doc_dir", str(Path(settings.DATA_PARSED_DIR) / doc_id)))

        image_elements = [el for el in content_list if el.get("type") == "image"]
        for idx, img_el in enumerate(image_elements):
            if img_el.get("oss_url"):
                continue
            rel_path = img_el.get("img_path", "")
            if not rel_path:
                img_el["oss_url"] = None
                continue
            if rel_path.startswith("http://") or rel_path.startswith("https://"):
                img_el["oss_url"] = rel_path
            else:
                full_img_path = doc_dir / rel_path
                if full_img_path.exists() and full_img_path.is_file():
                    with open(full_img_path, "rb") as f:
                        img_bytes = f.read()
                    oss_path = f"rag_storage/parsed/{doc_id}/images/img_{idx}_{Path(rel_path).name}"
                    img_oss_url = await asyncio.to_thread(oss_service.upload_file, img_bytes, oss_path, content_type="image/jpeg")
                    img_el["oss_url"] = img_oss_url
                else:
                    raise FileNotFoundError(f"【切图错误】未能找到图元文件: {full_img_path}，无法完成 OSS 上传！")

        table_elements = [el for el in content_list if el.get("type") == "table"]
        for idx, tbl_el in enumerate(table_elements):
            if tbl_el.get("img_url"):
                continue
            tbl_img_path = tbl_el.get("img_path", "")
            if tbl_img_path:
                if tbl_img_path.startswith("http://") or tbl_img_path.startswith("https://"):
                    tbl_el["img_url"] = tbl_img_path
                else:
                    full_tbl_img = doc_dir / tbl_img_path
                    if full_tbl_img.exists() and full_tbl_img.is_file():
                        with open(full_tbl_img, "rb") as f:
                            tbl_bytes = f.read()
                        oss_path = f"rag_storage/parsed/{doc_id}/tables/tbl_{idx}_{Path(tbl_img_path).name}"
                        tbl_oss_url = await asyncio.to_thread(oss_service.upload_file, tbl_bytes, oss_path, content_type="image/jpeg")
                        tbl_el["img_url"] = tbl_oss_url
                    else:
                        tbl_el["img_url"] = None

    async def _execute_step_5_images(
        self,
        instance: PipelineInstance,
        content_list: List[Dict[str, Any]],
        file_name: str
    ):
        """执行节点 5: 图片多模态语义描述 (VLM / Grok 4.5)"""
        step_start = time.time()
        instance.update_step_status(5, "running")
        await instance.broadcast_event("step_progress", {"step": 5, "status": "running", "detail": "正在分析多模态配图语义..."})

        image_elements = [el for el in content_list if el.get("type") == "image"]
        img_sem = asyncio.Semaphore(4)

        async def _process_image(idx: int, img_el: Dict[str, Any]):
            raw_url = img_el.get("oss_url", "")
            if not raw_url:
                img_el["vlm_description"] = img_el.get("alt", "") or "文档配图"
                return
            logger.info(f" [VLM] 正在分析第 {idx + 1}/{len(image_elements)} 张配图语义 (页码: {img_el.get('page_idx', 0) + 1})...")
            signed_img_url = oss_service.sign_url(raw_url, expires=3600) if ("aliyuncs.com" in raw_url and "OSSAccessKeyId" not in raw_url) else raw_url
            is_flowchart = code_flowchart_service.is_flowchart_image(img_el)
            img_el["is_flowchart"] = is_flowchart

            ctx = {
                "doc_name": file_name,
                "heading": img_el.get("heading", ""),
                "surrounding_text": img_el.get("alt", "") or file_name,
                "element_type": "flowchart" if is_flowchart else "image"
            }
            async with img_sem:
                if is_flowchart:
                    desc = await asyncio.to_thread(vlm_service.describe_flowchart, signed_img_url, ctx)
                else:
                    desc = await asyncio.to_thread(vlm_service.describe_image, signed_img_url, ctx)
                img_el["vlm_description"] = desc

        if image_elements:
            await asyncio.gather(*[_process_image(i, el) for i, el in enumerate(image_elements)])
        else:
            logger.info(f" [VLM] 当前文档 (doc_id={instance.doc_id}) 无独立配图切图资产，自动跳过多模态看图描述。")

        duration = int((time.time() - step_start) * 1000)
        instance.update_step_status(5, "completed", duration)
        await instance.broadcast_event("step_progress", {"step": 5, "status": "completed", "duration_ms": duration})
        CheckpointManager.save_checkpoint(instance)

    async def _execute_step_6_tables_and_code(
        self,
        instance: PipelineInstance,
        parse_res: Dict[str, Any],
        content_list: List[Dict[str, Any]],
        file_name: str
    ):
        """执行节点 6: 表格转文本与跨页缝合、代码块与流程图语义提炼"""
        step_start = time.time()
        doc_id = instance.doc_id
        instance.update_step_status(6, "running")
        await instance.broadcast_event("step_progress", {"step": 6, "status": "running", "detail": "正在执行表格跨页缝合与结构化解析..."})

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
                logger.info(f" [表格处理] 正在调用 AI 模型对表格进行语义提取 (第 {idx + 1}/{len(processed_tables)} 个, 页码: {tbl.display_page})...")
                desc = await asyncio.to_thread(vlm_service.describe_table, tbl.html_content or tbl.raw_content, ctx, tbl_img)
                tbl.semantic_summary = desc

        if processed_tables:
            await asyncio.gather(*[_process_table(i, tbl) for i, tbl in enumerate(processed_tables)])

        instance.context["processed_tables"] = processed_tables

        doc_dir = Path(parse_res.get("doc_dir", ""))
        full_md_path = doc_dir / "full.md"
        full_md_text = ""
        if full_md_path.exists():
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
                logger.info(f" [代码块提炼] 正在调用 AI 模型提炼代码业务语义 (第 {idx + 1}/{len(processed_code_blocks)} 个)...")
                llm_desc = await asyncio.to_thread(vlm_service.describe_code, cb.raw_code, cb.language, ctx)
                cb.semantic_summary = llm_desc

        async def _process_flowchart_block(idx: int, fb: ParsedFlowchartBlock):
            ctx = {
                "doc_name": file_name,
                "heading": f"第 {fb.display_page} 页流程图",
                "title": fb.title,
            }
            async with code_sem:
                logger.info(f" [流程图提炼] 正在调用 AI 模型提炼 Mermaid 架构语义 (第 {idx + 1}/{len(processed_mermaid_flowcharts)} 个)...")
                llm_desc = await asyncio.to_thread(vlm_service.describe_flowchart_text, fb.raw_content, ctx)
                fb.semantic_summary = llm_desc

        model_tasks = []
        if processed_code_blocks:
            model_tasks.extend([_process_code_block(i, cb) for i, cb in enumerate(processed_code_blocks)])
        if processed_mermaid_flowcharts:
            model_tasks.extend([_process_flowchart_block(i, fb) for i, fb in enumerate(processed_mermaid_flowcharts)])

        if model_tasks:
            await asyncio.gather(*model_tasks)

        instance.context["processed_code_blocks"] = processed_code_blocks
        instance.context["processed_mermaid_flowcharts"] = processed_mermaid_flowcharts

        duration = int((time.time() - step_start) * 1000)
        instance.update_step_status(6, "completed", duration)
        await instance.broadcast_event("step_progress", {"step": 6, "status": "completed", "duration_ms": duration})
        CheckpointManager.save_checkpoint(instance)

    async def _execute_step_7_review(
        self,
        instance: PipelineInstance,
        parse_res: Dict[str, Any],
        content_list: List[Dict[str, Any]],
        file_name: str,
        skip_review: bool = False
    ) -> bool:
        """执行节点 7: 人工审核挂起 (ReviewDialog) 与确认"""
        step_start = time.time()
        doc_id = instance.doc_id
        instance.update_step_status(7, "running")

        doc_dir = Path(parse_res.get("doc_dir", ""))
        full_md_path = doc_dir / "full.md"
        md_lines: List[str] = []
        if full_md_path.exists():
            with open(full_md_path, "r", encoding="utf-8") as f:
                md_lines = f.readlines()

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

        processed_tables: List[ParsedTableBlock] = instance.context.get("processed_tables", [])
        image_elements = [el for el in content_list if el.get("type") == "image"]
        processed_mermaid_flowcharts: List[ParsedFlowchartBlock] = instance.context.get("processed_mermaid_flowcharts", [])
        processed_code_blocks: List[ParsedCodeBlock] = instance.context.get("processed_code_blocks", [])

        review_items: List[ReviewItem] = []
        for idx, tbl in enumerate(processed_tables):
            real_line = resolve_table_physical_line(tbl)
            table_title = f"表格 {idx + 1} (第 {tbl.display_page} 页)" if tbl.total_parts == 1 else f"表格 {idx + 1} (第 {tbl.display_page} 页 - 分片 {tbl.part_index}/{tbl.total_parts})"
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

        for idx, img in enumerate(image_elements):
            real_line = resolve_image_physical_line(img)
            is_flow = img.get("is_flowchart", False)
            item_type = "flowchart" if is_flow else "image"
            item_title = f"流程图 {idx + 1} (第 {img.get('page_idx', 0) + 1} 页)" if is_flow else f"配图 {idx + 1} (第 {img.get('page_idx', 0) + 1} 页)"
            raw_img = img.get("oss_url", "")
            img_key = f"img_rev_{idx}"
            img_proxy = f"/api/v1/documents/{doc_id}/review-items/{img_key}/asset" if raw_img else ""
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
                vlm_description=img.get("vlm_description", "图片语义描述"),
                user_description=img.get("vlm_description"),
                status="pending"
            ))

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
                vlm_description=flow.semantic_summary,
                user_description=flow.semantic_summary,
                status="pending"
            ))

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
                vlm_description=code_blk.semantic_summary,
                user_description=code_blk.semantic_summary,
                language=code_blk.language,
                status="pending"
            ))

        instance.pending_reviews = review_items

        if review_items and not skip_review:
            instance.overall_status = "pending_review"
            instance.update_step_status(7, "pending_review")
            
            loop = asyncio.get_running_loop()
            instance.review_future = loop.create_future()
            
            serialized_items = [item.model_dump() for item in review_items]
            await instance.broadcast_event("review_required", {
                "doc_id": doc_id,
                "review_count": len(review_items),
                "items": serialized_items,
                "pending_reviews": serialized_items
            })
            CheckpointManager.save_checkpoint(instance)

            try:
                await instance.review_future
            except asyncio.CancelledError:
                instance.overall_status = "failed"
                instance.update_step_status(7, "failed", error="审核被主动取消")
                return False

        duration = int((time.time() - step_start) * 1000)
        instance.overall_status = "running"
        instance.update_step_status(7, "completed", duration)
        await instance.broadcast_event("step_progress", {"step": 7, "status": "completed", "duration_ms": duration})
        CheckpointManager.save_checkpoint(instance)
        return True

    async def _execute_step_8_backfill(self, instance: PipelineInstance, parse_res: Dict[str, Any]) -> str:
        """执行节点 8: 表格与多模态描述原位回填并生成 full.enhanced.md (达成 Checkpoint 3)"""
        step_start = time.time()
        instance.update_step_status(8, "running")
        await instance.broadcast_event("step_progress", {"step": 8, "status": "running", "detail": "正在回填增强版 Markdown 与图文元数据..."})

        self._backfill_enhanced_markdown(instance, parse_res)

        enhanced_md_path = instance.context.get("enhanced_md_path")
        if not enhanced_md_path or not Path(enhanced_md_path).exists():
            raise RuntimeError(f"【回填错误】未能找到生成的增强版 Markdown 文件: {enhanced_md_path}")

        with open(enhanced_md_path, "r", encoding="utf-8") as f:
            enhanced_md_content = f.read()

        duration = int((time.time() - step_start) * 1000)
        instance.update_step_status(8, "completed", duration)
        await instance.broadcast_event("step_progress", {"step": 8, "status": "completed", "duration_ms": duration})
        CheckpointManager.save_checkpoint(instance)
        return enhanced_md_content

    async def _execute_step_9_chunking(self, instance: PipelineInstance, enhanced_md_content: str, doc_dir: Path) -> List[Any]:
        """执行节点 9: Markdown 两阶段混合切分"""
        step_start = time.time()
        instance.update_step_status(9, "running")
        await instance.broadcast_event("step_progress", {"step": 9, "status": "running", "detail": "正在执行两阶段语义与代码切分..."})

        content_list = instance.context.get("content_list")
        if not content_list:
            cl_path = doc_dir / f"{instance.doc_id}_content_list.json"
            if cl_path.exists():
                try:
                    with open(cl_path, "r", encoding="utf-8") as f:
                        content_list = json.load(f)
                except Exception:
                    content_list = []

        raw_chunks = chunker.split_enhanced_markdown(
            markdown_text=enhanced_md_content,
            department=instance.department,
            content_list=content_list
        )
        instance.context["raw_chunks"] = raw_chunks

        duration = int((time.time() - step_start) * 1000)
        instance.update_step_status(9, "completed", duration)
        await instance.broadcast_event("step_progress", {
            "step": 9,
            "status": "completed",
            "duration_ms": duration,
            "chunk_count": len(raw_chunks)
        })
        CheckpointManager.save_checkpoint(instance)
        return raw_chunks

    async def _execute_step_10_embedding(self, instance: PipelineInstance, raw_chunks: List[Any]):
        """执行节点 10: 三路混合索引计算 (Dense + Normalized Sparse + TSVector)"""
        step_start = time.time()
        instance.update_step_status(10, "running")
        await instance.broadcast_event("step_progress", {"step": 10, "status": "running", "detail": "正在生成三路混合语义与稠密向量索引..."})

        chunk_texts = [c.content for c in raw_chunks]
        dense_embeddings = await embedding_service.embed_texts(chunk_texts, batch_size=16)

        sparse_vectors = [extract_sparse_vector(c.content) for c in raw_chunks]
        tsv_strings = [format_tsvector_tokens(c.content) for c in raw_chunks]

        instance.context["dense_embeddings"] = dense_embeddings
        instance.context["sparse_vectors"] = sparse_vectors
        instance.context["tsv_strings"] = tsv_strings

        duration = int((time.time() - step_start) * 1000)
        instance.update_step_status(10, "completed", duration)
        await instance.broadcast_event("step_progress", {"step": 10, "status": "completed", "duration_ms": duration})
        CheckpointManager.save_checkpoint(instance)

    async def _execute_step_11_storage(self, instance: PipelineInstance, enhanced_md_content: str, skip_review: bool = False):
        """执行节点 11: 标题向量防重与蓝绿安全入库"""
        step_start = time.time()
        instance.update_step_status(11, "running")
        await instance.broadcast_event("step_progress", {"step": 11, "status": "running", "detail": "正在执行标题向量防重检测与数据库入库..."})

        success = await self._execute_step_11_ingest(instance, enhanced_md_content, skip_review)
        if not success:
            return

        duration = int((time.time() - step_start) * 1000)
        instance.update_step_status(11, "completed", duration)
        await instance.broadcast_event("step_progress", {"step": 11, "status": "completed", "duration_ms": duration})

        instance.overall_status = "completed"
        raw_chunks = instance.context.get("raw_chunks", [])
        await instance.broadcast_event("pipeline_completed", {
            "doc_id": instance.doc_id,
            "file_name": instance.file_name,
            "status": "completed",
            "chunk_count": len(raw_chunks),
            "version": instance.context.get("target_version", 1),
            "message": "Phase 2 混合两阶段切分与三路索引入库已全部圆满完成！"
        })
        CheckpointManager.save_checkpoint(instance)


    async def _execute_step_11_ingest(
        self,
        instance: PipelineInstance,
        enhanced_md_content: str,
        skip_review: bool = False
    ) -> bool:
        """
        执行第 11 节点数据库与向量蓝绿幂等入库，防重检测与版本升级
        返回 True 表示成功入库，返回 False 表示命中重复预警并挂起等待用户决策
        """
        doc_id = instance.doc_id
        file_name = instance.file_name

        async with AsyncSessionLocal() as session:
            # 寻找匹配的部门
            dept_stmt = select(Department).where(Department.name == instance.department)
            dept_res = await session.execute(dept_stmt)
            dept_obj = dept_res.scalar_one_or_none()
            if not dept_obj:
                dept_obj = Department(
                    id=uuid.uuid4(),
                    name=instance.department,
                    path=f"/总公司/动态部门/{instance.department}"
                )
                session.add(dept_obj)
                await session.flush()

            department_id = dept_obj.id
            department_path = dept_obj.path

            # 标题向量防重检测
            dedup_res = await dedup_service.check_duplicate(
                session=session,
                doc_id=doc_id,
                file_name=file_name,
                markdown_text=enhanced_md_content
            )
            instance.context["dedup_res"] = dedup_res
            title_embedding = dedup_res["title_embedding"]

            is_dup = dedup_res["is_duplicate"]
            force_overwrite = instance.context.get("force_overwrite", False)

            if is_dup and not force_overwrite and not skip_review:
                # 命中重复预警且未显式指定覆盖，挂起等待前端决策
                instance.overall_status = "duplicate_warning"
                instance.update_step_status(11, "duplicate_warning")
                await instance.broadcast_event("duplicate_warning", {
                    "doc_id": doc_id,
                    "similarity": dedup_res["similarity"],
                    "matched_doc_id": dedup_res["matched_doc_id"],
                    "matched_title": dedup_res["matched_title"],
                    "message": dedup_res["message"]
                })
                return False

            target_version = (dedup_res["matched_version"] + 1) if (is_dup and force_overwrite) else 1
            instance.context["target_version"] = target_version
            old_doc_id = uuid.UUID(dedup_res["matched_doc_id"]) if (is_dup and force_overwrite and dedup_res.get("matched_doc_id")) else None

            raw_chunks = instance.context["raw_chunks"]
            dense_embeddings = instance.context["dense_embeddings"]
            sparse_vectors = instance.context["sparse_vectors"]
            tsv_strings = instance.context["tsv_strings"]

            await update_service.ingest_document_with_chunks(
                session=session,
                doc_id=uuid.UUID(doc_id),
                file_name=file_name,
                file_hash=instance.context.get("sha256", ""),
                raw_oss_url=instance.context.get("raw_oss_url", ""),
                enhanced_md_oss_url=instance.context.get("enhanced_md_oss_url"),
                department_id=department_id,
                department_path=department_path,
                category=instance.category,
                tags=instance.tags,
                title_embedding=title_embedding,
                chunks=raw_chunks,
                dense_embeddings=dense_embeddings,
                sparse_vectors=sparse_vectors,
                tsv_strings=tsv_strings,
                target_version=target_version,
                old_doc_id=old_doc_id
            )
            return True

    def _backfill_enhanced_markdown(self, instance: PipelineInstance, parse_res: Dict[str, Any]):
        """将人工审核确认的语义描述回填至 Markdown 占位符并产出 full.enhanced.md"""
        doc_dir = Path(parse_res["doc_dir"])
        full_md_path = doc_dir / "full.md"
        enhanced_md_path = doc_dir / "full.enhanced.md"

        if not full_md_path.exists():
            return

        with open(full_md_path, "r", encoding="utf-8") as f:
            content = f.read()

        # 统一规整换行符为 Unix 风格 \n，彻底杜绝 Windows CRLF 导致的代码块与表格精确定位匹配失败问题
        content = content.replace("\r\n", "\n")

        # 1. 回填配图与流程图的多模态增强块
        content_list: List[Dict[str, Any]] = instance.context.get("content_list", [])
        image_elements = [el for el in content_list if el.get("type") == "image"]
        for idx, img in enumerate(image_elements):
            page_idx = img.get("page_idx", 0)
            display_page = page_idx + 1
            oss_url = img.get("oss_url", "")
            alt_text = img.get("alt", f"图片{idx + 1}")
            original_path = img.get("img_path", "")

            # 严格根据唯一 item_id (img_rev_{idx}) 精准匹配，杜绝按 page_idx 模糊兜底导致的同页多图串号覆盖
            matching_review = next(
                (r for r in instance.pending_reviews if r.item_id == f"img_rev_{idx}"),
                None
            )
            final_desc = matching_review.user_description if matching_review and matching_review.user_description else (img.get("vlm_description") or "")
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
            # 定位并替换 Markdown 中的原图语法
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
                logger.warning(f"【配图回填警告】未能精确定位配图位置 [doc_id={instance.doc_id}, path={original_path}], 保持原地未替换，杜绝文末数据污染！")

        # 2. 回填表格的多模态增强块（废弃大坨 HTML/Markdown 表格，统一采用纯文本序列化 Chunk）
        processed_tables: List[ParsedTableBlock] = instance.context.get("processed_tables", [])

        # 按母表 ID 分组，将同一长表拆分出的所有子表块合并一次性原位回填，杜绝覆写丢失
        from collections import defaultdict
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
                # 严格根据唯一 item_id 精准匹配，杜绝按 page_idx 模糊兜底导致的多表串号覆盖
                matching_review = next(
                    (r for r in instance.pending_reviews if r.item_id == f"tbl_rev_{tbl.table_id}"),
                    None
                )
                final_text = matching_review.user_description if matching_review and matching_review.user_description else tbl.semantic_summary

                chunk_html = f"""
<div class="rag-table-chunk rag-multimodal-block" data-type="table" data-table-id="{tbl.table_id}" data-page="{tbl.display_page}" data-asset="{tbl.image_url or ''}">
<!-- VLM_VERIFIED_DESCRIPTION_START -->
{final_text}
<!-- VLM_VERIFIED_DESCRIPTION_END -->
</div>"""
                sub_chunks.append(chunk_html.strip())

            combined_blocks = "\n\n".join(sub_chunks)
            replaced = False

            # 第一优先级: 母表原始 table_body 字符串精确定位替换
            parent_raw = parts[0].raw_content
            if parent_raw and parent_raw in content:
                content = content.replace(parent_raw, combined_blocks, 1)
                replaced = True

            # 第二优先级: HTML <table> 标签正则特征匹配替换
            if not replaced and "<table" in content.lower():
                target_cell = parts[0].headers[0] if parts[0].headers else (parts[0].rows[0][0] if parts[0].rows and parts[0].rows[0] else None)
                if target_cell:
                    pattern = re.compile(rf'<table[^>]*>(?:(?!<table).)*?{re.escape(target_cell)}.*?</table>', re.DOTALL | re.IGNORECASE)
                    m = pattern.search(content)
                    if m:
                        content = content[:m.start()] + combined_blocks + content[m.end():]
                        replaced = True

            # 第三优先级: 原生 Markdown 表格文本匹配替换
            if not replaced and parts[0].markdown_content and parts[0].markdown_content in content:
                content = content.replace(parts[0].markdown_content, combined_blocks, 1)
                replaced = True

            # 核心红线控制: 若未能匹配，坚决杜绝追加到文末造成严重数据污染
            if not replaced:
                logger.warning(f"【表格回填警告】未能精确定位表格位置 [parent_id={parent_id}, page={parts[0].display_page}], 保持原地未替换，杜绝文末数据污染！")

        # 3. 回填 Mermaid 流程图的多模态增强块
        processed_mermaid_flowcharts: List[ParsedFlowchartBlock] = instance.context.get("processed_mermaid_flowcharts", [])
        for flow in processed_mermaid_flowcharts:
            matching_review = next(
                (r for r in instance.pending_reviews if r.item_id == f"flow_rev_{flow.chart_id}"),
                None
            )
            final_desc = matching_review.user_description if matching_review and matching_review.user_description else flow.semantic_summary

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
                logger.warning(f"【流程图回填警告】未能精确在 Markdown 中匹配到 Mermaid 流程图 [chart_id={flow.chart_id}], 保持原地未替换！")

        # 4. 回填代码块的多模态增强块
        processed_code_blocks: List[ParsedCodeBlock] = instance.context.get("processed_code_blocks", [])
        for code_blk in processed_code_blocks:
            matching_review = next(
                (r for r in instance.pending_reviews if r.item_id == f"code_rev_{code_blk.block_id}"),
                None
            )
            final_desc = matching_review.user_description if matching_review and matching_review.user_description else code_blk.semantic_summary

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
                logger.warning(f"【代码块回填警告】未能精确在 Markdown 中匹配到代码块 [block_id={code_blk.block_id}], 保持原地未替换！")

        with open(enhanced_md_path, "w", encoding="utf-8") as f:
            f.write(content)

        instance.context["enhanced_md_path"] = str(enhanced_md_path)

        # -------------------------------------------------------------
        # 严格持久化至阿里云 OSS（绝不残留为纯本地磁盘文件）
        # 将原始 full.md、增强版 full.enhanced.md 以及版面结构元数据全部归档上云
        # -------------------------------------------------------------
        doc_id = instance.doc_id
        try:
            # 1. 上传增强版 Markdown
            enhanced_bytes = content.encode("utf-8")
            enhanced_oss_path = f"rag_storage/parsed/{doc_id}/full.enhanced.md"
            enhanced_oss_url = oss_service.upload_file(enhanced_bytes, enhanced_oss_path, content_type="text/markdown; charset=utf-8")
            instance.context["enhanced_md_oss_url"] = enhanced_oss_url

            # 2. 上传原始 full.md
            if full_md_path.exists():
                with open(full_md_path, "rb") as f:
                    full_md_bytes = f.read()
                full_md_oss_path = f"rag_storage/parsed/{doc_id}/full.md"
                full_md_oss_url = oss_service.upload_file(full_md_bytes, full_md_oss_path, content_type="text/markdown; charset=utf-8")
                instance.context["full_md_oss_url"] = full_md_oss_url

            # 3. 上传版面结构元数据 content_list.json 与 layout.json
            cl_path = doc_dir / f"{doc_id}_content_list.json"
            if cl_path.exists():
                with open(cl_path, "rb") as f:
                    cl_bytes = f.read()
                cl_oss_path = f"rag_storage/parsed/{doc_id}/{doc_id}_content_list.json"
                oss_service.upload_file(cl_bytes, cl_oss_path, content_type="application/json; charset=utf-8")

            layout_path = doc_dir / "layout.json"
            if layout_path.exists():
                with open(layout_path, "rb") as f:
                    layout_bytes = f.read()
                layout_oss_path = f"rag_storage/parsed/{doc_id}/layout.json"
                oss_service.upload_file(layout_bytes, layout_oss_path, content_type="application/json; charset=utf-8")

            logger.info(f" [OSS] 已成功归档 Markdown 与版面元数据至云端: {enhanced_oss_path}")
        except Exception as e:
            raise RuntimeError(f"【多模态 Markdown 上传阿里云 OSS 失败】: {e}") from e

    async def resume_pipeline(
        self,
        instance: PipelineInstance,
        from_step: int = 9
    ):
        """
        全生命周期分层断点续跑执行引擎：
        - 若 from_step <= 8 (如 Step 5 VLM / Step 6 表格)：
          基于 Checkpoint 1 物理资产 (full.md + content_list.json) 续跑，彻底跳过 Step 1~4 接入与 MinerU 重复解析！
        - 若 from_step >= 9 (如 Step 9 切分 / Step 10 向量化)：
          基于 Checkpoint 3 物理资产 (full.enhanced.md) 续跑，彻底跳过 Step 1~8 耗时大模型与人工审核！
        - 严格校验先决条件，拒绝虚假 OSS 404 误报，保护真实失败根因。
        """
        doc_id = instance.doc_id
        file_name = instance.file_name
        doc_dir = Path(settings.DATA_PARSED_DIR) / doc_id
        instance.overall_status = "running"
        instance.error_message = None
        instance.failed_step = None
        # 清理续跑节点及后续节点的残留历史错误信息
        for s_idx in range(from_step, 12):
            if s_idx <= len(instance.steps):
                instance.steps[s_idx - 1].error_message = None
                instance.steps[s_idx - 1].status = "running" if s_idx == from_step else "waiting"
        CheckpointManager.save_checkpoint(instance)
        await instance.broadcast_event("pipeline_resumed", {
            "doc_id": doc_id,
            "from_step": from_step,
            "detail": f"正在从第 {from_step} 步启动断点续跑..."
        })

        try:
            if from_step <= 4:
                # -------------------------------------------------------------
                # 分层断点续跑 阶段 A：原生解析与版面切图重试 (Step 1~4)
                # 彻底移除原有一刀切寻找 full.md 的错误逻辑！真实重新触发解析与切图！
                # -------------------------------------------------------------
                instance.update_step_status(1, "completed")

                if from_step <= 2:
                    # 重新执行 Step 2: MinerU 解析
                    step_start = time.time()
                    instance.update_step_status(2, "running")
                    await instance.broadcast_event("step_progress", {"step": 2, "status": "running", "detail": "正在重新调用 MinerU 解析服务..."})

                    raw_oss_url = instance.context.get("raw_oss_url")
                    if not raw_oss_url and hasattr(instance, "assets") and instance.assets:
                        raw_oss_url = getattr(instance.assets, "raw_oss_url", None)
                    if not raw_oss_url:
                        raise RuntimeError(f"【重试失败】未能找到文档 {doc_id} 的原始 OSS 存储地址，请重新上传！")

                    raw_file_path = doc_dir / file_name
                    if not raw_file_path.exists():
                        doc_dir.mkdir(parents=True, exist_ok=True)
                        raw_bytes = await asyncio.to_thread(oss_service.download_file, raw_oss_url)
                        with open(raw_file_path, "wb") as f:
                            f.write(raw_bytes)

                    parse_res = await asyncio.to_thread(
                        mineru_adapter.parse_document,
                        doc_id=doc_id,
                        file_path=str(raw_file_path),
                        file_name=file_name,
                        file_url=raw_oss_url
                    )
                    instance.context["parse_res"] = parse_res
                    duration = int((time.time() - step_start) * 1000)
                    instance.update_step_status(2, "completed", duration)
                    await instance.broadcast_event("step_progress", {"step": 2, "status": "completed", "duration_ms": duration})
                    CheckpointManager.save_checkpoint(instance)
                else:
                    parse_res = instance.context.get("parse_res", {})

                if from_step <= 3:
                    step_start = time.time()
                    instance.update_step_status(3, "running")
                    await instance.broadcast_event("step_progress", {"step": 3, "status": "running"})
                    content_list = parse_res.get("content_list", [])
                    instance.context["content_list"] = content_list
                    duration = int((time.time() - step_start) * 1000)
                    instance.update_step_status(3, "completed", duration)
                    await instance.broadcast_event("step_progress", {"step": 3, "status": "completed", "duration_ms": duration})
                    CheckpointManager.save_checkpoint(instance)
                else:
                    content_list = instance.context.get("content_list", [])

                if from_step <= 4:
                    step_start = time.time()
                    instance.update_step_status(4, "running")
                    await instance.broadcast_event("step_progress", {"step": 4, "status": "running"})
                    await self._execute_step_4_images_and_tables(instance, parse_res, content_list)
                    duration = int((time.time() - step_start) * 1000)
                    instance.update_step_status(4, "completed", duration)
                    await instance.broadcast_event("step_progress", {"step": 4, "status": "completed", "duration_ms": duration})
                    CheckpointManager.save_checkpoint(instance)

                # Step 1~4 均已完成，顺流推进后续 Step 5~11
                await self._execute_step_5_images(instance, content_list, file_name)
                await self._execute_step_6_tables_and_code(instance, parse_res, content_list, file_name)
                review_ok = await self._execute_step_7_review(instance, parse_res, content_list, file_name, skip_review=False)
                if not review_ok:
                    return
                enhanced_md_content = await self._execute_step_8_backfill(instance, parse_res)
                raw_chunks = await self._execute_step_9_chunking(instance, enhanced_md_content, doc_dir)
                await self._execute_step_10_embedding(instance, raw_chunks)
                await self._execute_step_11_storage(instance, enhanced_md_content, skip_review=False)

            elif from_step <= 8:
                # -------------------------------------------------------------
                # 分层断点续跑 阶段 B：多模态增强续跑 (基于 Checkpoint 1: full.md + content_list.json)
                # 严格限定在 5 <= from_step <= 8！彻底跳过 Step 1~4，无需重复调用 MinerU 解析 API！
                # -------------------------------------------------------------
                full_md_path = doc_dir / "full.md"
                cl_path = doc_dir / f"{doc_id}_content_list.json"

                # 若本地缺失，尝试从 OSS 真实拉回
                if not full_md_path.exists():
                    try:
                        md_bytes = await asyncio.to_thread(oss_service.download_file, f"rag_storage/parsed/{doc_id}/full.md")
                        doc_dir.mkdir(parents=True, exist_ok=True)
                        with open(full_md_path, "wb") as f:
                            f.write(md_bytes)
                    except Exception as err:
                        raise RuntimeError(f"【断点续跑失败】未能找到文档 {doc_id} 的前置 full.md 资产 (本地与 OSS 均不可用: {err})，尚未达成 Checkpoint 1，请重新上传！") from err

                if not cl_path.exists():
                    try:
                        cl_bytes = await asyncio.to_thread(oss_service.download_file, f"rag_storage/parsed/{doc_id}/{doc_id}_content_list.json")
                        doc_dir.mkdir(parents=True, exist_ok=True)
                        with open(cl_path, "wb") as f:
                            f.write(cl_bytes)
                    except Exception as err:
                        raise RuntimeError(f"【断点续跑失败】未能找到文档 {doc_id} 的版面结构元数据 content_list.json (本地与 OSS 均不可用: {err})，请重新上传！") from err

                with open(cl_path, "r", encoding="utf-8") as f:
                    content_list = json.load(f)

                parse_res = {
                    "doc_dir": str(doc_dir),
                    "full_md_path": str(full_md_path),
                    "content_list": content_list
                }
                instance.context["parse_res"] = parse_res
                instance.context["content_list"] = content_list

                # 确保 Step 4 图元 OSS 链接在 content_list 中可用
                await self._execute_step_4_images_and_tables(instance, parse_res, content_list)

                # 前序完成的节点标记为 completed
                for prev_step in range(1, from_step):
                    instance.update_step_status(prev_step, "completed")

                if from_step <= 5:
                    await self._execute_step_5_images(instance, content_list, file_name)

                if from_step <= 6:
                    await self._execute_step_6_tables_and_code(instance, parse_res, content_list, file_name)

                if from_step <= 7:
                    review_ok = await self._execute_step_7_review(instance, parse_res, content_list, file_name, skip_review=False)
                    if not review_ok:
                        return

                if from_step <= 8:
                    enhanced_md_content = await self._execute_step_8_backfill(instance, parse_res)
                else:
                    enhanced_md_path = doc_dir / "full.enhanced.md"
                    with open(enhanced_md_path, "r", encoding="utf-8") as f:
                        enhanced_md_content = f.read()

                raw_chunks = await self._execute_step_9_chunking(instance, enhanced_md_content, doc_dir)
                await self._execute_step_10_embedding(instance, raw_chunks)
                await self._execute_step_11_storage(instance, enhanced_md_content, skip_review=False)

            else:
                # -------------------------------------------------------------
                # 后半段分层断点续跑 (基于 Checkpoint 3: full.enhanced.md)
                # 彻底跳过 Step 1~8 耗时大模型与人工审核！
                # -------------------------------------------------------------
                for prev_step in range(1, from_step):
                    instance.update_step_status(prev_step, "completed")

                enhanced_md_path = doc_dir / "full.enhanced.md"
                if not enhanced_md_path.exists():
                    oss_key = f"rag_storage/parsed/{doc_id}/full.enhanced.md"
                    try:
                        content_bytes = await asyncio.to_thread(oss_service.download_file, oss_key)
                        doc_dir.mkdir(parents=True, exist_ok=True)
                        with open(enhanced_md_path, "wb") as f:
                            f.write(content_bytes)
                    except Exception as dl_err:
                        # 检查是否有 Checkpoint 2 (review_snapshot.json) 与 Checkpoint 1 (full.md)
                        review_snap_path = doc_dir / "review_snapshot.json"
                        full_md_path = doc_dir / "full.md"
                        if review_snap_path.exists() and full_md_path.exists():
                            try:
                                parse_res = {"doc_dir": str(doc_dir), "content_list": instance.context.get("content_list", [])}
                                await self._execute_step_8_backfill(instance, parse_res)
                            except Exception as bf_err:
                                raise RuntimeError(
                                    f"【断点重试先决条件不满足】文档尚未完成 Step 7 人工审核与 Step 8 表格回填（未生成 full.enhanced.md 且回填失败: {bf_err}），无法从第 {from_step} 步直接续跑。请从第 5 步（图片描述）重新续跑或重新上传！"
                                ) from bf_err
                        else:
                            raise RuntimeError(
                                f"【断点重试先决条件不满足】文档尚未完成 Step 7 人工审核与 Step 8 表格回填（未生成 full.enhanced.md），无法从第 {from_step} 步直接续跑。请从第 5 步（图片描述）重新续跑或重新上传！"
                            ) from dl_err

                with open(enhanced_md_path, "r", encoding="utf-8") as f:
                    enhanced_md_content = f.read()
                instance.context["enhanced_md_path"] = str(enhanced_md_path)

                if from_step <= 9:
                    raw_chunks = await self._execute_step_9_chunking(instance, enhanced_md_content, doc_dir)
                else:
                    raw_chunks = instance.context.get("raw_chunks")
                    if not raw_chunks:
                        raw_chunks = await self._execute_step_9_chunking(instance, enhanced_md_content, doc_dir)

                if from_step <= 10:
                    await self._execute_step_10_embedding(instance, raw_chunks)

                if from_step <= 11:
                    await self._execute_step_11_storage(instance, enhanced_md_content, skip_review=False)

        except Exception as e:
            instance.overall_status = "failed"
            instance.failed_step = instance.current_step_index
            instance.error_message = str(e)
            instance.root_cause_error = str(e)
            instance.update_step_status(instance.current_step_index, "failed", error=str(e))
            CheckpointManager.save_checkpoint(instance, failed_step=instance.current_step_index, error=str(e))
            await instance.broadcast_event("pipeline_failed", {
                "doc_id": doc_id,
                "step_index": instance.current_step_index,
                "error": str(e)
            })
            raise

pipeline_executor = PipelineExecutor()
