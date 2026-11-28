"""backend/tests/test_step13_multidim_kb_authz.py —— 企业级知识库多维权限端到端全量验收测试集。

严格覆盖《RAG_project_企业知识库多维权限改造实施方案_v2.md》§4.3 全部 6 项强制验收准则，
全部直连真实 PostgreSQL 16 + pgvector、真实 Redis、真实 Aliyun OSS 与真实入库的 9 篇 L1~L4 业务语料：
1. 真实样本入库完整性核验（8 篇 Markdown + 1 篇 PDF，1024 维稠密 + 稀疏 + TSVector + OSS 原件，零污染 default_tenant）；
2. L1~L4 密级继承与单篇文档密级隔离（4 路召回 + 流式检索 + MCP，阳性对照 + 阴性零泄露）；
3. 多角色动作分桶合并与多部门双来源（IDP + MANUAL 对称移除/去重，ALL_ORG READ 不扩散至 DELETE）；
4. 职级密级动态生效与单篇 PDF 在线独立改密（实时失效旧 Range 票据、穿透 PDF 进程缓存拦截 /pages 与 /chunks/{id}/asset）；
5. 精细化 ACL（ALLOW/DENY/自然到期无版本号变更）、对象失配、跨租户隔离、Redis 失败 DB 兜底、会话历史重验、去重侧信道防泄密、MCP 与全链路零 raw_oss_url；
6. 权限治理职责分离（sys_ops_admin 无法越级读 L3/L4 或脱密）与强审计阻断（审计失败回滚改密并阻断 L3/L4 敏感读出）。
"""

from __future__ import annotations

import asyncio
import base64
import json
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from sqlalchemy import delete, func, select, text

from conftest import make_rs256_test_token
from rag_kb.api.deps import (
    STREAM_TICKET_KEY_PREFIX,
    get_current_user,
    issue_stream_ticket,
    verify_stream_ticket,
)
from rag_kb.api.admin import router as admin_router
from rag_kb.api.knowledge_bases import router as kb_router
from rag_kb.api.retrieval import router as retrieval_router
from rag_kb.api.routes import router as documents_router, router_chunks as chunks_router
from rag_kb.api.sessions import router as sessions_router
from rag_kb.core.authz import (
    invalidate_authz_cache as clear_authz_cache,
    load_effective_authz_context,
    resolve_authorized_target_kb,
)
from rag_kb.core.config import settings
from rag_kb.core.logging.audit import AuditWriteError
from rag_kb.core.redis_client import get_redis_client
from rag_kb.db.chat_store import append_message, build_authorized_history
from rag_kb.db.models import (
    AuditLog,
    ChatMessage,
    ChatSession,
    Department,
    Document,
    DocumentChunk,
    KbGrant,
    KnowledgeBase,
    Position,
    Role,
    SysUser,
    TenantPolicyEpoch,
    UserDepartment,
)
from rag_kb.db.session import AsyncSessionLocal
from contextlib import contextmanager
from fastmcp.server.http import _current_http_request
from starlette.requests import Request
from rag_kb.services.mcp import (
    rag_document_status,
    rag_hybrid_search,
)
from rag_kb.services.pipeline.dedup_service import DeduplicationService, REDACTED_HIGH_CLASSIFICATION_TITLE
from rag_kb.utils import tokenize
from rag_kb.utils.embedding import embedding_service
from rag_kb.utils.oss import oss_service
from rag_search.channels import bm25 as bm25_channel
from rag_search.channels import dense as dense_channel
from rag_search.channels import sparse as sparse_channel


@contextmanager
def _mcp_http_auth_context(token: str):
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "headers": [(b"authorization", f"Bearer {token}".encode("utf-8"))],
    }
    tok = _current_http_request.set(Request(scope))
    try:
        yield
    finally:
        _current_http_request.reset(tok)

pytestmark = pytest.mark.usefixtures("local_rsa_jwt_env")

MANIFEST_PATH = Path(__file__).resolve().parent / "fixtures" / "authz_verify_manifest.json"
TINY_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _load_manifest() -> Dict[str, Any]:
    assert MANIFEST_PATH.is_file(), (
        f"缺少真实入库清单 {MANIFEST_PATH}，请先运行 python scripts/seed_authz_kb_documents.py"
    )
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


async def _mint_token_for_user(
    manifest: Dict[str, Any],
    user_key: str,
    extra_roles: Optional[List[str]] = None,
    roles_override: Optional[List[str]] = None,
) -> str:
    """根据数据库当前真实版本号、策略纪元与企业 IdP 角色样例为指定测试账号签发 cv=1 RS256 令牌"""
    tenant_id = manifest["tenant_id"]
    u_meta = manifest["users"][user_key]
    uid = uuid.UUID(u_meta["id"])
    base_roles = list(roles_override) if roles_override is not None else list(u_meta.get("roles") or [])
    roles = list(dict.fromkeys(base_roles + list(extra_roles or [])))

    async with AsyncSessionLocal() as db:
        u = await db.get(SysUser, uid)
        assert u is not None, f"验证账号 {user_key} 不存在"
        epoch = await db.get(TenantPolicyEpoch, tenant_id)
        policy_epoch = int(epoch.policy_epoch) if epoch else 1

        ud_rows = (
            await db.execute(
                select(UserDepartment.dept_path, UserDepartment.is_primary)
                .where(
                    UserDepartment.tenant_id == tenant_id,
                    UserDepartment.user_id == uid,
                )
                .order_by(UserDepartment.is_primary.desc(), UserDepartment.created_at.asc())
            )
        ).all()
        primary_dept = next((p for p, is_prim in ud_rows if is_prim), "")
        dept_paths_list = list(dict.fromkeys([p for p, _ in ud_rows if p]))

        pos = await db.get(Position, (tenant_id, u.position_code))
        eff_level = int(pos.default_clearance_level)

        return make_rs256_test_token(
            user_id=str(uid),
            tenant_id=tenant_id,
            username=u.real_name,
            dept_path=primary_dept,
            dept_paths=dept_paths_list,
            roles=roles,
            perms=["kb:retrieve"],
            position_code=u.position_code,
            clearance_level=eff_level,
            authz_ver=int(u.authz_version),
            policy_epoch=policy_epoch,
            cv=1,
        )


def _build_test_app() -> FastAPI:
    app = FastAPI()
    app.include_router(admin_router, prefix="/api/v1")
    app.include_router(kb_router, prefix="/api/v1")
    app.include_router(documents_router, prefix="/api/v1")
    app.include_router(chunks_router, prefix="/api/v1")
    app.include_router(retrieval_router, prefix="/api/v1")
    app.include_router(sessions_router, prefix="/api/v1")
    return app


async def _run_all_channels_search(
    ctx,
    query: str,
    kb_ids: Optional[List[uuid.UUID]] = None,
) -> Dict[str, List[Dict[str, Any]]]:
    """同时在真实 PostgreSQL 16 上执行稠密/稀疏/BM25 三路直查，验证每一路 SQL 前置权限过滤无漏网"""
    q_vec = (await embedding_service.embed_texts([query]))[0]
    q_sparse = tokenize.extract_sparse_vector(query)
    q_ts = tokenize.cut_for_index(query)
    async with AsyncSessionLocal() as db:
        dense_hits = await dense_channel.search(
            session=db,
            query_dense=q_vec,
            authz_filter_spec=ctx,
            kb_ids=kb_ids,
            top_k=20,
        )
        sparse_hits = await sparse_channel.search(
            session=db,
            query_sparse=q_sparse,
            authz_filter_spec=ctx,
            kb_ids=kb_ids,
            top_k=20,
        )
        bm25_hits = await bm25_channel.search(
            session=db,
            tsquery_tokens=q_ts,
            authz_filter_spec=ctx,
            kb_ids=kb_ids,
            top_k=20,
        )
    return {
        "dense": dense_hits,
        "sparse": sparse_hits,
        "bm25": bm25_hits,
    }


def _collect_doc_ids(channel_results: Dict[str, List[Dict[str, Any]]]) -> set[str]:
    found: set[str] = set()
    for hits in channel_results.values():
        for h in hits:
            found.add(str(h.get("doc_id") or h.get("document_id")))
    return found


def _collect_contents(channel_results: Dict[str, List[Dict[str, Any]]]) -> str:
    parts: List[str] = []
    for hits in channel_results.values():
        for h in hits:
            parts.append(str(h.get("content") or ""))
    return "\n".join(parts)


# ==============================================================================
# 验收准则 1：真实样本入库完整性与隔离性核验
# ==============================================================================


@pytest.mark.asyncio
async def test_01_real_corpus_ingestion_integrity():
    """准则 1: 8 篇 L1~L4 Markdown + 1 篇 PDF 真实入库，1024 维稠密/稀疏/TSVector 与 OSS 完整，且不污染 default_tenant"""
    manifest = _load_manifest()
    tenant_id = manifest["tenant_id"]
    docs = manifest["documents"]
    assert len(docs) == 9

    async with AsyncSessionLocal() as db:
        for key, info in docs.items():
            doc = await db.get(Document, uuid.UUID(info["doc_id"]))
            assert doc is not None, f"文档 {key} 不存在"
            assert doc.tenant_id == tenant_id
            assert str(doc.kb_id) == info["kb_id"]
            assert doc.overall_status == "active"
            assert doc.is_active is True
            assert doc.raw_oss_url and not doc.raw_oss_url.startswith("/static/")

            stats = (
                await db.execute(
                    text(
                        """
                        SELECT
                            COUNT(*)::int AS total,
                            COUNT(*) FILTER (WHERE dense_embedding IS NOT NULL AND vector_dims(dense_embedding) = 1024)::int AS d1024,
                            COUNT(*) FILTER (WHERE sparse_vector IS NOT NULL)::int AS sp,
                            COUNT(*) FILTER (WHERE tsv_content IS NOT NULL)::int AS fts
                        FROM rag_document_chunks
                        WHERE document_id = :doc_id
                        """
                    ),
                    {"doc_id": info["doc_id"]},
                )
            ).mappings().one()
            assert stats["total"] > 0
            assert stats["d1024"] == stats["total"]
            assert stats["sp"] == stats["total"]
            assert stats["fts"] == stats["total"]

        # 确认未污染 default_tenant 原有角色
        emp_role = await db.get(Role, ("default_tenant", "employee"))
        assert emp_role is not None
        assert emp_role.data_scope == "DEPT_AND_SUB"


# ==============================================================================
# 验收准则 2：L1~L4 密级继承与单篇文档密级隔离（4 路召回 + 流式接口 + MCP）
# ==============================================================================


@pytest.mark.asyncio
async def test_02_l1_to_l4_classification_and_kb_isolation():
    """准则 2: 阳性对照 + 阴性零泄露，验证 intern/tech_staff/tech_manager/corp_exec 四层密级与 RESTRICTED 专库隔离"""
    manifest = _load_manifest()
    docs = manifest["documents"]
    kbs = manifest["knowledge_bases"]
    clear_authz_cache()

    intern_ctx = await get_current_user(authorization=f"Bearer {await _mint_token_for_user(manifest, 'intern_user')}")
    staff_ctx = await get_current_user(authorization=f"Bearer {await _mint_token_for_user(manifest, 'tech_staff')}")
    mgr_ctx = await get_current_user(authorization=f"Bearer {await _mint_token_for_user(manifest, 'tech_manager')}")
    exec_ctx = await get_current_user(authorization=f"Bearer {await _mint_token_for_user(manifest, 'corp_exec')}")

    # 2.1 阳性对照：高管 (L4) 能真实召回 L4 绝密文档（Project-Titan & Nebula-AI）与 L3 预算/合同
    res_exec_titan = await _run_all_channels_search(exec_ctx, "Project-Titan 底层引擎源代码评估 收购技术最高限价")
    assert docs["l4_tech_titan"]["doc_id"] in _collect_doc_ids(res_exec_titan)
    assert "Project-Titan" in _collect_contents(res_exec_titan)

    res_exec_nebula = await _run_all_channels_search(exec_ctx, "Nebula-AI 海外并购估值 3.6亿美元 对赌条款")
    assert docs["l4_board_strategy"]["doc_id"] in _collect_doc_ids(res_exec_nebula)
    assert "Nebula-AI" in _collect_contents(res_exec_nebula)

    # 2.2 intern_user (L1): 仅命中 L1 员工手册，严禁命中任何 L2/L3/L4
    res_intern_l1 = await _run_all_channels_search(intern_ctx, "弹性打卡窗口 09:30 一线城市差旅住宿标准 650元")
    assert docs["l1_handbook"]["doc_id"] in _collect_doc_ids(res_intern_l1)
    assert "650元" in _collect_contents(res_intern_l1)

    for forbidden_q, forbidden_key in [
        ("核心网关限流阈值 生产灰度发布窗口 每周二/四 22:00", "l2_tech_spec"),
        ("报销凭证自动入账回调签名算法 HMAC-SHA256", "l2_fin_api"),
        ("H800 算力集群采购预算 1850万元", "l3_tech_budget"),
        ("Project-Titan 收购技术最高限价", "l4_tech_titan"),
    ]:
        res = await _run_all_channels_search(intern_ctx, forbidden_q)
        assert docs[forbidden_key]["doc_id"] not in _collect_doc_ids(res)

    # 2.3 tech_staff (L2):
    # - 不传 kb_ids：命中 L1 公开 + L2 技术部 + L2 共享库，严禁命中 L3/L4/L2 人事
    res_staff_l2 = await _run_all_channels_search(staff_ctx, "生产灰度发布窗口 每周二/四 22:00")
    assert docs["l2_tech_spec"]["doc_id"] in _collect_doc_ids(res_staff_l2)

    res_staff_shared = await _run_all_channels_search(staff_ctx, "报销凭证自动入账回调签名算法 HMAC-SHA256")
    assert docs["l2_fin_api"]["doc_id"] in _collect_doc_ids(res_staff_shared)

    for forbidden_q, forbidden_key, forbidden_fact in [
        ("H800 算力集群采购预算 1850万元", "l3_tech_budget", "1850万元"),
        ("ERP 外部实施商合同总价 420万元", "l3_fin_contract", "420万元"),
        ("Project-Titan 收购技术最高限价", "l4_tech_titan", "Project-Titan"),
        ("人事入职交接批次 HR-ONB-27", "l2_hr_onboard", "HR-ONB-27"),
        ("Nebula-AI 海外并购估值 3.6亿美元", "l4_board_strategy", "Nebula-AI"),
    ]:
        res = await _run_all_channels_search(staff_ctx, forbidden_q)
        assert docs[forbidden_key]["doc_id"] not in _collect_doc_ids(res)
        assert forbidden_fact not in _collect_contents(res)

    # - 显式收窄 kb_ids=[tech_kb]：排除业财共享库
    res_staff_only_tech = await _run_all_channels_search(
        staff_ctx,
        "报销凭证自动入账回调签名算法",
        kb_ids=[uuid.UUID(kbs["tech_kb"]["id"])],
    )
    assert docs["l2_fin_api"]["doc_id"] not in _collect_doc_ids(res_staff_only_tech)

    # - 显式收窄 kb_ids=[fin_tech_shared_kb]：命中 L2 接口文档，严禁命中同库 L3 合同
    res_staff_only_shared = await _run_all_channels_search(
        staff_ctx,
        "报销凭证回调签名算法 ERP 外部实施商合同总价 420万元",
        kb_ids=[uuid.UUID(kbs["fin_tech_shared_kb"]["id"])],
    )
    assert docs["l2_fin_api"]["doc_id"] in _collect_doc_ids(res_staff_only_shared)
    assert docs["l3_fin_contract"]["doc_id"] not in _collect_doc_ids(res_staff_only_shared)

    # - 显式传未授权 kb_ids=[hr_kb] / [board_kb] -> HTTP 403
    test_app = _build_test_app()
    staff_tok = await _mint_token_for_user(manifest, "tech_staff")
    transport = httpx.ASGITransport(app=test_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        for unauth_kb_key in ("hr_kb", "board_kb"):
            r = await client.get(
                "/api/v1/documents",
                params={"kb_id": kbs[unauth_kb_key]["id"]},
                headers={"Authorization": f"Bearer {staff_tok}"},
            )
            assert r.status_code == 403

    # 2.4 tech_manager (L3): 命中 L3 技术部预算 (1850万元) 与 L3 业财合同 (420万元)，严禁命中 L4/HR/Board
    res_mgr_budget = await _run_all_channels_search(mgr_ctx, "H800 算力集群采购预算 1850万元")
    assert docs["l3_tech_budget"]["doc_id"] in _collect_doc_ids(res_mgr_budget)

    res_mgr_contract = await _run_all_channels_search(mgr_ctx, "ERP 外部实施商合同总价 420万元")
    assert docs["l3_fin_contract"]["doc_id"] in _collect_doc_ids(res_mgr_contract)

    for forbidden_q, forbidden_key in [
        ("Project-Titan 底层引擎源代码评估", "l4_tech_titan"),
        ("人事入职交接批次 HR-ONB-27", "l2_hr_onboard"),
        ("Nebula-AI 海外并购估值", "l4_board_strategy"),
    ]:
        res = await _run_all_channels_search(mgr_ctx, forbidden_q)
        assert docs[forbidden_key]["doc_id"] not in _collect_doc_ids(res)

    # 2.5 corp_exec (L4) 移除董事会专库显式 ACL -> 立即不可见 Nebula-AI，即便具备 L4 + ALL_ORG
    board_kb_uuid = uuid.UUID(kbs["board_kb"]["id"])
    exec_uid = manifest["users"]["corp_exec"]["id"]
    saved_grants: List[Dict[str, Any]] = []
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(KbGrant).where(
                    KbGrant.tenant_id == manifest["tenant_id"],
                    KbGrant.kb_id == board_kb_uuid,
                    KbGrant.subject_type == "USER",
                    KbGrant.subject_id == exec_uid,
                )
            )
        ).scalars().all()
        for g in rows:
            saved_grants.append(
                {
                    "id": g.id,
                    "action": g.action,
                    "effect": g.effect,
                    "reason": g.reason,
                    "granted_by": g.granted_by,
                }
            )
            await db.delete(g)
        await db.commit()

    clear_authz_cache()
    try:
        res_exec_after = await _run_all_channels_search(exec_ctx, "Nebula-AI 海外并购估值 3.6亿美元")
        assert docs["l4_board_strategy"]["doc_id"] not in _collect_doc_ids(res_exec_after)
        assert "Nebula-AI" not in _collect_contents(res_exec_after)
    finally:
        async with AsyncSessionLocal() as db:
            for sg in saved_grants:
                db.add(
                    KbGrant(
                        id=sg["id"],
                        tenant_id=manifest["tenant_id"],
                        kb_id=board_kb_uuid,
                        subject_type="USER",
                        subject_id=exec_uid,
                        include_sub_depts=False,
                        action=sg["action"],
                        effect=sg["effect"],
                        reason=sg["reason"],
                        granted_by=sg["granted_by"],
                    )
                )
            await db.commit()
        clear_authz_cache()


# ==============================================================================
# 验收准则 3：多角色动作分桶合并与多部门双来源（IDP + MANUAL）
# ==============================================================================


@pytest.mark.asyncio
async def test_03_multi_role_action_bucket_and_multi_dept_dual_source():
    """准则 3: multi_dept_staff 双来源部门对称性与去重；ALL_ORG READ 与 DEPT_AND_SUB DELETE 动作分桶防扩散"""
    manifest = _load_manifest()
    tenant_id = manifest["tenant_id"]
    docs = manifest["documents"]
    kbs = manifest["knowledge_bases"]
    depts = manifest["departments"]
    clear_authz_cache()

    # 3.1 multi_dept_staff 基线：同时命中技术部 L2 与人事行政部 L2
    md_ctx = await get_current_user(authorization=f"Bearer {await _mint_token_for_user(manifest, 'multi_dept_staff')}")
    assert "/公司/技术部" in md_ctx.dept_paths
    assert "/公司/人事行政部" in md_ctx.dept_paths

    res_md_tech = await _run_all_channels_search(md_ctx, "生产灰度发布窗口 每周二/四 22:00")
    assert docs["l2_tech_spec"]["doc_id"] in _collect_doc_ids(res_md_tech)
    res_md_hr = await _run_all_channels_search(md_ctx, "人事入职交接批次 HR-ONB-27")
    assert docs["l2_hr_onboard"]["doc_id"] in _collect_doc_ids(res_md_hr)

    # 3.2 企业 IdP 多部门同步撤销：移除人事部同步记录后立即可见性归零，技术部仍可读
    md_uid = uuid.UUID(manifest["users"]["multi_dept_staff"]["id"])
    hr_dept_id = uuid.UUID(depts["hr"]["id"])
    try:
        async with AsyncSessionLocal() as db:
            await db.execute(
                delete(UserDepartment).where(
                    UserDepartment.tenant_id == tenant_id,
                    UserDepartment.user_id == md_uid,
                    UserDepartment.department_id == hr_dept_id,
                )
            )
            await db.commit()
        clear_authz_cache()

        md_ctx_none = await get_current_user(
            authorization=f"Bearer {await _mint_token_for_user(manifest, 'multi_dept_staff')}"
        )
        assert "/公司/人事行政部" not in md_ctx_none.dept_paths
        assert docs["l2_hr_onboard"]["doc_id"] not in _collect_doc_ids(
            await _run_all_channels_search(md_ctx_none, "人事入职交接批次 HR-ONB-27")
        )
        assert docs["l2_tech_spec"]["doc_id"] in _collect_doc_ids(
            await _run_all_channels_search(md_ctx_none, "生产灰度发布窗口 每周二/四 22:00")
        )
    finally:
        async with AsyncSessionLocal() as db:
            await db.execute(
                delete(UserDepartment).where(
                    UserDepartment.tenant_id == tenant_id,
                    UserDepartment.user_id == md_uid,
                    UserDepartment.department_id == hr_dept_id,
                )
            )
            db.add(
                UserDepartment(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    user_id=md_uid,
                    department_id=hr_dept_id,
                    dept_path="/公司/人事行政部",
                    is_primary=False,
                )
            )
            await db.commit()
        clear_authz_cache()

    # 3.3 多角色动作分桶：模拟企业 IdP 为 tech_staff 同时下发 test_developer + test_org_reader -> 可读 HR 文档，但 DELETE HR 必须 403！
    test_app = _build_test_app()
    staff_tok = await _mint_token_for_user(manifest, "tech_staff", extra_roles=["test_org_reader"])
    staff_ctx_merged = await get_current_user(authorization=f"Bearer {staff_tok}")
    assert staff_ctx_merged.scope_by_action.get("READ") == "ALL_ORG"
    assert staff_ctx_merged.scope_by_action.get("DELETE") == "DEPT_AND_SUB"

    res_hr = await _run_all_channels_search(staff_ctx_merged, "人事入职交接批次 HR-ONB-27")
    assert docs["l2_hr_onboard"]["doc_id"] in _collect_doc_ids(res_hr)

    transport = httpx.ASGITransport(app=test_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        r_kbs = await client.get(
            "/api/v1/knowledge-bases",
            params={"action": "READ"},
            headers={"Authorization": f"Bearer {staff_tok}"},
        )
        assert r_kbs.status_code == 200
        kb_by_id = {item["id"]: item for item in r_kbs.json()}
        assert kb_by_id[kbs["tech_kb"]["id"]]["can_delete"] is True
        assert kb_by_id[kbs["hr_kb"]["id"]]["can_delete"] is False

        r_del = await client.delete(
            f"/api/v1/documents/{docs['l2_hr_onboard']['doc_id']}",
            headers={"Authorization": f"Bearer {staff_tok}"},
        )
        assert r_del.status_code == 403

    async with AsyncSessionLocal() as db:
        hr_doc = await db.get(Document, uuid.UUID(docs["l2_hr_onboard"]["doc_id"]))
        assert hr_doc is not None and hr_doc.is_active is True


# ==============================================================================
# 验收准则 4：职级密级动态生效 & 单篇 PDF 在线独立改密（实时失效短票 + 穿透缓存）
# ==============================================================================


@pytest.mark.asyncio
async def test_04_dynamic_position_clearance_and_online_pdf_reclassification():
    """准则 4: SUPERVISOR L2->L3->L2 实时生效；单篇 PDF L2->L3 在线改密后旧短票、/pages、/chunks、/asset 立刻阻断"""
    manifest = _load_manifest()
    tenant_id = manifest["tenant_id"]
    docs = manifest["documents"]
    clear_authz_cache()

    # 4.1 职级密级动态调整：SUPERVISOR L2 -> L3 -> L2
    sup_ctx_l2 = await get_current_user(authorization=f"Bearer {await _mint_token_for_user(manifest, 'tech_supervisor')}")
    assert sup_ctx_l2.effective_clearance == 2
    assert docs["l3_tech_budget"]["doc_id"] not in _collect_doc_ids(
        await _run_all_channels_search(sup_ctx_l2, "H800 算力集群采购预算 1850万元")
    )

    try:
        async with AsyncSessionLocal() as db:
            pos = await db.get(Position, (tenant_id, "SUPERVISOR"))
            pos.default_clearance_level = 3
            epoch = await db.get(TenantPolicyEpoch, tenant_id)
            epoch.policy_epoch = int(epoch.policy_epoch) + 1
            await db.commit()
        clear_authz_cache()

        # tech_supervisor 升至 L3 后立即可检索 1850万元，同时普通员工 tech_staff (L2) 仍不可见
        sup_ctx_l3 = await get_current_user(
            authorization=f"Bearer {await _mint_token_for_user(manifest, 'tech_supervisor')}"
        )
        assert sup_ctx_l3.effective_clearance == 3
        assert docs["l3_tech_budget"]["doc_id"] in _collect_doc_ids(
            await _run_all_channels_search(sup_ctx_l3, "H800 算力集群采购预算 1850万元")
        )

        staff_ctx_still_l2 = await get_current_user(
            authorization=f"Bearer {await _mint_token_for_user(manifest, 'tech_staff')}"
        )
        assert staff_ctx_still_l2.effective_clearance == 2
        assert docs["l3_tech_budget"]["doc_id"] not in _collect_doc_ids(
            await _run_all_channels_search(staff_ctx_still_l2, "H800 算力集群采购预算 1850万元")
        )
    finally:
        async with AsyncSessionLocal() as db:
            pos = await db.get(Position, (tenant_id, "SUPERVISOR"))
            pos.default_clearance_level = 2
            epoch = await db.get(TenantPolicyEpoch, tenant_id)
            epoch.policy_epoch = int(epoch.policy_epoch) + 1
            await db.commit()
        clear_authz_cache()

    # 4.2 单篇 PDF 在线独立升密 (L2 -> L3) 全链路穿透验证
    pdf_doc_id = docs["l2_tech_pdf"]["doc_id"]
    pdf_chunk_id = docs["l2_tech_pdf"]["first_chunk_id"]

    # 为真实 PDF 的首个切片挂载一张真实上传至阿里云 OSS 的 PNG 资产，实测 /chunks/{id}/asset
    oss_png_key = f"{settings.OSS_KEY_PREFIX}/assets/{tenant_id}/verify_{uuid.uuid4().hex[:8]}.png"
    uploaded_png_key = await asyncio.to_thread(oss_service.upload_bytes, TINY_PNG_BYTES, oss_png_key, "image/png")

    orig_asset_url: Optional[str] = None
    async with AsyncSessionLocal() as db:
        chunk_obj = await db.get(DocumentChunk, uuid.UUID(pdf_chunk_id))
        orig_asset_url = chunk_obj.asset_url
        chunk_obj.asset_url = uploaded_png_key
        await db.commit()

    test_app = _build_test_app()
    ticket = None
    try:
        staff_tok = await _mint_token_for_user(manifest, "tech_staff")
        mgr_tok = await _mint_token_for_user(manifest, "tech_manager")

        transport = httpx.ASGITransport(app=test_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            # (a) 改密前 (L2)：tech_staff 能正常签发短票、Range 206 预览、渲染第 1 页（预热 _PDF_DOCUMENT_CACHE）、读切片与 PNG 资产
            tk_r = await client.post(
                f"/api/v1/documents/{pdf_doc_id}/stream-ticket",
                json={"purpose": "preview"},
                headers={"Authorization": f"Bearer {staff_tok}"},
            )
            assert tk_r.status_code == 200
            ticket = tk_r.json()["ticket"]

            r_range_before = await client.get(
                f"/api/v1/documents/{pdf_doc_id}/preview?ticket={ticket}",
                headers={"Range": "bytes=0-31"},
            )
            assert r_range_before.status_code == 206
            assert r_range_before.content.startswith(b"%PDF-")

            r_page_before = await client.get(
                f"/api/v1/documents/{pdf_doc_id}/pages/1",
                headers={"Authorization": f"Bearer {staff_tok}"},
            )
            assert r_page_before.status_code == 200
            assert r_page_before.content.startswith(b"\x89PNG")

            r_chunks_before = await client.get(
                f"/api/v1/documents/{pdf_doc_id}/chunks",
                headers={"Authorization": f"Bearer {staff_tok}"},
            )
            assert r_chunks_before.status_code == 200 and len(r_chunks_before.json()) > 0

            r_asset_before = await client.get(
                f"/api/v1/chunks/{pdf_chunk_id}/asset",
                headers={"Authorization": f"Bearer {staff_tok}"},
            )
            assert r_asset_before.status_code == 200
            assert r_asset_before.content == TINY_PNG_BYTES

            # (b) tech_manager (L3) 调用在线改密接口将 PDF 升级为 L3 管理层机密
            r_patch = await client.patch(
                f"/api/v1/documents/{pdf_doc_id}/classification",
                json={"classification_level": 3, "reason": "架构评审发现核心容量参数，升级为 L3"},
                headers={"Authorization": f"Bearer {mgr_tok}"},
            )
            assert r_patch.status_code == 200
            assert r_patch.json()["classification_level"] == 3

            # (c) 改密后：
            # - tech_staff 持有改密前的旧 Range 预览短票立即失效 (401/403)
            r_range_stale = await client.get(
                f"/api/v1/documents/{pdf_doc_id}/preview?ticket={ticket}",
                headers={"Range": "bytes=0-31"},
            )
            assert r_range_stale.status_code in (401, 403)

            # - 即便换新令牌（最新 policy_epoch）且 _PDF_DOCUMENT_CACHE 已预热，L2 员工访问 /pages/1、/chunks、/asset 全被 403 拦截！
            staff_tok_fresh = await _mint_token_for_user(manifest, "tech_staff")
            for blocked_url in (
                f"/api/v1/documents/{pdf_doc_id}/pages/1",
                f"/api/v1/documents/{pdf_doc_id}/chunks",
                f"/api/v1/chunks/{pdf_chunk_id}/asset",
            ):
                r_blk = await client.get(blocked_url, headers={"Authorization": f"Bearer {staff_tok_fresh}"})
                assert r_blk.status_code == 403, f"L2 员工应被 403 拦截: {blocked_url}, got {r_blk.status_code}"

            # - 同知识库内的 L2 微服务规范文档对 tech_staff 依然完全可读
            r_sibling = await client.get(
                f"/api/v1/documents/{docs['l2_tech_spec']['doc_id']}/chunks",
                headers={"Authorization": f"Bearer {staff_tok_fresh}"},
            )
            assert r_sibling.status_code == 200 and len(r_sibling.json()) > 0

            # - L3 部门经理 tech_manager 换新令牌后仍可正常访问升级后的 L3 PDF
            mgr_tok_fresh = await _mint_token_for_user(manifest, "tech_manager")
            r_mgr_page = await client.get(
                f"/api/v1/documents/{pdf_doc_id}/pages/1",
                headers={"Authorization": f"Bearer {mgr_tok_fresh}"},
            )
            assert r_mgr_page.status_code == 200
            r_mgr_asset = await client.get(
                f"/api/v1/chunks/{pdf_chunk_id}/asset",
                headers={"Authorization": f"Bearer {mgr_tok_fresh}"},
            )
            assert r_mgr_asset.status_code == 200
    finally:
        if ticket:
            await get_redis_client().delete(f"{STREAM_TICKET_KEY_PREFIX}{ticket}")
        try:
            await asyncio.to_thread(oss_service.get_client().delete_object, uploaded_png_key)
        except Exception:
            pass
        async with AsyncSessionLocal() as db:
            pdf_doc = await db.get(Document, uuid.UUID(pdf_doc_id))
            if pdf_doc is not None:
                pdf_doc.classification_level = 2
            chunk_obj = await db.get(DocumentChunk, uuid.UUID(pdf_chunk_id))
            if chunk_obj is not None:
                chunk_obj.asset_url = orig_asset_url
            epoch = await db.get(TenantPolicyEpoch, tenant_id)
            if epoch is not None:
                epoch.policy_epoch = int(epoch.policy_epoch) + 1
            await db.commit()
        clear_authz_cache()


# ==============================================================================
# 验收准则 5：ACL DENY/自然到期、跨租户、Redis 故障兜底、会话历史重验、去重防泄密、MCP 与零 raw_oss_url
# ==============================================================================


@pytest.mark.asyncio
async def test_05_acl_expiry_cross_tenant_redis_history_dedup_mcp_no_oss_url():
    """准则 5: 全方位安全边界闭环验证"""
    manifest = _load_manifest()
    tenant_id = manifest["tenant_id"]
    docs = manifest["documents"]
    kbs = manifest["knowledge_bases"]
    clear_authz_cache()
    test_app = _build_test_app()
    transport = httpx.ASGITransport(app=test_app)

    # 5.1 个人 DENY 覆盖部门 ALLOW + 会话历史逐条权限重验
    mgr_uid = manifest["users"]["tech_manager"]["id"]
    shared_kb_uuid = uuid.UUID(kbs["fin_tech_shared_kb"]["id"])
    contract_doc_id = docs["l3_fin_contract"]["doc_id"]
    deny_grant_id = uuid.uuid4()
    session_id: Optional[uuid.UUID] = None

    async with AsyncSessionLocal() as db:
        sess = ChatSession(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            user_id=mgr_uid,
            title="业财合同会话重验",
        )
        db.add(sess)
        await db.commit()
        session_id = sess.id

    try:
        # 先在有权限状态下写入一条引用 L3 业财合同的对话回答
        await append_message(
            session_id=str(session_id),
            role="user",
            content="ERP 外部实施商合同总价是多少？",
            tenant_id=tenant_id,
            user_id=mgr_uid,
        )
        await append_message(
            session_id=str(session_id),
            role="assistant",
            content="根据商务合同，ERP 外部实施商合同总价为 420万元，违约金比例为每日 0.3%。",
            citations=[
                {
                    "doc_id": contract_doc_id,
                    "chunk_id": docs["l3_fin_contract"]["first_chunk_id"],
                    "file_name": docs["l3_fin_contract"]["file_name"],
                    "raw_oss_url": "should_never_be_persisted",
                }
            ],
            source_doc_ids=[contract_doc_id],
            tenant_id=tenant_id,
            user_id=mgr_uid,
        )

        # 验证持久化 citation 已自动剥离 raw_oss_url，且撤权前可读
        mgr_tok = await _mint_token_for_user(manifest, "tech_manager")
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            r_hist_before = await client.get(
                f"/api/v1/sessions/{session_id}/messages",
                headers={"Authorization": f"Bearer {mgr_tok}"},
            )
            assert r_hist_before.status_code == 200
            msgs_before = r_hist_before.json()
            assert any("420万元" in m["content"] for m in msgs_before)
            assert "raw_oss_url" not in json.dumps(msgs_before, ensure_ascii=False)

        # 给 tech_manager 添加针对业财协作库的个人 DENY READ（应覆盖技术部部门 ALLOW READ）
        async with AsyncSessionLocal() as db:
            db.add(
                KbGrant(
                    id=deny_grant_id,
                    tenant_id=tenant_id,
                    kb_id=shared_kb_uuid,
                    subject_type="USER",
                    subject_id=mgr_uid,
                    include_sub_depts=False,
                    action="READ",
                    effect="DENY",
                    reason="个人显式 DENY 覆盖部门 ALLOW 测试",
                )
            )
            await db.commit()
        clear_authz_cache()

        mgr_ctx_denied = await get_current_user(authorization=f"Bearer {mgr_tok}")
        res_denied = await _run_all_channels_search(mgr_ctx_denied, "ERP 外部实施商合同总价 420万元")
        assert contract_doc_id not in _collect_doc_ids(res_denied)

        # 撤权后重新拉取历史消息 & 组装追问历史：严禁再返回或向 LLM 投喂含 420万元 的历史回答！
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            r_hist_after = await client.get(
                f"/api/v1/sessions/{session_id}/messages",
                headers={"Authorization": f"Bearer {mgr_tok}"},
            )
            assert r_hist_after.status_code == 200
            dumped_after = json.dumps(r_hist_after.json(), ensure_ascii=False)
            assert "420万元" not in dumped_after
            assert contract_doc_id not in dumped_after

        async with AsyncSessionLocal() as db:
            safe_history = await build_authorized_history(
                session_id=str(session_id),
                ctx=mgr_ctx_denied,
                max_messages=20,
                db=db,
            )
            assert all("420万元" not in item["content"] for item in safe_history)
    finally:
        async with AsyncSessionLocal() as db:
            await db.execute(delete(KbGrant).where(KbGrant.id == deny_grant_id))
            if session_id:
                await db.execute(delete(ChatMessage).where(ChatMessage.session_id == session_id))
                await db.execute(delete(ChatSession).where(ChatSession.id == session_id))
            await db.commit()
        clear_authz_cache()

    # 5.2 ACL 自然到期（无任何管理员写操作、无 authz_version / policy_epoch 递增）
    temp_acl_id = uuid.uuid4()
    hr_kb_uuid = uuid.UUID(kbs["hr_kb"]["id"])
    staff_uid = manifest["users"]["tech_staff"]["id"]
    hr_doc_id = docs["l2_hr_onboard"]["doc_id"]
    staff_tok_for_expiry = await _mint_token_for_user(manifest, "tech_staff")
    expiry_ticket = None
    try:
        async with AsyncSessionLocal() as db:
            future_ts = (await db.execute(text("SELECT NOW() + INTERVAL '10 minutes'"))).scalar_one()
            db.add(
                KbGrant(
                    id=temp_acl_id,
                    tenant_id=tenant_id,
                    kb_id=hr_kb_uuid,
                    subject_type="USER",
                    subject_id=staff_uid,
                    include_sub_depts=False,
                    action="READ",
                    effect="ALLOW",
                    expires_at=future_ts,
                    reason="临时跨部门访问 HR 文档",
                )
            )
            await db.commit()
        clear_authz_cache()

        # 到期前：旧 JWT 可读 HR 文档并签发预览短票
        ctx_valid = await get_current_user(authorization=f"Bearer {staff_tok_for_expiry}")
        assert hr_doc_id in _collect_doc_ids(await _run_all_channels_search(ctx_valid, "人事入职交接批次 HR-ONB-27"))
        expiry_ticket, _ = await issue_stream_ticket(ctx_valid, doc_id=hr_doc_id, purpose="preview")

        # 模拟自然时间推移至过期（不动 authz_version，也不动 policy_epoch！）
        async with AsyncSessionLocal() as db:
            await db.execute(
                text("UPDATE kb_grants SET expires_at = NOW() - INTERVAL '5 seconds' WHERE id = :gid"),
                {"gid": temp_acl_id},
            )
            await db.commit()
        clear_authz_cache()

        # 到期后：持同一个未过期旧 JWT 检索 HR -> 0 命中；持同一个旧短票预览 -> 403！
        ctx_expired_acl = await get_current_user(authorization=f"Bearer {staff_tok_for_expiry}")
        assert hr_doc_id not in _collect_doc_ids(
            await _run_all_channels_search(ctx_expired_acl, "人事入职交接批次 HR-ONB-27")
        )
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            r_exp_prev = await client.get(f"/api/v1/documents/{hr_doc_id}/preview?ticket={expiry_ticket}")
            assert r_exp_prev.status_code == 403
    finally:
        if expiry_ticket:
            await get_redis_client().delete(f"{STREAM_TICKET_KEY_PREFIX}{expiry_ticket}")
        async with AsyncSessionLocal() as db:
            await db.execute(delete(KbGrant).where(KbGrant.id == temp_acl_id))
            await db.commit()
        clear_authz_cache()

    # 5.3 PostgreSQL 已提交版本递增但 Redis 同步失败 -> 旧 JWT 必须立即 401
    old_staff_tok = await _mint_token_for_user(manifest, "tech_staff")
    async with AsyncSessionLocal() as db:
        await db.execute(
            text("UPDATE sys_users SET authz_version = authz_version + 1 WHERE id = :uid"),
            {"uid": uuid.UUID(staff_uid)},
        )
        await db.commit()
    clear_authz_cache()
    try:
        with pytest.raises(HTTPException) as exc_ver:
            await get_current_user(authorization=f"Bearer {old_staff_tok}")
        assert exc_ver.value.status_code == 401
    finally:
        async with AsyncSessionLocal() as db:
            await db.execute(
                text("UPDATE sys_users SET authz_version = GREATEST(1, authz_version - 1) WHERE id = :uid"),
                {"uid": uuid.UUID(staff_uid)},
            )
            await db.commit()
        clear_authz_cache()

    # 5.4 跨租户同名部门/同名角色强隔离
    other_tenant = f"isolated_tenant_{uuid.uuid4().hex[:6]}"
    other_uid = uuid.uuid4()
    other_dept_id = uuid.uuid4()
    try:
        async with AsyncSessionLocal() as db:
            db.add(TenantPolicyEpoch(tenant_id=other_tenant, policy_epoch=1))
            db.add(Department(id=other_dept_id, tenant_id=other_tenant, name="技术部", path="/公司/技术部", status="ACTIVE"))
            db.add(
                Position(
                    tenant_id=other_tenant,
                    position_code="EXECUTIVE",
                    position_name="高管",
                    rank_order=60,
                    default_clearance_level=4,
                    min_allowed_clearance=4,
                    max_allowed_clearance=4,
                )
            )
            db.add(
                Role(
                    tenant_id=other_tenant,
                    role_code="executive",
                    role_name="高管",
                    data_scope="ALL_ORG",
                    permissions=["kb:retrieve"],
                )
            )
            await db.flush()
            db.add(
                SysUser(
                    id=other_uid,
                    tenant_id=other_tenant,
                    external_id=f"ext-{other_tenant}",
                    id_source="oidc:other",
                    real_name="外部租户高管",
                    position_code="EXECUTIVE",
                    authz_version=1,
                    is_active=True,
                )
            )
            await db.flush()
            db.add(
                UserDepartment(
                    id=uuid.uuid4(),
                    tenant_id=other_tenant,
                    user_id=other_uid,
                    department_id=other_dept_id,
                    dept_path="/公司/技术部",
                    is_primary=True,
                )
            )
            await db.commit()

        other_tok = make_rs256_test_token(
            user_id=str(other_uid),
            tenant_id=other_tenant,
            username="外部租户高管",
            dept_path="/公司/技术部",
            dept_paths=["/公司/技术部"],
            roles=["executive"],
            position_code="EXECUTIVE",
            clearance_level=4,
            authz_ver=1,
            policy_epoch=1,
        )
        other_ctx = await get_current_user(authorization=f"Bearer {other_tok}")
        res_other = await _run_all_channels_search(other_ctx, "Project-Titan 弹性打卡窗口 1850万元")
        assert len(_collect_doc_ids(res_other)) == 0
    finally:
        async with AsyncSessionLocal() as db:
            await db.execute(delete(SysUser).where(SysUser.tenant_id == other_tenant))
            await db.execute(delete(Role).where(Role.tenant_id == other_tenant))
            await db.execute(delete(Position).where(Position.tenant_id == other_tenant))
            await db.execute(delete(Department).where(Department.tenant_id == other_tenant))
            await db.execute(delete(TenantPolicyEpoch).where(TenantPolicyEpoch.tenant_id == other_tenant))
            await db.commit()
        clear_authz_cache()

    # 5.5 去重侧信道防泄密：L2 员工以与 L4 Project-Titan 高度相似的标题触发防重，绝不返回 L4 标题或允许覆盖
    titan_doc_id = docs["l4_tech_titan"]["doc_id"]
    async with AsyncSessionLocal() as db:
        dedup_res = await DeduplicationService().check_duplicate(
            session=db,
            doc_id=str(uuid.uuid4()),
            file_name="L4_技术部核心引擎收购技术尽调报告.md",
            tenant_id=tenant_id,
            kb_id=kbs["tech_kb"]["id"],
            uploader_clearance=2,
        )
    assert "Project-Titan" not in json.dumps(dedup_res, ensure_ascii=False)
    assert "收购技术尽调报告" not in json.dumps(dedup_res, ensure_ascii=False)
    if dedup_res.get("is_duplicate"):
        assert dedup_res.get("matched_title") == REDACTED_HIGH_CLASSIFICATION_TITLE
        assert dedup_res.get("can_overwrite") is False

    # 5.6 MCP 工具调用 + 全链路零 raw_oss_url 泄露核验
    staff_tok_clean = await _mint_token_for_user(manifest, "tech_staff")
    with _mcp_http_auth_context(staff_tok_clean):
        mcp_res = await rag_hybrid_search(
            query="生产灰度发布窗口 每周二/四 22:00",
            top_k=5,
            enable_hyde=False,
        )
    mcp_dump = json.dumps(mcp_res, ensure_ascii=False, default=str)
    assert "raw_oss_url" not in mcp_dump
    assert "Project-Titan" not in mcp_dump

    # MCP 查询未授权文档状态 -> 必须失败且零快照泄露
    with pytest.raises(Exception):
        with _mcp_http_auth_context(staff_tok_clean):
            await rag_document_status(doc_id=titan_doc_id)

    # HTTP 文档列表响应零 raw_oss_url
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        r_docs = await client.get("/api/v1/documents", headers={"Authorization": f"Bearer {staff_tok_clean}"})
        assert r_docs.status_code == 200
        assert "raw_oss_url" not in r_docs.text


# ==============================================================================
# 验收准则 6：权限治理职责分离与强审计阻断
# ==============================================================================


@pytest.mark.asyncio
async def test_06_separation_of_duties_and_mandatory_audit():
    """准则 6: sys_ops_admin (L2) 无法越级读 L3/L4 或给不可见文档脱密；审计写失败时回滚改密并阻断 L3/L4 敏感读出"""
    manifest = _load_manifest()
    tenant_id = manifest["tenant_id"]
    docs = manifest["documents"]
    clear_authz_cache()

    test_app = _build_test_app()
    transport = httpx.ASGITransport(app=test_app)

    # 6.1 职责分离：sys_ops_admin 虽持有 admin 角色，但职级仅 STAFF (L2)，无法检索或读取 L3/L4 文档，也无法脱密不可见文档
    ops_tok = await _mint_token_for_user(manifest, "sys_ops_admin")
    ops_ctx = await get_current_user(authorization=f"Bearer {ops_tok}")
    assert ops_ctx.effective_clearance == 2

    res_ops_l3 = await _run_all_channels_search(ops_ctx, "H800 算力集群采购预算 1850万元")
    assert docs["l3_tech_budget"]["doc_id"] not in _collect_doc_ids(res_ops_l3)
    res_ops_l4 = await _run_all_channels_search(ops_ctx, "Project-Titan Nebula-AI 3.6亿美元")
    assert docs["l4_tech_titan"]["doc_id"] not in _collect_doc_ids(res_ops_l4)
    assert docs["l4_board_strategy"]["doc_id"] not in _collect_doc_ids(res_ops_l4)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        # 尝试访问 L3/L4 文档切片 -> 403，且写入 AUTHZ_DENY 审计记录
        r_ops_blk = await client.get(
            f"/api/v1/documents/{docs['l4_tech_titan']['doc_id']}/chunks",
            headers={"Authorization": f"Bearer {ops_tok}"},
        )
        assert r_ops_blk.status_code == 403

        # 尝试给自身无权阅读的 L4 文档降密脱密 -> 403
        r_ops_declass = await client.patch(
            f"/api/v1/documents/{docs['l4_tech_titan']['doc_id']}/classification",
            json={"classification_level": 2, "reason": "尝试违规脱密"},
            headers={"Authorization": f"Bearer {ops_tok}"},
        )
        assert r_ops_declass.status_code == 403

    # 6.2 校验真实 PostgreSQL sys_audit_logs 存在结构化安全审计记录
    async with AsyncSessionLocal() as db:
        audit_rows = (
            await db.execute(
                select(AuditLog)
                .where(AuditLog.tenant_id == tenant_id)
                .order_by(AuditLog.created_at.desc())
                .limit(50)
            )
        ).scalars().all()
        actions = {a.action for a in audit_rows}
        assert any(a.endswith("_DENIED") for a in actions), f"未记录越权拦截审计: {actions}"
        assert "DOCUMENT_CLASSIFICATION_UPDATE" in actions, f"未记录单文档改密审计: {actions}"
        assert any(a.startswith("HIGH_CLASSIFICATION_") for a in actions), f"未记录高密读出审计: {actions}"

    # 6.3 强审计阻断：模拟审计落库失败 -> 在线改密必须回滚（DB 密级不变），L3 敏感读出必须在输出首字节前阻断
    mgr_tok = await _mint_token_for_user(manifest, "tech_manager")
    l2_doc_id = docs["l2_tech_spec"]["doc_id"]
    l3_doc_id = docs["l3_tech_budget"]["doc_id"]

    with patch(
        "rag_kb.api.routes.record_security_audit_in_tx",
        side_effect=AuditWriteError("模拟审计落库故障"),
    ):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            r_fail_patch = await client.patch(
                f"/api/v1/documents/{l2_doc_id}/classification",
                json={"classification_level": 3, "reason": "审计故障回滚验证"},
                headers={"Authorization": f"Bearer {mgr_tok}"},
            )
            assert r_fail_patch.status_code >= 500

    async with AsyncSessionLocal() as db:
        doc_after = await db.get(Document, uuid.UUID(l2_doc_id))
        assert doc_after.classification_level == 2  # 事务已回滚，未发生静默改密

    with patch(
        "rag_kb.core.authz.guards.record_mandatory_security_audit",
        side_effect=AuditWriteError("模拟高密读出审计故障"),
    ):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            r_fail_read = await client.get(
                f"/api/v1/documents/{l3_doc_id}/chunks",
                headers={"Authorization": f"Bearer {mgr_tok}"},
            )
            assert r_fail_read.status_code >= 500
            assert "1850万元" not in r_fail_read.text


@pytest.mark.asyncio
async def test_07_python_admin_governance_api_closed_loop():
    """测试 7: Python /api/v1/admin/* 治理控制面防间接自提权、知识库+ACL 闭环与审计故障回滚"""
    manifest = _load_manifest()
    tenant_id = manifest["tenant_id"]
    app = _build_test_app()
    transport = httpx.ASGITransport(app=app)

    ops_tok = await _mint_token_for_user(manifest, "sys_ops_admin")  # position=STAFF (L2), role=admin (ALL_ORG, system:admin)
    created_kb_id: Optional[str] = None

    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            # 1. 只读部门、员工、职级、角色与审计列表
            r_depts = await client.get("/api/v1/admin/departments", headers={"Authorization": f"Bearer {ops_tok}"})
            assert r_depts.status_code == 200 and len(r_depts.json()) >= 1
            r_users = await client.get("/api/v1/admin/users", headers={"Authorization": f"Bearer {ops_tok}"})
            assert r_users.status_code == 200 and len(r_users.json()) >= 1
            r_pos = await client.get("/api/v1/admin/positions", headers={"Authorization": f"Bearer {ops_tok}"})
            assert r_pos.status_code == 200 and any(p["positionCode"] == "STAFF" for p in r_pos.json())
            r_roles = await client.get("/api/v1/admin/roles", headers={"Authorization": f"Bearer {ops_tok}"})
            assert r_roles.status_code == 200 and any(r["roleCode"] == "test_developer" for r in r_roles.json())

            # 2. 防间接自提权：sys_ops_admin (STAFF L2) 企图上调 STAFF 默认密级至 L4 -> 403
            r_self_esc = await client.put(
                "/api/v1/admin/positions",
                json={
                    "positionCode": "STAFF",
                    "positionName": "普通员工",
                    "rankOrder": 20,
                    "defaultClearanceLevel": 4,
                    "reason": "企图通过改职级表间接自提权",
                },
                headers={"Authorization": f"Bearer {ops_tok}"},
            )
            assert r_self_esc.status_code == 403

            # 3. 审计落库失败必须回滚职级修改事务
            with patch(
                "rag_kb.api.admin.record_security_audit_in_tx",
                side_effect=AuditWriteError("模拟治理审计写失败"),
            ):
                r_audit_fail = await client.put(
                    "/api/v1/admin/positions",
                    json={
                        "positionCode": "SUPERVISOR",
                        "positionName": "部门主管",
                        "rankOrder": 30,
                        "defaultClearanceLevel": 3,
                        "reason": "审计故障回滚校验",
                    },
                    headers={"Authorization": f"Bearer {ops_tok}"},
                )
                assert r_audit_fail.status_code >= 500

            async with AsyncSessionLocal() as db:
                sup_after = await db.get(Position, (tenant_id, "SUPERVISOR"))
                assert sup_after is not None and int(sup_after.default_clearance_level) == 2

            # 4. 知识库新建 + ACL 授权 + 删除闭环（自动推进 policy_epoch 并同步 Redis）
            tech_dept_id = manifest["departments"]["tech"]["id"]
            kb_name = f"临时治理验证库_{uuid.uuid4().hex[:8]}"
            r_kb = await client.post(
                "/api/v1/admin/knowledge-bases",
                json={
                    "name": kb_name,
                    "ownerDepartmentId": tech_dept_id,
                    "visibility": "RESTRICTED",
                    "minClassificationLevel": 2,
                    "defaultDocLevel": 2,
                    "maxDocLevel": 2,
                    "reason": "Python 控制面集成验证",
                },
                headers={"Authorization": f"Bearer {ops_tok}"},
            )
            assert r_kb.status_code == 201, r_kb.text
            created_kb_id = r_kb.json()["id"]

            # 策略版本已推进 -> 旧 ops_tok 立即 401，刷新后继续操作 ACL
            r_stale = await client.get("/api/v1/admin/knowledge-bases", headers={"Authorization": f"Bearer {ops_tok}"})
            assert r_stale.status_code == 401

            ops_tok2 = await _mint_token_for_user(manifest, "sys_ops_admin")
            r_grant = await client.post(
                f"/api/v1/admin/knowledge-bases/{created_kb_id}/grants",
                json={
                    "subjectType": "USER",
                    "subjectId": manifest["users"]["tech_staff"]["id"],
                    "action": "READ",
                    "effect": "ALLOW",
                    "reason": "临时共享验证",
                },
                headers={"Authorization": f"Bearer {ops_tok2}"},
            )
            assert r_grant.status_code == 201, r_grant.text
            grant_id = r_grant.json()["id"]

            ops_tok3 = await _mint_token_for_user(manifest, "sys_ops_admin")
            r_del_grant = await client.delete(
                f"/api/v1/admin/knowledge-bases/{created_kb_id}/grants/{grant_id}",
                headers={"Authorization": f"Bearer {ops_tok3}"},
            )
            assert r_del_grant.status_code == 200

            ops_tok4 = await _mint_token_for_user(manifest, "sys_ops_admin")
            r_del_kb = await client.delete(
                f"/api/v1/admin/knowledge-bases/{created_kb_id}",
                headers={"Authorization": f"Bearer {ops_tok4}"},
            )
            assert r_del_kb.status_code == 200
            created_kb_id = None

            ops_tok5 = await _mint_token_for_user(manifest, "sys_ops_admin")
            r_audits = await client.get(
                "/api/v1/admin/audit-logs",
                params={"limit": 10},
                headers={"Authorization": f"Bearer {ops_tok5}"},
            )
            assert r_audits.status_code == 200 and len(r_audits.json()) >= 1
    finally:
        if created_kb_id:
            async with AsyncSessionLocal() as db:
                await db.execute(delete(KnowledgeBase).where(KnowledgeBase.id == uuid.UUID(created_kb_id)))
                await db.commit()
        clear_authz_cache()


@pytest.mark.asyncio
async def test_08_review_security_hardening_regressions():
    """测试 8: 本轮未提交变更审查 12 项修复点的全量安全回归验证"""
    from datetime import datetime, timedelta, timezone
    from fastapi import HTTPException
    from rag_kb.core.authz import EffectiveAuthzContext, resolve_authorized_target_kb
    from rag_kb.services.pipeline.chunker import RawChunkData
    from rag_kb.services.pipeline.dedup_service import (
        REDACTED_HIGH_CLASSIFICATION_TITLE,
        dedup_service,
    )
    from rag_kb.services.pipeline.update_service import update_service

    manifest = _load_manifest()
    tenant_id = manifest["tenant_id"]
    kbs = manifest["knowledge_bases"]
    docs = manifest["documents"]
    tech_kb_id = kbs["tech_kb"]["id"]
    shared_kb_id = kbs["fin_tech_shared_kb"]["id"]

    # 8.1 防重服务 Fail-Closed：uploader_clearance=None 时必须脱敏标题并禁止覆盖
    async with AsyncSessionLocal() as db:
        dedup_unknown = await dedup_service.check_duplicate(
            session=db,
            doc_id=str(uuid.uuid4()),
            file_name=docs["l3_tech_budget"]["file_name"],
            markdown_text=f"# {docs['l3_tech_budget']['file_name']}",
            tenant_id=tenant_id,
            kb_id=tech_kb_id,
            uploader_clearance=None,
        )
        assert dedup_unknown["matched_doc_id"] is not None
        assert dedup_unknown["is_higher_classification"] is True
        assert dedup_unknown["matched_title"] == REDACTED_HIGH_CLASSIFICATION_TITLE
        assert dedup_unknown["can_overwrite"] is False

    # 8.2 排队入库期间管理员提密 -> 后台最终落库绝不将 L3 覆写回旧 L2；且 uploader_clearance=None 严禁覆盖旧文档
    temp_doc_id = uuid.uuid4()
    tech_dept_id = uuid.UUID(manifest["departments"]["tech"]["id"])
    l3_existing_doc_id = uuid.UUID(docs["l3_tech_budget"]["doc_id"])
    dummy_vec = [0.01] * 1024
    dummy_chunk = RawChunkData(
        chunk_index=0,
        content="并发改密防覆盖回归测试切片内容",
        enriched_content="并发改密防覆盖回归测试切片内容",
        heading_path="# 回归测试",
        chunk_type="text",
        page_num=1,
    )
    try:
        async with AsyncSessionLocal() as db:
            db.add(
                Document(
                    id=temp_doc_id,
                    tenant_id=tenant_id,
                    kb_id=uuid.UUID(tech_kb_id),
                    classification_level=3,  # 模拟管理员在排队期间已将密级提高至 L3
                    created_by=manifest["users"]["tech_staff"]["id"],
                    title="并发改密防回滚验证.md",
                    file_hash=f"hash_{temp_doc_id.hex}",
                    raw_oss_url="https://oss.invalid/regression.md",
                    department_id=tech_dept_id,
                    department_path="/公司/技术部",
                    category="技术规范",
                    tags=[],
                    overall_status="queued",
                    is_active=False,
                )
            )
            await db.commit()

        # 1) 模拟 Worker 携带旧消息 classification_level=2 落库 -> 必须保留 DB 当前的 L3
        async with AsyncSessionLocal() as db:
            saved = await update_service.ingest_document_with_chunks(
                session=db,
                doc_id=temp_doc_id,
                file_name="并发改密防回滚验证.md",
                file_hash=f"hash_{temp_doc_id.hex}",
                raw_oss_url="https://oss.invalid/regression.md",
                enhanced_md_oss_url=None,
                department_id=tech_dept_id,
                department_path="/公司/技术部",
                category="技术规范",
                tags=[],
                title_embedding=dummy_vec,
                chunks=[dummy_chunk],
                dense_embeddings=[dummy_vec],
                sparse_vectors=[{"1": 1.0}],
                tsv_strings=["回归 测试"],
                tenant_id=tenant_id,
                kb_id=uuid.UUID(tech_kb_id),
                classification_level=2,  # 旧队列消息中的 L2
                created_by=manifest["users"]["tech_staff"]["id"],
                uploader_clearance=3,
            )
            assert int(saved.classification_level) == 3

        # 2) 模拟 uploader_clearance=None 尝试覆盖存量文档 -> 必须 Fail-Closed 抛出 PermissionError
        async with AsyncSessionLocal() as db:
            with pytest.raises(PermissionError):
                await update_service.ingest_document_with_chunks(
                    session=db,
                    doc_id=uuid.uuid4(),
                    file_name="尝试无密级覆盖.md",
                    file_hash=f"hash_overwrite_{uuid.uuid4().hex}",
                    raw_oss_url="https://oss.invalid/overwrite.md",
                    enhanced_md_oss_url=None,
                    department_id=tech_dept_id,
                    department_path="/公司/技术部",
                    category="技术规范",
                    tags=[],
                    title_embedding=dummy_vec,
                    chunks=[dummy_chunk],
                    dense_embeddings=[dummy_vec],
                    sparse_vectors=[{"1": 1.0}],
                    tsv_strings=["覆盖 测试"],
                    tenant_id=tenant_id,
                    kb_id=uuid.UUID(tech_kb_id),
                    classification_level=2,
                    created_by=manifest["users"]["tech_staff"]["id"],
                    uploader_clearance=None,
                    target_version=2,
                    old_doc_id=l3_existing_doc_id,
                )
    finally:
        async with AsyncSessionLocal() as db:
            await db.execute(delete(Document).where(Document.id == temp_doc_id))
            await db.commit()

    # 8.3 省略 kb_id 且主部门无唯一默认可上传库（仅有跨部门共享库 UPLOAD 权限）-> 必须 400，严禁静默选共享库
    shared_upload_grant_id = uuid.uuid4()
    staff_uid = manifest["users"]["tech_staff"]["id"]
    staff_tok = await _mint_token_for_user(manifest, "tech_staff")
    try:
        async with AsyncSessionLocal() as db:
            db.add(
                KbGrant(
                    id=shared_upload_grant_id,
                    tenant_id=tenant_id,
                    kb_id=uuid.UUID(shared_kb_id),
                    subject_type="USER",
                    subject_id=staff_uid,
                    action="UPLOAD",
                    effect="ALLOW",
                    reason="测试共享库可上传",
                )
            )
            await db.commit()
        clear_authz_cache()
        staff_ctx = await get_current_user(authorization=f"Bearer {staff_tok}")
        # 构造主部门指向无部门库的虚拟路径，仅能匹配到跨部门共享库
        ctx_no_primary_kb = staff_ctx.model_copy(
            update={
                "dept_path": "/公司/无部门库的新部门",
                "dept_paths": ["/公司/无部门库的新部门"],
                "dept_paths_by_action": {
                    "READ": ["/公司/无部门库的新部门"],
                    "UPLOAD": ["/公司/无部门库的新部门"],
                    "REVIEW": ["/公司/无部门库的新部门"],
                    "DELETE": ["/公司/无部门库的新部门"],
                },
            }
        )
        async with AsyncSessionLocal() as db:
            with pytest.raises(HTTPException) as exc_info:
                await resolve_authorized_target_kb(db, ctx_no_primary_kb, kb_id=None, classification_level=2)
            assert exc_info.value.status_code == 400
    finally:
        async with AsyncSessionLocal() as db:
            await db.execute(delete(KbGrant).where(KbGrant.id == shared_upload_grant_id))
            await db.commit()
        clear_authz_cache()

    # 8.4 知识库治理回归：dept_manager（无 kb:grant、非 ALL_ORG）禁止转为 PUBLIC、禁止收窄密级冲突、禁止增删 ACL
    test_app = FastAPI()
    test_app.include_router(admin_router, prefix="/api/v1")
    transport = httpx.ASGITransport(app=test_app)
    mgr_tok = await _mint_token_for_user(manifest, "tech_manager")
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        # 1) 部门经理企图将本部门库转为 PUBLIC -> 403
        r_pub = await client.patch(
            f"/api/v1/admin/knowledge-bases/{tech_kb_id}",
            json={"visibility": "PUBLIC", "minClassificationLevel": 1, "defaultDocLevel": 1, "maxDocLevel": 1},
            headers={"Authorization": f"Bearer {mgr_tok}"},
        )
        assert r_pub.status_code == 403, r_pub.text

        # 2) 将含 L3/L4 文档的技术部库 maxDocLevel 收窄为 2 -> 409 Conflict
        r_narrow = await client.patch(
            f"/api/v1/admin/knowledge-bases/{tech_kb_id}",
            json={"minClassificationLevel": 2, "defaultDocLevel": 2, "maxDocLevel": 2},
            headers={"Authorization": f"Bearer {mgr_tok}"},
        )
        assert r_narrow.status_code == 409, r_narrow.text

        # 3) 构造仅持 kb:manage、不含 kb:grant 的角色 -> 禁止创建 ACL -> 403
        manage_only_role = "kb_manage_only_reg_test"
        try:
            async with AsyncSessionLocal() as db:
                # 清理此前误建的同名测试 grant（如有）
                await db.execute(
                    delete(KbGrant).where(
                        KbGrant.tenant_id == tenant_id,
                        KbGrant.kb_id == uuid.UUID(tech_kb_id),
                        KbGrant.reason == "越权对外共享测试",
                    )
                )
                existing_role = await db.get(Role, (tenant_id, manage_only_role))
                if existing_role is None:
                    db.add(
                        Role(
                            tenant_id=tenant_id,
                            role_code=manage_only_role,
                            role_name="仅管理不含授权回归角色",
                            data_scope="DEPT_AND_SUB",
                            permissions=["kb:retrieve", "kb:manage"],
                        )
                    )
                await db.commit()
            clear_authz_cache()
            mgr_no_grant_tok = await _mint_token_for_user(
                manifest, "tech_manager", roles_override=[manage_only_role]
            )
            r_grant_denied = await client.post(
                f"/api/v1/admin/knowledge-bases/{tech_kb_id}/grants",
                json={
                    "subjectType": "USER",
                    "subjectId": staff_uid,
                    "action": "READ",
                    "effect": "ALLOW",
                    "reason": "越权对外共享测试",
                },
                headers={"Authorization": f"Bearer {mgr_no_grant_tok}"},
            )
            assert r_grant_denied.status_code == 403, r_grant_denied.text
        finally:
            async with AsyncSessionLocal() as db:
                await db.execute(
                    delete(Role).where(Role.tenant_id == tenant_id, Role.role_code == manage_only_role)
                )
                await db.commit()
            clear_authz_cache()

    # 8.5 超过 50 条消息的长会话：build_authorized_history 必须返回最新的 6 条且按时间正序排列
    long_session_id = uuid.uuid4()
    mgr_ctx = await get_current_user(authorization=f"Bearer {mgr_tok}")
    try:
        base_ts = datetime.now(timezone.utc) - timedelta(minutes=100)
        async with AsyncSessionLocal() as db:
            db.add(
                ChatSession(
                    id=long_session_id,
                    tenant_id=tenant_id,
                    user_id=manifest["users"]["tech_manager"]["id"],
                    title="超50条长会话回归测试",
                )
            )
            await db.flush()
            for idx in range(1, 56):
                db.add(
                    ChatMessage(
                        id=uuid.uuid4(),
                        session_id=long_session_id,
                        role="user",
                        content=f"第{idx:02d}条最新上下文验证消息",
                        created_at=base_ts + timedelta(seconds=idx),
                    )
                )
            await db.commit()

        async with AsyncSessionLocal() as db:
            hist = await build_authorized_history(
                session_id=str(long_session_id),
                ctx=mgr_ctx,
                max_messages=6,
                db=db,
            )
            assert [m["content"] for m in hist] == [
                f"第{idx:02d}条最新上下文验证消息" for idx in range(50, 56)
            ]
    finally:
        async with AsyncSessionLocal() as db:
            await db.execute(delete(ChatMessage).where(ChatMessage.session_id == long_session_id))
            await db.execute(delete(ChatSession).where(ChatSession.id == long_session_id))
            await db.commit()


@pytest.mark.asyncio
async def test_09_second_review_fixes_regression(local_rsa_jwt_env):
    """
    第二轮复审 7 项 P1 + 4 项 P2 端到端回归验收：
    9.1 并发改密行锁串行化：L2->L4 提交后，并发 L2->L3 无降密权限请求必须 403 且不得覆盖回 L3；
    9.2 流水线鉴权以数据库当前密级为准：解析期提密至 L3 后即便缓存被写回旧 L2，L2 读者仍被 403；
    9.3 公开知识库冲突检查包含 is_active=False 的排队文档，不得放行 L2 排队文档所在库降级为 L1 公开库；
    9.4 建库管理范围严格排除 CUSTOM_ACL，不得将其当作 DEPT_AND_SUB 允许在子部门建受限库；
    9.5 重复文档共享 SSE 告警按接收者实时渲染，L2 订阅者收不到 L4 命中文档的真实标题；
    9.6 最终入库删文档/删库后 Fail-Fast 终止，严禁改投同部门其他知识库或重建已删文档；
    9.7 仅有 doc:upload 无 kb:retrieve 的角色请求 GET /knowledge-bases?action=UPLOAD 必须 200，action=READ 才 403。
    """
    from rag_kb.api.sse import pipeline_manager
    from rag_kb.graph.state import get_default_state as get_default_ingest_state
    from rag_kb.nodes.store import StoreNode
    from rag_kb.services.pipeline.chunker import RawChunkData
    from rag_kb.services.pipeline.dedup_service import (
        REDACTED_HIGH_CLASSIFICATION_TITLE,
        render_duplicate_warning_for_viewer,
    )
    from rag_kb.services.pipeline.ingest_queue import ingest_queue_service
    from rag_kb.services.pipeline.update_service import update_service

    manifest = _load_manifest()
    tenant_id = manifest["tenant_id"]
    kbs = manifest["knowledge_bases"]
    docs = manifest["documents"]
    tech_kb_id = uuid.UUID(kbs["tech_kb"]["id"])
    tech_dept_id = uuid.UUID(manifest["departments"]["tech"]["id"])

    app = _build_test_app()
    transport = httpx.ASGITransport(app=app)

    # 9.1 并发改密行锁竞争：事务 1 持行锁将 L2->L4，并发无降密权请求尝试改 L3 -> 必须阻塞到 L4 提交后被 403 拒绝
    race_doc_id = uuid.uuid4()
    no_declassify_role = f"classify_only_{uuid.uuid4().hex[:6]}"
    try:
        async with AsyncSessionLocal() as db:
            db.add(
                Role(
                    tenant_id=tenant_id,
                    role_code=no_declassify_role,
                    role_name="仅提密无降密角色",
                    data_scope="ALL_ORG",
                    permissions=["kb:retrieve", "kb:manage", "security:classify"],
                )
            )
            db.add(
                Document(
                    id=race_doc_id,
                    tenant_id=tenant_id,
                    kb_id=tech_kb_id,
                    classification_level=2,
                    created_by=manifest["users"]["tech_staff"]["id"],
                    title="并发改密行锁测试.md",
                    file_hash=f"race_{race_doc_id.hex}",
                    raw_oss_url="https://oss.invalid/race.md",
                    department_id=tech_dept_id,
                    department_path="/公司/技术部",
                    category="技术规范",
                    tags=[],
                    overall_status="active",
                    is_active=True,
                )
            )
            await db.commit()

        # 构造拥有 L4 密级但无 security:declassify 的角色令牌
        exec_uid = manifest["users"]["corp_exec"]["id"]
        async with AsyncSessionLocal() as db:
            exec_user = await db.get(SysUser, uuid.UUID(exec_uid))
            epoch = (
                await db.execute(select(TenantPolicyEpoch.policy_epoch).where(TenantPolicyEpoch.tenant_id == tenant_id))
            ).scalar_one()
        classify_only_tok = make_rs256_test_token(
            user_id=exec_uid,
            tenant_id=tenant_id,
            username="无降密高管",
            dept_path="/公司/总裁办",
            dept_paths=["/公司/总裁办"],
            roles=[no_declassify_role],
            position_code="EXECUTIVE",
            clearance_level=4,
            authz_ver=int(exec_user.authz_version),
            policy_epoch=int(epoch),
        )

        # 会话 A 先锁定文档行并将密级改为 L4，期间发起并发 HTTP 请求尝试改为 L3
        async with AsyncSessionLocal() as lock_db:
            locked_doc = (
                await lock_db.execute(select(Document).where(Document.id == race_doc_id).with_for_update())
            ).scalar_one()
            locked_doc.classification_level = 4

            async def _send_concurrent_l3():
                async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                    return await client.patch(
                        f"/api/v1/documents/{race_doc_id}/classification",
                        json={"classification_level": 3, "reason": "并发尝试改为L3"},
                        headers={"Authorization": f"Bearer {classify_only_tok}"},
                    )

            concurrent_task = asyncio.create_task(_send_concurrent_l3())
            await asyncio.sleep(0.15)
            assert not concurrent_task.done(), "并发改密请求必须被文档行锁阻塞等待"
            await lock_db.commit()

            resp_l3 = await concurrent_task
            assert resp_l3.status_code == 403, f"L4 提交后无降密权限改为 L3 必须被 403 拦截，实际: {resp_l3.status_code} {resp_l3.text}"

        async with AsyncSessionLocal() as db:
            final_doc = await db.get(Document, race_doc_id)
            assert int(final_doc.classification_level) == 4
    finally:
        async with AsyncSessionLocal() as db:
            await db.execute(delete(Document).where(Document.id == race_doc_id))
            await db.execute(delete(Role).where(Role.tenant_id == tenant_id, Role.role_code == no_declassify_role))
            await db.commit()
        clear_authz_cache()

    # 9.2 流水线鉴权缓存陈旧：管理员在解析期把 DB 文档提密至 L3，即便队列写回旧 L2 快照，L2 读者仍被 403
    stale_doc_id = uuid.uuid4()
    try:
        async with AsyncSessionLocal() as db:
            db.add(
                Document(
                    id=stale_doc_id,
                    tenant_id=tenant_id,
                    kb_id=tech_kb_id,
                    classification_level=3,  # DB 已被提密为 L3
                    created_by=manifest["users"]["tech_staff"]["id"],
                    title="解析期提密测试.md",
                    file_hash=f"stale_{stale_doc_id.hex}",
                    raw_oss_url="https://oss.invalid/stale.md",
                    department_id=tech_dept_id,
                    department_path="/公司/技术部",
                    category="技术规范",
                    tags=[],
                    overall_status="pending_review",
                    is_active=False,
                )
            )
            await db.commit()

        stale_state = get_default_ingest_state(
            doc_id=str(stale_doc_id),
            file_name="解析期提密测试.md",
            department="/公司/技术部",
            tenant_id=tenant_id,
            kb_id=str(tech_kb_id),
            kb_name=kbs["tech_kb"]["name"],
            classification_level=2,  # 陈旧 L2 缓存
        )
        stale_state["pending_reviews"] = [
            {
                "item_id": "img_01",
                "doc_id": str(stale_doc_id),
                "type": "image",
                "title": "测试图元",
                "page_idx": 0,
                "display_page": 1,
                "asset_url": f"/api/v1/documents/{stale_doc_id}/review-items/img_01/asset",
                "raw_oss_url": "https://oss.invalid/img_01.png",
                "vlm_description": "测试图片描述",
                "status": "pending",
            }
        ]
        pipeline_manager.register_pipeline(str(stale_doc_id), stale_state)

        staff_tok = await _mint_token_for_user(manifest, "tech_staff")
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            r_status = await client.get(
                f"/api/v1/documents/{stale_doc_id}/status",
                headers={"Authorization": f"Bearer {staff_tok}"},
            )
            assert r_status.status_code == 403, f"DB 已为 L3，L2 读者查询 /status 必须 403: {r_status.status_code}"

            r_asset = await client.get(
                f"/api/v1/documents/{stale_doc_id}/review-items/img_01/asset",
                headers={"Authorization": f"Bearer {staff_tok}"},
            )
            assert r_asset.status_code == 403, f"DB 已为 L3，L2 读者查询审核资产必须 403: {r_asset.status_code}"

        # 模拟队列写回陈旧 L2 图快照 -> _update_mem_state_with_db_truth 必须以 DB L3 对齐
        stale_state["classification_level"] = 2
        await ingest_queue_service._update_mem_state_with_db_truth(str(stale_doc_id), stale_state)
        assert pipeline_manager.get_state(str(stale_doc_id))["classification_level"] == 3
    finally:
        pipeline_manager.remove_pipeline(str(stale_doc_id))
        async with AsyncSessionLocal() as db:
            await db.execute(delete(Document).where(Document.id == stale_doc_id))
            await db.commit()

    # 9.3 知识库公开冲突检查必须包含 is_active=False 的排队文档
    temp_kb_id = uuid.uuid4()
    queued_l2_doc_id = uuid.uuid4()
    admin_tok = await _mint_token_for_user(manifest, "sys_ops_admin")
    try:
        async with AsyncSessionLocal() as db:
            db.add(
                KnowledgeBase(
                    id=temp_kb_id,
                    tenant_id=tenant_id,
                    name=f"排队文档公开检查库_{temp_kb_id.hex[:6]}",
                    owner_department_id=tech_dept_id,
                    owner_dept_path="/公司/技术部",
                    visibility="DEPARTMENT",
                    min_classification_level=1,
                    default_doc_level=2,
                    max_doc_level=3,
                    status="ACTIVE",
                )
            )
            await db.flush()
            db.add(
                Document(
                    id=queued_l2_doc_id,
                    tenant_id=tenant_id,
                    kb_id=temp_kb_id,
                    classification_level=2,
                    created_by=manifest["users"]["tech_staff"]["id"],
                    title="排队中L2文档.md",
                    file_hash=f"queued_{queued_l2_doc_id.hex}",
                    raw_oss_url="https://oss.invalid/queued_l2.md",
                    department_id=tech_dept_id,
                    department_path="/公司/技术部",
                    category="技术规范",
                    tags=[],
                    overall_status="queued",
                    is_active=False,
                )
            )
            await db.commit()

        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            r_pub = await client.patch(
                f"/api/v1/admin/knowledge-bases/{temp_kb_id}",
                json={
                    "visibility": "PUBLIC",
                    "minClassificationLevel": 1,
                    "defaultDocLevel": 1,
                    "maxDocLevel": 1,
                    "reason": "尝试将含排队L2文档的库改为L1公开库",
                },
                headers={"Authorization": f"Bearer {admin_tok}"},
            )
            assert r_pub.status_code == 409, f"存在排队 L2 文档时必须 409 阻断公开降级，实际: {r_pub.status_code} {r_pub.text}"
    finally:
        async with AsyncSessionLocal() as db:
            await db.execute(delete(Document).where(Document.id == queued_l2_doc_id))
            await db.execute(delete(KnowledgeBase).where(KnowledgeBase.id == temp_kb_id))
            await db.commit()

    # 9.4 建库管理范围严格排除 CUSTOM_ACL
    custom_acl_role = f"custom_mgr_{uuid.uuid4().hex[:6]}"
    try:
        async with AsyncSessionLocal() as db:
            db.add(
                Role(
                    tenant_id=tenant_id,
                    role_code=custom_acl_role,
                    role_name="仅指定库管理角色",
                    data_scope="CUSTOM_ACL",
                    permissions=["kb:retrieve", "kb:manage"],
                )
            )
            await db.commit()
            staff_user = await db.get(SysUser, uuid.UUID(manifest["users"]["tech_staff"]["id"]))
            epoch = (
                await db.execute(select(TenantPolicyEpoch.policy_epoch).where(TenantPolicyEpoch.tenant_id == tenant_id))
            ).scalar_one()

        custom_mgr_tok = make_rs256_test_token(
            user_id=manifest["users"]["tech_staff"]["id"],
            tenant_id=tenant_id,
            username="仅指定库管理者",
            dept_path="/公司/技术部",
            dept_paths=["/公司/技术部"],
            roles=[custom_acl_role],
            position_code="STAFF",
            clearance_level=2,
            authz_ver=int(staff_user.authz_version),
            policy_epoch=int(epoch),
        )
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            r_create = await client.post(
                "/api/v1/admin/knowledge-bases",
                json={
                    "name": f"越权自建受限库_{uuid.uuid4().hex[:6]}",
                    "ownerDeptPath": "/公司/技术部",
                    "visibility": "RESTRICTED",
                    "minClassificationLevel": 1,
                    "defaultDocLevel": 2,
                    "maxDocLevel": 2,
                },
                headers={"Authorization": f"Bearer {custom_mgr_tok}"},
            )
            assert r_create.status_code == 403, f"CUSTOM_ACL 角色严禁创建知识库，实际: {r_create.status_code} {r_create.text}"
    finally:
        async with AsyncSessionLocal() as db:
            await db.execute(delete(Role).where(Role.tenant_id == tenant_id, Role.role_code == custom_acl_role))
            await db.commit()
        clear_authz_cache()

    # 9.5 重复文档告警按接收者权限实时渲染：L2 订阅者看不到 L4 命中文档真实标题，L4 订阅者可见
    titan_doc_id = docs["l4_tech_titan"]["doc_id"]
    shared_event = {
        "event": "duplicate_warning",
        "doc_id": str(uuid.uuid4()),
        "similarity": 0.95,
        "matched_doc_id": titan_doc_id,
        "matched_title": REDACTED_HIGH_CLASSIFICATION_TITLE,
        "can_overwrite": False,
        "is_higher_classification": True,
        "message": "检测到知识库内存在高度相似文档 (相似度 95.00%)，请确认处理方式。",
    }
    staff_tok = await _mint_token_for_user(manifest, "tech_staff")
    exec_tok = await _mint_token_for_user(manifest, "corp_exec")
    staff_ctx = await get_current_user(authorization=f"Bearer {staff_tok}")
    exec_ctx = await get_current_user(authorization=f"Bearer {exec_tok}")
    async with AsyncSessionLocal() as db:
        rendered_l2 = await render_duplicate_warning_for_viewer(db, staff_ctx, shared_event)
        assert rendered_l2["matched_title"] == REDACTED_HIGH_CLASSIFICATION_TITLE
        assert rendered_l2["can_overwrite"] is False
        assert "Project-Titan" not in json.dumps(rendered_l2, ensure_ascii=False)

        rendered_l4 = await render_duplicate_warning_for_viewer(db, exec_ctx, shared_event)
        assert rendered_l4["matched_title"] == docs["l4_tech_titan"]["file_name"]
        assert rendered_l4["can_overwrite"] is True

    # 9.6 最终入库删文档/删库后 Fail-Fast 终止，严禁改投同部门其他知识库或重建已删文档
    deleted_doc_id = uuid.uuid4()
    deleted_kb_id = uuid.uuid4()
    dummy_vec = [0.01] * 1024
    dummy_chunk = RawChunkData(
        chunk_index=0,
        content="已删除目标测试切片",
        enriched_content="已删除目标测试切片",
        heading_path="# 测试",
        chunk_type="text",
        page_num=1,
    )
    store_state = get_default_ingest_state(
        doc_id=str(deleted_doc_id),
        file_name="已删目标文档.md",
        department="/公司/技术部",
        tenant_id=tenant_id,
        kb_id=str(deleted_kb_id),
        classification_level=2,
        uploader_clearance=2,
    )
    store_state["raw_chunks"] = [dummy_chunk]
    store_state["dense_embeddings"] = [dummy_vec]
    store_state["sparse_vectors"] = [{"1": 1.0}]
    store_state["tsv_strings"] = ["测试 切片"]

    with pytest.raises(RuntimeError, match="已被删除"):
        await StoreNode().process(store_state)

    async with AsyncSessionLocal() as db:
        assert await db.get(Document, deleted_doc_id) is None
        with pytest.raises(RuntimeError, match="已不存在"):
            await update_service.ingest_document_with_chunks(
                session=db,
                doc_id=deleted_doc_id,
                file_name="已删目标文档.md",
                file_hash=f"del_{deleted_doc_id.hex}",
                raw_oss_url="https://oss.invalid/del.md",
                enhanced_md_oss_url=None,
                department_id=tech_dept_id,
                department_path="/公司/技术部",
                category="技术规范",
                tags=[],
                title_embedding=dummy_vec,
                chunks=[dummy_chunk],
                dense_embeddings=[dummy_vec],
                sparse_vectors=[{"1": 1.0}],
                tsv_strings=["测试 切片"],
                tenant_id=tenant_id,
                kb_id=tech_kb_id,
                classification_level=2,
                created_by=manifest["users"]["tech_staff"]["id"],
                uploader_clearance=2,
            )

    # 9.7 仅有 doc:upload 无 kb:retrieve 的角色可拉取 action=UPLOAD 目标库列表，但 action=READ 必须 403
    upload_only_role = f"upload_only_{uuid.uuid4().hex[:6]}"
    try:
        async with AsyncSessionLocal() as db:
            db.add(
                Role(
                    tenant_id=tenant_id,
                    role_code=upload_only_role,
                    role_name="仅上传专员",
                    data_scope="DEPT_AND_SUB",
                    permissions=["doc:upload"],
                )
            )
            await db.commit()
            staff_user = await db.get(SysUser, uuid.UUID(manifest["users"]["tech_staff"]["id"]))
            epoch = (
                await db.execute(select(TenantPolicyEpoch.policy_epoch).where(TenantPolicyEpoch.tenant_id == tenant_id))
            ).scalar_one()

        upload_only_tok = make_rs256_test_token(
            user_id=manifest["users"]["tech_staff"]["id"],
            tenant_id=tenant_id,
            username="仅上传专员",
            dept_path="/公司/技术部",
            dept_paths=["/公司/技术部"],
            roles=[upload_only_role],
            position_code="STAFF",
            clearance_level=2,
            authz_ver=int(staff_user.authz_version),
            policy_epoch=int(epoch),
        )
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            r_up = await client.get(
                "/api/v1/knowledge-bases?action=UPLOAD",
                headers={"Authorization": f"Bearer {upload_only_tok}"},
            )
            assert r_up.status_code == 200, f"仅有 doc:upload 请求 action=UPLOAD 应 200，实际: {r_up.status_code} {r_up.text}"
            assert any(item["id"] == str(tech_kb_id) for item in r_up.json())

            r_rd = await client.get(
                "/api/v1/knowledge-bases?action=READ",
                headers={"Authorization": f"Bearer {upload_only_tok}"},
            )
            assert r_rd.status_code == 403, f"无 kb:retrieve 请求 action=READ 应 403，实际: {r_rd.status_code}"
    finally:
        async with AsyncSessionLocal() as db:
            await db.execute(delete(Role).where(Role.tenant_id == tenant_id, Role.role_code == upload_only_role))
            await db.commit()
        clear_authz_cache()


@pytest.mark.asyncio
async def test_10_restricted_kb_creator_full_access_and_scoped_grant(local_rsa_jwt_env):
    """
    受限知识库创建者开箱全权限与部门经理分层授权闭环回归：
    10.1 dept_manager (tech_manager) 创建 RESTRICTED 库后，自动获得 MANAGE/READ/UPLOAD/REVIEW/DELETE，无需管理员代授权即可上传与检索；
    10.2 dept_manager 可按 USER 跨部门拉协作成员 (READ/UPLOAD)，且可按本部门/下级子部门批量授权；
    10.3 dept_manager 企图跨部门授权 DEPARTMENT、按 ROLE 批量授权、或跨部门转授 MANAGE 时必须 403 拦截；
    10.4 dept_manager 企图撤销自身在 RESTRICTED 库上的唯一 MANAGE 授权时必须 409 防自锁；
    10.5 无 doc:upload/doc:delete 的 admin 创建 RESTRICTED 库后，不得越权获授 UPLOAD/REVIEW/DELETE。
    """
    manifest = _load_manifest()
    tenant_id = manifest["tenant_id"]
    depts = manifest["departments"]
    tech_dept_id = depts["tech"]["id"]
    tech_rd1_id = depts["tech_rd1"]["id"]
    finance_dept_id = depts["finance"]["id"]
    exec_uid = manifest["users"]["corp_exec"]["id"]

    app = _build_test_app()
    transport = httpx.ASGITransport(app=app)
    created_kb_ids: List[uuid.UUID] = []

    try:
        mgr_tok = await _mint_token_for_user(manifest, "tech_manager")
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            # 10.1 tech_manager 创建「数字化」RESTRICTED 库
            r_create = await client.post(
                "/api/v1/admin/knowledge-bases",
                json={
                    "name": f"数字化项目专库_{uuid.uuid4().hex[:6]}",
                    "ownerDepartmentId": tech_dept_id,
                    "visibility": "RESTRICTED",
                    "minClassificationLevel": 2,
                    "defaultDocLevel": 2,
                    "maxDocLevel": 3,
                    "reason": "部门经理自建项目协作库",
                },
                headers={"Authorization": f"Bearer {mgr_tok}"},
            )
            assert r_create.status_code == 201, r_create.text
            kb_id_str = r_create.json()["id"]
            kb_uuid = uuid.UUID(kb_id_str)
            created_kb_ids.append(kb_uuid)

            # 策略版本推进后换新令牌，直接校验个人可用目录与五动作能力矩阵
            mgr_tok2 = await _mint_token_for_user(manifest, "tech_manager")
            r_up_list = await client.get(
                "/api/v1/knowledge-bases?action=UPLOAD",
                headers={"Authorization": f"Bearer {mgr_tok2}"},
            )
            assert r_up_list.status_code == 200, r_up_list.text
            hit_up = [x for x in r_up_list.json() if x["id"] == kb_id_str]
            assert len(hit_up) == 1, "受限库创建者必须立即出现在 action=UPLOAD 目录中"
            assert hit_up[0]["can_read"] is True
            assert hit_up[0]["can_upload"] is True
            assert hit_up[0]["can_review"] is True
            assert hit_up[0]["can_delete"] is True
            assert hit_up[0]["can_manage"] is True

            r_rd_list = await client.get(
                "/api/v1/knowledge-bases?action=READ",
                headers={"Authorization": f"Bearer {mgr_tok2}"},
            )
            assert r_rd_list.status_code == 200
            assert any(x["id"] == kb_id_str for x in r_rd_list.json()), "受限库创建者必须立即具备 READ 检索权限"

            mgr_ctx = await get_current_user(authorization=f"Bearer {mgr_tok2}")
            async with AsyncSessionLocal() as db:
                resolved_kb, resolved_lvl = await resolve_authorized_target_kb(
                    db, mgr_ctx, kb_id=kb_id_str, classification_level=2
                )
                assert resolved_kb.id == kb_uuid
                assert resolved_lvl == 2

            r_grants = await client.get(
                f"/api/v1/admin/knowledge-bases/{kb_id_str}/grants",
                headers={"Authorization": f"Bearer {mgr_tok2}"},
            )
            assert r_grants.status_code == 200
            grant_rows = r_grants.json()
            assert {g["action"] for g in grant_rows} == {"MANAGE", "READ", "UPLOAD", "REVIEW", "DELETE"}
            self_manage_grant = next(g for g in grant_rows if g["action"] == "MANAGE")

            # 10.2 允许按 USER 跨部门点对点拉入协作人员 (corp_exec 归属 /公司/总裁办) + 允许本部门下级子部门授权
            r_cross_user_read = await client.post(
                f"/api/v1/admin/knowledge-bases/{kb_id_str}/grants",
                json={
                    "subjectType": "USER",
                    "subjectId": exec_uid,
                    "action": "READ",
                    "effect": "ALLOW",
                    "reason": "项目A跨部门单点协同查阅",
                },
                headers={"Authorization": f"Bearer {mgr_tok2}"},
            )
            assert r_cross_user_read.status_code == 201, r_cross_user_read.text

            exec_tok = await _mint_token_for_user(manifest, "corp_exec")
            r_exec_rd = await client.get(
                "/api/v1/knowledge-bases?action=READ",
                headers={"Authorization": f"Bearer {exec_tok}"},
            )
            assert r_exec_rd.status_code == 200
            assert any(x["id"] == kb_id_str for x in r_exec_rd.json())

            mgr_tok_sub = await _mint_token_for_user(manifest, "tech_manager")
            r_sub_dept = await client.post(
                f"/api/v1/admin/knowledge-bases/{kb_id_str}/grants",
                json={
                    "subjectType": "DEPARTMENT",
                    "subjectId": tech_rd1_id,
                    "includeSubDepts": True,
                    "action": "UPLOAD",
                    "effect": "ALLOW",
                    "reason": "授权下级研发一组上传资料",
                },
                headers={"Authorization": f"Bearer {mgr_tok_sub}"},
            )
            assert r_sub_dept.status_code == 201, r_sub_dept.text

            # 10.3 越权护栏：禁止跨部门 DEPARTMENT、禁止 ROLE、禁止跨部门 USER MANAGE
            mgr_tok3 = await _mint_token_for_user(manifest, "tech_manager")
            r_cross_dept = await client.post(
                f"/api/v1/admin/knowledge-bases/{kb_id_str}/grants",
                json={
                    "subjectType": "DEPARTMENT",
                    "subjectId": finance_dept_id,
                    "action": "READ",
                    "effect": "ALLOW",
                    "reason": "企图整部门对外批量共享",
                },
                headers={"Authorization": f"Bearer {mgr_tok3}"},
            )
            assert r_cross_dept.status_code == 403, r_cross_dept.text

            r_role_grant = await client.post(
                f"/api/v1/admin/knowledge-bases/{kb_id_str}/grants",
                json={
                    "subjectType": "ROLE",
                    "subjectId": "test_reader",
                    "action": "READ",
                    "effect": "ALLOW",
                    "reason": "企图按全租户角色批量授权",
                },
                headers={"Authorization": f"Bearer {mgr_tok3}"},
            )
            assert r_role_grant.status_code == 403, r_role_grant.text

            r_cross_manage = await client.post(
                f"/api/v1/admin/knowledge-bases/{kb_id_str}/grants",
                json={
                    "subjectType": "USER",
                    "subjectId": exec_uid,
                    "action": "MANAGE",
                    "effect": "ALLOW",
                    "reason": "企图跨部门转授管理权",
                },
                headers={"Authorization": f"Bearer {mgr_tok3}"},
            )
            assert r_cross_manage.status_code == 403, r_cross_manage.text

            # 10.4 防自锁：禁止撤销自身在 RESTRICTED 库上的唯一 MANAGE 授权
            r_self_lock = await client.delete(
                f"/api/v1/admin/knowledge-bases/{kb_id_str}/grants/{self_manage_grant['id']}",
                headers={"Authorization": f"Bearer {mgr_tok3}"},
            )
            assert r_self_lock.status_code == 409, r_self_lock.text

            # 10.5 职责分离校验：仅有 kb:manage/kb:retrieve 而无 doc:upload/doc:delete 的 admin 建库不越权得 UPLOAD
            ops_tok = await _mint_token_for_user(manifest, "sys_ops_admin")
            r_ops_kb = await client.post(
                "/api/v1/admin/knowledge-bases",
                json={
                    "name": f"运维职责分离受限库_{uuid.uuid4().hex[:6]}",
                    "ownerDepartmentId": tech_dept_id,
                    "visibility": "RESTRICTED",
                    "minClassificationLevel": 1,
                    "defaultDocLevel": 2,
                    "maxDocLevel": 2,
                },
                headers={"Authorization": f"Bearer {ops_tok}"},
            )
            assert r_ops_kb.status_code == 201, r_ops_kb.text
            ops_kb_id = r_ops_kb.json()["id"]
            created_kb_ids.append(uuid.UUID(ops_kb_id))

            ops_tok2 = await _mint_token_for_user(manifest, "sys_ops_admin")
            r_ops_grants = await client.get(
                f"/api/v1/admin/knowledge-bases/{ops_kb_id}/grants",
                headers={"Authorization": f"Bearer {ops_tok2}"},
            )
            assert r_ops_grants.status_code == 200
            assert {g["action"] for g in r_ops_grants.json()} == {"MANAGE", "READ"}
    finally:
        if created_kb_ids:
            async with AsyncSessionLocal() as db:
                await db.execute(delete(KnowledgeBase).where(KnowledgeBase.id.in_(created_kb_ids)))
                await db.commit()
        clear_authz_cache()


@pytest.mark.asyncio
async def test_11_kb_grant_review_fixes_and_concurrency(local_rsa_jwt_env):
    """
    知识库部门授权复审修复项与 DEPT_ONLY 下线回归：
    11.1 下线 DEPT_ONLY：角色策略接口拒绝 dataScope=DEPT_ONLY (400)；
    11.2 撤销禁令越权拦截：部门经理不可撤销外部门 DENY、全租户 ROLE 规则或跨部门 USER MANAGE，但可撤销跨部门 USER 业务授权；
    11.3 已停用部门不计入内部归属：DISABLED 子部门与 sys_users.dept_path 展示字段不得放行跨部门 MANAGE 转授，且不出现在有效归属列表中；
    11.4 新增 DENY MANAGE 防自锁：在 RESTRICTED 与 DEPARTMENT 库上新增命中操作者自身的 DENY MANAGE 必须 409 拦截；
    11.5 并发双删防孤库：同时撤销个人与部门两条 ALLOW MANAGE 时，库级行锁保证最多成功一条，经理保留管理权。
    """
    manifest = _load_manifest()
    tenant_id = manifest["tenant_id"]
    depts = manifest["departments"]
    kbs = manifest["knowledge_bases"]
    tech_dept_id = depts["tech"]["id"]
    finance_dept_id = depts["finance"]["id"]
    exec_uid = manifest["users"]["corp_exec"]["id"]
    mgr_uid = manifest["users"]["tech_manager"]["id"]
    tech_kb_id = kbs["tech_kb"]["id"]

    app = _build_test_app()
    transport = httpx.ASGITransport(app=app)
    created_kb_ids: List[uuid.UUID] = []
    created_dept_ids: List[uuid.UUID] = []
    created_user_ids: List[uuid.UUID] = []

    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            # 11.1 DEPT_ONLY 已下线，拒绝写入
            ops_tok = await _mint_token_for_user(manifest, "sys_ops_admin")
            r_bad_scope = await client.put(
                "/api/v1/admin/roles",
                json={
                    "roleCode": f"legacy_dept_only_{uuid.uuid4().hex[:6]}",
                    "roleName": "废弃范围角色",
                    "dataScope": "DEPT_ONLY",
                    "permissions": ["kb:retrieve"],
                    "allowedPositions": ["*"],
                    "allowedDeptPrefixes": ["*"],
                    "primaryDeptOnly": False,
                },
                headers={"Authorization": f"Bearer {ops_tok}"},
            )
            assert r_bad_scope.status_code == 400, r_bad_scope.text

            # 创建测试用 RESTRICTED 库
            mgr_tok = await _mint_token_for_user(manifest, "tech_manager")
            r_create = await client.post(
                "/api/v1/admin/knowledge-bases",
                json={
                    "name": f"复审回归专库_{uuid.uuid4().hex[:6]}",
                    "ownerDepartmentId": tech_dept_id,
                    "visibility": "RESTRICTED",
                    "minClassificationLevel": 2,
                    "defaultDocLevel": 2,
                    "maxDocLevel": 3,
                },
                headers={"Authorization": f"Bearer {mgr_tok}"},
            )
            assert r_create.status_code == 201, r_create.text
            kb_id_str = r_create.json()["id"]
            kb_uuid = uuid.UUID(kb_id_str)
            created_kb_ids.append(kb_uuid)

            # 11.2 管理员配置 ROLE / 外部门 DENY / 跨部门 USER MANAGE，部门经理均不可撤销
            ops_tok = await _mint_token_for_user(manifest, "sys_ops_admin")
            r_role_allow = await client.post(
                f"/api/v1/admin/knowledge-bases/{kb_id_str}/grants",
                json={"subjectType": "ROLE", "subjectId": "test_reader", "action": "READ", "effect": "ALLOW"},
                headers={"Authorization": f"Bearer {ops_tok}"},
            )
            assert r_role_allow.status_code == 201, r_role_allow.text
            role_grant_id = r_role_allow.json()["id"]

            ops_tok = await _mint_token_for_user(manifest, "sys_ops_admin")
            r_fin_deny = await client.post(
                f"/api/v1/admin/knowledge-bases/{kb_id_str}/grants",
                json={"subjectType": "DEPARTMENT", "subjectId": finance_dept_id, "action": "READ", "effect": "DENY"},
                headers={"Authorization": f"Bearer {ops_tok}"},
            )
            assert r_fin_deny.status_code == 201, r_fin_deny.text
            fin_deny_grant_id = r_fin_deny.json()["id"]

            ops_tok = await _mint_token_for_user(manifest, "sys_ops_admin")
            r_ext_manage = await client.post(
                f"/api/v1/admin/knowledge-bases/{kb_id_str}/grants",
                json={"subjectType": "USER", "subjectId": exec_uid, "action": "MANAGE", "effect": "ALLOW"},
                headers={"Authorization": f"Bearer {ops_tok}"},
            )
            assert r_ext_manage.status_code == 201, r_ext_manage.text
            ext_manage_grant_id = r_ext_manage.json()["id"]

            mgr_tok = await _mint_token_for_user(manifest, "tech_manager")
            r_del_fin_deny = await client.delete(
                f"/api/v1/admin/knowledge-bases/{kb_id_str}/grants/{fin_deny_grant_id}",
                headers={"Authorization": f"Bearer {mgr_tok}"},
            )
            assert r_del_fin_deny.status_code == 403, r_del_fin_deny.text

            r_del_role = await client.delete(
                f"/api/v1/admin/knowledge-bases/{kb_id_str}/grants/{role_grant_id}",
                headers={"Authorization": f"Bearer {mgr_tok}"},
            )
            assert r_del_role.status_code == 403, r_del_role.text

            r_del_ext_manage = await client.delete(
                f"/api/v1/admin/knowledge-bases/{kb_id_str}/grants/{ext_manage_grant_id}",
                headers={"Authorization": f"Bearer {mgr_tok}"},
            )
            assert r_del_ext_manage.status_code == 403, r_del_ext_manage.text

            # 跨部门 USER 业务授权 (READ) 仍允许新增与撤销
            r_ext_read = await client.post(
                f"/api/v1/admin/knowledge-bases/{kb_id_str}/grants",
                json={"subjectType": "USER", "subjectId": exec_uid, "action": "READ", "effect": "ALLOW"},
                headers={"Authorization": f"Bearer {mgr_tok}"},
            )
            assert r_ext_read.status_code == 201, r_ext_read.text
            ext_read_grant_id = r_ext_read.json()["id"]

            mgr_tok = await _mint_token_for_user(manifest, "tech_manager")
            r_del_ext_read = await client.delete(
                f"/api/v1/admin/knowledge-bases/{kb_id_str}/grants/{ext_read_grant_id}",
                headers={"Authorization": f"Bearer {mgr_tok}"},
            )
            assert r_del_ext_read.status_code == 200, r_del_ext_read.text

            # 11.3 已停用部门不得被算作内部归属，且其 DISABLED 主部门不回退猜成副部门
            disabled_dept_id = uuid.uuid4()
            disabled_dept_path = f"/公司/技术部/已停用测试组_{uuid.uuid4().hex[:6]}"
            temp_user_id = uuid.uuid4()
            created_dept_ids.append(disabled_dept_id)
            created_user_ids.append(temp_user_id)
            async with AsyncSessionLocal() as db:
                db.add(
                    Department(
                        id=disabled_dept_id,
                        tenant_id=tenant_id,
                        parent_id=uuid.UUID(tech_dept_id),
                        name="已停用测试组",
                        path=disabled_dept_path,
                        sort_order=99,
                        status="DISABLED",
                    )
                )
                db.add(
                    SysUser(
                        id=temp_user_id,
                        tenant_id=tenant_id,
                        external_id=f"disabled_dept_user_{uuid.uuid4().hex[:6]}",
                        id_source="test",
                        real_name="停用部门调岗员工",
                        is_active=True,
                        position_code="STAFF",
                        authz_version=1,
                    )
                )
                await db.flush()
                db.add_all([
                    UserDepartment(
                        id=uuid.uuid4(),
                        tenant_id=tenant_id,
                        user_id=temp_user_id,
                        department_id=disabled_dept_id,
                        dept_path=disabled_dept_path,
                        is_primary=True,
                    ),
                    UserDepartment(
                        id=uuid.uuid4(),
                        tenant_id=tenant_id,
                        user_id=temp_user_id,
                        department_id=uuid.UUID(finance_dept_id),
                        dept_path="/公司/财务部",
                        is_primary=False,
                    ),
                ])
                await db.commit()

            mgr_tok = await _mint_token_for_user(manifest, "tech_manager")
            r_disabled_manage = await client.post(
                f"/api/v1/admin/knowledge-bases/{kb_id_str}/grants",
                json={"subjectType": "USER", "subjectId": str(temp_user_id), "action": "MANAGE", "effect": "ALLOW"},
                headers={"Authorization": f"Bearer {mgr_tok}"},
            )
            assert r_disabled_manage.status_code == 403, r_disabled_manage.text

            r_disabled_dept_grant = await client.post(
                f"/api/v1/admin/knowledge-bases/{kb_id_str}/grants",
                json={"subjectType": "DEPARTMENT", "subjectId": str(disabled_dept_id), "action": "READ", "effect": "ALLOW"},
                headers={"Authorization": f"Bearer {mgr_tok}"},
            )
            assert r_disabled_dept_grant.status_code == 404, r_disabled_dept_grant.text

            r_users = await client.get(
                "/api/v1/admin/users",
                headers={"Authorization": f"Bearer {mgr_tok}"},
            )
            assert r_users.status_code == 200, r_users.text
            temp_user_row = next(u for u in r_users.json() if u["userId"] == str(temp_user_id))
            assert temp_user_row["effectiveDeptPaths"] == ["/公司/财务部"]
            assert temp_user_row["primaryDeptPath"] == ""

            # 11.4 新增命中自身的 DENY MANAGE 防自锁（RESTRICTED 与 DEPARTMENT 均拦截）
            r_self_deny_dept = await client.post(
                f"/api/v1/admin/knowledge-bases/{kb_id_str}/grants",
                json={"subjectType": "DEPARTMENT", "subjectId": tech_dept_id, "action": "MANAGE", "effect": "DENY"},
                headers={"Authorization": f"Bearer {mgr_tok}"},
            )
            assert r_self_deny_dept.status_code == 409, r_self_deny_dept.text

            r_self_deny_user = await client.post(
                f"/api/v1/admin/knowledge-bases/{kb_id_str}/grants",
                json={"subjectType": "USER", "subjectId": mgr_uid, "action": "MANAGE", "effect": "DENY"},
                headers={"Authorization": f"Bearer {mgr_tok}"},
            )
            assert r_self_deny_user.status_code == 409, r_self_deny_user.text

            r_dept_kb_self_deny = await client.post(
                f"/api/v1/admin/knowledge-bases/{tech_kb_id}/grants",
                json={"subjectType": "DEPARTMENT", "subjectId": tech_dept_id, "action": "MANAGE", "effect": "DENY"},
                headers={"Authorization": f"Bearer {mgr_tok}"},
            )
            assert r_dept_kb_self_deny.status_code == 409, r_dept_kb_self_deny.text

            # 11.5 并发同时撤销个人与部门两条 ALLOW MANAGE：最多成功一条，必须保底留存管理权
            r_grants = await client.get(
                f"/api/v1/admin/knowledge-bases/{kb_id_str}/grants",
                headers={"Authorization": f"Bearer {mgr_tok}"},
            )
            assert r_grants.status_code == 200
            personal_manage_id = next(
                g["id"] for g in r_grants.json()
                if g["subjectType"] == "USER" and g["subjectId"] == mgr_uid and g["action"] == "MANAGE" and g["effect"] == "ALLOW"
            )

            r_dept_manage = await client.post(
                f"/api/v1/admin/knowledge-bases/{kb_id_str}/grants",
                json={"subjectType": "DEPARTMENT", "subjectId": tech_dept_id, "action": "MANAGE", "effect": "ALLOW"},
                headers={"Authorization": f"Bearer {mgr_tok}"},
            )
            assert r_dept_manage.status_code == 201, r_dept_manage.text
            dept_manage_id = r_dept_manage.json()["id"]

            mgr_tok_race = await _mint_token_for_user(manifest, "tech_manager")
            mgr_ctx_race = await get_current_user(authorization=f"Bearer {mgr_tok_race}")
            from rag_kb.api.admin import delete_kb_grant as _delete_kb_grant_handler

            async def _invoke_delete_in_own_session(target_gid: str) -> int:
                async with AsyncSessionLocal() as race_db:
                    try:
                        await _delete_kb_grant_handler(
                            kb_id=kb_id_str,
                            grant_id=target_gid,
                            db=race_db,
                            user=mgr_ctx_race,
                        )
                        return 200
                    except HTTPException as exc:
                        return exc.status_code

            code_a, code_b = await asyncio.gather(
                _invoke_delete_in_own_session(personal_manage_id),
                _invoke_delete_in_own_session(dept_manage_id),
            )
            assert sorted([code_a, code_b]) == [200, 409], (
                f"并发撤销两条 MANAGE 在库级行锁保护下必须恰好 1 条成功 (200) + 1 条拦截 (409)，实际: {[code_a, code_b]}"
            )

            async with AsyncSessionLocal() as db:
                rem_ids = (
                    await db.execute(
                        select(KbGrant.id).where(
                            KbGrant.tenant_id == tenant_id,
                            KbGrant.kb_id == kb_uuid,
                            KbGrant.id.in_([uuid.UUID(personal_manage_id), uuid.UUID(dept_manage_id)]),
                        )
                    )
                ).scalars().all()
                assert len(rem_ids) == 1, f"并发撤销后应恰好保留 1 条 MANAGE 授权，实际剩余: {len(rem_ids)}"

            mgr_tok_after = await _mint_token_for_user(manifest, "tech_manager")
            r_still_ok = await client.get(
                f"/api/v1/admin/knowledge-bases/{kb_id_str}/grants",
                headers={"Authorization": f"Bearer {mgr_tok_after}"},
            )
            assert r_still_ok.status_code == 200, r_still_ok.text
    finally:
        async with AsyncSessionLocal() as db:
            if created_kb_ids:
                await db.execute(delete(KnowledgeBase).where(KnowledgeBase.id.in_(created_kb_ids)))
            if created_user_ids:
                await db.execute(delete(SysUser).where(SysUser.id.in_(created_user_ids)))
            if created_dept_ids:
                await db.execute(delete(Department).where(Department.id.in_(created_dept_ids)))
            await db.commit()
        clear_authz_cache()


@pytest.mark.asyncio
async def test_12_human_review_single_interrupt_and_no_second_popup(local_rsa_jwt_env):
    """
    第 7 步人工审核单次弹窗防重回归：
    12.1 LangGraph ReviewNode 在首次 interrupt 时仅通过 __interrupt__ 产出 review_required，Command(resume) 重放节点时绝不再触发 review_required，且输出 overall_status=running；
    12.2 POST /{doc_id}/review 返回 200 前即同步将内存状态切出 pending_review，即使后台恢复协程尚未启动，紧接着拉取 GET /{doc_id}/status 的 requires_review 也必须为 False，杜绝二次弹窗。
    """
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import Command
    from rag_kb.api.sse import pipeline_manager
    from rag_kb.core.constants import IngestStatus
    from rag_kb.graph.state import IngestState, get_default_state
    from rag_kb.nodes.review import review_node

    # 12.1 验证真实 LangGraph ReviewNode 的 interrupt + Command(resume) 事件流
    mini_graph_builder = StateGraph(IngestState)
    mini_graph_builder.add_node("review", review_node)
    mini_graph_builder.add_edge(START, "review")
    mini_graph_builder.add_edge("review", END)
    mini_graph = mini_graph_builder.compile(checkpointer=MemorySaver())

    doc_id_graph = str(uuid.uuid4())
    init_state = get_default_state(doc_id=doc_id_graph, file_name="review_once.md", department="/公司/技术部")
    init_state["pending_reviews"] = [
        {
            "item_id": "tbl_once_0",
            "doc_id": doc_id_graph,
            "type": "table",
            "title": "核对表",
            "page_idx": 0,
            "display_page": 1,
            "asset_url": "",
            "vlm_description": "初版描述",
            "user_description": "初版描述",
            "status": "pending",
        }
    ]
    cfg = {"configurable": {"thread_id": doc_id_graph}}

    run1_custom: List[Dict[str, Any]] = []
    run1_interrupts: List[Any] = []
    async for mode, chunk in mini_graph.astream(init_state, config=cfg, stream_mode=["custom", "values"]):
        if mode == "custom" and isinstance(chunk, dict):
            run1_custom.append(chunk)
        elif mode == "values" and isinstance(chunk, dict) and chunk.get("__interrupt__"):
            run1_interrupts.extend(chunk["__interrupt__"])

    assert not any(e.get("event") == "review_required" for e in run1_custom), (
        f"ReviewNode 禁止在 interrupt 前通过 custom writer 广播 review_required: {run1_custom}"
    )
    assert len(run1_interrupts) == 1
    intr_payload = getattr(run1_interrupts[0], "value", run1_interrupts[0])
    assert intr_payload["event"] == "review_required"
    assert len(intr_payload["pending_reviews"]) == 1
    assert "items" not in intr_payload

    resume_custom: List[Dict[str, Any]] = []
    resume_interrupts: List[Any] = []
    final_state: Dict[str, Any] = {}
    async for mode, chunk in mini_graph.astream(
        Command(resume=[{"item_id": "tbl_once_0", "user_description": "人工修正描述", "remark": "已核对"}]),
        config=cfg,
        stream_mode=["custom", "values"],
    ):
        if mode == "custom" and isinstance(chunk, dict):
            resume_custom.append(chunk)
        elif mode == "values" and isinstance(chunk, dict):
            if chunk.get("__interrupt__"):
                resume_interrupts.extend(chunk["__interrupt__"])
            final_state = chunk

    assert not any(e.get("event") == "review_required" for e in resume_custom), (
        f"Command(resume) 重放期间绝不可再次广播 review_required: {resume_custom}"
    )
    assert len(resume_interrupts) == 0
    assert final_state.get("overall_status") == "running"
    assert final_state["pending_reviews"][0]["user_description"] == "人工修正描述"
    assert final_state["pending_reviews"][0]["status"] == "modified"

    # 12.2 验证 POST /review 同步切状态，阻断紧邻 GET /status 快照回弹二次弹窗
    manifest = _load_manifest()
    tenant_id = manifest["tenant_id"]
    tech_kb = manifest["knowledge_bases"]["tech_kb"]
    mgr_tok = await _mint_token_for_user(manifest, "tech_manager")

    doc_id_api = str(uuid.uuid4())
    api_state = get_default_state(
        doc_id=doc_id_api,
        file_name="review_api_race.md",
        department=tech_kb["owner_dept_path"],
        tenant_id=tenant_id,
        kb_id=tech_kb["id"],
        kb_name=tech_kb["name"],
        classification_level=2,
    )
    api_state["overall_status"] = IngestStatus.REVIEW.value
    api_state["current_step_index"] = 7
    api_state["pending_reviews"] = [
        {
            "item_id": "tbl_api_0",
            "doc_id": doc_id_api,
            "type": "table",
            "title": "核对表",
            "page_idx": 0,
            "display_page": 1,
            "asset_url": "",
            "vlm_description": "初始描述",
            "user_description": "初始描述",
            "status": "pending",
        }
    ]
    pipeline_manager.register_pipeline(doc_id_api, api_state)

    scheduled_coros: List[Any] = []
    app = _build_test_app()
    transport = httpx.ASGITransport(app=app)
    try:
        with patch("rag_kb.api.routes.ingest_queue_service.schedule_background_resume", side_effect=scheduled_coros.append):
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                r_submit = await client.post(
                    f"/api/v1/documents/{doc_id_api}/review",
                    json={
                        "doc_id": doc_id_api,
                        "items": [
                            {
                                "item_id": "tbl_api_0",
                                "user_description": "审核确认后的描述",
                                "remark": "确认无误",
                                "approved": True,
                            }
                        ],
                    },
                    headers={"Authorization": f"Bearer {mgr_tok}"},
                )
                assert r_submit.status_code == 200, r_submit.text
                assert len(scheduled_coros) == 1

                # 模拟前端在后台协程尚未运行前立即调用 GET /status 刷新快照：绝不能返回 requires_review=True
                r_snap = await client.get(
                    f"/api/v1/documents/{doc_id_api}/status",
                    headers={"Authorization": f"Bearer {mgr_tok}"},
                )
                assert r_snap.status_code == 200, r_snap.text
                snap_body = r_snap.json()
                assert snap_body["overall_status"] == "running"
                assert snap_body["requires_review"] is False
                assert snap_body["pending_reviews"][0]["status"] == "modified"

                # 重复提交应立即返回 400
                r_dup = await client.post(
                    f"/api/v1/documents/{doc_id_api}/review",
                    json={
                        "doc_id": doc_id_api,
                        "items": [{"item_id": "tbl_api_0", "user_description": "重复提交", "approved": True}],
                    },
                    headers={"Authorization": f"Bearer {mgr_tok}"},
                )
                assert r_dup.status_code == 400

                history_events = [e["event"] for e in pipeline_manager.get_history(doc_id_api)]
                assert history_events.count("review_completed") == 1
                assert "review_required" not in history_events
    finally:
        for coro in scheduled_coros:
            coro.close()
        pipeline_manager.remove_pipeline(doc_id_api)
        clear_authz_cache()


@pytest.mark.asyncio
async def test_13_review_asset_map_citation_table_and_inactive_events_ticket(local_rsa_jwt_env):
    """
    审核切图私有映射、引用表格透传与失活流水线短票拦截回归：
    13.1 内存态 pending_reviews 严格不含 raw_oss_url，切图代理从 review_asset_oss_map 拉流返回 200；
    13.2 无活跃内存流水线时，POST /stream-ticket (purpose=events) 直接返回 404，防止前端陷入 /events 404 循环；
    13.3 CitationSource 正确透传 is_table 与 table_html，且仍物理剥离 raw_oss_url。
    """
    from rag_kb.api.schemas import CitationSource
    from rag_kb.api.sse import pipeline_manager
    from rag_kb.core.constants import IngestStatus
    from rag_kb.graph.state import get_default_state

    manifest = _load_manifest()
    tenant_id = manifest["tenant_id"]
    tech_kb = manifest["knowledge_bases"]["tech_kb"]
    l2_doc_id = manifest["documents"]["l2_tech_spec"]["doc_id"]
    mgr_tok = await _mint_token_for_user(manifest, "tech_manager")

    # 13.1 验证 review_asset_oss_map 代理成功 + 对外 /status 零 raw_oss_url 泄露
    doc_id_asset = str(uuid.uuid4())
    state = get_default_state(
        doc_id=doc_id_asset,
        file_name="asset_map_test.pdf",
        department=tech_kb["owner_dept_path"],
        tenant_id=tenant_id,
        kb_id=tech_kb["id"],
        kb_name=tech_kb["name"],
        classification_level=2,
    )
    state["overall_status"] = IngestStatus.REVIEW.value
    state["current_step_index"] = 7
    state["pending_reviews"] = [
        {
            "item_id": "img_rev_0",
            "doc_id": doc_id_asset,
            "type": "image",
            "title": "配图 1 (第 1 页)",
            "page_idx": 0,
            "display_page": 1,
            "asset_url": f"/api/v1/documents/{doc_id_asset}/review-items/img_rev_0/asset",
            "vlm_description": "测试配图描述",
            "user_description": "测试配图描述",
            "status": "pending",
        }
    ]
    state["review_asset_oss_map"] = {
        "img_rev_0": "rag_storage/images/test/img_rev_0.png",
    }
    pipeline_manager.register_pipeline(doc_id_asset, state)

    app = _build_test_app()
    transport = httpx.ASGITransport(app=app)
    try:
        def _fake_stream(target_url: str):
            assert target_url == "rag_storage/images/test/img_rev_0.png"
            return iter([TINY_PNG_BYTES]), len(TINY_PNG_BYTES), target_url

        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            r_status = await client.get(
                f"/api/v1/documents/{doc_id_asset}/status",
                headers={"Authorization": f"Bearer {mgr_tok}"},
            )
            assert r_status.status_code == 200, r_status.text
            assert "raw_oss_url" not in r_status.text

            with patch("rag_kb.api.routes.oss_service.get_object_stream", side_effect=_fake_stream):
                r_asset = await client.get(
                    f"/api/v1/documents/{doc_id_asset}/review-items/img_rev_0/asset",
                    headers={"Authorization": f"Bearer {mgr_tok}"},
                )
            assert r_asset.status_code == 200, r_asset.text
            assert r_asset.headers["content-type"].startswith("image/png")
            assert r_asset.content == TINY_PNG_BYTES

            # 13.2 无活跃内存流水线的历史文档申请 events 票据应返回 404，而 preview 票据仍正常 200
            r_events_ticket = await client.post(
                f"/api/v1/documents/{l2_doc_id}/stream-ticket",
                json={"purpose": "events"},
                headers={"Authorization": f"Bearer {mgr_tok}"},
            )
            assert r_events_ticket.status_code == 404, r_events_ticket.text

            r_preview_ticket = await client.post(
                f"/api/v1/documents/{l2_doc_id}/stream-ticket",
                json={"purpose": "preview"},
                headers={"Authorization": f"Bearer {mgr_tok}"},
            )
            assert r_preview_ticket.status_code == 200, r_preview_ticket.text
    finally:
        pipeline_manager.remove_pipeline(doc_id_asset)
        clear_authz_cache()

    # 13.3 CitationSource 透传 is_table / table_html 且不包含 raw_oss_url
    cit = CitationSource(
        citation_id=1,
        chunk_id=str(uuid.uuid4()),
        document_id=l2_doc_id,
        document_title="table_demo.md",
        chunk_label="表格 1",
        page_idx=0,
        snippet="表格摘要",
        is_table=True,
        table_html="<table><tr><th>A</th></tr><tr><td>1</td></tr></table>",
        raw_oss_url="https://should-be-dropped.example.com/a.md",
    )
    dumped = cit.model_dump()
    assert dumped["is_table"] is True
    assert dumped["table_html"] == "<table><tr><th>A</th></tr><tr><td>1</td></tr></table>"
    assert "raw_oss_url" not in dumped


@pytest.mark.asyncio
async def test_14_abac_position_dept_prefix_primary_only_and_governance_regression():
    """
    第 14 组端到端回归：验证企业 ABAC 三维准入策略、动作级部门分桶及控制面治理约束：
    1. 角色职级准入拦截（STAFF 持 admin / dept_manager -> 403）；
    2. 多部门员工在 READ vs UPLOAD/DELETE 间按 allowed_dept_prefixes 严格分桶（不越界）；
    3. primary_dept_only=TRUE 主部门收口（仅主部门获 MANAGE，副部门库建库/授 ACL 均 403；无明确主部门时 403 不猜测）；
    4. PUT /api/v1/admin/roles 合法保存、policy_epoch 推进、非法参数 400、以及持有 system:admin 角色防自提权 403；
    5. PUT /api/v1/admin/positions 严格校验证 min_allowed_clearance <= defaultClearanceLevel <= max_allowed_clearance (400/200)。
    """
    from rag_kb.core.authz import assert_kb_access
    from rag_kb.core.constants import KbAction

    reg_tenant = f"abac_reg_{uuid.uuid4().hex[:8]}"
    p_tech = "/公司/研发中心/技术部"
    p_hr = "/公司/职能中心/人事行政部"
    p_fin = "/公司/职能中心/财务部"

    dept_tech_id = uuid.uuid4()
    dept_hr_id = uuid.uuid4()
    dept_fin_id = uuid.uuid4()
    kb_tech_id = uuid.uuid4()
    kb_hr_id = uuid.uuid4()
    uid_staff_multi = uuid.uuid4()
    uid_staff_fin = uuid.uuid4()
    uid_mgr_dual = uuid.uuid4()
    uid_mgr_noprim = uuid.uuid4()
    uid_exec_admin = uuid.uuid4()
    uid_limited_admin = uuid.uuid4()

    created_kb_ids: List[uuid.UUID] = [kb_tech_id, kb_hr_id]

    try:
        async with AsyncSessionLocal() as db:
            db.add(TenantPolicyEpoch(tenant_id=reg_tenant, policy_epoch=1))
            db.add_all([
                Department(id=dept_tech_id, tenant_id=reg_tenant, name="技术部", path=p_tech, sort_order=10, status="ACTIVE"),
                Department(id=dept_hr_id, tenant_id=reg_tenant, name="人事行政部", path=p_hr, sort_order=20, status="ACTIVE"),
                Department(id=dept_fin_id, tenant_id=reg_tenant, name="财务部", path=p_fin, sort_order=30, status="ACTIVE"),
            ])
            db.add_all([
                Position(tenant_id=reg_tenant, position_code="INTERN_EXT", position_name="实习", rank_order=10, default_clearance_level=1, min_allowed_clearance=1, max_allowed_clearance=1),
                Position(tenant_id=reg_tenant, position_code="STAFF", position_name="普通员工", rank_order=20, default_clearance_level=2, min_allowed_clearance=1, max_allowed_clearance=2),
                Position(tenant_id=reg_tenant, position_code="SUPERVISOR", position_name="主管", rank_order=30, default_clearance_level=2, min_allowed_clearance=2, max_allowed_clearance=3),
                Position(tenant_id=reg_tenant, position_code="MANAGER", position_name="部门经理", rank_order=40, default_clearance_level=3, min_allowed_clearance=3, max_allowed_clearance=3),
                Position(tenant_id=reg_tenant, position_code="DIRECTOR", position_name="总监", rank_order=50, default_clearance_level=3, min_allowed_clearance=3, max_allowed_clearance=3),
                Position(tenant_id=reg_tenant, position_code="EXECUTIVE", position_name="高管", rank_order=60, default_clearance_level=4, min_allowed_clearance=4, max_allowed_clearance=4),
            ])
            db.add_all([
                Role(
                    tenant_id=reg_tenant, role_code="employee", role_name="员工", data_scope="DEPT_AND_SUB",
                    permissions=["kb:retrieve"], allowed_positions=["*"], allowed_dept_prefixes=["*"], primary_dept_only=False,
                ),
                Role(
                    tenant_id=reg_tenant, role_code="developer", role_name="研发工程师", data_scope="DEPT_AND_SUB",
                    permissions=["kb:retrieve", "doc:upload", "doc:review", "doc:delete"],
                    allowed_positions=["INTERN_EXT", "STAFF", "SUPERVISOR", "MANAGER", "DIRECTOR"],
                    allowed_dept_prefixes=["/公司/研发中心"], primary_dept_only=False,
                ),
                Role(
                    tenant_id=reg_tenant, role_code="dept_manager", role_name="部门经理", data_scope="DEPT_AND_SUB",
                    permissions=["kb:retrieve", "doc:upload", "doc:review", "doc:delete", "kb:manage", "kb:grant"],
                    allowed_positions=["MANAGER", "DIRECTOR"],
                    allowed_dept_prefixes=["*"], primary_dept_only=True,
                ),
                Role(
                    tenant_id=reg_tenant, role_code="admin", role_name="管理员", data_scope="ALL_ORG",
                    permissions=["*"], allowed_positions=["DIRECTOR", "EXECUTIVE"],
                    allowed_dept_prefixes=["*"], primary_dept_only=False,
                ),
                Role(
                    tenant_id=reg_tenant, role_code="limited_sys_admin", role_name="研发策略管理员", data_scope="DEPT_AND_SUB",
                    permissions=["kb:retrieve", "kb:manage", "kb:grant", "system:admin"],
                    allowed_positions=["MANAGER"], allowed_dept_prefixes=["/公司/研发中心"], primary_dept_only=True,
                ),
            ])
            await db.flush()
            db.add_all([
                SysUser(id=uid_staff_multi, tenant_id=reg_tenant, external_id="abac-staff-multi", id_source="oidc:reg", real_name="兼职研发", position_code="STAFF", is_active=True, authz_version=1),
                SysUser(id=uid_staff_fin, tenant_id=reg_tenant, external_id="abac-staff-fin", id_source="oidc:reg", real_name="财务专员", position_code="STAFF", is_active=True, authz_version=1),
                SysUser(id=uid_mgr_dual, tenant_id=reg_tenant, external_id="abac-mgr-dual", id_source="oidc:reg", real_name="双部门经理", position_code="MANAGER", is_active=True, authz_version=1),
                SysUser(id=uid_mgr_noprim, tenant_id=reg_tenant, external_id="abac-mgr-noprim", id_source="oidc:reg", real_name="缺主部门经理", position_code="MANAGER", is_active=True, authz_version=1),
                SysUser(id=uid_exec_admin, tenant_id=reg_tenant, external_id="abac-exec-adm", id_source="oidc:reg", real_name="高管超管", position_code="EXECUTIVE", is_active=True, authz_version=1),
                SysUser(id=uid_limited_admin, tenant_id=reg_tenant, external_id="abac-lim-adm", id_source="oidc:reg", real_name="受限策略管理员", position_code="MANAGER", is_active=True, authz_version=1),
            ])
            await db.flush()
            db.add_all([
                UserDepartment(id=uuid.uuid4(), tenant_id=reg_tenant, user_id=uid_staff_multi, department_id=dept_tech_id, dept_path=p_tech, is_primary=True),
                UserDepartment(id=uuid.uuid4(), tenant_id=reg_tenant, user_id=uid_staff_multi, department_id=dept_hr_id, dept_path=p_hr, is_primary=False),
                UserDepartment(id=uuid.uuid4(), tenant_id=reg_tenant, user_id=uid_staff_fin, department_id=dept_fin_id, dept_path=p_fin, is_primary=True),
                UserDepartment(id=uuid.uuid4(), tenant_id=reg_tenant, user_id=uid_mgr_dual, department_id=dept_tech_id, dept_path=p_tech, is_primary=True),
                UserDepartment(id=uuid.uuid4(), tenant_id=reg_tenant, user_id=uid_mgr_dual, department_id=dept_hr_id, dept_path=p_hr, is_primary=False),
                UserDepartment(id=uuid.uuid4(), tenant_id=reg_tenant, user_id=uid_mgr_noprim, department_id=dept_tech_id, dept_path=p_tech, is_primary=False),
                UserDepartment(id=uuid.uuid4(), tenant_id=reg_tenant, user_id=uid_mgr_noprim, department_id=dept_hr_id, dept_path=p_hr, is_primary=False),
                UserDepartment(id=uuid.uuid4(), tenant_id=reg_tenant, user_id=uid_exec_admin, department_id=dept_tech_id, dept_path=p_tech, is_primary=True),
                UserDepartment(id=uuid.uuid4(), tenant_id=reg_tenant, user_id=uid_limited_admin, department_id=dept_tech_id, dept_path=p_tech, is_primary=True),
            ])
            kb_tech = KnowledgeBase(
                id=kb_tech_id, tenant_id=reg_tenant, name="技术研发库",
                owner_department_id=dept_tech_id, owner_dept_path=p_tech,
                visibility="DEPARTMENT", min_classification_level=1, default_doc_level=2, max_doc_level=4, status="ACTIVE",
            )
            kb_hr = KnowledgeBase(
                id=kb_hr_id, tenant_id=reg_tenant, name="人事制度库",
                owner_department_id=dept_hr_id, owner_dept_path=p_hr,
                visibility="DEPARTMENT", min_classification_level=1, default_doc_level=2, max_doc_level=4, status="ACTIVE",
            )
            db.add_all([kb_tech, kb_hr])
            await db.commit()

        clear_authz_cache(tenant_id=reg_tenant)

        # 1. 职级准入拦截：STAFF 持有 admin 或 dept_manager -> 403
        for bad_role in ("admin", "dept_manager"):
            bad_tok = make_rs256_test_token(
                user_id=str(uid_staff_multi), tenant_id=reg_tenant, username="兼职研发",
                dept_path=p_tech, dept_paths=[p_tech, p_hr], roles=[bad_role], position_code="STAFF",
            )
            with pytest.raises(HTTPException) as exc_pos:
                await get_current_user(authorization=f"Bearer {bad_tok}")
            assert exc_pos.value.status_code == 403

        # 2. 多部门员工动作分桶：READ=[hr, tech], UPLOAD/DELETE=[tech]；仅财务部持 developer -> 403
        multi_tok = make_rs256_test_token(
            user_id=str(uid_staff_multi), tenant_id=reg_tenant, username="兼职研发",
            dept_path=p_tech, dept_paths=[p_tech, p_hr], roles=["developer", "employee"], position_code="STAFF",
        )
        multi_ctx = await get_current_user(authorization=f"Bearer {multi_tok}")
        assert multi_ctx.dept_paths_by_action["READ"] == sorted([p_hr, p_tech])
        assert multi_ctx.dept_paths_by_action["UPLOAD"] == [p_tech]
        assert multi_ctx.dept_paths_by_action["DELETE"] == [p_tech]

        async with AsyncSessionLocal() as db:
            kb_t_row = await db.get(KnowledgeBase, kb_tech_id)
            kb_h_row = await db.get(KnowledgeBase, kb_hr_id)
            await assert_kb_access(db, multi_ctx, kb_t_row, KbAction.UPLOAD.value, audit_denial=False)
            await assert_kb_access(db, multi_ctx, kb_h_row, KbAction.READ.value, audit_denial=False)
            with pytest.raises(HTTPException) as exc_up_hr:
                await assert_kb_access(db, multi_ctx, kb_h_row, KbAction.UPLOAD.value, audit_denial=False)
            assert exc_up_hr.value.status_code == 403

        fin_dev_tok = make_rs256_test_token(
            user_id=str(uid_staff_fin), tenant_id=reg_tenant, username="财务专员",
            dept_path=p_fin, dept_paths=[p_fin], roles=["developer"], position_code="STAFF",
        )
        with pytest.raises(HTTPException) as exc_fin_dev:
            await get_current_user(authorization=f"Bearer {fin_dev_tok}")
        assert exc_fin_dev.value.status_code == 403

        # 3. primary_dept_only 收口：双部门 MANAGER 仅对主部门(技术部)享 MANAGE，副部门(人事)建库/ACL 均 403；无主部门 MANAGER 403
        app = _build_test_app()
        transport = httpx.ASGITransport(app=app)
        dual_mgr_tok = make_rs256_test_token(
            user_id=str(uid_mgr_dual), tenant_id=reg_tenant, username="双部门经理",
            dept_path=p_tech, dept_paths=[p_tech, p_hr], roles=["dept_manager", "employee"],
            position_code="MANAGER", clearance_level=3,
        )
        dual_mgr_ctx = await get_current_user(authorization=f"Bearer {dual_mgr_tok}")
        assert dual_mgr_ctx.dept_paths_by_action["READ"] == sorted([p_hr, p_tech])
        assert dual_mgr_ctx.dept_paths_by_action["MANAGE"] == [p_tech]

        noprim_tok = make_rs256_test_token(
            user_id=str(uid_mgr_noprim), tenant_id=reg_tenant, username="缺主部门经理",
            dept_path="", dept_paths=[p_tech, p_hr], roles=["dept_manager"], position_code="MANAGER", clearance_level=3,
        )
        with pytest.raises(HTTPException) as exc_noprim:
            await get_current_user(authorization=f"Bearer {noprim_tok}")
        assert exc_noprim.value.status_code == 403

        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            r_up_list = await client.get("/api/v1/knowledge-bases?action=UPLOAD", headers={"Authorization": f"Bearer {multi_tok}"})
            assert r_up_list.status_code == 200
            assert [k["id"] for k in r_up_list.json()] == [str(kb_tech_id)]

            r_hr_kb_create = await client.post(
                "/api/v1/admin/knowledge-bases",
                json={"name": "越权副部门建库", "ownerDepartmentId": str(dept_hr_id), "visibility": "DEPARTMENT"},
                headers={"Authorization": f"Bearer {dual_mgr_tok}"},
            )
            assert r_hr_kb_create.status_code == 403

            r_hr_grant = await client.post(
                f"/api/v1/admin/knowledge-bases/{kb_hr_id}/grants",
                json={"subjectType": "USER", "subjectId": str(uid_staff_multi), "action": "READ", "effect": "ALLOW"},
                headers={"Authorization": f"Bearer {dual_mgr_tok}"},
            )
            assert r_hr_grant.status_code == 403

            r_tech_kb_create = await client.post(
                "/api/v1/admin/knowledge-bases",
                json={"name": f"主部门合规库_{uuid.uuid4().hex[:6]}", "ownerDepartmentId": str(dept_tech_id), "visibility": "DEPARTMENT"},
                headers={"Authorization": f"Bearer {dual_mgr_tok}"},
            )
            assert r_tech_kb_create.status_code == 201, r_tech_kb_create.text
            created_kb_ids.append(uuid.UUID(r_tech_kb_create.json()["id"]))

            # 4. PUT /admin/roles 合法保存 + 参数校验 (400) + policy_epoch 推进 + 自提权拦截 (403)
            exec_tok_e2 = make_rs256_test_token(
                user_id=str(uid_exec_admin), tenant_id=reg_tenant, username="高管超管",
                dept_path=p_tech, dept_paths=[p_tech], roles=["admin"], perms=["*"],
                scope_by_action={"READ": "ALL_ORG", "MANAGE": "ALL_ORG"},
                position_code="EXECUTIVE", clearance_level=4, policy_epoch=2,
            )
            for bad_role_body in (
                {"roleCode": "developer", "roleName": "研发", "dataScope": "DEPT_AND_SUB", "permissions": ["kb:retrieve"], "allowedPositions": ["NON_EXIST_POS"], "allowedDeptPrefixes": ["*"], "primaryDeptOnly": False},
                {"roleCode": "developer", "roleName": "研发", "dataScope": "DEPT_AND_SUB", "permissions": ["kb:retrieve"], "allowedPositions": [], "allowedDeptPrefixes": ["*"], "primaryDeptOnly": False},
                {"roleCode": "developer", "roleName": "研发", "dataScope": "DEPT_AND_SUB", "permissions": ["kb:retrieve"], "allowedPositions": ["*"], "allowedDeptPrefixes": ["relative/no_slash"], "primaryDeptOnly": False},
                {"roleCode": "kb_admin", "roleName": "废弃", "dataScope": "DEPT_AND_SUB", "permissions": ["kb:retrieve"], "allowedPositions": ["*"], "allowedDeptPrefixes": ["*"], "primaryDeptOnly": False},
                {"roleCode": "developer", "roleName": "研发", "dataScope": "DEPT_AND_SUB", "permissions": ["user:authz:manage"], "allowedPositions": ["*"], "allowedDeptPrefixes": ["*"], "primaryDeptOnly": False},
            ):
                r_bad = await client.put("/api/v1/admin/roles", json=bad_role_body, headers={"Authorization": f"Bearer {exec_tok_e2}"})
                assert r_bad.status_code == 400, f"应返回 400: {bad_role_body} -> {r_bad.status_code} {r_bad.text}"

            r_role_ok = await client.put(
                "/api/v1/admin/roles",
                json={
                    "roleCode": "developer",
                    "roleName": "研发工程师(严管版)",
                    "dataScope": "DEPT_AND_SUB",
                    "permissions": ["kb:retrieve", "doc:upload"],
                    "allowedPositions": ["STAFF", "MANAGER"],
                    "allowedDeptPrefixes": ["/公司/研发中心/技术部"],
                    "primaryDeptOnly": True,
                },
                headers={"Authorization": f"Bearer {exec_tok_e2}"},
            )
            assert r_role_ok.status_code == 200, r_role_ok.text
            saved_role = r_role_ok.json()
            assert saved_role["allowedPositions"] == ["STAFF", "MANAGER"]
            assert saved_role["allowedDeptPrefixes"] == ["/公司/研发中心/技术部"]
            assert saved_role["primaryDeptOnly"] is True

            # 验证 policy_epoch 已推进至 3，旧 policy_epoch=2 令牌立即 401
            r_stale_ep = await client.get("/api/v1/admin/roles", headers={"Authorization": f"Bearer {exec_tok_e2}"})
            assert r_stale_ep.status_code == 401

            lim_tok = make_rs256_test_token(
                user_id=str(uid_limited_admin), tenant_id=reg_tenant, username="受限策略管理员",
                dept_path=p_tech, dept_paths=[p_tech], roles=["limited_sys_admin"],
                perms=["kb:retrieve", "kb:manage", "kb:grant", "system:admin"],
                position_code="MANAGER", clearance_level=3, policy_epoch=3,
            )
            for self_esc_body in (
                {"roleCode": "limited_sys_admin", "roleName": "扩职级", "dataScope": "DEPT_AND_SUB", "permissions": ["kb:retrieve", "kb:manage", "kb:grant", "system:admin"], "allowedPositions": ["STAFF", "MANAGER"], "allowedDeptPrefixes": ["/公司/研发中心"], "primaryDeptOnly": True},
                {"roleCode": "limited_sys_admin", "roleName": "扩部门", "dataScope": "DEPT_AND_SUB", "permissions": ["kb:retrieve", "kb:manage", "kb:grant", "system:admin"], "allowedPositions": ["MANAGER"], "allowedDeptPrefixes": ["*"], "primaryDeptOnly": True},
                {"roleCode": "limited_sys_admin", "roleName": "放宽主部门", "dataScope": "DEPT_AND_SUB", "permissions": ["kb:retrieve", "kb:manage", "kb:grant", "system:admin"], "allowedPositions": ["MANAGER"], "allowedDeptPrefixes": ["/公司/研发中心"], "primaryDeptOnly": False},
            ):
                r_esc = await client.put("/api/v1/admin/roles", json=self_esc_body, headers={"Authorization": f"Bearer {lim_tok}"})
                assert r_esc.status_code == 403, f"应触发 403 防自提权: {self_esc_body} -> {r_esc.status_code}"

            # 5. PUT /admin/positions 职级密级边界校验 [min_allowed_clearance, max_allowed_clearance]
            exec_tok_e3 = make_rs256_test_token(
                user_id=str(uid_exec_admin), tenant_id=reg_tenant, username="高管超管",
                dept_path=p_tech, dept_paths=[p_tech], roles=["admin"], perms=["*"],
                scope_by_action={"READ": "ALL_ORG", "MANAGE": "ALL_ORG"},
                position_code="EXECUTIVE", clearance_level=4, policy_epoch=3,
            )
            r_pos_list = await client.get("/api/v1/admin/positions", headers={"Authorization": f"Bearer {exec_tok_e3}"})
            assert r_pos_list.status_code == 200
            staff_pos = next(p for p in r_pos_list.json() if p["positionCode"] == "STAFF")
            assert (staff_pos["minAllowedClearance"], staff_pos["maxAllowedClearance"]) == (1, 2)

            for bad_pos_body in (
                {"positionCode": "STAFF", "positionName": "普通员工", "rankOrder": 20, "defaultClearanceLevel": 3, "minAllowedClearance": 1, "maxAllowedClearance": 2},
                {"positionCode": "SUPERVISOR", "positionName": "主管", "rankOrder": 30, "defaultClearanceLevel": 1, "minAllowedClearance": 2, "maxAllowedClearance": 3},
                {"positionCode": "STAFF", "positionName": "普通员工", "rankOrder": 20, "defaultClearanceLevel": 2, "minAllowedClearance": 3, "maxAllowedClearance": 2},
            ):
                r_bad_pos = await client.put("/api/v1/admin/positions", json=bad_pos_body, headers={"Authorization": f"Bearer {exec_tok_e3}"})
                assert r_bad_pos.status_code == 400, f"职级越界应报 400: {bad_pos_body} -> {r_bad_pos.status_code}"

            r_pos_ok = await client.put(
                "/api/v1/admin/positions",
                json={"positionCode": "SUPERVISOR", "positionName": "主管", "rankOrder": 30, "defaultClearanceLevel": 3, "minAllowedClearance": 2, "maxAllowedClearance": 3},
                headers={"Authorization": f"Bearer {exec_tok_e3}"},
            )
            assert r_pos_ok.status_code == 200, r_pos_ok.text
            assert r_pos_ok.json()["defaultClearanceLevel"] == 3
    finally:
        clear_authz_cache(tenant_id=reg_tenant)
        async with AsyncSessionLocal() as db:
            await db.execute(delete(AuditLog).where(AuditLog.tenant_id == reg_tenant))
            await db.execute(delete(KbGrant).where(KbGrant.tenant_id == reg_tenant))
            await db.execute(delete(KnowledgeBase).where(KnowledgeBase.id.in_(created_kb_ids)))
            await db.execute(delete(UserDepartment).where(UserDepartment.tenant_id == reg_tenant))
            await db.execute(delete(SysUser).where(SysUser.tenant_id == reg_tenant))
            await db.execute(delete(Role).where(Role.tenant_id == reg_tenant))
            await db.execute(delete(Position).where(Position.tenant_id == reg_tenant))
            await db.execute(delete(Department).where(Department.tenant_id == reg_tenant))
            await db.execute(delete(TenantPolicyEpoch).where(TenantPolicyEpoch.tenant_id == reg_tenant))
            await db.commit()
