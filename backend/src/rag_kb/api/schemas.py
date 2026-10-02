"""src/rag_kb/api/schemas.py —— HTTP 边界与数据契约模型定义。

包含：
1. 检索与问答契约：ChatQueryRequest, ChatQueryResponse, ThinkingStep, ThinkingProcess, CitationSource, RetrievedChunk
2. 入库流水线契约：PipelineStepStatus, PipelineSnapshotResponse, ReviewItem
3. 部门与会话模型
严格与当前前端 Vue 3 TypeScript 契约 100% 兼容。
"""

import uuid
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field

# ==================== 1. 对话与检索模型 ====================

class ChatMessage(BaseModel):
    """历史对话消息"""
    role: str = Field(..., description="角色: user, assistant, system")
    content: str = Field(..., description="消息文本内容")

class ChatQueryRequest(BaseModel):
    """智能问答检索请求体"""
    query: str = Field(..., min_length=1, description="用户原始提问")
    conversation_id: Optional[str] = Field(None, description="会话 ID")
    history: List[ChatMessage] = Field(default_factory=list, description="多轮会话历史")
    department_scope: Optional[List[str]] = Field(None, description="部门权限路径范围，例如 ['/总公司/研发中心/技术部']")
    category_scope: Optional[str] = Field(None, description="知识分类限定范围")
    tags_scope: Optional[List[str]] = Field(None, description="业务标签限定范围")
    scene_type: str = Field("general", description="业务场景类型: general, academic, troubleshooting, compliance, technical")
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
    channel_ranks: Dict[str, int] = Field(default_factory=dict, description="各通道排位，如 {'dense': 1, 'bm25': 3}")
    dense_embedding: Optional[List[float]] = Field(None, description="稠密特征向量")
    remark: Optional[str] = Field(None, description="丢弃审计标记")

class ThinkingStep(BaseModel):
    """检索过程思考看板单个阶段"""
    name: str = Field(..., description="阶段名称，如 '查询预处理', 'RRF 融合'")
    status: str = Field("已执行", description="状态微标: 执行中... / 已执行 / 已熔断/跳过")
    duration_ms: int = Field(0, description="毫秒耗时")
    count_change: Optional[str] = Field(None, description="条数变迁，如 '75 -> 29 条'")
    summary: Optional[str] = Field(None, description="阶段摘要说明")

class ThinkingProcess(BaseModel):
    """检索思考全流程时间线看板"""
    total_retrieve_ms: int = Field(0, description="检索阶段总耗时")
    total_overall_ms: int = Field(0, description="全流程综合总耗时")
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
    preview_url: Optional[str] = Field(None, description="后端安全流式内联预览接口路径")
    page_render_url: Optional[str] = Field(None, description="单页高清 PNG 路径")
    asset_proxy_url: Optional[str] = Field(None, description="切片关联资产图代理路径")
    score: float = 0.0

class ChatQueryResponse(BaseModel):
    """非流式问答响应数据体"""
    conversation_id: Optional[str] = None
    rewritten_query: str
    answer: str
    thinking: ThinkingProcess
    citations: List[CitationSource]
    final_chunks: List[RetrievedChunk]

# ==================== 2. 入库流水线模型 ====================

class PipelineStepStatus(BaseModel):
    """流水线单步状态"""
    step_index: int
    name: str
    status: str = "waiting" # waiting | running | completed | failed | pending_review
    duration_ms: int = 0
    error: Optional[str] = None

class ReviewItem(BaseModel):
    """人机审核项"""
    item_id: str
    doc_id: str
    type: str # table | image | flowchart | code
    title: str
    page_idx: int
    display_page: int
    line_number: Optional[int] = None
    asset_url: str
    raw_oss_url: Optional[str] = None
    asset_proxy_url: Optional[str] = None
    raw_content: Optional[str] = None
    vlm_description: str
    user_description: Optional[str] = None
    language: Optional[str] = None
    status: str = "pending" # pending | approved | modified

class PipelineSnapshotResponse(BaseModel):
    """流水线快照响应"""
    doc_id: str
    file_name: str
    overall_status: str
    current_step_index: int
    current_step_name: str
    steps: List[PipelineStepStatus]
    requires_review: bool = False
    pending_reviews: List[ReviewItem] = Field(default_factory=list)
    is_duplicate_warning: bool = False
    matched_doc_id: Optional[str] = None

class ReviewItemUpdate(BaseModel):
    """单项审核提交"""
    item_id: str
    user_description: str
    remark: Optional[str] = None
    approved: bool = True

class ReviewSubmitRequest(BaseModel):
    """人工审核全量提交请求"""
    doc_id: str
    items: List[ReviewItemUpdate]

# ==================== 3. 文档管理与部门模型 ====================

class DocumentUploadResponse(BaseModel):
    """文件上传初始化的统一返回体"""
    doc_id: str = Field(..., description="文档唯一UUID标识")
    file_name: str = Field(..., description="文档名称")
    file_size: int = Field(..., description="文件大小（字节）")
    file_hash: str = Field(..., description="SHA-256 哈希值")
    department: str = Field(..., description="归属部门")
    category: str = Field(..., description="文档分类")
    tags: List[str] = Field(default_factory=list, description="标签胶囊列表")
    status: str = Field(default="ingested", description="当前入库状态")
    oss_url: str = Field(..., description="OSS 原文件访问地址")

class DocumentItemSchema(BaseModel):
    """文档列表中的单项模型"""
    doc_id: str
    file_name: str
    department: str
    category: str
    tags: List[str] = Field(default_factory=list)
    status: str
    step_index: int = 11
    created_at: Optional[Any] = None
    updated_at: Optional[Any] = None

class DepartmentItemSchema(BaseModel):
    """部门信息"""
    id: int
    name: str
    path: str

class ChunkItemSchema(BaseModel):
    """切片明细模型"""
    id: Optional[str] = None
    document_id: str
    chunk_index: int
    chunk_label: str
    content: str
    is_code: bool = False
    is_table: bool = False
    page_idx: int = 0
    display_page: int = 1
    breadcrumb: List[str] = Field(default_factory=list)
    asset_url: Optional[str] = None
    table_id: Optional[str] = None
    table_html: Optional[str] = None
    sparse_token_count: int = 0

class DuplicateResolveRequest(BaseModel):
    """防重冲突解决请求"""
    action: str = Field(..., description="处理动作: overwrite (覆盖升级), create_new (独立新建), cancel (取消放弃)")
    matched_doc_id: Optional[str] = Field(None, description="匹配到的旧文档ID")

class DuplicateResolveResponse(BaseModel):
    """防重解决响应"""
    doc_id: str
    status: str
    message: str
    version: int = 1

class RetryPipelineRequest(BaseModel):
    """流水线重试请求"""
    from_step: Optional[int] = Field(None, description="从指定步骤编号重试 (1~11)")

