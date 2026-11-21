"""src/rag_kb/services/mcp/__init__.py —— 企业级公共能力 FastMCP 协议封装包导出门面。"""

from rag_kb.services.mcp.auth_guard import (
    MCPCallerContext,
    enforce_on_behalf_of,
    extract_mcp_credentials,
    get_current_mcp_caller,
    load_mcp_agent_policies,
    mcp_secure_tool,
)
from rag_kb.services.mcp.server import (
    build_core_document_status_snapshot,
    execute_core_document_ingest_from_temp_file,
    execute_core_embeddings_extraction,
    execute_core_hybrid_retrieval,
    get_mcp_http_app,
    mcp_server,
    rag_document_ingest,
    rag_document_status,
    rag_get_embeddings,
    rag_hybrid_search,
    rag_resilient_llm,
    resolve_authorized_department_path,
    resolve_or_create_department,
)

__all__ = [
    "MCPCallerContext",
    "mcp_secure_tool",
    "get_current_mcp_caller",
    "extract_mcp_credentials",
    "enforce_on_behalf_of",
    "load_mcp_agent_policies",
    "mcp_server",
    "get_mcp_http_app",
    "rag_resilient_llm",
    "rag_hybrid_search",
    "rag_get_embeddings",
    "rag_document_ingest",
    "rag_document_status",
    "resolve_or_create_department",
    "resolve_authorized_department_path",
    "execute_core_embeddings_extraction",
    "execute_core_hybrid_retrieval",
    "execute_core_document_ingest_from_temp_file",
    "build_core_document_status_snapshot",
]
