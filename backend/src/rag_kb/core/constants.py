"""src/rag_kb/core/constants.py —— 入库流水线的状态枚举 IngestStatus 与步骤字典。

定义文档入库各阶段（PENDING → … → STORED，另有 FAILED）的状态字符串，
作为入库图与对外 API 之间共享的状态字典，避免状态值在多处手写而拼错、不一致。
"""

from enum import Enum
from typing import List

class IngestStatus(str, Enum):
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
    "MinerU 解析",     # 2
    "MD 加载",         # 3
    "图片上传",        # 4
    "图片描述",        # 5
    "表格转文本",      # 6
    "人工审核",        # 7
    "表格回填",        # 8
    "切分",            # 9
    "向量化",          # 10
    "入库"             # 11
]
