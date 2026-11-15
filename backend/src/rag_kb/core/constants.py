"""src/rag_kb/core/constants.py —— 入库流水线的状态枚举 IngestStatus 与步骤字典。

定义文档入库各阶段（PENDING → … → STORED，另有 FAILED）的状态字符串，
作为入库图与对外 API 之间共享的状态字典，避免状态值在多处手写而拼错、不一致。
"""

from enum import Enum
from typing import List, Set


class Permissions(str, Enum):
    """全局 RBAC 权限点单一事实源（与 V1__rbac.sql 及 Java PermissionResolver 严格对齐）"""
    ALL = "*"
    KB_RETRIEVE = "kb:retrieve"
    DOC_UPLOAD = "doc:upload"
    DOC_DELETE = "doc:delete"
    DOC_REVIEW = "doc:review"
    SYSTEM_ADMIN = "system:admin"


KNOWN_PERMISSION_VALUES: Set[str] = {p.value for p in Permissions}

class IngestStatus(str, Enum):
    QUEUED = "queued"
    PENDING = "pending"
    INGEST = "ingest"
    MINERU = "mineru"
    LOADER = "loader"
    UPLOAD = "upload"
    ENRICH = "enrich"
    TABLE = "table"
    REVIEW = "pending_review"
    TABLE_APPLY = "table_apply"
    SPLITTER = "splitter"
    EMBEDDER = "embedder"
    STORED = "completed"
    COMPLETED = "completed"
    FAILED = "failed"
    DUPLICATE_WARNING = "duplicate_warning"

# 11 个标准执行节点名称
STEP_NAMES: List[str] = [
    "接入",            # 1
    "版面解析",        # 2
    "内容加载",        # 3
    "图片上传",        # 4
    "图片描述",        # 5
    "表格转文本",      # 6
    "人工审核",        # 7
    "表格回填",        # 8
    "切分",            # 9
    "向量化",          # 10
    "入库"             # 11
]
