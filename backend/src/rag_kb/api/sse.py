"""src/rag_kb/api/sse.py —— SSE (Server-Sent Events) 流式推流管理器与格式化工具。

负责：
1. 规范化 SSE 帧输出（协议名、ID、JSON 载荷规整）；
2. 管理入库流水线事件广播队列（订阅者分发、历史事件重传与断网重连回放）；
3. 问答流打字机（Thinking、Citations、Message、Done）事件封包。
"""

import json
import asyncio
from typing import Dict, Any, List, Optional, AsyncGenerator
from rag_kb.api.schemas import PipelineSnapshotResponse, PipelineStepStatus, ReviewItem
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
    """内存级活跃流水线状态与 SSE 事件广播中心"""

    def __init__(self):
        self._subscribers: Dict[str, List[asyncio.Queue]] = {}
        self._event_history: Dict[str, List[Dict[str, Any]]] = {}
        self._event_counters: Dict[str, int] = {}
        self._active_states: Dict[str, IngestState] = {}

    def register_pipeline(self, doc_id: str, initial_state: IngestState):
        """注册新的活跃流水线实例"""
        self._active_states[doc_id] = initial_state
        self._event_history[doc_id] = []
        self._event_counters[doc_id] = 0
        if doc_id not in self._subscribers:
            self._subscribers[doc_id] = []

    def get_state(self, doc_id: str) -> Optional[IngestState]:
        return self._active_states.get(doc_id)

    def update_state(self, doc_id: str, state: IngestState):
        self._active_states[doc_id] = state

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

        # 同步状态
        state = self._active_states.get(doc_id)
        if state:
            if "pending_reviews" in data and data["pending_reviews"]:
                state["pending_reviews"] = data["pending_reviews"]
            if event_name == "step_progress":
                step_idx = data.get("step")
                status = data.get("status")
                if step_idx and 1 <= step_idx <= len(state.get("steps", [])):
                    state["steps"][step_idx - 1]["status"] = status
                    state["current_step_index"] = step_idx
                    if "duration_ms" in data:
                        state["steps"][step_idx - 1]["duration_ms"] = data["duration_ms"]
                    if "error" in data:
                        state["steps"][step_idx - 1]["error"] = data["error"]
            elif event_name == "review_required":
                state["overall_status"] = IngestStatus.REVIEW.value
            elif event_name == "pipeline_completed":
                state["overall_status"] = IngestStatus.COMPLETED.value
            elif event_name == "pipeline_failed":
                state["overall_status"] = IngestStatus.FAILED.value

        subscribers = self._subscribers.get(doc_id, [])
        for q in subscribers:
            await q.put(event_packet)

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
                duration_ms=s.get("duration_ms", 0),
                error=s.get("error")
            )
            for i, s in enumerate(state.get("steps", []))
        ]

        pending_reviews_raw = state.get("pending_reviews", [])
        reviews = [
            ReviewItem(**r) if isinstance(r, dict) else r
            for r in pending_reviews_raw
        ]

        is_dup = bool(state.get("dedup_res", {}).get("is_duplicate"))
        matched_id = state.get("dedup_res", {}).get("matched_doc_id")

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
