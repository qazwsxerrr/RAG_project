from datetime import datetime, timezone
from typing import List, Optional
from pydantic import BaseModel, Field

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
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), description="上传时间")

class DocumentItemSchema(BaseModel):
    """文档列表中的单项模型"""
    doc_id: str
    file_name: str
    department: str
    category: str
    tags: List[str]
    status: str
    step_index: int
    created_at: datetime
    updated_at: datetime

class DepartmentItemSchema(BaseModel):
    """部门信息"""
    id: int
    name: str
    path: str

class ChunkItemSchema(BaseModel):
    """切片明细模型 (供调试与前端实拍照展示)"""
    id: Optional[str] = None
    document_id: str
    chunk_index: int
    chunk_label: str
    content: str
    is_code: bool = False
    is_table: bool = False
    page_idx: int
    display_page: int
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

