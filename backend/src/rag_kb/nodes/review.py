"""src/rag_kb/nodes/review.py —— 人工审核节点 ReviewNode。

流水线第七环：
利用 LangGraph 原生 interrupt 机制挂起流水线，
对外广播 review_required 事件，等待人机审核结果；
在外部注入 Command(resume=review_data) 唤醒后，更新审核描述并继续下一步。
"""

import logging
from typing import Dict, Any, List, Optional
from langchain_core.runnables import RunnableConfig
from langgraph.types import StreamWriter, interrupt
from rag_kb.core.constants import IngestStatus
from rag_kb.graph.state import IngestState
from rag_kb.nodes.base import BaseNode

logger = logging.getLogger(__name__)

class ReviewNode(BaseNode):
    """节点 7: 人工审核挂起 (Human-in-the-loop)"""

    def __init__(self):
        super().__init__(step_index=7, name="人工审核确认")

    async def process(
        self,
        state: IngestState,
        config: Optional[RunnableConfig] = None,
        writer: Optional[StreamWriter] = None
    ) -> IngestState:
        doc_id = state.get("doc_id")
        pending_reviews = state.get("pending_reviews", [])
        skip_review = state.get("skip_review", False)

        if not pending_reviews or skip_review:
            logger.info(f"▶ [人工审核] 无待审核项或已显式配置 skip_review=True (doc_id={doc_id})，直接放行")
            state["overall_status"] = IngestStatus.REVIEW.value
            return state

        # 广播 review_required 事件通知前端
        if writer and callable(writer):
            writer({
                "event": "review_required",
                "doc_id": doc_id,
                "review_count": len(pending_reviews),
                "items": pending_reviews,
                "pending_reviews": pending_reviews
            })

        logger.info(f"⏸ [人工审核] 正在挂起等待人工审核确认 (doc_id={doc_id}, 共 {len(pending_reviews)} 项)...")

        # 原生 interrupt 挂起当前协程并保存 Checkpoint
        resumed_data = interrupt({
            "doc_id": doc_id,
            "review_count": len(pending_reviews),
            "items": pending_reviews,
            "pending_reviews": pending_reviews
        })

        logger.info(f"▶ [人工审核] 收到审核反馈，正在唤醒流水线 (doc_id={doc_id})...")

        # 将审核反馈同步至 pending_reviews
        if resumed_data:
            state["review_results"] = resumed_data
            items_list = []
            if isinstance(resumed_data, dict):
                items_list = resumed_data.get("items", [])
            elif isinstance(resumed_data, list):
                items_list = resumed_data

            update_map = {}
            for item in items_list:
                if isinstance(item, dict) and "item_id" in item:
                    update_map[item["item_id"]] = item
                elif hasattr(item, "item_id"):
                    update_map[item.item_id] = item

            for rev in pending_reviews:
                item_id = rev.get("item_id")
                if item_id in update_map:
                    up = update_map[item_id]
                    if isinstance(up, dict):
                        new_desc = up.get("user_description")
                        new_remark = up.get("remark")
                    else:
                        new_desc = getattr(up, "user_description", None)
                        new_remark = getattr(up, "remark", None)

                    if new_desc is not None:
                        rev["user_description"] = new_desc
                    if new_remark is not None:
                        rev["remark"] = new_remark
                    rev["status"] = "modified" if rev.get("user_description") != rev.get("vlm_description") else "approved"

            state["pending_reviews"] = pending_reviews

        if writer and callable(writer):
            writer({
                "event": "review_completed",
                "doc_id": doc_id,
                "reviewed_items_count": len(pending_reviews)
            })

        state["overall_status"] = IngestStatus.REVIEW.value
        return state

review_node = ReviewNode()
