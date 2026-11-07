"""src/rag_kb/api/sse.py —— SSE (Server-Sent Events) 流式推流管理器与格式化工具。

负责：
1. 规范化 SSE 帧输出（协议名、ID、JSON 载荷规整）；
2. 管理入库流水线事件广播队列（订阅者分发、历史事件重传与断网重连回放）；
3. 问答流打字机（Thinking、Citations、Message、Done）事件封包。
"""

import json
import asyncio
from collections import OrderedDict
from typing import Dict, Any, List, Optional, AsyncGenerator
from rag_kb.api.schemas import PipelineSnapshotResponse, PipelineStepStatus, ReviewItem
from rag_kb.core.config import settings
from rag_kb.core.constants import IngestStatus, STEP_NAMES
from rag_kb.graph.state import IngestState

def format_sse(event: str, data: Any, event_id: Optional[int] = None) -> str:
    """格式化标准 SSE 协议帧"""
    lines = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event}")
    if isinstance(data, (dict, list)):
        payload_str = json.dumps(data, default=str, ensure_ascii=False)
    else:
        payload_str = str(data)
    lines.append(f"data: {payload_str}")
    return "\n".join(lines) + "\n\n"

class IngestPipelineManager:
    """内存级活跃流水线状态与 SSE 事件广播中心（内置已完成流水线 LRU 定长回收，防止高并发 OOM）"""

    def __init__(self, max_completed_history: Optional[int] = None):
        self._subscribers: Dict[str, List[asyncio.Queue]] = {}
        self._event_history: Dict[str, List[Dict[str, Any]]] = {}
        self._event_counters: Dict[str, int] = {}
        self._active_states: OrderedDict[str, IngestState] = OrderedDict()
        self._max_completed_history = max_completed_history or settings.PIPELINE_MEMORY_HISTORY_LIMIT

    def _evict_finished_if_needed(self):
        """淘汰最早已完成或已失败且无活跃订阅者的历史内存快照，根除万级文档入库后的内存泄漏"""
        if len(self._active_states) <= self._max_completed_history:
            return
        terminal_statuses = {
            IngestStatus.COMPLETED.value,
            IngestStatus.FAILED.value,
            "completed",
            "failed",
            "cancelled",
            "active",
        }
        to_remove = []
        excess = len(self._active_states) - self._max_completed_history
        for did, st in self._active_states.items():
            if excess <= 0:
                break
            has_subs = bool(self._subscribers.get(did))
            if not has_subs and st.get("overall_status") in terminal_statuses:
                to_remove.append(did)
                excess -= 1
        for did in to_remove:
            self._active_states.pop(did, None)
            self._event_history.pop(did, None)
            self._event_counters.pop(did, None)
            self._subscribers.pop(did, None)

    def register_pipeline(self, doc_id: str, initial_state: IngestState):
        """注册新的活跃流水线实例（自动剥离大体积二进制防内存堆积）"""
        initial_state["file_bytes"] = None
        self._active_states[doc_id] = initial_state
        self._active_states.move_to_end(doc_id)
        self._event_history[doc_id] = []
        self._event_counters[doc_id] = 0
        if doc_id not in self._subscribers:
            self._subscribers[doc_id] = []
        self._evict_finished_if_needed()

    def register_state(self, doc_id: str, state: IngestState):
        """兼容恢复/重试时的状态注册别名"""
        state["file_bytes"] = None
        self._active_states[doc_id] = state
        self._active_states.move_to_end(doc_id)
        if doc_id not in self._event_history:
            self._event_history[doc_id] = []
        if doc_id not in self._event_counters:
            self._event_counters[doc_id] = 0
        if doc_id not in self._subscribers:
            self._subscribers[doc_id] = []
        self._evict_finished_if_needed()

    def remove_pipeline(self, doc_id: str) -> None:
        """当上传失败中断或删除文档时，彻底移除内存中的半成品状态与事件缓存"""
        self._active_states.pop(doc_id, None)
        self._event_history.pop(doc_id, None)
        self._event_counters.pop(doc_id, None)
        self._subscribers.pop(doc_id, None)

    def get_state(self, doc_id: str) -> Optional[IngestState]:
        return self._active_states.get(doc_id)

    def update_state(self, doc_id: str, state: IngestState):
        state["file_bytes"] = None
        self._active_states[doc_id] = state
        self._active_states.move_to_end(doc_id)

    async def broadcast(self, doc_id: str, event_name: str, data: Dict[str, Any]):
        """向所有在线订阅者广播事件，并记录入事件历史"""
        counter = self._event_counters.get(doc_id, 0) + 1
        self._event_counters[doc_id] = counter

        event_packet = {
            "id": counter,
            "event": event_name,
            "data": data
        }
        if doc_id not in self._event_history:
            self._event_history[doc_id] = []
        self._event_history[doc_id].append(event_packet)
        if len(self._event_history[doc_id]) > 200:
            self._event_history[doc_id] = self._event_history[doc_id][-200:]

        # 同步状态
        state = self._active_states.get(doc_id)
        if state:
            if "pending_reviews" in data and data["pending_reviews"]:
                state["pending_reviews"] = data["pending_reviews"]
            if event_name == "step_progress":
                step_idx = data.get("step")
                status = data.get("status")
                if status in ("running", "completed") and state.get("overall_status") in (
                    IngestStatus.QUEUED.value,
                    IngestStatus.PENDING.value,
                    "queued",
                    "pending",
                ):
                    state["overall_status"] = "running"
                if step_idx and 1 <= step_idx <= len(state.get("steps", [])):
                    state["steps"][step_idx - 1]["status"] = status
                    state["current_step_index"] = step_idx
                    if "duration_ms" in data:
                        state["steps"][step_idx - 1]["duration_ms"] = data["duration_ms"]
                    if "error" in data:
                        state["steps"][step_idx - 1]["error"] = data["error"]
                    if "detail" in data and data["detail"]:
                        state["steps"][step_idx - 1]["detail"] = data["detail"]
            elif event_name == "pipeline_queued":
                state["overall_status"] = IngestStatus.QUEUED.value
            elif event_name == "review_required":
                state["overall_status"] = IngestStatus.REVIEW.value
            elif event_name == "duplicate_warning":
                state["overall_status"] = IngestStatus.DUPLICATE_WARNING.value
            elif event_name == "pipeline_completed":
                state["overall_status"] = IngestStatus.COMPLETED.value
            elif event_name == "pipeline_failed":
                state["overall_status"] = IngestStatus.FAILED.value
                if "error" in data and data["error"]:
                    state["error"] = data["error"]

        subscribers = list(self._subscribers.get(doc_id, []))
        for q in subscribers:
            await q.put(event_packet)

        if event_name in ("pipeline_completed", "pipeline_failed"):
            self._evict_finished_if_needed()

    def subscribe(self, doc_id: str) -> asyncio.Queue:
        """为客户端连接创建 SSE 事件接收队列"""
        if doc_id not in self._subscribers:
            self._subscribers[doc_id] = []
        q = asyncio.Queue()
        self._subscribers[doc_id].append(q)
        return q

    def unsubscribe(self, doc_id: str, q: asyncio.Queue):
        """客户端断开连接时注销队列"""
        subscribers = self._subscribers.get(doc_id, [])
        if q in subscribers:
            subscribers.remove(q)

    def get_history(self, doc_id: str) -> List[Dict[str, Any]]:
        return self._event_history.get(doc_id, [])

    def to_snapshot(self, doc_id: str) -> Optional[PipelineSnapshotResponse]:
        """构建与前端兼容的流水线状态快照"""
        state = self._active_states.get(doc_id)
        if not state:
            return None

        step_statuses = [
            PipelineStepStatus(
                step_index=s.get("step_index", i + 1),
                name=s.get("name", STEP_NAMES[i]),
                status=s.get("status", "waiting"),
                duration_ms=int(s.get("duration_ms") or 0),
                error=s.get("error")
            )
            for i, s in enumerate(state.get("steps", []))
        ]

        pending_reviews_raw = state.get("pending_reviews", [])
        reviews = [
            ReviewItem(**r) if isinstance(r, dict) else r
            for r in pending_reviews_raw
        ]

        dedup_info = state.get("dedup_res") or {}
        is_dup = bool(dedup_info.get("is_duplicate"))
        matched_id = dedup_info.get("matched_doc_id")

        return PipelineSnapshotResponse(
            doc_id=doc_id,
            file_name=state.get("file_name", "unknown"),
            overall_status=state.get("overall_status", "pending"),
            current_step_index=state.get("current_step_index", 1),
            current_step_name=state.get("current_step_name", STEP_NAMES[0]),
            steps=step_statuses,
            requires_review=bool(reviews and state.get("overall_status") == IngestStatus.REVIEW.value),
            pending_reviews=reviews,
            is_duplicate_warning=is_dup,
            matched_doc_id=matched_id
        )

# 全局单例管理器
pipeline_manager = IngestPipelineManager()
