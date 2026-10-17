"""src/rag_search/answer.py —— RAG 的生成侧：把召回片段交给大模型做流式答案生成。

不在检索图内（图到 mmr 即结束），入参是精选后的黄金片段 hits 与 citations。
组织严格遵循企业红线规范与防幻觉约束的 System Prompt 与 User Prompt，
片段的编号顺序就是回答里 [1]、[2] 引用的来源序号。提供流式打字机 token 输出。
"""

import logging
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple
from rag_kb.core.config import settings
from rag_kb.core.llm import llm_gateway
from rag_kb.core.security import validate_prompt_security, default_stream_moderator

logger = logging.getLogger(__name__)

def build_generation_prompts(
    query: str,
    hits: List[Dict[str, Any]],
    citations: Optional[List[Dict[str, Any]]] = None
) -> Tuple[str, str]:
    """阶段 7: Prompt 动态编排（注入防幻觉红线约束与 [1][2] 引文角标规范）"""
    materials = []
    cits = citations or []
    for idx, chunk in enumerate(hits):
        cit_id = idx + 1
        doc_title = chunk.get("document_title", "知识库文档")
        page_idx = chunk.get("page_idx", 0)
        chunk_label = chunk.get("chunk_label", f"第 {cit_id} 片")
        content = chunk.get("content", "")

        materials.append(
            f"[{cit_id}] 文档：《{doc_title}》| 页码：第 {page_idx + 1} 页 | 标签：{chunk_label}\n"
            f"内容：\n{content}\n"
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

async def stream_answer(
    query: str,
    hits: List[Dict[str, Any]],
    citations: Optional[List[Dict[str, Any]]] = None
) -> AsyncGenerator[Tuple[str, str], None]:
    """流式打字机生成解答增量 token，分流返回 (event_type, token)，内置敏感词脱敏与 Prompt 注入防御"""
    # 1. 前置 Prompt 注入与越狱指令拦截
    is_safe, error_msg = validate_prompt_security(query)
    if not is_safe:
        yield ("message", f"【安全合规拦截】{error_msg}")
        return

    system_prompt, user_prompt = build_generation_prompts(query, hits, citations)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt}
    ]

    # 2. 从网关获取原始双流并通过安全管道实时脱敏正式回答流
    raw_stream = llm_gateway.stream_chat(
        messages=messages,
        temperature=0.2
    )

    async for event_type, token in default_stream_moderator.filter_chat_stream(raw_stream):
        yield (event_type, token)

async def generate_answer(
    query: str,
    hits: List[Dict[str, Any]],
    citations: Optional[List[Dict[str, Any]]] = None
) -> str:
    """非流式一次性生成解答全文（仅提取正式答案部分）"""
    tokens = []
    async for evt_type, token in stream_answer(query, hits, citations):
        if evt_type == "message":
            tokens.append(token)
    return "".join(tokens)
