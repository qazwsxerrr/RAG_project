from datetime import datetime, timezone
from typing import List, Optional, Any, Dict
from pydantic import BaseModel, Field

class PipelineStepStatus(BaseModel):
    """流水线单节点状态"""
    step_index: int = Field(..., description="节点编号 1~11")
    name: str = Field(..., description="节点名称")
    status: str = Field(..., description="waiting | running | completed | failed | pending_review")
    duration_ms: int = Field(default=0, description="耗时毫秒数")
    error_message: Optional[str] = Field(None, description="异常信息")
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

class ReviewItem(BaseModel):
    """人工审核单项（表格、图片、流程图、代码块）"""
    item_id: str = Field(..., description="审核项目唯一ID")
    doc_id: str = Field(..., description="所属文档ID")
    type: str = Field(..., description="'table' | 'image' | 'flowchart' | 'code'")
    title: str = Field(..., description="展示标题，如 '表格 1 (md 第 135 行)'")
    page_idx: int = Field(..., description="0-indexed 原始页码")
    display_page: int = Field(..., description="1-indexed 显示页码")
    line_number: Optional[int] = Field(None, description="在 Markdown 中的行号")
    asset_url: str = Field(default="", description="图片或表格切图访问 URL，无切图时为空字符串")
    raw_oss_url: Optional[str] = Field(None, description="原始阿里云 OSS 地址")
    asset_proxy_url: Optional[str] = Field(None, description="后端安全流式反向代理路径")
    raw_content: Optional[str] = Field(None, description="原始 HTML 表格、代码或 Mermaid 内容")
    vlm_description: str = Field(..., description="VLM 或大模型提取的初版语义描述")
    user_description: Optional[str] = Field(None, description="人工核对修改后的描述")
    remark: Optional[str] = Field(None, description="人工备注")
    language: Optional[str] = Field(None, description="代码语言 (python/sql/bash 等)")
    status: str = Field(default="pending", description="'pending' | 'approved' | 'modified'")

class PipelineSnapshotResponse(BaseModel):
    """流水线状态快照（对应 Img 04 重新连接状态恢复）"""
    doc_id: str
    file_name: str
    overall_status: str = Field(..., description="'running' | 'pending_review' | 'duplicate_warning' | 'parsed_ready_for_chunking' | 'completed' | 'failed'")
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
