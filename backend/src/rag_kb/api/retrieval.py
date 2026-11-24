"""src/rag_kb/api/retrieval.py —— 知识检索与智能问答 HTTP 端点层（全面接入多维权限 PDP 与高密交付前审计）。"""

import time
import uuid
import logging
from typing import AsyncGenerator, Dict, List, Optional
from fastapi import APIRouter, HTTPException, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from rag_kb.core.authz import (
    EffectiveAuthzContext,
    audit_high_classification_read,
    validate_requested_kb_ids,
)
from rag_kb.core.config import settings
from rag_kb.core.constants import KbAction, Permissions
from rag_kb.core.logging.audit import record_audit_log
from rag_kb.core.rate_limit import (
    enforce_user_rate_limit,
    acquire_chat_stream_slot,
    release_chat_stream_slot,
)
from rag_kb.core.security import validate_prompt_security
from rag_kb.api.deps import (
    require_permission,
    get_session_auth_error,
    SESSION_AUTH_ERROR_DETAILS,
)
from rag_kb.api.schemas import (
    ChatQueryRequest,
    ChatQueryResponse,
    ThinkingProcess,
    ThinkingStep,
    CitationSource,
    RetrievedChunk,
)
from rag_kb.api.sse import format_sse
from rag_kb.db.chat_store import chat_store
from rag_kb.db.session import get_db
from rag_search.build import get_retrieval_graph
from rag_search.answer import stream_answer
from rag_search.state import get_default_state

logger = logging.getLogger(__name__)

router = APIRouter(tags=["检索与智能问答"])


class ThinkingProcessBuilder:
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
                summary=step_payload.get("summary"),
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
            steps=[self.steps[name] for name in self.active_order],
        )


async def _audit_retrieved_high_classification_docs(
    ctx: EffectiveAuthzContext,
    action: str,
    final_chunks: List[dict],
    query: str,
) -> None:
    """若检索/问答结果命中 L3/L4 高密文档，在向客户端交付首字节前持久化强制安全审计"""
    seen_docs = set()
    for c in final_chunks:
        lvl = int(c.get("classification_level") or 0)
        doc_id = str(c.get("document_id") or "")
        if lvl >= 3 and doc_id and doc_id not in seen_docs:
            seen_docs.add(doc_id)
            await audit_high_classification_read(
                ctx=ctx,
                action=action,
                doc_id=doc_id,
                kb_id=str(c.get("kb_id") or ""),
                effective_level=lvl,
                extra_details={"query": query[:200], "document_title": c.get("document_title")},
            )


@router.post("/retrieval/search", response_model=ChatQueryResponse)
async def search_endpoint(
    http_request: Request,
    request: ChatQueryRequest,
    db: AsyncSession = Depends(get_db),
    user: EffectiveAuthzContext = Depends(require_permission(Permissions.KB_RETRIEVE)),
):
    """纯检索接口：接入多维权限 PDP、kb_ids 授权校验、高密交付前审计与 raw_oss_url 剥离"""
    await enforce_user_rate_limit(
        request=http_request,
        tenant_id=user.tenant_id,
        user_id=user.user_id,
        scope="retrieval_search",
        limit=settings.RATE_LIMIT_RETRIEVAL_PER_MINUTE,
    )

    # 校验前端传入的 kb_ids（任一无权 kb_id 立即 403 拦截并记录拒绝审计）
    authorized_kb_ids = await validate_requested_kb_ids(db, user, request.kb_ids, action=KbAction.READ.value)

    is_safe, error_msg = validate_prompt_security(request.query)
    if not is_safe:
        await record_audit_log(
            action="RETRIEVAL_SEARCH_BLOCKED",
            resource_id=request.conversation_id,
            status="BLOCKED",
            error_msg=error_msg,
            details={"query": request.query},
        )
        raise HTTPException(status_code=400, detail=f"【安全合规拦截】{error_msg}")

    # 仅使用经服务端复核的历史消息（不信任前端缓存历史）
    if request.conversation_id:
        sess = await chat_store.get_session(request.conversation_id, tenant_id=user.tenant_id, user_id=user.user_id)
        if not sess:
            raise HTTPException(status_code=404, detail="未找到该会话或无权访问他人会话")
        history_dicts = await chat_store.build_authorized_history(request.conversation_id, user, max_messages=6, db=db)
    else:
        history_dicts = []

    builder = ThinkingProcessBuilder()

    try:
        from rag_kb.services.mcp.server import execute_core_hybrid_retrieval

        latest_state, thinking_steps, citations_raw, retrieve_ms = await execute_core_hybrid_retrieval(
            query=request.query,
            authz_ctx=user,
            kb_ids=authorized_kb_ids,
            conversation_id=request.conversation_id,
            history=history_dicts,
            category_scope=request.category_scope,
            tags_scope=request.tags_scope,
            scene_type=request.scene_type,
            enable_hyde=request.enable_hyde,
        )
        for st in thinking_steps:
            builder.update_step(st)
        builder.retrieve_ms = retrieve_ms
        thinking_process = builder.to_thinking_process()

        final_chunks_raw = latest_state.get("results") or latest_state.get("final_chunks", [])

        # L3/L4 高密命中在返回前强制落盘审计，写入失败直接中止返回
        await _audit_retrieved_high_classification_docs(
            ctx=user,
            action="HIGH_CLASSIFICATION_RETRIEVAL_READ",
            final_chunks=final_chunks_raw,
            query=request.query,
        )

        citations = [CitationSource(**{k: v for k, v in c.items() if k != "raw_oss_url"}) for c in citations_raw]
        final_chunks = [
            RetrievedChunk(
                chunk_id=c["chunk_id"],
                document_id=c["document_id"],
                document_title=c.get("document_title", ""),
                kb_id=c.get("kb_id"),
                kb_name=c.get("kb_name"),
                kb_full_path=c.get("kb_full_path"),
                classification_level=c.get("classification_level"),
                chunk_index=c.get("chunk_index", 0),
                chunk_label=c.get("chunk_label", ""),
                content=c.get("content", ""),
                page_idx=c.get("page_idx", 0),
                bbox=c.get("bbox", []),
                breadcrumb=c.get("breadcrumb", []),
                asset_url=f"/api/v1/chunks/{c['chunk_id']}/asset" if c.get("asset_url") else None,
                department_path=c.get("department_path", ""),
                score=c.get("score", 0.0),
                channel_ranks=c.get("channel_ranks", {}),
                dense_embedding=c.get("dense_embedding"),
                remark=c.get("remark"),
            )
            for c in final_chunks_raw
        ]

        await record_audit_log(
            action="RETRIEVAL_SEARCH",
            resource_id=request.conversation_id,
            duration_ms=builder.retrieve_ms,
            status="SUCCESS",
            details={
                "query": request.query,
                "kb_ids": [str(k) for k in authorized_kb_ids] if authorized_kb_ids else None,
                "chunks_count": len(final_chunks),
                "citations_count": len(citations),
            },
        )

        return ChatQueryResponse(
            conversation_id=request.conversation_id,
            rewritten_query=latest_state.get("rewritten_query", request.query),
            answer="",
            thinking=thinking_process,
            citations=citations,
            final_chunks=final_chunks,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"检索执行失败: {e}", exc_info=True)
        await record_audit_log(
            action="RETRIEVAL_SEARCH",
            resource_id=request.conversation_id,
            status="FAILED",
            error_msg=str(e),
            details={"query": request.query},
        )
        raise HTTPException(status_code=500, detail=f"检索服务异常: {str(e)}")


@router.post("/retrieval/chat")
async def chat_endpoint(
    http_request: Request,
    request: ChatQueryRequest,
    db: AsyncSession = Depends(get_db),
    user: EffectiveAuthzContext = Depends(require_permission(Permissions.KB_RETRIEVE)),
):
    """阶段 1 ~ 8 全流程智能问答流式接口（多维权限 PDP + 历史来源复核 + 高密交付前审计 + SSE 实时复核）"""
    await enforce_user_rate_limit(
        request=http_request,
        tenant_id=user.tenant_id,
        user_id=user.user_id,
        scope="retrieval_chat",
        limit=settings.RATE_LIMIT_RETRIEVAL_PER_MINUTE,
    )

    authorized_kb_ids = await validate_requested_kb_ids(db, user, request.kb_ids, action=KbAction.READ.value)

    active_stream_slot = await acquire_chat_stream_slot(tenant_id=user.tenant_id, user_id=user.user_id)

    try:
        # 严格由服务端基于当前 READ 权限复核构造多轮历史，严禁直接采信前端缓存的 history
        history_dicts: List[Dict[str, str]] = []
        if request.conversation_id:
            existing_sess = await chat_store.get_session(
                request.conversation_id, tenant_id=user.tenant_id, user_id=user.user_id
            )
            if existing_sess is not None:
                history_dicts = await chat_store.build_authorized_history(
                    request.conversation_id, user, max_messages=6, db=db
                )

        initial_state = get_default_state(
            query=request.query,
            conversation_id=request.conversation_id,
            history=history_dicts,
            authz_filter_spec=user,
            kb_ids=authorized_kb_ids,
            category_scope=request.category_scope,
            tags_scope=request.tags_scope,
            scene_type=request.scene_type,
            enable_hyde=request.enable_hyde,
        )

        if request.conversation_id:
            try:
                await chat_store.append_message(
                    request.conversation_id,
                    "user",
                    request.query,
                    source_doc_ids=[],
                    tenant_id=user.tenant_id,
                    user_id=user.user_id,
                )
            except PermissionError as pe:
                await release_chat_stream_slot(user.tenant_id, user.user_id, active_stream_slot)
                raise HTTPException(status_code=403, detail=str(pe))
            except Exception as e:
                logger.warning(f"记录用户消息失败: {e}")

        is_safe, error_msg = validate_prompt_security(request.query)
        if not is_safe:
            async def blocked_stream() -> AsyncGenerator[str, None]:
                try:
                    block_reply = f"【安全合规拦截】{error_msg}"
                    yield format_sse("message", {"content": block_reply})
                    yield format_sse("done", {"total_overall_ms": 1, "total_retrieve_ms": 0})
                    if request.conversation_id:
                        try:
                            await chat_store.append_message(
                                request.conversation_id,
                                "assistant",
                                block_reply,
                                source_doc_ids=[],
                                tenant_id=user.tenant_id,
                                user_id=user.user_id,
                            )
                        except Exception as e:
                            logger.warning(f"记录合规拒绝消息失败: {e}")
                    await record_audit_log(
                        action="RETRIEVAL_CHAT_BLOCKED",
                        resource_id=request.conversation_id,
                        status="BLOCKED",
                        error_msg=error_msg,
                        details={"query": request.query},
                    )
                finally:
                    await release_chat_stream_slot(user.tenant_id, user.user_id, active_stream_slot)

            return StreamingResponse(
                blocked_stream(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )
    except Exception:
        await release_chat_stream_slot(user.tenant_id, user.user_id, active_stream_slot)
        raise

    async def chat_event_stream() -> AsyncGenerator[str, None]:
        t0 = time.perf_counter()
        citations: List[dict] = []
        final_chunks: List[dict] = []
        watched_doc_uuids: List[uuid.UUID] = []
        builder = ThinkingProcessBuilder()
        last_revocation_check = t0

        async def _get_auth_error(force: bool = False) -> Optional[str]:
            nonlocal last_revocation_check
            now_mono = time.perf_counter()
            if not force and (now_mono - last_revocation_check) < 0.8:
                return None
            last_revocation_check = now_mono
            return await get_session_auth_error(
                user,
                token_iat_ms=user.token_iat_ms,
                token_exp=user.token_exp,
                watched_doc_ids=watched_doc_uuids or None,
            )

        try:
            auth_error = await _get_auth_error(force=True)
            if auth_error:
                yield format_sse(auth_error, {"detail": SESSION_AUTH_ERROR_DETAILS[auth_error]})
                return

            graph = get_retrieval_graph()
            latest_state = initial_state
            pending_citations_to_emit: Optional[List[dict]] = None

            async for mode, chunk in graph.astream(initial_state, stream_mode=["custom", "values"]):
                auth_error = await _get_auth_error()
                if auth_error:
                    await record_audit_log(
                        action="RETRIEVAL_CHAT",
                        resource_id=request.conversation_id,
                        duration_ms=int((time.perf_counter() - t0) * 1000),
                        status="EXPIRED" if auth_error == "auth_expired" else "REVOKED",
                        error_msg=SESSION_AUTH_ERROR_DETAILS[auth_error],
                        details={"query": request.query},
                    )
                    yield format_sse(auth_error, {"detail": SESSION_AUTH_ERROR_DETAILS[auth_error]})
                    return
                if mode == "custom" and isinstance(chunk, dict):
                    if chunk.get("type") == "thinking_step" and "step" in chunk:
                        builder.update_step(chunk["step"])
                        yield format_sse("thinking", builder.to_thinking_process().model_dump())
                    elif chunk.get("type") == "citations" and "citations" in chunk:
                        # 延后至高密审计持久化完成后再下发 citations，确保交付首字节前完成留痕
                        pending_citations_to_emit = [
                            {k: v for k, v in c.items() if k != "raw_oss_url"} for c in chunk["citations"]
                        ]
                elif mode == "values" and isinstance(chunk, dict):
                    latest_state = chunk

            builder.retrieve_ms = int((time.perf_counter() - t0) * 1000)

            final_chunks = latest_state.get("results") or latest_state.get("final_chunks", [])
            citations = pending_citations_to_emit or [
                {k: v for k, v in c.items() if k != "raw_oss_url"}
                for c in (latest_state.get("citations") or [])
            ]

            used_doc_ids = sorted({str(c["document_id"]) for c in final_chunks if c.get("document_id")})
            for did in used_doc_ids:
                try:
                    watched_doc_uuids.append(uuid.UUID(did))
                except ValueError:
                    pass

            # 交付 citations 与大模型首字前：先复核权限版本/自然到期 + 强制落盘 L3/L4 高密访问审计
            auth_error = await _get_auth_error(force=True)
            if auth_error:
                yield format_sse(auth_error, {"detail": SESSION_AUTH_ERROR_DETAILS[auth_error]})
                return

            await _audit_retrieved_high_classification_docs(
                ctx=user,
                action="HIGH_CLASSIFICATION_CHAT_READ",
                final_chunks=final_chunks,
                query=request.query,
            )

            if citations:
                yield format_sse("citations", citations)

            full_answer_parts: List[str] = []
            full_reasoning_parts: List[str] = []
            async for evt_type, token in stream_answer(request.query, final_chunks, citations):
                auth_error = await _get_auth_error()
                if auth_error:
                    await record_audit_log(
                        action="RETRIEVAL_CHAT",
                        resource_id=request.conversation_id,
                        duration_ms=int((time.perf_counter() - t0) * 1000),
                        status="EXPIRED" if auth_error == "auth_expired" else "REVOKED",
                        error_msg=SESSION_AUTH_ERROR_DETAILS[auth_error],
                        details={"query": request.query, "aborted_chars": len("".join(full_answer_parts))},
                    )
                    yield format_sse(auth_error, {"detail": SESSION_AUTH_ERROR_DETAILS[auth_error]})
                    return
                if evt_type == "reasoning":
                    full_reasoning_parts.append(token)
                    yield format_sse("reasoning", {"content": token})
                else:
                    full_answer_parts.append(token)
                    yield format_sse("message", {"content": token})

            full_answer = "".join(full_answer_parts)
            full_reasoning = "".join(full_reasoning_parts)
            total_overall_ms = int((time.perf_counter() - t0) * 1000)
            thinking_payload = builder.to_thinking_process(total_overall_ms=total_overall_ms).model_dump()

            # 持久化助手消息，完整保存实际使用的 source_doc_ids（供后续回看与多轮上下文复核）
            if request.conversation_id:
                try:
                    await chat_store.append_message(
                        request.conversation_id,
                        "assistant",
                        full_answer,
                        citations=citations,
                        source_doc_ids=used_doc_ids,
                        reasoning_content=full_reasoning if full_reasoning else None,
                        thinking_process=thinking_payload if thinking_payload else None,
                        tenant_id=user.tenant_id,
                        user_id=user.user_id,
                    )
                except Exception as e:
                    logger.warning(f"记录助手消息失败: {e}")

            await record_audit_log(
                action="RETRIEVAL_CHAT",
                resource_id=request.conversation_id,
                duration_ms=total_overall_ms,
                status="SUCCESS",
                details={
                    "query": request.query,
                    "answer_length": len(full_answer),
                    "citations_count": len(citations),
                    "source_doc_ids": used_doc_ids,
                },
            )

            yield format_sse("done", {
                "total_overall_ms": total_overall_ms,
                "total_retrieve_ms": builder.retrieve_ms,
            })

        except HTTPException as http_err:
            yield format_sse("error", {"error": http_err.detail})
        except Exception as err:
            logger.error(f"问答推流异常: {err}", exc_info=True)
            await record_audit_log(
                action="RETRIEVAL_CHAT",
                resource_id=request.conversation_id,
                status="FAILED",
                error_msg=str(err),
                details={"query": request.query},
            )
            yield format_sse("error", {"error": str(err)})
        finally:
            await release_chat_stream_slot(user.tenant_id, user.user_id, active_stream_slot)

    return StreamingResponse(
        chat_event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
