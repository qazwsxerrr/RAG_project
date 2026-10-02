"""src/rag_kb/nodes/base.py —— 入库流水线所有节点的公共基类 BaseNode。

把「节点 = 一个可调用对象」标准化：注入配置、统一记录耗时与状态流转、
捕获业务异常并转换为失败状态，但放行人工审核的 GraphInterrupt。
"""

import time
import logging
from typing import Optional
from langchain_core.runnables import RunnableConfig
from langgraph.types import StreamWriter
from langgraph.errors import GraphInterrupt
from rag_kb.core.config import settings
from rag_kb.core.constants import IngestStatus
from rag_kb.graph.state import IngestState

logger = logging.getLogger(__name__)

class BaseNode:
    """入库流水线节点基类"""

    def __init__(self, step_index: int, name: str):
        self.step_index = step_index
        self.name = name
        self.logger = logging.getLogger(f"rag_kb.nodes.{name}")

    async def __call__(
        self,
        state: IngestState,
        config: RunnableConfig = None,
        writer: StreamWriter = None
    ) -> IngestState:
        """统一生命周期执行包装器"""
        # 更新状态追踪
        state["current_step_index"] = self.step_index
        state["current_step_name"] = self.name
        idx = self.step_index - 1
        if 0 <= idx < len(state.get("steps", [])):
            state["steps"][idx]["status"] = "running"

        if writer and callable(writer):
            writer({
                "event": "step_progress",
                "step": self.step_index,
                "status": "running"
            })

        self.logger.info(f"▶ [节点 {self.step_index}/11: {self.name}] 开始执行 (doc_id={state.get('doc_id')})...")
        t0 = time.perf_counter()

        try:
            result_state = await self.process(state, config=config, writer=writer)
            duration_ms = int((time.perf_counter() - t0) * 1000)

            if 0 <= idx < len(result_state.get("steps", [])):
                result_state["steps"][idx]["status"] = "completed"
                result_state["steps"][idx]["duration_ms"] = duration_ms

            if writer and callable(writer):
                writer({
                    "event": "step_progress",
                    "step": self.step_index,
                    "status": "completed",
                    "duration_ms": duration_ms
                })

            self.logger.info(f"✔ [节点 {self.step_index}/11: {self.name}] 执行完成，耗时 {duration_ms}ms")
            return result_state

        except GraphInterrupt:
            # 人机审核挂起中断必须原样放行，供 LangGraph 内核捕获并持久化 Checkpoint
            duration_ms = int((time.perf_counter() - t0) * 1000)
            if 0 <= idx < len(state.get("steps", [])):
                state["steps"][idx]["status"] = "pending_review"
                state["steps"][idx]["duration_ms"] = duration_ms

            if writer and callable(writer):
                writer({
                    "event": "step_progress",
                    "step": self.step_index,
                    "status": "pending_review",
                    "duration_ms": duration_ms
                })
            self.logger.info(f"⏸ [节点 {self.step_index}/11: {self.name}] 触发人工审核挂起 (GraphInterrupt)")
            raise

        except Exception as e:
            duration_ms = int((time.perf_counter() - t0) * 1000)
            err_msg = str(e)
            self.logger.error(f"✖ [节点 {self.step_index}/11: {self.name}] 执行异常: {err_msg}", exc_info=True)

            state["error"] = err_msg
            state["overall_status"] = IngestStatus.FAILED.value
            state["failed_step"] = self.step_index
            if 0 <= idx < len(state.get("steps", [])):
                state["steps"][idx]["status"] = "failed"
                state["steps"][idx]["duration_ms"] = duration_ms
                state["steps"][idx]["error"] = err_msg

            if writer and callable(writer):
                writer({
                    "event": "step_progress",
                    "step": self.step_index,
                    "status": "failed",
                    "duration_ms": duration_ms,
                    "error": err_msg
                })

            return state

    async def process(
        self,
        state: IngestState,
        config: Optional[RunnableConfig] = None,
        writer: Optional[StreamWriter] = None
    ) -> IngestState:
        """具体节点业务逻辑，由各子类继承实现"""
        raise NotImplementedError

