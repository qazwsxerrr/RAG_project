"""src/rag_kb/nodes/upload.py —— 图片与表格切图上传 OSS 节点 UploadNode。

流水线第四环：
遍历 content_list 中的所有配图与表格切图，以幂等方式上传至阿里云 OSS 私有存储桶，
将生成的云端 OSS 链接回填至 content_list 对应图元项中，供多模态提炼与前端引用。
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
from rag_kb.utils.oss import oss_service

class UploadNode(BaseNode):
    """节点 4: 图片与表格切图上传 OSS"""

    def __init__(self):
        super().__init__(step_index=4, name="切图OSS上传")

    async def process(
        self,
        state: IngestState,
        config: Optional[RunnableConfig] = None,
        writer: Optional[StreamWriter] = None
    ) -> IngestState:
        doc_id = state.get("doc_id")
        parsed_dir = state.get("parsed_dir")
        doc_dir = Path(parsed_dir) if parsed_dir else (Path(settings.DATA_PARSED_DIR) / doc_id)
        content_list = state.get("content_list", [])

        image_urls = state.get("image_urls") or {}

        # 1. 遍历上传配图
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
                image_urls[rel_path] = rel_path
            else:
                full_img_path = doc_dir / rel_path
                if full_img_path.exists() and full_img_path.is_file():
                    with open(full_img_path, "rb") as f:
                        img_bytes = f.read()
                    oss_path = f"rag_storage/parsed/{doc_id}/images/img_{idx}_{Path(rel_path).name}"
                    img_oss_url = await asyncio.to_thread(
                        oss_service.upload_file,
                        img_bytes,
                        oss_path,
                        content_type="image/jpeg"
                    )
                    img_el["oss_url"] = img_oss_url
                    image_urls[rel_path] = img_oss_url
                else:
                    raise FileNotFoundError(f"【切图错误】未能找到配图文件: {full_img_path}，无法完成 OSS 上传！")

        # 2. 遍历上传表格切图
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
                        tbl_oss_url = await asyncio.to_thread(
                            oss_service.upload_file,
                            tbl_bytes,
                            oss_path,
                            content_type="image/jpeg"
                        )
                        tbl_el["img_url"] = tbl_oss_url
                    else:
                        tbl_el["img_url"] = None

        state["image_urls"] = image_urls
        state["content_list"] = content_list
        state["overall_status"] = IngestStatus.UPLOAD.value

        if writer and callable(writer):
            writer({
                "event": "step_progress",
                "step": 4,
                "status": "completed",
                "detail": f"完成切图上云，共上传 {len(image_elements)} 张配图与 {len(table_elements)} 个表格图"
            })

        return state

upload_node = UploadNode()
