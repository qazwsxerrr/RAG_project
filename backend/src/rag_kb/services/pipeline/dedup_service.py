"""src/rag_kb/services/pipeline/dedup_service.py —— 按 (tenant_id, kb_id) 防重检测与高密侧信道脱敏。

安全红线（方案 §3.3.4 & §4.3.5）：
1. 严格限定在同 (tenant_id, kb_id) 知识库内比对标题向量；
2. 若命中存量文档的生效密级高于上传者有效访问密级 (uploader_clearance)，
   必须脱敏隐藏其真实标题（返回 '<受限高密文档>'），且置 can_overwrite=False，禁止低密级用户覆盖高密级文档。
"""

import logging
import uuid
from typing import Dict, Any, Optional, Tuple
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from rag_kb.core.authz.guards import assert_doc_access
from rag_kb.core.authz.models import EffectiveAuthzContext
from rag_kb.core.constants import KbAction
from rag_kb.db.models import Document
from rag_kb.utils.embedding import embedding_service

logger = logging.getLogger(__name__)

REDACTED_HIGH_CLASSIFICATION_TITLE = "<受限高密文档>"


class DeduplicationService:
    """文档标题特征向量防重检测服务 (节点 11 前置判定)"""

    SIMILARITY_THRESHOLD = 0.88

    def extract_top_headings(self, markdown_text: str) -> Tuple[str, str]:
        h1, h2 = "", ""
        for line in markdown_text.splitlines():
            line_s = line.strip()
            if not h1 and line_s.startswith("# ") and not line_s.startswith("## "):
                h1 = line_s[2:].strip()
            elif not h2 and line_s.startswith("## ") and not line_s.startswith("### "):
                h2 = line_s[3:].strip()
            if h1 and h2:
                break
        return h1, h2

    async def check_duplicate(
        self,
        session: AsyncSession,
        doc_id: str,
        file_name: str,
        markdown_text: str = "",
        h1: str = "",
        h2: str = "",
        tenant_id: Optional[str] = None,
        kb_id: Optional[str] = None,
        department_path: Optional[str] = None,
        uploader_clearance: Optional[int] = None,
    ) -> Dict[str, Any]:
        """按 (tenant_id, kb_id) 执行标题向量防重，对高密级存量文档执行标题脱敏与覆盖禁令"""
        if not h1 and not h2 and markdown_text:
            h1, h2 = self.extract_top_headings(markdown_text)

        title_vec = await embedding_service.embed_title(file_name, h1, h2)
        title_vec_str = f"[{','.join(str(x) for x in title_vec)}]"

        where_clauses = [
            "d.title_embedding IS NOT NULL",
            "d.id != CAST(:doc_id AS uuid)",
            "d.overall_status IN ('active', 'completed', 'indexing', 'parsed_ready_for_chunking', 'awaiting_review')",
        ]
        params: Dict[str, Any] = {"vec": title_vec_str, "doc_id": str(doc_id)}

        if tenant_id:
            where_clauses.append("d.tenant_id = :tenant_id")
            params["tenant_id"] = tenant_id
        if not kb_id:
            raise ValueError("防重检测必须提供明确的知识库 ID (kb_id)")
        where_clauses.append("d.kb_id = CAST(:kb_id AS uuid)")
        params["kb_id"] = str(kb_id)

        query = text(f"""
            SELECT d.id,
                   d.title,
                   d.file_hash,
                   d.version,
                   d.overall_status,
                   GREATEST(kb.min_classification_level, d.classification_level) AS effective_level,
                   1 - (d.title_embedding <=> CAST(:vec AS vector)) AS similarity
            FROM rag_documents d
            JOIN knowledge_bases kb
              ON kb.id = d.kb_id AND kb.tenant_id = d.tenant_id
            WHERE {' AND '.join(where_clauses)}
            ORDER BY d.title_embedding <=> CAST(:vec AS vector)
            LIMIT 1;
        """)

        result = await session.execute(query, params)
        row = result.fetchone()

        if row and row.similarity is not None:
            sim = float(row.similarity)
            matched_id = str(row.id)
            real_matched_title = row.title
            matched_version = int(row.version or 1)
            matched_eff_level = int(row.effective_level or 2)

            # 侧信道防护（Fail-Closed）：若无法核实上传者密级或存量文档生效密级高于上传者密级，
            # 严禁回显真实标题，且禁止 overwrite
            is_higher_classification = (
                uploader_clearance is None or matched_eff_level > int(uploader_clearance)
            )
            exposed_title = REDACTED_HIGH_CLASSIFICATION_TITLE if is_higher_classification else real_matched_title
            can_overwrite = not is_higher_classification

            logger.info(
                f"【防重比对】doc_id={doc_id} 最高匹配相似度={sim:.4f}, "
                f"matched_id={matched_id}, redacted={is_higher_classification}"
            )

            if sim >= self.SIMILARITY_THRESHOLD:
                if is_higher_classification:
                    msg = (
                        f"检测到知识库内存在高度相似的受限高密文档 (相似度 {sim:.2%})，"
                        "您当前密级不可查看其标题且禁止覆盖更新，仅允许作为独立新文档入库或取消。"
                    )
                else:
                    msg = f"检测到高度相似文档《{exposed_title}》(相似度 {sim:.2%})，请选择是覆盖更新还是作为独立新文档入库"

                return {
                    "is_duplicate": True,
                    "similarity": round(sim, 4),
                    "matched_doc_id": matched_id,
                    "matched_title": exposed_title,
                    "matched_version": matched_version,
                    "matched_effective_level": matched_eff_level,
                    "is_higher_classification": is_higher_classification,
                    "can_overwrite": can_overwrite,
                    "title_embedding": title_vec,
                    "message": msg,
                }

            return {
                "is_duplicate": False,
                "similarity": round(sim, 4),
                "matched_doc_id": matched_id,
                "matched_title": exposed_title,
                "matched_version": matched_version,
                "matched_effective_level": matched_eff_level,
                "is_higher_classification": is_higher_classification,
                "can_overwrite": can_overwrite,
                "title_embedding": title_vec,
                "message": "文档特征未命中重复预警，可直接入库",
            }

        return {
            "is_duplicate": False,
            "similarity": 0.0,
            "matched_doc_id": None,
            "matched_title": None,
            "matched_version": 0,
            "matched_effective_level": None,
            "is_higher_classification": False,
            "can_overwrite": True,
            "title_embedding": title_vec,
            "message": "知识库暂无存量文档或特征未命中",
        }


async def render_duplicate_warning_for_viewer(
    db: AsyncSession,
    viewer_ctx: EffectiveAuthzContext,
    event_data: Dict[str, Any],
) -> Dict[str, Any]:
    """按 SSE 接收者对命中文档的实时读取权限渲染 duplicate_warning 载荷，杜绝共享事件泄露高密真实标题"""
    rendered = dict(event_data or {})
    sim = float(rendered.get("similarity") or 0.0)
    raw_matched_id = rendered.get("matched_doc_id")

    # 默认 Fail-Closed 脱敏
    rendered["matched_title"] = REDACTED_HIGH_CLASSIFICATION_TITLE
    rendered["is_higher_classification"] = True
    rendered["can_overwrite"] = False
    rendered["message"] = (
        f"检测到知识库内存在高度相似的受限高密文档 (相似度 {sim:.2%})，"
        "您当前密级不可查看其标题且禁止覆盖更新，仅允许作为独立新文档入库或取消。"
    )

    if not raw_matched_id:
        return rendered

    try:
        matched_uuid = uuid.UUID(str(raw_matched_id))
    except ValueError:
        return rendered

    matched_doc = await db.get(Document, matched_uuid)
    if matched_doc is None:
        return rendered

    try:
        _, eff_level = await assert_doc_access(
            db, viewer_ctx, matched_doc, KbAction.READ.value, audit_denial=False
        )
    except HTTPException:
        return rendered

    can_overwrite = int(viewer_ctx.effective_clearance) >= int(eff_level)
    rendered["matched_title"] = matched_doc.title
    rendered["is_higher_classification"] = not can_overwrite
    rendered["can_overwrite"] = can_overwrite
    rendered["message"] = (
        f"检测到高度相似文档《{matched_doc.title}》(相似度 {sim:.2%})，请选择是覆盖更新还是作为独立新文档入库"
    )
    return rendered


dedup_service = DeduplicationService()
