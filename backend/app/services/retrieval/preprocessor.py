import logging
from typing import List, Dict, Any
import httpx
from backend.app.core.config import settings
from backend.app.schemas.retrieval import ChatMessage
from backend.app.services.retrieval.embedding_service import embedding_service
from backend.app.services.retrieval.vector_utils import (
    extract_sparse_vector,
    format_tsvector_tokens
)

logger = logging.getLogger(__name__)

class QueryPreprocessor:
    """阶段 1：查询预处理器

    职责：
    1. 结合历史对话执行多轮指代消除 (Coreference Resolution) 与语义改写；
    2. 提取特征：1024 维 BGE-M3 稠密向量、L2 归一化稀疏向量及 PostgreSQL TSVector 分词序列。
    """

    def __init__(self):
        self.api_key = settings.active_llm_api_key
        self.base_url = settings.active_llm_base_url.rstrip("/")
        self.model_name = settings.active_llm_model

    async def rewrite_query(self, query: str, history: List[ChatMessage]) -> str:
        """多轮指代消除与查询补全改写

        若无历史对话或提问已完整，直接返回原 query；
        若有历史，则调用大模型消除代词歧义，生成自包含的独立检索句。
        严格遵循 Fail-Fast 原则。
        """
        clean_q = query.strip()
        if not history:
            return clean_q

        # 过滤掉内容为空或只包含当前提问的历史记录
        valid_history = [
            msg for msg in history
            if msg.content and msg.content.strip() and msg.content.strip() != clean_q
        ]
        if not valid_history:
            return clean_q

        # 若历史中不存在已完成的 assistant 回答，说明前序无实质上下文对话，直接返回原句
        has_assistant_reply = any(msg.role == "assistant" and msg.content.strip() for msg in valid_history)
        if not has_assistant_reply:
            return clean_q

        if not self.api_key:
            raise RuntimeError("【预处理错误】未配置大模型 API_KEY，无法进行多轮指代消除改写！")

        # 仅取最近 6 条（3 轮）历史以控制上下文质量
        recent_history = valid_history[-6:]
        conv_text = "\n".join([f"{msg.role}: {msg.content}" for msg in recent_history])

        system_prompt = (
            "你是一个专业的智能检索查询预处理器。你的任务是根据多轮对话历史，对用户的最新提问进行指代消除与主语补全，"
            "将口语化或包含代词（如'它'、'它们'、'这二者'、'上一条'、'其配置如何'）的句子改写为语义明确、主谓完整的独立检索句。\n"
            "【改写原则】：\n"
            "1. 仅补全必要的实体、主语和背景词，严禁改变用户的原始意图。\n"
            "2. 若提问本身已经语义明确且不含指代模糊，请直接输出原句。\n"
            "3. 严禁输出任何解释、前后缀或思考过程，仅输出最终改写后的一句话。"
        )

        user_prompt = f"【对话历史】：\n{conv_text}\n\n【最新用户提问】：\n{clean_q}\n\n请输出改写后的独立检索查询："

        endpoint = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": self.model_name,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            "temperature": 0.1,
            "max_tokens": 150
        }

        transport = httpx.AsyncHTTPTransport(retries=2)
        async with httpx.AsyncClient(timeout=60.0, transport=transport) as client:
            try:
                res = await client.post(endpoint, headers=headers, json=payload)
            except Exception as e:
                err_detail = f"{type(e).__name__}: {str(e) or '网络连接中断或代理握手失败'}"
                logger.error(f"【指代消除网络异常】请求失败: {err_detail}")
                raise RuntimeError(f"指代消除 LLM 调用失败: {err_detail}") from e

            if res.status_code != 200:
                raise RuntimeError(f"【指代消除接口异常】HTTP {res.status_code}: {res.text}")

            data = res.json()
            try:
                rewritten = data["choices"][0]["message"]["content"].strip()
                # 过滤可能残留的双引号或括号
                rewritten = rewritten.strip('"\'')
                if not rewritten:
                    return clean_q
                logger.info(f"【指代消除成功】原 Query: '{clean_q}' -> 改写 Query: '{rewritten}'")
                return rewritten
            except Exception as e:
                raise RuntimeError(f"【指代消除解析异常】响应格式不符合规范: {data}") from e

    async def generate_features(self, query: str) -> Dict[str, Any]:
        """同步提取查询的稠密向量、稀疏向量与全文分词特征"""
        if not query or not query.strip():
            raise ValueError("【预处理错误】输入查询文本不能为空！")

        dense_embedding = await embedding_service.embed_query(query)
        sparse_vector = extract_sparse_vector(query)
        tsquery_tokens = format_tsvector_tokens(query)

        return {
            "dense_embedding": dense_embedding,
            "sparse_vector": sparse_vector,
            "tsquery_tokens": tsquery_tokens
        }

query_preprocessor = QueryPreprocessor()
