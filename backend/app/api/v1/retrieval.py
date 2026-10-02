import json
import re
import time
import logging
from typing import List, AsyncGenerator
import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config import settings
from backend.app.core.database import get_db
from backend.app.schemas.retrieval import (
    ChatQueryRequest,
    ChatQueryResponse,
    ThinkingStep,
    ThinkingProcess,
    CitationSource,
    RetrievedChunk
)
from backend.app.services.retrieval.preprocessor import query_preprocessor
from backend.app.services.retrieval.multi_retriever import multi_retriever
from backend.app.services.retrieval.hyde_service import hyde_service
from backend.app.services.retrieval.rrf_fusion import rrf_fusion
from backend.app.services.retrieval.rerank_service import rerank_service
from backend.app.services.retrieval.mmr_filter import mmr_filter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/retrieval", tags=["多阶段混合检索与问答"])

class RetrievalCoordinator:
    """8 阶段检索与流式问答总线编排引擎"""

    async def stream_retrieval_pipeline(
        self,
        session: AsyncSession,
        request: ChatQueryRequest
    ):
        """流式逐步执行阶段 1 至 阶段 6 检索流水线，逐阶段 yield 实时进度看板 ThinkingProcess 与最终结果"""
        start_all = time.perf_counter()
        steps: List[ThinkingStep] = []

        # ---------------- 阶段 1：查询预处理 ----------------
        steps.append(ThinkingStep(
            name="查询预处理",
            status="执行中...",
            duration_ms=0,
            summary="正在多轮会话指代消除并生成高维稠密与稀疏特征向量..."
        ))
        yield ThinkingProcess(total_retrieve_ms=0, total_overall_ms=0, steps=list(steps))

        t1_start = time.perf_counter()
        rewritten_query = await query_preprocessor.rewrite_query(
            query=request.query,
            history=request.history
        )
        features = await query_preprocessor.generate_features(rewritten_query)
        t1_ms = int((time.perf_counter() - t1_start) * 1000)

        if rewritten_query != request.query.strip():
            summary_desc = f"完成多轮指代消除 (改写为: '{rewritten_query[:30]}...')，提取稠密与稀疏特征"
        else:
            summary_desc = "查询语义自包含完整，提取高维稠密特征与分词字典"

        steps[-1] = ThinkingStep(
            name="查询预处理",
            status="已执行",
            duration_ms=t1_ms,
            count_change="1 句",
            summary=summary_desc
        )

        # ---------------- 阶段 2：三路并行基础召回 ----------------
        steps.append(ThinkingStep(
            name="多路并行召回",
            status="执行中...",
            duration_ms=0,
            summary="并发发起 HNSW 稠密语义、PG TSVector 关键词与稀疏内积检索..."
        ))
        yield ThinkingProcess(total_retrieve_ms=0, total_overall_ms=0, steps=list(steps))

        t2_start = time.perf_counter()
        recall_res = await multi_retriever.parallel_recall(
            session=session,
            query_dense=features["dense_embedding"],
            query_sparse=features["sparse_vector"],
            tsquery_tokens=features["tsquery_tokens"],
            department_scope=request.department_scope,
            category_scope=request.category_scope,
            tags_scope=request.tags_scope,
            top_k=20
        )
        dense_chunks = recall_res["dense"]
        sparse_chunks = recall_res["sparse"]
        bm25_chunks = recall_res["bm25"]
        t2_ms = int((time.perf_counter() - t2_start) * 1000)

        # 构造生效的范围限定摘要
        scope_tags = []
        if request.department_scope:
            scope_tags.append(f"部门:{','.join(request.department_scope)}")
        if request.category_scope and request.category_scope != "全部":
            scope_tags.append(f"分类:{request.category_scope}")
        if request.tags_scope:
            scope_tags.append(f"标签:{','.join(request.tags_scope)}")
        scope_suffix = f" ({' · '.join(scope_tags)})" if scope_tags else ""

        steps.pop()  # 移除临时 "多路并行召回"
        steps.append(ThinkingStep(
            name="稠密召回",
            status="已执行",
            duration_ms=t2_ms,
            count_change=f"{len(dense_chunks)} 条",
            summary=f"HNSW 1024 维语义向量召回 Top-{len(dense_chunks)}{scope_suffix}"
        ))
        steps.append(ThinkingStep(
            name="关键词召回",
            status="已执行",
            duration_ms=t2_ms,
            count_change=f"{len(bm25_chunks)} 条",
            summary=f"PostgreSQL TSVector 倒排索引召回 Top-{len(bm25_chunks)}{scope_suffix}"
        ))
        steps.append(ThinkingStep(
            name="稀疏召回",
            status="已执行",
            duration_ms=t2_ms,
            count_change=f"{len(sparse_chunks)} 条",
            summary=f"L2 归一化稀疏内积召回 Top-{len(sparse_chunks)}{scope_suffix}"
        ))

        # ---------------- 阶段 3：HyDE 假设性推理 ----------------
        hyde_chunks: List[RetrievedChunk] = []
        if request.enable_hyde:
            steps.append(ThinkingStep(
                name="假设性搜索",
                status="执行中...",
                duration_ms=0,
                summary="大模型技术顾问正在构思撰写假想技术解答并向量化二次召回 (深度思考约30-60s)..."
            ))
            yield ThinkingProcess(total_retrieve_ms=0, total_overall_ms=0, steps=list(steps))

            t3_start = time.perf_counter()
            hyde_chunks = await hyde_service.search_with_circuit_breaker(
                session=session,
                query=rewritten_query,
                department_scope=request.department_scope,
                category_scope=request.category_scope,
                tags_scope=request.tags_scope,
                top_k=15
            )
            t3_ms = int((time.perf_counter() - t3_start) * 1000)
            steps[-1] = ThinkingStep(
                name="假设性搜索",
                status="已执行" if hyde_chunks else "已熔断/跳过",
                duration_ms=t3_ms,
                count_change=f"{len(hyde_chunks)} 条",
                summary=f"专家假想技术文档向量化召回 Top-{len(hyde_chunks)}{scope_suffix} ({int(hyde_service.timeout_seconds)}s 熔断保护)"
            )

        # ---------------- 阶段 4：RRF 倒数融合 ----------------
        steps.append(ThinkingStep(
            name="RRF 融合",
            status="执行中...",
            duration_ms=0,
            summary=f"场景 '{request.scene_type}' 动态加权倒数排名去重融合与物理隔离中..."
        ))
        yield ThinkingProcess(total_retrieve_ms=0, total_overall_ms=0, steps=list(steps))

        t4_start = time.perf_counter()
        recall_pools = {
            "dense": dense_chunks,
            "sparse": sparse_chunks,
            "bm25": bm25_chunks
        }
        if hyde_chunks:
            recall_pools["hyde"] = hyde_chunks

        total_recall_candidates = sum(len(c) for c in recall_pools.values())
        fused_chunks = rrf_fusion.fuse(
            recall_pools=recall_pools,
            scene_type=request.scene_type,
            top_n=29
        )
        t4_ms = int((time.perf_counter() - t4_start) * 1000)

        steps[-1] = ThinkingStep(
            name="RRF 融合",
            status="已执行",
            duration_ms=t4_ms,
            count_change=f"{total_recall_candidates} -> {len(fused_chunks)} 条",
            summary=f"场景 '{request.scene_type}' 动态加权倒数排名去重融合，外网数据物理隔离"
        )

        # ---------------- 阶段 5：BGE-Reranker 交叉精排 ----------------
        steps.append(ThinkingStep(
            name="重排",
            status="执行中...",
            duration_ms=0,
            summary="正在使用 BGE-Reranker-v2 交叉注意力深度精排打分..."
        ))
        yield ThinkingProcess(total_retrieve_ms=0, total_overall_ms=0, steps=list(steps))

        t5_start = time.perf_counter()
        reranked_chunks, dropped_chunks = await rerank_service.rerank(
            query=rewritten_query,
            chunks=fused_chunks,
            top_n=20
        )
        t5_ms = int((time.perf_counter() - t5_start) * 1000)

        steps[-1] = ThinkingStep(
            name="重排",
            status="已执行",
            duration_ms=t5_ms,
            count_change=f"{len(fused_chunks)} -> {len(reranked_chunks)} 条",
            summary=f"Cross-Encoder 交叉注意力打分，检出丢弃审计 {len(dropped_chunks)} 条"
        )

        # ---------------- 阶段 6：MMR 多样性去重 ----------------
        steps.append(ThinkingStep(
            name="MMR 去重",
            status="执行中...",
            duration_ms=0,
            summary="正在使用最大边际相关性贪心筛选黄金切片..."
        ))
        yield ThinkingProcess(total_retrieve_ms=0, total_overall_ms=0, steps=list(steps))

        t6_start = time.perf_counter()
        final_chunks = mmr_filter.filter(
            query_embedding=features["dense_embedding"],
            chunks=reranked_chunks,
            top_k=5,
            lambda_param=0.7
        )
        t6_ms = int((time.perf_counter() - t6_start) * 1000)

        steps[-1] = ThinkingStep(
            name="MMR 去重",
            status="已执行",
            duration_ms=t6_ms,
            count_change=f"{len(reranked_chunks)} -> {len(final_chunks)} 条",
            summary="最大边际相关性贪心选择 (lambda=0.7)，提炼 5 条无冗余互补黄金切片"
        )

        total_retrieve_ms = int((time.perf_counter() - start_all) * 1000)

        # 构建精准溯源卡片列表 (供角标溯源)
        citations: List[CitationSource] = []
        for idx, c in enumerate(final_chunks):
            # 物理自然页码 (1-based)
            target_page = (c.page_idx or 0) + 1
            page_render_url = f"/api/v1/documents/{c.document_id}/pages/{target_page}"
            asset_proxy_url = f"/api/v1/documents/chunks/{c.chunk_id}/asset" if c.asset_url else None

            cit = CitationSource(
                citation_id=idx + 1,
                chunk_id=str(c.chunk_id),
                document_id=str(c.document_id),
                document_title=c.document_title,
                chunk_label=c.chunk_label,
                page_idx=c.page_idx,
                display_page=target_page,
                bbox=c.bbox,
                snippet=c.content[:220].strip(),
                asset_url=c.asset_url,
                raw_oss_url=c.raw_oss_url,
                preview_url=f"/api/v1/documents/{c.document_id}/preview",
                page_render_url=page_render_url,
                asset_proxy_url=asset_proxy_url,
                score=c.score
            )
            citations.append(cit)

        # 最终推一次检索完成状态 (附带总检索耗时)
        yield ThinkingProcess(
            total_retrieve_ms=total_retrieve_ms,
            total_overall_ms=total_retrieve_ms,
            steps=list(steps)
        )

        # 最终输出全量流水线结算对象
        yield {
            "is_final_result": True,
            "rewritten_query": rewritten_query,
            "final_chunks": final_chunks,
            "citations": citations,
            "dropped_chunks": dropped_chunks,
            "steps": steps,
            "total_retrieve_ms": total_retrieve_ms,
            "start_time": start_all
        }

    async def execute_retrieval_pipeline(
        self,
        session: AsyncSession,
        request: ChatQueryRequest
    ):
        """执行阶段 1 至 阶段 6 检索流水线，供非流式 search 接口直接获取结果"""
        final_res = None
        async for item in self.stream_retrieval_pipeline(session, request):
            if isinstance(item, dict) and item.get("is_final_result"):
                final_res = item
        return final_res

    def build_generation_prompts(
        self,
        query: str,
        citations: List[CitationSource],
        final_chunks: List[RetrievedChunk]
    ) -> tuple[str, str]:
        """阶段 7：Prompt 动态编排 (注入防幻觉约束与 [1], [2] 角标规范)"""
        materials = []
        for idx, (cit, chunk) in enumerate(zip(citations, final_chunks)):
            materials.append(
                f"[{cit.citation_id}] 文档：《{cit.document_title}》| 页码：第 {cit.page_idx} 页 | 标签：{cit.chunk_label}\n"
                f"内容：\n{chunk.content}\n"
            )
        context_str = "\n".join(materials)

        system_prompt = (
            "你是一名严谨的企业级知识问答顾问。请完全且仅依据下列检索到的【参考材料】回答用户的问题。\n\n"
            "【回答规范与红线准则】：\n"
            "1. 必须开门见山，第一句直接给出核心定义与明确解答。严禁输出任何形式的思考前言、审题说明、材料提取梳理或自言自语（例如“正在梳理...”、“已提取关键信息...”等）。\n"
            "2. 答案中每一个核心论点必须显式用角标标注引用的材料序号，例如引用第 1 篇材料标记 [1]，引用第 2 篇标记 [2]，支持多重标注如 [1][2]。\n"
            "3. 严禁捏造参考材料中未提及的事实、参数、因果或结论。若材料中未包含回答该问题所需的信息，请明确告知“依据现有参考知识库，未找到相关依据”。\n"
            "4. 若材料中包含对比表格或结构化数据，请采用标准 Markdown 表格直观展现。\n"
            "5. 保持客观、专业、条理清晰，严格直接给出结构化解答。"
        )

        user_prompt = (
            f"【参考材料】：\n{context_str}\n\n"
            f"【用户提问】：\n{query}\n\n"
            "请依据上述参考材料给出专业、带引文角标 [1], [2] 的解答："
        )

        return system_prompt, user_prompt

coordinator = RetrievalCoordinator()

@router.post("/search", response_model=ChatQueryResponse)
async def search_chunks(
    request: ChatQueryRequest,
    session: AsyncSession = Depends(get_db)
):
    """纯检索接口 (阶段 1 ~ 阶段 6)：获取经过多阶段漏斗精选的切片、耗时思考流及丢弃审计"""
    pipe_res = await coordinator.execute_retrieval_pipeline(session, request)
    total_ms = int((time.perf_counter() - pipe_res["start_time"]) * 1000)

    thinking = ThinkingProcess(
        total_retrieve_ms=pipe_res["total_retrieve_ms"],
        total_overall_ms=total_ms,
        steps=pipe_res["steps"]
    )

    return ChatQueryResponse(
        conversation_id=request.conversation_id,
        rewritten_query=pipe_res["rewritten_query"],
        answer="检索完成，已提取最优切片候选。",
        thinking=thinking,
        citations=pipe_res["citations"],
        final_chunks=pipe_res["final_chunks"]
    )

@router.post("/chat")
async def chat_with_retrieval(
    request: ChatQueryRequest,
    session: AsyncSession = Depends(get_db)
):
    """阶段 1 ~ 阶段 8 全流程智能问答接口 (支持 SSE 打字机流式输出与思考流看板)"""
    # 1. 如果不需要流式，直接调用完整返回
    if not request.stream:
        pipe_res = await coordinator.execute_retrieval_pipeline(session, request)
        system_prompt, user_prompt = coordinator.build_generation_prompts(
            query=pipe_res["rewritten_query"],
            citations=pipe_res["citations"],
            final_chunks=pipe_res["final_chunks"]
        )

        endpoint = f"{settings.active_llm_base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {settings.active_llm_api_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": settings.active_llm_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            "temperature": 0.2
        }

        async with httpx.AsyncClient(timeout=60.0) as client:
            res = await client.post(endpoint, headers=headers, json=payload)
            if res.status_code != 200:
                raise HTTPException(status_code=502, detail=f"LLM 生成失败: {res.text}")
            data = res.json()
            raw_content = data["choices"][0]["message"].get("content", "") or ""
            reasoning_text = data["choices"][0]["message"].get("reasoning_content", "") or ""

            # 兼容模型在 content 中输出 <think>...</think> 的情况
            think_match = re.search(r"<think>(.*?)</think>", raw_content, flags=re.DOTALL)
            if think_match:
                extracted_thinking = think_match.group(1).strip()
                answer_text = re.sub(r"<think>.*?</think>\s*", "", raw_content, flags=re.DOTALL).strip()
                if not reasoning_text and extracted_thinking:
                    reasoning_text = extracted_thinking
            else:
                answer_text = raw_content

            if reasoning_text and pipe_res.get("steps"):
                pipe_res["steps"][-1].summary = f"深度推演完成 ({len(reasoning_text)} 字符思维链)"

        total_ms = int((time.perf_counter() - pipe_res["start_time"]) * 1000)
        thinking = ThinkingProcess(
            total_retrieve_ms=pipe_res["total_retrieve_ms"],
            total_overall_ms=total_ms,
            steps=pipe_res["steps"]
        )

        return ChatQueryResponse(
            conversation_id=request.conversation_id,
            rewritten_query=pipe_res["rewritten_query"],
            answer=answer_text,
            thinking=thinking,
            citations=pipe_res["citations"],
            final_chunks=pipe_res["final_chunks"]
        )

    # 2. SSE 流式打字机生成：立即返回连接，在流内逐步推流检索思考过程与大模型生成
    async def event_generator() -> AsyncGenerator[str, None]:
        pipe_res = None
        try:
            async for item in coordinator.stream_retrieval_pipeline(session, request):
                if isinstance(item, ThinkingProcess):
                    yield f"event: thinking\ndata: {item.model_dump_json()}\n\n"
                elif isinstance(item, dict) and item.get("is_final_result"):
                    pipe_res = item
        except Exception as e:
            logger.error(f"【检索流水线异常】: {e}", exc_info=True)
            yield f"event: error\ndata: {json.dumps({'error': f'检索流水线异常: {str(e)}'}, ensure_ascii=False)}\n\n"
            return

        if not pipe_res:
            yield f"event: error\ndata: {json.dumps({'error': '检索流水线未返回有效结果'}, ensure_ascii=False)}\n\n"
            return

        system_prompt, user_prompt = coordinator.build_generation_prompts(
            query=pipe_res["rewritten_query"],
            citations=pipe_res["citations"],
            final_chunks=pipe_res["final_chunks"]
        )

        start_time = pipe_res["start_time"]
        total_retrieve_ms = pipe_res["total_retrieve_ms"]

        # 下发溯源卡片数据: event: citations
        citations_json = json.dumps([c.model_dump() for c in pipe_res["citations"]], ensure_ascii=False)
        yield f"event: citations\ndata: {citations_json}\n\n"

        # 阶段 7 & 8: 调用大模型流式打字机吐字
        gen_start = time.perf_counter()
        steps = list(pipe_res["steps"])
        gen_step = ThinkingStep(
            name="大模型解答生成",
            status="深度推演中...",
            duration_ms=0,
            summary="大模型技术顾问正在组织依据材料并推演解答逻辑..."
        )
        steps.append(gen_step)
        yield f"event: thinking\ndata: {ThinkingProcess(total_retrieve_ms=total_retrieve_ms, total_overall_ms=0, steps=list(steps)).model_dump_json()}\n\n"

        endpoint = f"{settings.active_llm_base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {settings.active_llm_api_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": settings.active_llm_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            "temperature": 0.2,
            "stream": True
        }

        reasoning_buffer: List[str] = []
        last_reasoning_time = time.perf_counter()
        first_token_received = False
        token_count = 0
        in_think_block = False
        stream_buf = ""

        async with httpx.AsyncClient(timeout=90.0) as client:
            try:
                async with client.stream("POST", endpoint, headers=headers, json=payload) as response:
                    if response.status_code != 200:
                        err_content = await response.aread()
                        yield f"event: error\ndata: {json.dumps({'error': f'LLM API 报错 {response.status_code}: {err_content.decode()}'}, ensure_ascii=False)}\n\n"
                        return

                    async for line in response.aiter_lines():
                        line = line.strip()
                        if not line:
                            continue
                        if line.startswith("data:"):
                            data_part = line[5:].strip()
                            if data_part == "[DONE]":
                                break
                            try:
                                chunk_json = json.loads(data_part)
                                delta = chunk_json["choices"][0].get("delta", {})
                                raw_token = delta.get("content", "")
                                explicit_reasoning = delta.get("reasoning_content", "")

                                # 1. 处理 API 显式返回的 reasoning_content 字段
                                if explicit_reasoning:
                                    reasoning_buffer.append(explicit_reasoning)
                                    now = time.perf_counter()
                                    if now - last_reasoning_time >= 0.8:
                                        last_reasoning_time = now
                                        snippet = "".join(reasoning_buffer).strip().replace("\n", " ")
                                        if len(snippet) > 80:
                                            snippet = "..." + snippet[-77:]
                                        steps[-1].summary = f"深度推演中: {snippet}"
                                        yield f"event: thinking\ndata: {ThinkingProcess(total_retrieve_ms=total_retrieve_ms, total_overall_ms=0, steps=list(steps)).model_dump_json()}\n\n"

                                # 2. 处理流式 content，并防范过滤 <think>...</think> 标签泄漏入正文
                                if raw_token:
                                    stream_buf += raw_token
                                    while stream_buf:
                                        if not in_think_block:
                                            if "<think>" in stream_buf:
                                                pre, post = stream_buf.split("<think>", 1)
                                                if pre:
                                                    token_count += len(pre)
                                                    if not first_token_received:
                                                        first_token_received = True
                                                        steps[-1].status = "流式输出中"
                                                        steps[-1].summary = "逻辑推演完成，正在打字输出结构化解答..."
                                                        yield f"event: thinking\ndata: {ThinkingProcess(total_retrieve_ms=total_retrieve_ms, total_overall_ms=0, steps=list(steps)).model_dump_json()}\n\n"
                                                    delta_msg = json.dumps({"content": pre}, ensure_ascii=False)
                                                    yield f"event: message\ndata: {delta_msg}\n\n"
                                                in_think_block = True
                                                stream_buf = post
                                            elif stream_buf.startswith("<") and len(stream_buf) < len("<think>"):
                                                # 暂存以防 <think> 跨 token 截断
                                                break
                                            else:
                                                if "<" in stream_buf:
                                                    idx = stream_buf.index("<")
                                                    emit_chunk = stream_buf[:idx]
                                                    stream_buf = stream_buf[idx:]
                                                else:
                                                    emit_chunk = stream_buf
                                                    stream_buf = ""

                                                if emit_chunk:
                                                    token_count += len(emit_chunk)
                                                    if not first_token_received:
                                                        first_token_received = True
                                                        steps[-1].status = "流式输出中"
                                                        steps[-1].summary = "逻辑推演完成，正在打字输出结构化解答..."
                                                        yield f"event: thinking\ndata: {ThinkingProcess(total_retrieve_ms=total_retrieve_ms, total_overall_ms=0, steps=list(steps)).model_dump_json()}\n\n"
                                                    delta_msg = json.dumps({"content": emit_chunk}, ensure_ascii=False)
                                                    yield f"event: message\ndata: {delta_msg}\n\n"
                                        else:
                                            # 处于 <think> 思考块内部
                                            if "</think>" in stream_buf:
                                                think_part, post = stream_buf.split("</think>", 1)
                                                if think_part:
                                                    reasoning_buffer.append(think_part)
                                                in_think_block = False
                                                stream_buf = post.lstrip("\r\n")

                                                snippet = "".join(reasoning_buffer).strip().replace("\n", " ")
                                                if len(snippet) > 80:
                                                    snippet = "..." + snippet[-77:]
                                                steps[-1].status = "流式输出中"
                                                steps[-1].summary = f"深度推演完成: {snippet}" if snippet else "逻辑推演完成，正在打字输出结构化解答..."
                                                yield f"event: thinking\ndata: {ThinkingProcess(total_retrieve_ms=total_retrieve_ms, total_overall_ms=0, steps=list(steps)).model_dump_json()}\n\n"
                                            elif "</" in stream_buf and (len(stream_buf) - stream_buf.rfind("</")) < len("</think>"):
                                                # 暂存以防 </think> 跨 token 截断
                                                idx = stream_buf.rfind("</")
                                                if idx > 0:
                                                    think_part = stream_buf[:idx]
                                                    reasoning_buffer.append(think_part)
                                                    stream_buf = stream_buf[idx:]
                                                break
                                            else:
                                                reasoning_buffer.append(stream_buf)
                                                stream_buf = ""
                                                now = time.perf_counter()
                                                if now - last_reasoning_time >= 0.8:
                                                    last_reasoning_time = now
                                                    snippet = "".join(reasoning_buffer).strip().replace("\n", " ")
                                                    if len(snippet) > 80:
                                                        snippet = "..." + snippet[-77:]
                                                    steps[-1].summary = f"深度推演中: {snippet}"
                                                    yield f"event: thinking\ndata: {ThinkingProcess(total_retrieve_ms=total_retrieve_ms, total_overall_ms=0, steps=list(steps)).model_dump_json()}\n\n"
                            except Exception:
                                continue

                    # 循环正常结束后，若仍有未推流的缓冲内容
                    if stream_buf:
                        if in_think_block:
                            reasoning_buffer.append(stream_buf)
                        else:
                            token_count += len(stream_buf)
                            if not first_token_received:
                                first_token_received = True
                                steps[-1].status = "流式输出中"
                                steps[-1].summary = "逻辑推演完成，正在打字输出结构化解答..."
                                yield f"event: thinking\ndata: {ThinkingProcess(total_retrieve_ms=total_retrieve_ms, total_overall_ms=0, steps=list(steps)).model_dump_json()}\n\n"
                            delta_msg = json.dumps({"content": stream_buf}, ensure_ascii=False)
                            yield f"event: message\ndata: {delta_msg}\n\n"
            except Exception as e:
                logger.error(f"【流式打字机网络异常】: {e}")
                yield f"event: error\ndata: {json.dumps({'error': str(e)}, ensure_ascii=False)}\n\n"
                return

        # 最终更新大模型生成步骤状态
        gen_duration_ms = int((time.perf_counter() - gen_start) * 1000)
        steps[-1].status = "已执行"
        steps[-1].duration_ms = gen_duration_ms
        steps[-1].summary = f"解答生成完毕 (耗时 {gen_duration_ms} ms, 共 {token_count} 字符)"
        yield f"event: thinking\ndata: {ThinkingProcess(total_retrieve_ms=total_retrieve_ms, total_overall_ms=0, steps=list(steps)).model_dump_json()}\n\n"

        # 最终推流完成事件: event: done
        total_overall_ms = int((time.perf_counter() - start_time) * 1000)
        done_payload = json.dumps({
            "finish_reason": "stop",
            "total_overall_ms": total_overall_ms,
            "total_retrieve_ms": total_retrieve_ms
        }, ensure_ascii=False)
        yield f"event: done\ndata: {done_payload}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )
