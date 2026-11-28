"""src/rag_kb/core/constants.py —— 全局权限点、四档组织范围、L1-L4 密级与入库状态枚举。

作为 Python 与 Java / V2__enterprise_multidim_authz.sql / V4__remove_dept_only_data_scope.sql 对齐的单一事实源：
1. Permissions: 11 个标准操作权限点；
2. DataScope: 3 档组织范围（CUSTOM_ACL / DEPT_AND_SUB / ALL_ORG）及优先级权重；
3. KbAction & ACTION_TO_PERMISSION: 五大资源动作与操作权限点的严格映射；
4. ClassificationLevel: L1-L4 密级枚举与文案；
5. IngestStatus & STEP_NAMES: 11 步入库流水线状态。
"""

from enum import Enum, IntEnum
from typing import Dict, List, Set


class Permissions(str, Enum):
    """全局 RBAC 权限点单一事实源（与 V1/V2 SQL 及 Java PermissionResolver 严格对齐）"""
    ALL = "*"
    KB_RETRIEVE = "kb:retrieve"
    DOC_UPLOAD = "doc:upload"
    DOC_DELETE = "doc:delete"
    DOC_REVIEW = "doc:review"
    SYSTEM_ADMIN = "system:admin"
    KB_MANAGE = "kb:manage"
    KB_GRANT = "kb:grant"
    SECURITY_CLASSIFY = "security:classify"
    SECURITY_DECLASSIFY = "security:declassify"


KNOWN_PERMISSION_VALUES: Set[str] = {p.value for p in Permissions}


class DataScope(str, Enum):
    """三档组织范围枚举（CUSTOM_ACL / DEPT_AND_SUB / ALL_ORG）"""
    CUSTOM_ACL = "CUSTOM_ACL"
    DEPT_AND_SUB = "DEPT_AND_SUB"
    ALL_ORG = "ALL_ORG"


DATA_SCOPE_WEIGHTS: Dict[str, int] = {
    DataScope.CUSTOM_ACL.value: 20,
    DataScope.DEPT_AND_SUB.value: 40,
    DataScope.ALL_ORG.value: 50,
}

VALID_DATA_SCOPES: Set[str] = set(DATA_SCOPE_WEIGHTS.keys())


class KbAction(str, Enum):
    """知识库与文档资源动作枚举"""
    READ = "READ"
    UPLOAD = "UPLOAD"
    REVIEW = "REVIEW"
    DELETE = "DELETE"
    MANAGE = "MANAGE"


ACTION_TO_PERMISSION: Dict[str, str] = {
    KbAction.READ.value: Permissions.KB_RETRIEVE.value,
    KbAction.UPLOAD.value: Permissions.DOC_UPLOAD.value,
    KbAction.REVIEW.value: Permissions.DOC_REVIEW.value,
    KbAction.DELETE.value: Permissions.DOC_DELETE.value,
    KbAction.MANAGE.value: Permissions.KB_MANAGE.value,
}


class ClassificationLevel(IntEnum):
    """企业文档与知识库 L1-L4 密级"""
    L1_PUBLIC = 1
    L2_DEPARTMENT = 2
    L3_CONFIDENTIAL = 3
    L4_TOP_SECRET = 4


CLASSIFICATION_LABELS: Dict[int, str] = {
    1: "L1 全员公开",
    2: "L2 部门内部",
    3: "L3 管理层机密",
    4: "L4 高层核心机密",
}


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
