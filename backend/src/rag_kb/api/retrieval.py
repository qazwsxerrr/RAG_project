"""src/rag_kb/api/retrieval.py —— 知识检索与智能问答 HTTP 端点层。

支持：
- POST /retrieval/search: 纯检索阶段接口（返回 8 阶段思考流元数据、引文卡片与黄金切片）
- POST /retrieval/chat: 阶段 1~8 全流程智能问答流式打字机接口 (POST SSE 协议)
- POST /chat/query: 兼容别名接口
"""

import time
import json
import logging
from typing import AsyncGenerator, Dict, List, Optional
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from rag_kb.api.schemas import (
    ChatQueryRequest,
    ChatQueryResponse,
    ThinkingProcess,
    ThinkingStep,
    CitationSource,
    RetrievedChunk
)
from rag_kb.api.sse import format_sse
from rag_kb.db.chat_store import chat_store
from rag_search.build import get_retrieval_graph
from rag_search.answer import stream_answer
from rag_search.state import get_default_state, RetrievalState

logger = logging.getLogger(__name__)

router = APIRouter(tags=["检索与智能问答"])

STANDARD_RETRIEVAL_STEPS = [
    "查询预处理",
    "稠密召回",
    "稀疏召回",
    "关键词召回",
    "假设性搜索",
    "RRF 融合",
    "重排",
    "MMR 去重"
]

class ThinkingProcessBuilder:
    """动态思考全流程时间线追踪构建器（仅动态记录已激活或已执行的阶段，实现逐行递增动效）"""

    def __init__(self):
        self.active_order: List[str] = []
        self.steps: Dict[str, ThinkingStep] = {}
        self.start_time = time.perf_counter()
        self.retrieve_ms = 0

    def update_step(self, step_payload: dict):
        name = step_payload.get("name")
        if not name:
            return
        if name not in self.steps:
            self.active_order.append(name)
            self.steps[name] = ThinkingStep(
                name=name,
                status=step_payload.get("status", "执行中..."),
                duration_ms=step_payload.get("duration_ms", 0),
                count_change=step_payload.get("count_change"),
                summary=step_payload.get("summary")
            )
        else:
            self.steps[name].status = step_payload.get("status", self.steps[name].status)
            self.steps[name].duration_ms = step_payload.get("duration_ms", self.steps[name].duration_ms)
            if step_payload.get("count_change"):
                self.steps[name].count_change = step_payload.get("count_change")
            if step_payload.get("summary"):
                self.steps[name].summary = step_payload.get("summary")

    def to_thinking_process(self, total_overall_ms: int = 0) -> ThinkingProcess:
        total_ret = self.retrieve_ms or int((time.perf_counter() - self.start_time) * 1000)
        return ThinkingProcess(
            total_retrieve_ms=total_ret,
            total_overall_ms=total_overall_ms or total_ret,
            steps=[self.steps[name] for name in self.active_order]
        )

@router.post("/retrieval/search", response_model=ChatQueryResponse)
async def search_endpoint(request: ChatQueryRequest):
    """纯检索接口 (阶段 1 ~ 阶段 6)：获取经过多阶段漏斗精选的切片与思考流"""
    history_dicts = [h.model_dump() for h in request.history] if request.history else []
    initial_state = get_default_state(
        query=request.query,
        conversation_id=request.conversation_id,
        history=history_dicts,
        department_scope=request.department_scope,
        category_scope=request.category_scope,
        tags_scope=request.tags_scope,
        scene_type=request.scene_type,
        enable_hyde=request.enable_hyde
    )

    builder = ThinkingProcessBuilder()
    graph = get_retrieval_graph()
    citations_raw = []

    try:
        latest_state = initial_state
        # 流式跑图以捕获全部阶段的思考动画帧，并从 values 模式获取最终状态，避免重复执行图
        async for mode, chunk in graph.astream(initial_state, stream_mode=["custom", "values"]):
            if mode == "custom" and isinstance(chunk, dict):
                if chunk.get("type") == "thinking_step" and "step" in chunk:
                    builder.update_step(chunk["step"])
                elif chunk.get("type") == "citations" and "citations" in chunk:
                    citations_raw = chunk["citations"]
            elif mode == "values" and isinstance(chunk, dict):
                latest_state = chunk

        builder.retrieve_ms = int((time.perf_counter() - builder.start_time) * 1000)
        thinking_process = builder.to_thinking_process()

        if not citations_raw:
            citations_raw = latest_state.get("citations", [])
        citations = [CitationSource(**c) for c in citations_raw]

        final_chunks_raw = latest_state.get("results") or latest_state.get("final_chunks", [])
        final_chunks = [
            RetrievedChunk(
                chunk_id=c["chunk_id"],
                document_id=c["document_id"],
                document_title=c.get("document_title", ""),
                chunk_index=c.get("chunk_index", 0),
                chunk_label=c.get("chunk_label", ""),
                content=c.get("content", ""),
                page_idx=c.get("page_idx", 0),
                bbox=c.get("bbox", []),
                breadcrumb=c.get("breadcrumb", []),
                asset_url=c.get("asset_url"),
                raw_oss_url=c.get("raw_oss_url"),
                department_path=c.get("department_path", ""),
                score=c.get("score", 0.0),
                channel_ranks=c.get("channel_ranks", {}),
                dense_embedding=c.get("dense_embedding"),
                remark=c.get("remark")
            )
            for c in final_chunks_raw
        ]

        return ChatQueryResponse(
            conversation_id=request.conversation_id,
            rewritten_query=latest_state.get("rewritten_query", request.query),
            answer="",
            thinking=thinking_process,
            citations=citations,
            final_chunks=final_chunks
        )
    except Exception as e:
        logger.error(f"检索执行失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"检索服务异常: {str(e)}")

@router.post("/retrieval/chat")
async def chat_endpoint(request: ChatQueryRequest):
    """
    阶段 1 ~ 阶段 8 全流程智能问答流式打字机接口 (POST SSE 协议)
    - 阶段 1~6: 执行 LangGraph 检索图，实时派发 event: thinking 思考流与 event: citations
    - 阶段 7~8: 动态组装防幻觉 Prompt，调用云端大模型流式派发 event: message 打字机输出
    - 结束: 派发 event: done
    """
    history_dicts = [h.model_dump() for h in request.history] if request.history else []
    initial_state = get_default_state(
        query=request.query,
        conversation_id=request.conversation_id,
        history=history_dicts,
        department_scope=request.department_scope,
        category_scope=request.category_scope,
        tags_scope=request.tags_scope,
        scene_type=request.scene_type,
        enable_hyde=request.enable_hyde
    )

    # 记录用户消息至会话历史
    if request.conversation_id:
        try:
            await chat_store.append_message(request.conversation_id, "user", request.query)
        except Exception as e:
            logger.warning(f"记录用户消息失败: {e}")

    async def chat_event_stream() -> AsyncGenerator[str, None]:
        t0 = time.perf_counter()
        graph = get_retrieval_graph()
        citations = []
        final_chunks = []
        builder = ThinkingProcessBuilder()

        try:
            latest_state = initial_state
            # 1. 跑检索图并派发思考流动画帧 (单次遍历 values+custom，彻底杜绝重复跑图延迟)
            async for mode, chunk in graph.astream(initial_state, stream_mode=["custom", "values"]):
                if mode == "custom" and isinstance(chunk, dict):
                    if chunk.get("type") == "thinking_step" and "step" in chunk:
                        builder.update_step(chunk["step"])
                        yield format_sse("thinking", builder.to_thinking_process().model_dump())
                    elif chunk.get("type") == "citations" and "citations" in chunk:
                        citations = chunk["citations"]
                        yield format_sse("citations", citations)
                elif mode == "values" and isinstance(chunk, dict):
                    latest_state = chunk

            builder.retrieve_ms = int((time.perf_counter() - t0) * 1000)

            # 直接提取最终图状态
            final_chunks = latest_state.get("results") or latest_state.get("final_chunks", [])
            if not citations:
                citations = latest_state.get("citations", [])
                if citations:
                    yield format_sse("citations", citations)

            # 2. 阶段 7~8: 大模型流式问答生成 (分流推理思考与正式解答)
            full_answer_parts = []
            async for evt_type, token in stream_answer(request.query, final_chunks, citations):
                if evt_type == "reasoning":
                    yield format_sse("reasoning", {"content": token})
                else:
                    full_answer_parts.append(token)
                    yield format_sse("message", {"content": token})

            full_answer = "".join(full_answer_parts)
            total_overall_ms = int((time.perf_counter() - t0) * 1000)

            # 记录 Assistant 消息
            if request.conversation_id:
                try:
                    await chat_store.append_message(
                        request.conversation_id,
                        "assistant",
                        full_answer,
                        citations=citations
                    )
                except Exception as e:
                    logger.warning(f"记录助手消息失败: {e}")

            # 3. 派发完成事件
            yield format_sse("done", {
                "total_overall_ms": total_overall_ms,
                "total_retrieve_ms": builder.retrieve_ms
            })

        except Exception as err:
            logger.error(f"问答推流异常: {err}", exc_info=True)
            yield format_sse("error", {"error": str(err)})

    return StreamingResponse(
        chat_event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )

@router.post("/chat/query")
async def chat_query_alias(request: ChatQueryRequest):
    """前端兼容别名路由"""
    if request.stream:
        return await chat_endpoint(request)
    return await search_endpoint(request)
