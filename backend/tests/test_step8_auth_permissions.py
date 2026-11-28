"""backend/tests/test_step8_auth_permissions.py —— 企业级 RBAC/ABAC cv=1 鉴权、BOLA 隔离与跨语言契约回归测试集。

严格验证（直连真实 PostgreSQL 16 + Redis + Aliyun OSS，零 Mock 降级）：
1. RS256 非对称签名与 cv=1 契约强校验（拒绝 cv=2 遗留令牌、布尔 cv、缺 dept_paths_by_action/必填字段、过期、签名/发行方/受众错误）；
2. To C 隔离：is_to_c=True 拒绝访问企业端 (403 Forbidden)；
3. 部门路径段边界防御：`/公司/技术部` 绝不误匹配 `/公司/技术部2`；
4. 细粒度权限拦截 (`require_permission`) 与 `/api/v1/auth/me` cv=1 档案返回；
5. 跨语言契约验证：消费 Java `JavaIssuedTokenFixtureGenerator` 真实签发的 cv=1 RS256 JWT；
6. 路由级 BOLA 防越权拦截：`DELETE /documents/{doc_id}`、`GET /documents/{doc_id}/chunks`、`GET /documents/{doc_id}/preview`、`POST /stream-ticket`；
7. 会话级 (Session-level) 同租户跨用户越权拦截；
8. 账号停用 DB + Redis 闭环、最小 TTL (600s) 校验与恢复后旧凭据防复活；
9. 真实阿里云 OSS Range 206 分片预览、cv=1 短票滑动续期、最大寿命上限、旧短票拒绝与渠道监控权限收紧；
10. 跨栈权限常量静态一致性扫描（V1/V2/V5 SQL、Java PermissionResolver、Python Permissions、前端 PERMISSIONS，确认 user:authz:manage 已退场）；
11. 大模型租约托管客户端 (`model_credentials`)：缓存、10 并发去重与 Fail-Fast。
"""

from __future__ import annotations

import asyncio
import io
import json
import re
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, List, Optional
from unittest.mock import patch

import httpx
import jwt
import pypdfium2 as pdfium
import pytest
from fastapi import FastAPI, HTTPException
from sqlalchemy import delete, select, text

from conftest import TEST_PRIVATE_KEY_PATH, make_rs256_test_token
from rag_kb.api.auth import router as auth_router
from rag_kb.api.deps import (
    PERM_CHANGED_USER_KEY_PREFIX,
    REVOKED_USER_KEY_PREFIX,
    STREAM_TICKET_KEY_PREFIX,
    EffectiveAuthzContext,
    get_current_user,
    get_session_auth_error,
    issue_stream_ticket,
    require_permission,
    restore_user_access,
    revoke_user_access,
    verify_jwt_setup_on_startup,
    verify_stream_ticket,
)
from rag_kb.api.knowledge_bases import router as kb_router
from rag_kb.api.retrieval import router as retrieval_router
from rag_kb.api.routes import router as documents_router, router_chunks as chunks_router
from rag_kb.api.sessions import router as sessions_router
from rag_kb.api.sse import pipeline_manager
from rag_kb.api.system import router as system_router
from rag_kb.core.authz import invalidate_authz_cache as clear_authz_cache, is_path_under_prefix as is_dept_under_or_equal
from rag_kb.core.config import settings
from rag_kb.core.constants import KNOWN_PERMISSION_VALUES, Permissions
from rag_kb.core.model_credentials import (
    _CREDENTIAL_CACHE,
    ModelCredential,
    get_cached_lease_info,
    get_credential,
)
from rag_kb.core.redis_client import get_redis_client
from rag_kb.db.models import (
    ChatMessage,
    ChatSession,
    Department,
    Document,
    KnowledgeBase,
    SysUser,
    TenantPolicyEpoch,
    UserDepartment,
)
from rag_kb.db.session import AsyncSessionLocal
from rag_kb.utils.oss import oss_service

pytestmark = pytest.mark.usefixtures("local_rsa_jwt_env")
PRIVATE_KEY_PEM = TEST_PRIVATE_KEY_PATH.read_text(encoding="utf-8")


@asynccontextmanager
async def _ephemeral_db_user(
    *,
    tenant_id: str = "default_tenant",
    username: str = "测试员工",
    dept_path: str = "/总公司/研发中心/技术部",
    position_code: str = "STAFF",
    role_codes: Optional[List[str]] = None,
):
    """在真实 PostgreSQL 16 中创建临时测试员工、部门关联与角色，退出时自动清理"""
    user_id = uuid.uuid4()
    ext_id = f"step8-{uuid.uuid4().hex[:12]}"
    created_dept_id: Optional[uuid.UUID] = None
    roles = role_codes if role_codes is not None else ["developer"]

    async with AsyncSessionLocal() as db:
        epoch = await db.get(TenantPolicyEpoch, tenant_id)
        if epoch is None:
            epoch = TenantPolicyEpoch(tenant_id=tenant_id, policy_epoch=1)
            db.add(epoch)
            await db.flush()
        policy_epoch = int(epoch.policy_epoch)

        dept = (
            await db.execute(
                select(Department).where(Department.tenant_id == tenant_id, Department.path == dept_path)
            )
        ).scalar_one_or_none()
        if dept is None:
            dept = Department(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                name=dept_path.strip("/").split("/")[-1] or "根部门",
                path=dept_path,
                status="ACTIVE",
            )
            db.add(dept)
            await db.flush()
            created_dept_id = dept.id

        user = SysUser(
            id=user_id,
            tenant_id=tenant_id,
            external_id=ext_id,
            id_source="oidc:test-step8",
            real_name=username,
            position_code=position_code,
            authz_version=1,
            is_active=True,
        )
        db.add(user)
        await db.flush()

        db.add(
            UserDepartment(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                user_id=user_id,
                department_id=dept.id,
                dept_path=dept_path,
                is_primary=True,
            )
        )
        await db.commit()

    clear_authz_cache()
    try:
        yield {
            "user_id": str(user_id),
            "tenant_id": tenant_id,
            "username": username,
            "dept_id": dept.id,
            "dept_path": dept_path,
            "position_code": position_code,
            "roles": roles,
            "authz_ver": 1,
            "policy_epoch": policy_epoch,
        }
    finally:
        clear_authz_cache()
        redis_cli = get_redis_client()
        await redis_cli.delete(
            f"{REVOKED_USER_KEY_PREFIX}{user_id}",
            f"{PERM_CHANGED_USER_KEY_PREFIX}{user_id}",
        )
        async with AsyncSessionLocal() as db:
            await db.execute(delete(SysUser).where(SysUser.id == user_id))
            if created_dept_id is not None:
                await db.execute(delete(Department).where(Department.id == created_dept_id))
            await db.commit()


# ==============================================================================
# 1. cv=1 RS256 令牌校验与 401/403 拦截测试
# ==============================================================================


@pytest.mark.asyncio
async def test_01_valid_cv1_rs256_token_and_db_truth_resolution():
    """测试 1: 验证合法 cv=1 RS256 令牌与 PostgreSQL 单一事实源上下文构建"""
    verify_jwt_setup_on_startup()
    async with _ephemeral_db_user(
        username="张工",
        dept_path="/总公司/研发中心/技术部",
        position_code="STAFF",
        role_codes=["developer"],
    ) as u:
        token = make_rs256_test_token(
            user_id=u["user_id"],
            tenant_id=u["tenant_id"],
            username=u["username"],
            dept_path=u["dept_path"],
            dept_paths=[u["dept_path"]],
            roles=u["roles"],
            position_code=u["position_code"],
            clearance_level=2,
            authz_ver=u["authz_ver"],
            policy_epoch=u["policy_epoch"],
            cv=1,
        )
        ctx = await get_current_user(authorization=f"Bearer {token}")
        assert isinstance(ctx, EffectiveAuthzContext)
        assert ctx.cv == 1
        assert ctx.user_id == u["user_id"]
        assert ctx.tenant_id == "default_tenant"
        assert ctx.dept_path == "/总公司/研发中心/技术部"
        assert "/总公司/研发中心/技术部" in ctx.dept_paths
        assert ctx.dept_paths_by_action.get("READ") == ["/总公司/研发中心/技术部"]
        assert ctx.effective_clearance == 2
        assert ctx.has_permission("kb:retrieve") is True
        assert ctx.has_permission("doc:upload") is True
        assert ctx.has_permission("system:admin") is False
        assert ctx.is_to_c is False


@pytest.mark.asyncio
async def test_02_invalid_tampered_and_legacy_cv2_jwt_raises_401():
    """测试 2: 缺失、损坏、过期、错签发方/受众、cv=2 遗留令牌、缺 dept_paths_by_action/必填字段均必须被 401 拦截"""
    with pytest.raises(HTTPException) as exc:
        await get_current_user(authorization=None)
    assert exc.value.status_code == 401

    with pytest.raises(HTTPException) as exc:
        await get_current_user(authorization="Basic dXNlcjpwYXNz")
    assert exc.value.status_code == 401

    with pytest.raises(HTTPException) as exc:
        await get_current_user(authorization="Bearer corrupted.jwt.token.string")
    assert exc.value.status_code == 401

    expired_token = make_rs256_test_token(exp_delta=-60)
    with pytest.raises(HTTPException) as exc:
        await get_current_user(authorization=f"Bearer {expired_token}")
    assert exc.value.status_code == 401

    wrong_iss_token = make_rs256_test_token(issuer="fake-admin")
    with pytest.raises(HTTPException) as exc:
        await get_current_user(authorization=f"Bearer {wrong_iss_token}")
    assert exc.value.status_code == 401

    wrong_aud_token = make_rs256_test_token(audience="fake-service")
    with pytest.raises(HTTPException) as exc:
        await get_current_user(authorization=f"Bearer {wrong_aud_token}")
    assert exc.value.status_code == 401

    # 遗留 cv=2 及布尔/非法 cv 令牌必须被拒绝（无兜底兼容）
    legacy_cv2_token = make_rs256_test_token(cv=2)
    with pytest.raises(HTTPException) as exc:
        await get_current_user(authorization=f"Bearer {legacy_cv2_token}")
    assert exc.value.status_code == 401
    assert "cv=2" in exc.value.detail

    bool_cv_token = make_rs256_test_token(custom_claims={"cv": True})
    with pytest.raises(HTTPException) as exc:
        await get_current_user(authorization=f"Bearer {bool_cv_token}")
    assert exc.value.status_code == 401

    # 缺少 cv=1 必填声明 (如 scope_by_action 或 dept_paths_by_action)
    now_ms = int(time.time() * 1000)
    now = now_ms // 1000
    missing_claim_payload = {
        "sub": str(uuid.uuid4()),
        "tenant_id": "default_tenant",
        "username": "u1",
        "dept_path": "/public",
        "dept_paths": ["/public"],
        "dept_paths_by_action": {},
        "position_code": "STAFF",
        "clearance_level": 2,
        "authz_ver": 1,
        "policy_epoch": 1,
        "roles": [],
        "perms": [],
        "is_to_c": False,
        "cv": 1,
        "typ": "access",
        "iat": now,
        "iat_ms": now_ms,
        "exp": now + 3600,
        "iss": "rag-admin",
        "aud": "rag-engine",
    }
    missing_token = jwt.encode(missing_claim_payload, PRIVATE_KEY_PEM, algorithm="RS256")
    with pytest.raises(HTTPException) as exc:
        await get_current_user(authorization=f"Bearer {missing_token}")
    assert exc.value.status_code == 401
    assert "scope_by_action" in exc.value.detail

    missing_dpa_payload = dict(missing_claim_payload)
    missing_dpa_payload.pop("dept_paths_by_action")
    missing_dpa_payload["scope_by_action"] = {"READ": "DEPT_AND_SUB"}
    missing_dpa_token = jwt.encode(missing_dpa_payload, PRIVATE_KEY_PEM, algorithm="RS256")
    with pytest.raises(HTTPException) as exc:
        await get_current_user(authorization=f"Bearer {missing_dpa_token}")
    assert exc.value.status_code == 401
    assert "dept_paths_by_action" in exc.value.detail


@pytest.mark.asyncio
async def test_03_to_c_user_isolation_forbidden_403():
    """测试 3: is_to_c=True 令牌强制 403 拦截"""
    toc_token = make_rs256_test_token(
        user_id=str(uuid.uuid4()),
        tenant_id="default_tenant",
        username="终端用户",
        dept_path="/public",
        is_to_c=True,
        cv=1,
    )
    with pytest.raises(HTTPException) as exc:
        await get_current_user(authorization=f"Bearer {toc_token}")
    assert exc.value.status_code == 403
    assert "拒绝 To C 访问" in exc.value.detail


def test_04_dept_path_segment_boundary_defense():
    """测试 4: 部门路径严格按 '/' 段边界匹配，杜绝 /技术部 误匹配 /技术部2"""
    assert is_dept_under_or_equal("/公司/技术部", "/公司/技术部") is True
    assert is_dept_under_or_equal("/公司/技术部/研发一组", "/公司/技术部") is True
    assert is_dept_under_or_equal("/公司/技术部2", "/公司/技术部") is False
    assert is_dept_under_or_equal("/公司/技术部2/研发一组", "/公司/技术部") is False
    assert is_dept_under_or_equal("/公司", "/公司/技术部") is False


@pytest.mark.asyncio
async def test_05_auth_me_and_permission_enforcement():
    """测试 5: /api/v1/auth/me 返回 cv=1 完整档案，且 require_permission 精确拦截缺失权限点"""
    test_app = FastAPI()
    test_app.include_router(auth_router, prefix="/api/v1")

    @test_app.get("/api/v1/protected-delete")
    async def _protected_delete(user: EffectiveAuthzContext = pytest.importorskip("fastapi").Depends(require_permission(Permissions.DOC_DELETE))):
        return {"user_id": user.user_id}

    async with _ephemeral_db_user(
        username="只读员工",
        dept_path="/总公司/研发中心/技术部",
        position_code="STAFF",
        role_codes=["employee"],
    ) as u:
        token = make_rs256_test_token(
            user_id=u["user_id"],
            tenant_id=u["tenant_id"],
            username=u["username"],
            dept_path=u["dept_path"],
            dept_paths=[u["dept_path"]],
            roles=u["roles"],
            perms=["kb:retrieve"],
            authz_ver=u["authz_ver"],
            policy_epoch=u["policy_epoch"],
            cv=1,
        )
        transport = httpx.ASGITransport(app=test_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            r_me = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
            assert r_me.status_code == 200
            body = r_me.json()
            assert body["user_id"] == u["user_id"]
            assert body["cv"] == 1
            assert "idp_roles" not in body
            assert body["position_code"] == "STAFF"
            assert body["effective_clearance"] == 2
            assert "/总公司/研发中心/技术部" in body["dept_paths"]
            assert isinstance(body["scope_by_action"], dict)
            assert isinstance(body["dept_paths_by_action"], dict)

            r_del = await client.get("/api/v1/protected-delete", headers={"Authorization": f"Bearer {token}"})
            assert r_del.status_code == 403
            assert "doc:delete" in r_del.json()["detail"]


# ==============================================================================
# 2. 跨语言契约验证 (Java NimbusJwtEncoder cv=1 -> Python PyJWT 消费)
# ==============================================================================


@pytest.mark.asyncio
async def test_06_cross_language_java_issued_cv1_tokens_contract():
    """测试 6: 消费 Java JavaIssuedTokenFixtureGenerator 签发的真实 cv=1 RS256 JWT Fixture"""
    fixture_path = Path(__file__).resolve().parent / "fixtures" / "java_issued_tokens.json"
    generate_hint = (
        "请先在 java-admin-service 目录执行 "
        "mvn -Dtest=JavaIssuedTokenFixtureGenerator test 生成最新 cv=1 JWT fixture"
    )
    assert fixture_path.is_file(), generate_hint
    tokens = json.loads(fixture_path.read_text(encoding="utf-8"))

    # 先校验所有活跃令牌的 cv=1 声明与短有效期 (<= 30 min)
    for key_name in ("developer_token", "accountant_token", "admin_token", "to_c_token"):
        unverified = jwt.decode(tokens[key_name], options={"verify_signature": False})
        assert unverified["cv"] == 1, f"{key_name} 必须为 cv=1 契约"
        assert "idp_roles" not in unverified, f"{key_name} 不得包含已下线的 idp_roles"
        assert 60 <= (int(unverified["exp"]) - int(unverified["iat"])) <= 1800
        assert "iat_ms" in unverified
        assert "scope_by_action" in unverified
        assert "dept_paths_by_action" in unverified
        assert "dept_paths" in unverified
        assert "position_code" in unverified
        assert "clearance_level" in unverified
        assert "authz_ver" in unverified
        assert "policy_epoch" in unverified
        assert int(unverified["exp"]) > int(time.time()) + 30, generate_hint

    # 为 fixture 中的真实 user sub 在 DB 中对齐对应用户记录，验证全链路验签 + DB 核验
    created_user_ids: List[uuid.UUID] = []
    async with AsyncSessionLocal() as db:
        epoch = await db.get(TenantPolicyEpoch, "default_tenant")
        epoch_val = int(epoch.policy_epoch) if epoch else 1

        for key_name in ("developer_token", "accountant_token", "admin_token"):
            claims = jwt.decode(tokens[key_name], options={"verify_signature": False})
            uid = uuid.UUID(str(claims["sub"]))
            existing = await db.get(SysUser, uid)
            if existing is None:
                user = SysUser(
                    id=uid,
                    tenant_id=str(claims["tenant_id"]),
                    external_id=f"java-fixture-{uid.hex[:8]}",
                    id_source="oidc:java-fixture",
                    real_name=str(claims["username"]),
                    position_code=str(claims["position_code"]),
                    authz_version=int(claims["authz_ver"]),
                    is_active=True,
                )
                db.add(user)
                await db.flush()
                dept = (
                    await db.execute(
                        select(Department).where(
                            Department.tenant_id == str(claims["tenant_id"]),
                            Department.path == str(claims["dept_path"]),
                        )
                    )
                ).scalar_one_or_none()
                if dept is not None:
                    db.add(
                        UserDepartment(
                            id=uuid.uuid4(),
                            tenant_id=str(claims["tenant_id"]),
                            user_id=uid,
                            department_id=dept.id,
                            dept_path=dept.path,
                            is_primary=True,
                        )
                    )
                created_user_ids.append(uid)
            else:
                # 同步 authz_version 与 policy_epoch 以匹配刚生成的 fixture
                existing.authz_version = int(claims["authz_ver"])
            if epoch is not None and int(claims["policy_epoch"]) != epoch_val:
                pass
        await db.commit()

    clear_authz_cache()
    try:
        dev_ctx = await get_current_user(authorization=f"Bearer {tokens['developer_token']}")
        assert dev_ctx.tenant_id == "default_tenant"
        assert dev_ctx.has_permission("kb:retrieve") is True
        assert dev_ctx.has_permission("doc:upload") is True

        acc_ctx = await get_current_user(authorization=f"Bearer {tokens['accountant_token']}")
        assert acc_ctx.has_permission("kb:retrieve") is True
        assert acc_ctx.has_permission("doc:upload") is False

        adm_ctx = await get_current_user(authorization=f"Bearer {tokens['admin_token']}")
        assert adm_ctx.has_permission("system:admin") is True

        with pytest.raises(HTTPException) as exc_toc:
            await get_current_user(authorization=f"Bearer {tokens['to_c_token']}")
        assert exc_toc.value.status_code == 403

        with pytest.raises(HTTPException) as exc_exp:
            await get_current_user(authorization=f"Bearer {tokens['expired_token']}")
        assert exc_exp.value.status_code == 401
    finally:
        clear_authz_cache()
        if created_user_ids:
            async with AsyncSessionLocal() as db:
                await db.execute(delete(SysUser).where(SysUser.id.in_(created_user_ids)))
                await db.commit()


# ==============================================================================
# 3. 路由级 BOLA 防越权拦截 & 会话跨用户隔离
# ==============================================================================


@pytest.mark.asyncio
async def test_07_document_routes_bola_and_permission_interception():
    """测试 7: 基于真实 PostgreSQL 知识库与文档，验证删除/切片/预览/短票的 BOLA 与权限拦截"""
    fin_doc_id = uuid.uuid4()
    fin_dept_id = uuid.uuid4()
    fin_kb_id = uuid.uuid4()
    suffix = uuid.uuid4().hex[:6]
    fin_dept_path = f"/总公司/职能中心/财务部/薪资组_{suffix}"

    test_app = FastAPI()
    test_app.include_router(documents_router, prefix="/api/v1")
    test_app.include_router(chunks_router, prefix="/api/v1")

    async with AsyncSessionLocal() as db:
        db.add(Department(id=fin_dept_id, tenant_id="default_tenant", name=f"薪资组_{suffix}", path=fin_dept_path))
        await db.flush()
        db.add(
            KnowledgeBase(
                id=fin_kb_id,
                tenant_id="default_tenant",
                name=f"财务保密库_{suffix}",
                owner_department_id=fin_dept_id,
                owner_dept_path=fin_dept_path,
                visibility="DEPARTMENT",
                min_classification_level=2,
                default_doc_level=2,
                max_doc_level=3,
                status="ACTIVE",
            )
        )
        await db.flush()
        db.add(
            Document(
                id=fin_doc_id,
                tenant_id="default_tenant",
                kb_id=fin_kb_id,
                classification_level=2,
                title="高管薪酬保密制度.pdf",
                file_hash=uuid.uuid4().hex + uuid.uuid4().hex,
                raw_oss_url="rag_storage/raw/non_accessible_bola.pdf",
                department_id=fin_dept_id,
                department_path=fin_dept_path,
                category="财务制度",
                tags=["机密"],
                overall_status="active",
                current_step_index=11,
                pipeline_steps=[],
                version=1,
                is_active=True,
            )
        )
        await db.commit()

    try:
        async with _ephemeral_db_user(
            username="技术部研发",
            dept_path="/总公司/研发中心/技术部",
            position_code="STAFF",
            role_codes=["developer"],
        ) as tech_u, _ephemeral_db_user(
            username="技术部只读",
            dept_path="/总公司/研发中心/技术部",
            position_code="STAFF",
            role_codes=["employee"],
        ) as emp_u:
            tech_token = make_rs256_test_token(
                user_id=tech_u["user_id"],
                username=tech_u["username"],
                dept_path=tech_u["dept_path"],
                dept_paths=[tech_u["dept_path"]],
                roles=tech_u["roles"],
                authz_ver=tech_u["authz_ver"],
                policy_epoch=tech_u["policy_epoch"],
            )
            emp_token = make_rs256_test_token(
                user_id=emp_u["user_id"],
                username=emp_u["username"],
                dept_path=emp_u["dept_path"],
                dept_paths=[emp_u["dept_path"]],
                roles=emp_u["roles"],
                authz_ver=emp_u["authz_ver"],
                policy_epoch=emp_u["policy_epoch"],
            )

            transport = httpx.ASGITransport(app=test_app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                # 无 doc:delete 权限 -> 403
                r1 = await client.delete(
                    f"/api/v1/documents/{fin_doc_id}",
                    headers={"Authorization": f"Bearer {emp_token}"},
                )
                assert r1.status_code == 403

                # 技术部研发尝试跨部门删除财务部文档 -> 403 BOLA
                r2 = await client.delete(
                    f"/api/v1/documents/{fin_doc_id}",
                    headers={"Authorization": f"Bearer {tech_token}"},
                )
                assert r2.status_code == 403

                # 跨部门查切片 / 预览 / 申请短票 -> 全部 403
                for url in (
                    f"/api/v1/documents/{fin_doc_id}/chunks",
                    f"/api/v1/documents/{fin_doc_id}/preview",
                ):
                    resp = await client.get(url, headers={"Authorization": f"Bearer {tech_token}"})
                    assert resp.status_code == 403

                r_tk = await client.post(
                    f"/api/v1/documents/{fin_doc_id}/stream-ticket",
                    json={"purpose": "preview"},
                    headers={"Authorization": f"Bearer {tech_token}"},
                )
                assert r_tk.status_code == 403
    finally:
        async with AsyncSessionLocal() as db:
            await db.execute(delete(Document).where(Document.id == fin_doc_id))
            await db.execute(delete(KnowledgeBase).where(KnowledgeBase.id == fin_kb_id))
            await db.execute(delete(Department).where(Department.id == fin_dept_id))
            await db.commit()


@pytest.mark.asyncio
async def test_08_session_routes_cross_user_isolation():
    """测试 8: 同租户下员工 B 严禁读取、修改、删除员工 A 的对话会话"""
    test_app = FastAPI()
    test_app.include_router(sessions_router, prefix="/api/v1")

    sess_a_id: Optional[str] = None
    async with _ephemeral_db_user(username="员工A", role_codes=["employee"]) as ua, _ephemeral_db_user(
        username="员工B", role_codes=["employee"]
    ) as ub:
        tok_a = make_rs256_test_token(
            user_id=ua["user_id"],
            username=ua["username"],
            dept_path=ua["dept_path"],
            dept_paths=[ua["dept_path"]],
            roles=ua["roles"],
            authz_ver=ua["authz_ver"],
            policy_epoch=ua["policy_epoch"],
        )
        tok_b = make_rs256_test_token(
            user_id=ub["user_id"],
            username=ub["username"],
            dept_path=ub["dept_path"],
            dept_paths=[ub["dept_path"]],
            roles=ub["roles"],
            authz_ver=ub["authz_ver"],
            policy_epoch=ub["policy_epoch"],
        )
        try:
            transport = httpx.ASGITransport(app=test_app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                cr = await client.post(
                    "/api/v1/sessions",
                    json={"title": "员工A私密会话"},
                    headers={"Authorization": f"Bearer {tok_a}"},
                )
                assert cr.status_code == 200
                sess_a_id = cr.json()["id"]

                assert (
                    await client.get(
                        f"/api/v1/sessions/{sess_a_id}/messages",
                        headers={"Authorization": f"Bearer {tok_b}"},
                    )
                ).status_code == 404
                assert (
                    await client.patch(
                        f"/api/v1/sessions/{sess_a_id}",
                        json={"title": "越权篡改"},
                        headers={"Authorization": f"Bearer {tok_b}"},
                    )
                ).status_code == 404
                assert (
                    await client.delete(
                        f"/api/v1/sessions/{sess_a_id}",
                        headers={"Authorization": f"Bearer {tok_b}"},
                    )
                ).status_code == 404
        finally:
            if sess_a_id:
                async with AsyncSessionLocal() as db:
                    await db.execute(delete(ChatMessage).where(ChatMessage.session_id == uuid.UUID(sess_a_id)))
                    await db.execute(delete(ChatSession).where(ChatSession.id == uuid.UUID(sess_a_id)))
                    await db.commit()


# ==============================================================================
# 4. 账号停用闭环 + 真实 OSS Range 206 滑动短票 + 跨栈静态一致性 + 租约客户端
# ==============================================================================


@pytest.mark.asyncio
async def test_09_account_deactivation_db_and_redis_closed_loop():
    """测试 9: POST/DELETE /api/v1/admin/users/{user_id}/revoke 最小 TTL 校验、DB+Redis 同步停用、恢复防复活及旧 /auth/revoke 已移除"""
    from rag_kb.api.admin import router as admin_router

    test_app = FastAPI()
    test_app.include_router(auth_router, prefix="/api/v1")
    test_app.include_router(admin_router, prefix="/api/v1")

    async with _ephemeral_db_user(username="待停用员工", role_codes=["employee"]) as target_u, _ephemeral_db_user(
        username="系统管理员", role_codes=["admin"], position_code="EXECUTIVE"
    ) as admin_u:
        uid = target_u["user_id"]
        emp_token = make_rs256_test_token(
            user_id=uid,
            username=target_u["username"],
            dept_path=target_u["dept_path"],
            dept_paths=[target_u["dept_path"]],
            roles=target_u["roles"],
            authz_ver=target_u["authz_ver"],
            policy_epoch=target_u["policy_epoch"],
        )
        admin_token = make_rs256_test_token(
            user_id=admin_u["user_id"],
            username=admin_u["username"],
            dept_path=admin_u["dept_path"],
            dept_paths=[admin_u["dept_path"]],
            roles=admin_u["roles"],
            position_code="EXECUTIVE",
            clearance_level=4,
            authz_ver=admin_u["authz_ver"],
            policy_epoch=admin_u["policy_epoch"],
        )

        ctx = await get_current_user(authorization=f"Bearer {emp_token}")
        ticket, _ = await issue_stream_ticket(ctx, doc_id="doc-rev-1", purpose="preview")

        transport = httpx.ASGITransport(app=test_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            # 确认旧冗余接口 /api/v1/auth/revoke/{uid} 已彻底移除 (404)
            removed_old = await client.post(
                f"/api/v1/auth/revoke/{uid}",
                json={"ttl_seconds": 600, "reason": "旧接口已移除校验"},
                headers={"Authorization": f"Bearer {admin_token}"},
            )
            assert removed_old.status_code == 404

            # ttl_seconds < 600 被 422 拒绝
            bad_ttl = await client.post(
                f"/api/v1/admin/users/{uid}/revoke",
                json={"ttl_seconds": 300, "reason": "过短TTL校验"},
                headers={"Authorization": f"Bearer {admin_token}"},
            )
            assert bad_ttl.status_code == 422

            rev_resp = await client.post(
                f"/api/v1/admin/users/{uid}/revoke",
                json={"ttl_seconds": 600, "reason": "员工离职停用"},
                headers={"Authorization": f"Bearer {admin_token}"},
            )
            assert rev_resp.status_code == 200
            assert rev_resp.json()["is_active"] is False

            # 旧 JWT 与旧短票立即 401
            with pytest.raises(HTTPException) as exc_jwt:
                await get_current_user(authorization=f"Bearer {emp_token}")
            assert exc_jwt.value.status_code == 401

            with pytest.raises(HTTPException) as exc_tk:
                await verify_stream_ticket(ticket, expected_doc_id="doc-rev-1", expected_purpose="preview")
            assert exc_tk.value.status_code == 401

            # 恢复账号：authz_version 已递增且写入 perm_changed_at，旧 JWT 与旧短票严禁复活
            res_resp = await client.delete(
                f"/api/v1/admin/users/{uid}/revoke",
                headers={"Authorization": f"Bearer {admin_token}"},
            )
            assert res_resp.status_code == 200
            assert res_resp.json()["is_active"] is True

            with pytest.raises(HTTPException) as stale_jwt_exc:
                await get_current_user(authorization=f"Bearer {emp_token}")
            assert stale_jwt_exc.value.status_code == 401

            with pytest.raises(HTTPException) as stale_tk_exc:
                await verify_stream_ticket(ticket, expected_doc_id="doc-rev-1", expected_purpose="preview")
            assert stale_tk_exc.value.status_code == 401

            # 持最新 authz_version 与新 iat_ms 签发的新令牌可正常恢复使用
            async with AsyncSessionLocal() as db:
                refreshed = await db.get(SysUser, uuid.UUID(uid))
                new_ver = int(refreshed.authz_version)
            perm_changed_ms = int(await get_redis_client().get(f"{PERM_CHANGED_USER_KEY_PREFIX}{uid}"))
            fresh_token = make_rs256_test_token(
                user_id=uid,
                username=target_u["username"],
                dept_path=target_u["dept_path"],
                dept_paths=[target_u["dept_path"]],
                roles=target_u["roles"],
                authz_ver=new_ver,
                policy_epoch=target_u["policy_epoch"],
                custom_claims={"iat_ms": max(int(time.time() * 1000), perm_changed_ms + 5)},
            )
            restored_ctx = await get_current_user(authorization=f"Bearer {fresh_token}")
            assert restored_ctx.user_id == uid
            assert restored_ctx.authz_ver == new_ver


@pytest.mark.asyncio
async def test_10_real_oss_range_preview_sliding_ticket_and_channel_monitoring():
    """测试 10: 直连真实阿里云 OSS + PostgreSQL 16，验证 Range 206 分片预览、cv=2 短票滑动续期、上限作废与无 raw_oss_url"""
    doc_id = uuid.uuid4()
    dept_id = uuid.uuid4()
    kb_id = uuid.uuid4()
    suffix = uuid.uuid4().hex[:6]
    dept_path = f"/总公司/研发中心/技术部/验证组_{suffix}"

    pdf_doc = pdfium.PdfDocument.new()
    pdf_doc.new_page(300, 400)
    pdf_buf = io.BytesIO()
    pdf_doc.save(pdf_buf)
    pdf_doc.close()
    real_pdf_bytes = pdf_buf.getvalue()
    assert real_pdf_bytes.startswith(b"%PDF-")

    oss_key = f"{settings.OSS_KEY_PREFIX}/raw/test_step8/{doc_id}_arch_whitepaper.pdf"
    uploaded_key = await asyncio.to_thread(oss_service.upload_bytes, real_pdf_bytes, oss_key, "application/pdf")

    async with AsyncSessionLocal() as db:
        db.add(Department(id=dept_id, tenant_id="default_tenant", name=f"验证组_{suffix}", path=dept_path))
        await db.flush()
        db.add(
            KnowledgeBase(
                id=kb_id,
                tenant_id="default_tenant",
                name=f"验证研发库_{suffix}",
                owner_department_id=dept_id,
                owner_dept_path=dept_path,
                visibility="DEPARTMENT",
                min_classification_level=2,
                default_doc_level=2,
                max_doc_level=3,
                status="ACTIVE",
            )
        )
        await db.flush()
        db.add(
            Document(
                id=doc_id,
                tenant_id="default_tenant",
                kb_id=kb_id,
                classification_level=2,
                title="架构白皮书.pdf",
                file_hash=oss_service.calculate_sha256(real_pdf_bytes),
                raw_oss_url=uploaded_key,
                department_id=dept_id,
                department_path=dept_path,
                category="技术规范",
                tags=["真实OSS"],
                overall_status="active",
                current_step_index=11,
                pipeline_steps=[],
                version=1,
                is_active=True,
            )
        )
        await db.commit()

    test_app = FastAPI()
    test_app.include_router(system_router, prefix="/api/v1")
    test_app.include_router(documents_router, prefix="/api/v1")

    ticket = None
    legacy_ticket = f"legacy_cv2_{uuid.uuid4().hex}"
    try:
        async with _ephemeral_db_user(
            username="研发预览测试员",
            dept_path=dept_path,
            position_code="STAFF",
            role_codes=["employee"],
        ) as emp_u:
            emp_token = make_rs256_test_token(
                user_id=emp_u["user_id"],
                username=emp_u["username"],
                dept_path=emp_u["dept_path"],
                dept_paths=[emp_u["dept_path"]],
                roles=emp_u["roles"],
                authz_ver=emp_u["authz_ver"],
                policy_epoch=emp_u["policy_epoch"],
            )

            transport = httpx.ASGITransport(app=test_app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                # 渠道监控普通员工可读但无敏感凭据字段，重置熔断 403
                r_ch = await client.get("/api/v1/system/llm/channels", headers={"Authorization": f"Bearer {emp_token}"})
                assert r_ch.status_code == 200
                r_reset = await client.post(
                    "/api/v1/system/llm/channels/primary-default/reset",
                    headers={"Authorization": f"Bearer {emp_token}"},
                )
                assert r_reset.status_code == 403

                # 签发单文档 cv=1 preview 短票
                tk_resp = await client.post(
                    f"/api/v1/documents/{doc_id}/stream-ticket",
                    json={"purpose": "preview"},
                    headers={"Authorization": f"Bearer {emp_token}"},
                )
                assert tk_resp.status_code == 200
                ticket = tk_resp.json()["ticket"]

                # 全量预览与 Range 206 分片预览
                full_resp = await client.get(f"/api/v1/documents/{doc_id}/preview?ticket={ticket}")
                assert full_resp.status_code == 200
                assert full_resp.headers["cache-control"] == "private, no-store"
                assert full_resp.content == real_pdf_bytes

                range_resp = await client.get(
                    f"/api/v1/documents/{doc_id}/preview?ticket={ticket}",
                    headers={"Range": "bytes=0-15"},
                )
                assert range_resp.status_code == 206
                assert range_resp.headers["content-range"] == f"bytes 0-15/{len(real_pdf_bytes)}"
                assert range_resp.content == real_pdf_bytes[:16]

                # 拒绝非 cv=1 流式短票及内嵌 user.cv=2 快照
                redis_cli = get_redis_client()
                await redis_cli.set(
                    f"{STREAM_TICKET_KEY_PREFIX}{legacy_ticket}",
                    json.dumps({
                        "cv": 2,
                        "purpose": "preview",
                        "doc_id": str(doc_id),
                        "issued_at": int(time.time()),
                        "user": {
                            "user_id": emp_u["user_id"],
                            "tenant_id": "default_tenant",
                            "cv": 2,
                            "roles": ["employee"],
                            "authz_ver": 1,
                            "policy_epoch": 1,
                            "token_iat_ms": 0,
                        },
                    }),
                    ex=60,
                )
                legacy_tk_resp = await client.get(f"/api/v1/documents/{doc_id}/preview?ticket={legacy_ticket}")
                assert legacy_tk_resp.status_code == 401

                # 超过最大绝对寿命上限 -> 401
                raw_rec = json.loads(await redis_cli.get(f"{STREAM_TICKET_KEY_PREFIX}{ticket}"))
                assert raw_rec["cv"] == 1
                assert raw_rec["user"]["cv"] == 1
                assert "idp_roles" not in raw_rec["user"]
                raw_rec["issued_at"] = int(time.time()) - int(settings.STREAM_TICKET_MAX_LIFETIME_SECONDS) - 5
                await redis_cli.set(f"{STREAM_TICKET_KEY_PREFIX}{ticket}", json.dumps(raw_rec), ex=120)
                expired_cap_resp = await client.get(
                    f"/api/v1/documents/{doc_id}/preview?ticket={ticket}",
                    headers={"Range": "bytes=0-7"},
                )
                assert expired_cap_resp.status_code == 401
    finally:
        await get_redis_client().delete(f"{STREAM_TICKET_KEY_PREFIX}{legacy_ticket}")
        if ticket:
            await get_redis_client().delete(f"{STREAM_TICKET_KEY_PREFIX}{ticket}")
        try:
            bucket = oss_service.get_client()
            await asyncio.to_thread(bucket.delete_object, uploaded_key)
        except Exception:
            pass
        async with AsyncSessionLocal() as db:
            await db.execute(delete(Document).where(Document.id == doc_id))
            await db.execute(delete(KnowledgeBase).where(KnowledgeBase.id == kb_id))
            await db.execute(delete(Department).where(Department.id == dept_id))
            await db.commit()


def test_11_cross_stack_permission_constants_static_alignment():
    """测试 11: 静态扫描 V1/V2/V5 SQL、Java PermissionResolver、Python Permissions 与前端 PERMISSIONS 字典对齐"""
    repo_root = Path(__file__).resolve().parent.parent.parent
    assert "user:authz:manage" not in KNOWN_PERMISSION_VALUES

    for sql_rel in (
        "java-admin-service/src/main/resources/db/migration/V1__rbac.sql",
        "java-admin-service/src/main/resources/db/migration/V2__enterprise_multidim_authz.sql",
    ):
        sql_text = (repo_root / sql_rel).read_text(encoding="utf-8")
        sql_perms = set(re.findall(r'"(\*|[a-z]+(?::[a-z_]+)+)"', sql_text))
        sql_perms.discard("user:authz:manage")
        assert sql_perms.issubset(KNOWN_PERMISSION_VALUES), f"{sql_rel} 存在未知权限点: {sql_perms - KNOWN_PERMISSION_VALUES}"

    v5_text = (repo_root / "java-admin-service/src/main/resources/db/migration/V5__abac_attribute_constraints.sql").read_text(
        encoding="utf-8"
    )
    assert '"user:authz:manage"' not in v5_text

    java_resolver = (
        repo_root / "java-admin-service/src/main/java/com/enterprise/rag/admin/identity/PermissionResolver.java"
    ).read_text(encoding="utf-8")
    java_perms = set(
        re.findall(r'"(\*|[a-z]+(?::[a-z_]+)+)"', java_resolver.split("KNOWN_PERMISSIONS")[1].split(";")[0])
    )
    assert java_perms == KNOWN_PERMISSION_VALUES

    fe_auth = (repo_root / "frontend/src/stores/auth.ts").read_text(encoding="utf-8")
    assert "withTokenUrl" not in fe_auth
    assert "user:authz:manage" not in fe_auth
    fe_perms = set(
        re.findall(r"'(\*|[a-z]+(?::[a-z_]+)+)'", fe_auth.split("PERMISSIONS =")[1].split("as const")[0])
    )
    assert fe_perms == KNOWN_PERMISSION_VALUES


@pytest.mark.asyncio
async def test_12_model_credentials_lease_caching_concurrency_and_fail_fast():
    """测试 12: 模型凭据租约缓存、10 并发去重与服务端不可达 Fail-Fast"""
    _CREDENTIAL_CACHE.clear()
    call_count = 0

    class _FakeResp:
        status_code = 200

        def json(self):
            return {
                "provider": "llm",
                "api_key": "sk-leased-test-key",
                "base_url": "https://example.invalid/v1",
                "expires_at": int(time.time()) + 300,
                "masked": "sk-****key",
            }

    async def _fake_post(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0.05)
        return _FakeResp()

    with patch("httpx.AsyncClient.post", side_effect=_fake_post):
        results = await asyncio.gather(*[get_credential("llm") for _ in range(10)])
        assert len(results) == 10
        assert all(isinstance(r, ModelCredential) and r.api_key == "sk-leased-test-key" for r in results)
        assert call_count == 1
        assert get_cached_lease_info("llm") is not None

    _CREDENTIAL_CACHE.clear()
    with patch("httpx.AsyncClient.post", side_effect=httpx.ConnectError("auth center unreachable")):
        with pytest.raises(RuntimeError):
            await get_credential("llm")
    _CREDENTIAL_CACHE.clear()
