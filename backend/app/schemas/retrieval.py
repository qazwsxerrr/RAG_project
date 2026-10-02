import uuid
from typing import List, Optional, Dict
from pydantic import BaseModel, Field

class ChatMessage(BaseModel):
    """历史对话消息"""
    role: str = Field(..., description="角色: user, assistant, system")
    content: str = Field(..., description="消息文本内容")

class ChatQueryRequest(BaseModel):
    """智能问答检索请求体"""
    query: str = Field(..., min_length=1, description="用户原始提问")
    conversation_id: Optional[str] = Field(None, description="会话 ID")
    history: List[ChatMessage] = Field(default_factory=list, description="多轮会话历史 (用于指代消除)")
    department_scope: Optional[List[str]] = Field(None, description="部门权限路径范围，例如 ['/总公司/技术部']")
    category_scope: Optional[str] = Field(None, description="知识分类限定范围，例如 '技术规范'")
    tags_scope: Optional[List[str]] = Field(None, description="业务标签限定范围，例如 ['RAG', '微调']")
    scene_type: str = Field("general", description="业务场景类型: general(通用), academic(理论概念), troubleshooting(排障法条)")
    enable_hyde: bool = Field(True, description="是否启用 HyDE 假设性问答增强")
    stream: bool = Field(True, description="是否以 SSE 流式输出")

class RetrievedChunk(BaseModel):
    """阶段检索候选切片数据实体"""
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    document_title: str
    chunk_index: int
    chunk_label: str
    content: str
    page_idx: int
    bbox: List[int] = Field(default_factory=list)
    breadcrumb: List[str] = Field(default_factory=list)
    asset_url: Optional[str] = None
    raw_oss_url: Optional[str] = None
    department_path: str
    score: float = 0.0
    channel_ranks: Dict[str, int] = Field(default_factory=dict, description="各召回通道排位，如 {'dense': 1, 'bm25': 3}")
    dense_embedding: Optional[List[float]] = Field(None, description="稠密特征向量")
    remark: Optional[str] = Field(None, description="审计标记，如 dense_recall_drop_in_rerank")

class ThinkingStep(BaseModel):
    """检索过程思考看板单个阶段 (严格契合 Phase-3 规范)"""
    name: str = Field(..., description="阶段名称，如 '查询预处理', 'RRF 融合'")
    status: str = Field("已执行", description="状态微标")
    duration_ms: int = Field(..., description="毫秒耗时")
    count_change: Optional[str] = Field(None, description="条数变迁，如 '75 -> 29 条'")
    summary: Optional[str] = Field(None, description="阶段摘要说明")

class ThinkingProcess(BaseModel):
    """检索思考全流程时间线看板"""
    total_retrieve_ms: int = Field(..., description="检索阶段总耗时")
    total_overall_ms: int = Field(..., description="全流程综合总耗时")
    steps: List[ThinkingStep] = Field(default_factory=list, description="8 阶段执行明细")

class CitationSource(BaseModel):
    """精准溯源引用卡片"""
    citation_id: int = Field(..., description="角标数字序号 [1], [2]...")
    chunk_id: str
    document_id: str
    document_title: str
    chunk_label: str
    page_idx: int
    display_page: Optional[int] = Field(None, description="自然展示页码 (1-based)")
    bbox: List[int] = Field(default_factory=list)
    snippet: str = Field(..., description="切片前 200 字摘要")
    asset_url: Optional[str] = None
    raw_oss_url: Optional[str] = None
    preview_url: Optional[str] = Field(None, description="后端安全流式内联预览接口路径 (免下载直接在网页/iframe中展示)")
    page_render_url: Optional[str] = Field(None, description="后端直接渲染好的原生单页高清 PNG 图片路径")
    asset_proxy_url: Optional[str] = Field(None, description="切片关联资产图 (表格/插图) 后端安全流式代理路径")
    score: float = 0.0

class ChatQueryResponse(BaseModel):
    """非流式问答响应数据体"""
    conversation_id: Optional[str] = None
    rewritten_query: str
    answer: str
    thinking: ThinkingProcess
    citations: List[CitationSource]
    final_chunks: List[RetrievedChunk]
