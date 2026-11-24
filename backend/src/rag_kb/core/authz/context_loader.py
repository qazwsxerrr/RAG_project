"""src/rag_kb/core/authz/context_loader.py —— 实时双版本核验、到期感知缓存与有效权限上下文加载。"""

import time
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence, Tuple
from fastapi import HTTPException, status
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from rag_kb.core.authz.models import EffectiveAuthzContext, normalize_dept_path
from rag_kb.core.authz.sql_filters import build_grant_subject_match
from rag_kb.core.constants import ACTION_TO_PERMISSION, DATA_SCOPE_WEIGHTS, KbAction, Permissions
from rag_kb.db.models import (
    Department,
    Document,
    KbGrant,
    Position,
    Role,
    SysUser,
    TenantPolicyEpoch,
    UserDepartment,
)

_CACHE_TTL_SECONDS = 15.0
_AUTHZ_CACHE: Dict[Tuple[str, str, int, int, Tuple[str, ...]], Tuple[float, EffectiveAuthzContext]] = {}


def invalidate_authz_cache(tenant_id: Optional[str] = None, user_id: Optional[str] = None) -> None:
    if tenant_id is None and user_id is None:
        _AUTHZ_CACHE.clear()
        return
    to_drop = [
        k for k in _AUTHZ_CACHE
        if (tenant_id is None or k[0] == tenant_id) and (user_id is None or k[1] == user_id)
    ]
    for k in to_drop:
        _AUTHZ_CACHE.pop(k, None)


def _compute_scope_and_perms(role_rows: Sequence[Role]) -> Tuple[List[str], Dict[str, str]]:
    perms_union: List[str] = []
    perms_seen = set()
    scope_by_action: Dict[str, str] = {}

    for role in role_rows:
        r_scope = str(role.data_scope or "").upper().strip()
        r_weight = DATA_SCOPE_WEIGHTS.get(r_scope, -1)
        if r_weight < 0:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"【策略配置错误】角色 [{role.role_code}] 存在非法 data_scope: {role.data_scope}",
            )
        r_perms = [str(p).strip() for p in (role.permissions or []) if str(p).strip()]
        for p in r_perms:
            if p not in perms_seen:
                perms_seen.add(p)
                perms_union.append(p)

        has_wildcard = Permissions.ALL.value in r_perms
        for action, req_perm in ACTION_TO_PERMISSION.items():
            if has_wildcard or req_perm in r_perms:
                cur_best = scope_by_action.get(action)
                if cur_best is None or r_weight > DATA_SCOPE_WEIGHTS.get(cur_best, -1):
                    scope_by_action[action] = r_scope

    if Permissions.ALL.value in perms_seen:
        perms_union = [Permissions.ALL.value]
    return perms_union, scope_by_action


async def _fetch_nearest_expiry_ts(
    db: AsyncSession,
    tenant_id: str,
    user_uuid: uuid.UUID,
    active_roles: Sequence[str],
    active_dept_ids: Sequence[str],
    active_dept_paths: Sequence[str],
    now_utc: datetime,
    dept_rows: Sequence[UserDepartment],
) -> Optional[float]:
    exp_candidates: List[float] = []
    for d in dept_rows:
        if d.expires_at is not None:
            exp = d.expires_at if d.expires_at.tzinfo else d.expires_at.replace(tzinfo=timezone.utc)
            if exp > now_utc:
                exp_candidates.append(exp.timestamp())

    temp_ctx = EffectiveAuthzContext(
        user_id=str(user_uuid),
        tenant_id=tenant_id,
        username="",
        roles=list(active_roles),
        dept_ids=list(active_dept_ids),
        dept_paths=list(active_dept_paths),
    )
    min_grant_exp = (
        await db.execute(
            select(func.min(KbGrant.expires_at)).where(
                KbGrant.tenant_id == tenant_id,
                KbGrant.expires_at.isnot(None),
                KbGrant.expires_at > now_utc,
                build_grant_subject_match(temp_ctx),
            )
        )
    ).scalar_one_or_none()
    if min_grant_exp is not None:
        exp = min_grant_exp if min_grant_exp.tzinfo else min_grant_exp.replace(tzinfo=timezone.utc)
        exp_candidates.append(exp.timestamp())
    return min(exp_candidates) if exp_candidates else None


async def load_effective_authz_context(
    db: AsyncSession,
    user_id: str,
    tenant_id: str,
    username: str,
    idp_roles: Sequence[str],
    token_authz_ver: int,
    token_policy_epoch: int,
    token_iat_ms: int,
    token_exp: int,
    delegated_agent_id: Optional[str] = None,
) -> EffectiveAuthzContext:
    """
    依据数据库实时核对账号状态与双版本号，并按企业 IdP 角色 + 同步多部门 + 职级密级映射表构建 EffectiveAuthzContext。
    """
    try:
        user_uuid = uuid.UUID(str(user_id).strip())
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="【鉴权失败】JWT sub 不是合法的员工 UUID",
            headers={"WWW-Authenticate": "Bearer"},
        )

    db_user = (
        await db.execute(select(SysUser).where(SysUser.tenant_id == tenant_id, SysUser.id == user_uuid))
    ).scalar_one_or_none()
    if db_user is None or not db_user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="【鉴权失败】员工账号不存在、跨租户或已停用",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if int(db_user.authz_version) != int(token_authz_ver):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="【凭据已失效】个人授权版本已变更，请重新换取最新业务令牌",
            headers={"WWW-Authenticate": "Bearer"},
        )

    epoch_row = await db.get(TenantPolicyEpoch, tenant_id)
    current_policy_epoch = int(epoch_row.policy_epoch) if epoch_row is not None else 1
    if current_policy_epoch != int(token_policy_epoch):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="【凭据已失效】租户安全策略版本已变更，请重新换取最新业务令牌",
            headers={"WWW-Authenticate": "Bearer"},
        )

    norm_roles = tuple(sorted({str(r).strip() for r in (idp_roles or []) if str(r).strip()}))
    cache_key = (tenant_id, str(user_uuid), int(db_user.authz_version), current_policy_epoch, norm_roles)
    now_ts = time.time()
    cached = _AUTHZ_CACHE.get(cache_key)
    if cached is not None:
        expires_at_ts, cached_ctx = cached
        if now_ts < expires_at_ts:
            return cached_ctx.model_copy(
                update={
                    "token_iat_ms": token_iat_ms,
                    "token_exp": token_exp,
                    "delegated_agent_id": delegated_agent_id,
                }
            )
        _AUTHZ_CACHE.pop(cache_key, None)

    if not norm_roles:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="【权限不足】员工当前无任何有效企业角色")

    role_defs = (
        await db.execute(select(Role).where(Role.tenant_id == tenant_id, Role.role_code.in_(norm_roles)))
    ).scalars().all()
    if len(role_defs) != len(norm_roles):
        missing = sorted(set(norm_roles) - {r.role_code for r in role_defs})
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"【权限拒绝】存在未在当前租户策略字典对齐的角色码: {missing}",
        )
    perms_union, scope_by_action = _compute_scope_and_perms(role_defs)

    db_now = (await db.execute(select(func.now()))).scalar_one()
    now_utc = db_now if db_now.tzinfo else db_now.replace(tzinfo=timezone.utc)

    dept_join_rows = (
        await db.execute(
            select(UserDepartment, Department)
            .join(
                Department,
                and_(Department.tenant_id == UserDepartment.tenant_id, Department.id == UserDepartment.department_id),
            )
            .where(
                UserDepartment.tenant_id == tenant_id,
                UserDepartment.user_id == user_uuid,
                Department.status == "ACTIVE",
                or_(UserDepartment.expires_at.is_(None), UserDepartment.expires_at > func.now()),
            )
            .order_by(UserDepartment.is_primary.desc(), UserDepartment.created_at.asc())
        )
    ).all()

    dedup_dept_paths: List[str] = []
    dedup_dept_ids: List[str] = []
    seen_dept_ids = set()
    primary_dept_path = ""
    raw_ud_rows: List[UserDepartment] = []
    for ud, dept in dept_join_rows:
        raw_ud_rows.append(ud)
        norm_p = normalize_dept_path(dept.path)
        if ud.is_primary and not primary_dept_path and norm_p:
            primary_dept_path = norm_p
        did_str = str(dept.id)
        if did_str not in seen_dept_ids and norm_p:
            seen_dept_ids.add(did_str)
            dedup_dept_ids.append(did_str)
            dedup_dept_paths.append(norm_p)

    pos_row = await db.get(Position, (tenant_id, db_user.position_code))
    if pos_row is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"【策略配置错误】员工职级 [{db_user.position_code}] 在 sys_positions 中不存在",
        )
    effective_clearance = int(pos_row.default_clearance_level)

    nearest_expiry_ts = await _fetch_nearest_expiry_ts(
        db=db, tenant_id=tenant_id, user_uuid=user_uuid,
        active_roles=norm_roles, active_dept_ids=dedup_dept_ids,
        active_dept_paths=dedup_dept_paths, now_utc=now_utc, dept_rows=raw_ud_rows,
    )

    ctx = EffectiveAuthzContext(
        user_id=str(user_uuid),
        tenant_id=tenant_id,
        username=db_user.real_name or username,
        is_active=True,
        dept_path=primary_dept_path,
        dept_paths=dedup_dept_paths,
        dept_ids=dedup_dept_ids,
        roles=list(norm_roles),
        idp_roles=list(norm_roles),
        perms=perms_union,
        scope_by_action=scope_by_action,
        position_code=db_user.position_code,
        effective_clearance=effective_clearance,
        authz_ver=int(db_user.authz_version),
        policy_epoch=current_policy_epoch,
        is_to_c=False,
        cv=2,
        token_iat_ms=token_iat_ms,
        token_exp=token_exp,
        delegated_agent_id=delegated_agent_id,
        nearest_expiry_ts=nearest_expiry_ts,
    )

    cache_until = now_ts + _CACHE_TTL_SECONDS
    if nearest_expiry_ts is not None:
        cache_until = min(cache_until, nearest_expiry_ts)
    if cache_until > now_ts:
        _AUTHZ_CACHE[cache_key] = (cache_until, ctx)
    return ctx


async def get_db_version_and_expiry_error(
    db: AsyncSession,
    ctx: EffectiveAuthzContext,
    watched_doc_ids: Optional[Sequence[uuid.UUID]] = None,
) -> Optional[str]:
    """SSE 与长连接发送受保护事件前的实时双版本与自然到期复核"""
    from rag_kb.core.authz.guards import assert_doc_access

    try:
        user_uuid = uuid.UUID(str(ctx.user_id))
    except ValueError:
        return "auth_revoked"

    db_user = (
        await db.execute(select(SysUser).where(SysUser.tenant_id == ctx.tenant_id, SysUser.id == user_uuid))
    ).scalar_one_or_none()
    if db_user is None or not db_user.is_active or int(db_user.authz_version) != int(ctx.authz_ver):
        return "auth_revoked"

    epoch_row = await db.get(TenantPolicyEpoch, ctx.tenant_id)
    if (int(epoch_row.policy_epoch) if epoch_row is not None else 1) != int(ctx.policy_epoch):
        return "auth_revoked"

    now_ts = time.time()
    if ctx.token_exp and int(now_ts) >= int(ctx.token_exp):
        return "auth_expired"

    if ctx.nearest_expiry_ts is not None and now_ts >= ctx.nearest_expiry_ts:
        invalidate_authz_cache(tenant_id=ctx.tenant_id, user_id=ctx.user_id)
        try:
            refreshed = await load_effective_authz_context(
                db=db, user_id=ctx.user_id, tenant_id=ctx.tenant_id, username=ctx.username,
                idp_roles=ctx.idp_roles, token_authz_ver=ctx.authz_ver,
                token_policy_epoch=ctx.policy_epoch, token_iat_ms=ctx.token_iat_ms,
                token_exp=ctx.token_exp, delegated_agent_id=ctx.delegated_agent_id,
            )
        except HTTPException:
            return "auth_revoked"

        if not refreshed.has_permission(Permissions.KB_RETRIEVE.value):
            return "auth_revoked"
        if watched_doc_ids:
            for did in watched_doc_ids:
                doc = await db.get(Document, did)
                if doc is None:
                    return "auth_revoked"
                try:
                    await assert_doc_access(db, refreshed, doc, KbAction.READ.value, audit_denial=False)
                except HTTPException:
                    return "auth_revoked"
        elif set(refreshed.dept_paths) != set(ctx.dept_paths):
            return "auth_revoked"
    return None
