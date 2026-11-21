"""src/rag_kb/services/mcp/schemas.py —— MCP 5 大核心工具协议边界常量、输出消毒与防间接提示注入辅助层。

核心职责：
1. 统一定义 MCP 工具的入参边界约束常量（批量向量上限、检索 top_k 上限，检索场景直接复用 SCENE_WEIGHT_PROFILES）；
2. 切片与引文输出消毒：
   - 物理剥离底层私有阿里云 OSS 直链 `raw_oss_url` 与内部向量 `dense_embedding`（对齐 SECURITY_MODEL.md 方向四）；
   - 扫描召回切片文本中潜在的间接提示注入指令（Indirect Prompt Injection），设置 `injection_warning: bool`；
   - 强制执行 PII 隐私数据正则脱敏掩码（手机号、身份证、邮箱、银行卡），不开放关闭开关。
"""

from typing import Any, Dict, Set
from rag_kb.core.security.prompt_guard import INJECTION_PATTERNS
from rag_kb.core.security.normalizer import normalize_text, clean_confusable_variant
from rag_kb.core.security.pii_masker import mask_all_pii
from rag_search.fusion import SCENE_WEIGHT_PROFILES

MCP_ALLOWED_SCENES: Set[str] = set(SCENE_WEIGHT_PROFILES.keys())
MCP_MAX_EMBED_BATCH: int = 32
MCP_MAX_SEARCH_TOP_K: int = 20
ALLOWED_INGEST_EXTENSIONS: Set[str] = {".pdf", ".docx", ".pptx", ".md", ".txt"}


def detect_indirect_prompt_injection(text: str) -> bool:
    """扫描知识库召回的文档切片文本是否包含试图劫持下游 Agent 的间接提示注入指令"""
    if not text or not text.strip():
        return False
    cleaned = text.strip()
    norm = normalize_text(cleaned)
    compact = clean_confusable_variant(cleaned)
    for candidate in (cleaned, norm, compact):
        for pattern in INJECTION_PATTERNS:
            if pattern.search(candidate):
                return True
    return False


def sanitize_mcp_chunk(chunk: Dict[str, Any]) -> Dict[str, Any]:
    """
    对检索召回的单个切片执行 MCP 出境安全消毒：
    1. 物理剥离 raw_oss_url 与 dense_embedding；
    2. 扫描标记 injection_warning；
    3. 强制执行 mask_all_pii 隐私脱敏。
    """
    raw_content = str(chunk.get("content") or "")
    has_injection = detect_indirect_prompt_injection(raw_content)
    safe_content = mask_all_pii(raw_content)

    page_idx = int(chunk.get("page_idx") or 0)
    chunk_id = str(chunk.get("chunk_id") or "")
    has_asset = bool(chunk.get("asset_url"))

    return {
        "chunk_id": chunk_id,
        "document_id": str(chunk.get("document_id") or ""),
        "document_title": str(chunk.get("document_title") or ""),
        "chunk_index": int(chunk.get("chunk_index") or 0),
        "chunk_label": str(chunk.get("chunk_label") or ""),
        "content": safe_content,
        "page_idx": page_idx,
        "display_page": page_idx + 1,
        "bbox": chunk.get("bbox") or [],
        "breadcrumb": chunk.get("breadcrumb") or [],
        "department_path": str(chunk.get("department_path") or ""),
        "asset_proxy_url": f"/api/v1/chunks/{chunk_id}/asset" if (chunk_id and has_asset) else None,
        "score": float(chunk.get("score") or 0.0),
        "injection_warning": has_injection,
    }


def sanitize_mcp_citation(cit: Dict[str, Any]) -> Dict[str, Any]:
    """
    对检索生成的引文溯源卡片执行 MCP 出境安全消毒：
    剥离 raw_oss_url 与内部 asset_url，仅保留受后端鉴权保护的相对代理路径，并强制执行 PII 脱敏。
    """
    raw_snippet = str(cit.get("snippet") or "")
    safe_snippet = mask_all_pii(raw_snippet)
    page_idx = int(cit.get("page_idx") or 0)
    display_page = int(cit.get("display_page") or (page_idx + 1))

    return {
        "citation_id": int(cit.get("citation_id") or 1),
        "chunk_id": str(cit.get("chunk_id") or ""),
        "document_id": str(cit.get("document_id") or ""),
        "document_title": str(cit.get("document_title") or ""),
        "chunk_label": str(cit.get("chunk_label") or ""),
        "page_idx": page_idx,
        "display_page": display_page,
        "bbox": cit.get("bbox") or [],
        "snippet": safe_snippet,
        "preview_url": cit.get("preview_url"),
        "page_render_url": cit.get("page_render_url"),
        "asset_proxy_url": cit.get("asset_proxy_url"),
        "is_pdf": bool(cit.get("is_pdf", False)),
        "score": float(cit.get("score") or 0.0),
    }
