"""src/rag_kb/core/authz/models.py —— 权限上下文模型与路径规范化工具。

策略判定单一事实源 (SSOT) 位于 sql_filters.py，本模块仅保存数据结构与路径工具，不再维护内存版重复判定逻辑。
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

from rag_kb.core.constants import Permissions
from rag_kb.db.models import KnowledgeBase


class EffectiveAuthzContext(BaseModel):
    """经 PostgreSQL 实时核验与策略字典重算后的当前用户有效权限上下文 (cv=2)"""

    user_id: str
    tenant_id: str = "default_tenant"
    username: str
    is_active: bool = True
    dept_path: str = ""
    dept_paths: List[str] = Field(default_factory=list)
    dept_ids: List[str] = Field(default_factory=list)
    roles: List[str] = Field(default_factory=list)
    idp_roles: List[str] = Field(default_factory=list)
    perms: List[str] = Field(default_factory=list)
    scope_by_action: Dict[str, str] = Field(default_factory=dict)
    position_code: str = "STAFF"
    effective_clearance: int = 2
    authz_ver: int = 1
    policy_epoch: int = 1
    is_to_c: bool = False
    cv: int = 2
    token_iat_ms: int = 0
    token_exp: int = 0
    delegated_agent_id: Optional[str] = None
    nearest_expiry_ts: Optional[float] = None

    def has_permission(self, permission: str) -> bool:
        perm_val = permission.value if isinstance(permission, Permissions) else str(permission)
        return Permissions.ALL.value in self.perms or perm_val in self.perms

    def scope_for_action(self, action: str) -> Optional[str]:
        return self.scope_by_action.get(str(action).upper().strip())

    def to_ticket_snapshot(self) -> Dict[str, Any]:
        return {
            "user_id": self.user_id,
            "tenant_id": self.tenant_id,
            "username": self.username,
            "idp_roles": list(self.idp_roles),
            "roles": list(self.roles),
            "authz_ver": self.authz_ver,
            "policy_epoch": self.policy_epoch,
            "cv": 2,
            "token_iat_ms": self.token_iat_ms,
            "token_exp": self.token_exp,
            "delegated_agent_id": self.delegated_agent_id,
        }


@dataclass
class AccessibleKnowledgeBase:
    kb: KnowledgeBase
    source_type: str  # PUBLIC | DEPARTMENT | SHARED_ACL
    can_read: bool
    can_upload: bool
    can_review: bool
    can_delete: bool
    can_manage: bool
    allowed_upload_levels: List[int]


def normalize_dept_path(raw_path: Optional[str]) -> str:
    if not raw_path:
        return ""
    trimmed = str(raw_path).strip()
    if not trimmed or trimmed == "/" or not trimmed.startswith("/"):
        return ""
    parts = [seg for seg in trimmed.split("/") if seg]
    return "/" + "/".join(parts) if parts else ""


def is_path_under_prefix(path: Optional[str], prefix: Optional[str]) -> bool:
    """按完整路径段校验 path 是否等于 prefix 或位于 prefix 子树下，杜绝 '/技术部' 误匹配 '/技术部2'"""
    norm_path = normalize_dept_path(path)
    norm_prefix = normalize_dept_path(prefix)
    if not norm_path or not norm_prefix:
        return False
    return norm_path == norm_prefix or norm_path.startswith(norm_prefix + "/")


def format_kb_full_path(dept_path: Optional[str], kb_name: Optional[str]) -> str:
    """构造「部门路径/知识库名称」逻辑归属全路径（如 '/总公司/研发中心/技术部/项目A'）"""
    norm_dept = normalize_dept_path(dept_path)
    clean_kb = str(kb_name or "").strip().strip("/")
    if norm_dept and clean_kb:
        return f"{norm_dept}/{clean_kb}"
    return norm_dept or clean_kb
