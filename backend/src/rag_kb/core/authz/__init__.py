"""src/rag_kb/core/authz/__init__.py —— Python 单一鉴权策略决策中心 (PDP) 导出入口。"""

from rag_kb.core.authz.context_loader import (
    get_db_version_and_expiry_error,
    invalidate_authz_cache,
    load_effective_authz_context,
)
from rag_kb.core.authz.guards import (
    assert_doc_access,
    assert_kb_access,
    assert_pipeline_state_access,
    audit_high_classification_read,
)
from rag_kb.core.authz.kb_resolver import (
    list_accessible_kbs,
    resolve_authorized_target_kb,
    validate_doc_classification_on_write,
    validate_requested_kb_ids,
)
from rag_kb.core.authz.models import (
    AccessibleKnowledgeBase,
    EffectiveAuthzContext,
    is_path_under_prefix,
    normalize_dept_path,
)
from rag_kb.core.authz.sql_filters import (
    apply_document_authz_join_and_filter,
    build_document_access_condition,
    build_kb_access_condition,
)

__all__ = [
    "EffectiveAuthzContext",
    "AccessibleKnowledgeBase",
    "normalize_dept_path",
    "is_path_under_prefix",
    "invalidate_authz_cache",
    "load_effective_authz_context",
    "get_db_version_and_expiry_error",
    "list_accessible_kbs",
    "assert_kb_access",
    "assert_doc_access",
    "assert_pipeline_state_access",
    "validate_doc_classification_on_write",
    "resolve_authorized_target_kb",
    "validate_requested_kb_ids",
    "audit_high_classification_read",
    "build_kb_access_condition",
    "build_document_access_condition",
    "apply_document_authz_join_and_filter",
]
