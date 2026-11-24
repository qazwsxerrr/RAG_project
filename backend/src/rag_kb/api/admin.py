"""src/rag_kb/api/admin.py —— RAG 权限关系治理控制面 API (/api/v1/admin/*)。

架构边界：
- 员工身份、多部门、角色归属与职级由外部企业 IdP/HR 同步（Java 认证网关仅负责 OIDC 登录与 RS256 cv=2 签发）；
- 本模块直接复用 Python SQLAlchemy ORM、deps.py 实时鉴权与 sql_filters.py 单一策略真源 (SSOT)，
  仅维护：职级→L1-L4 密级映射、角色→RAG 权限映射、知识库、精细化 ACL、应急封禁与安全审计查询。
"""

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import and_, desc, func, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from rag_kb.api.deps import (
    EffectiveAuthzContext,
    get_current_user_with_db,
    publish_tenant_policy_epoch_to_redis,
    restore_user_access,
    revoke_user_access,
)
from rag_kb.core.authz.guards import assert_kb_access
from rag_kb.core.authz.models import is_path_under_prefix, normalize_dept_path
from rag_kb.core.authz.sql_filters import build_kb_access_condition
from rag_kb.core.constants import (
    ACTION_TO_PERMISSION,
    DATA_SCOPE_WEIGHTS,
    DataScope,
    KbAction,
    Permissions,
)
from rag_kb.core.logging.audit import AuditWriteError, record_security_audit_in_tx
from rag_kb.db.models import (
    AuditLog,
    Department,
    Document,
    KbGrant,
    KnowledgeBase,
    Position,
    Role,
    SysUser,
    UserDepartment,
)
from rag_kb.db.session import get_db

router = APIRouter(prefix="/admin", tags=["RAG 权限关系治理"])

VALID_DATA_SCOPES = {s.value for s in DataScope}
VALID_VISIBILITIES = {"PUBLIC", "DEPARTMENT", "RESTRICTED"}
VALID_KB_STATUSES = {"ACTIVE", "ARCHIVED"}
VALID_SUBJECT_TYPES = {"USER", "DEPARTMENT", "ROLE"}
VALID_KB_ACTIONS = {a.value for a in KbAction}
VALID_GRANT_EFFECTS = {"ALLOW", "DENY"}


class UpsertPositionReq(BaseModel):
    positionCode: str = Field(..., min_length=1, max_length=50)
    positionName: str = Field(..., min_length=1, max_length=100)
    rankOrder: int = Field(default=20, ge=0, le=9999)
    defaultClearanceLevel: int = Field(..., ge=1, le=4)
    description: Optional[str] = None
    reason: Optional[str] = None


class UpsertRoleReq(BaseModel):
    roleCode: str = Field(..., min_length=1, max_length=50)
    roleName: str = Field(..., min_length=1, max_length=100)
    dataScope: str
    permissions: List[str] = Field(default_factory=list)
    reason: Optional[str] = None


class CreateKbReq(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    description: Optional[str] = None
    ownerDepartmentId: Optional[str] = None
    ownerDeptPath: Optional[str] = None
    visibility: str = "DEPARTMENT"
    minClassificationLevel: int = Field(default=1, ge=1, le=4)
    defaultDocLevel: int = Field(default=2, ge=1, le=4)
    maxDocLevel: int = Field(default=4, ge=1, le=4)
    reason: Optional[str] = None


class UpdateKbReq(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    visibility: Optional[str] = None
    minClassificationLevel: Optional[int] = Field(default=None, ge=1, le=4)
    defaultDocLevel: Optional[int] = Field(default=None, ge=1, le=4)
    maxDocLevel: Optional[int] = Field(default=None, ge=1, le=4)
    status: Optional[str] = None
    reason: Optional[str] = None


class CreateKbGrantReq(BaseModel):
    subjectType: str
    subjectId: str = Field(..., min_length=1, max_length=128)
    includeSubDepts: bool = False
    action: str
    effect: str = "ALLOW"
    expiresAt: Optional[datetime] = None
    reason: Optional[str] = None


# ==================== 公共校验、序列化与原子事务提交 ====================


def _require_any_perm(user: EffectiveAuthzContext, *perms: Permissions) -> None:
    if any(user.has_permission(p) for p in perms):
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=f"【权限不足】需要以下任一权限点: [{', '.join(p.value for p in perms)}]",
    )


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _parse_uuid(raw: str, label: str) -> uuid.UUID:
    try:
        return uuid.UUID(raw.strip())
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"无效的 {label} UUID")


def _validate_levels(min_lvl: int, default_lvl: int, max_lvl: int, visibility: Optional[str] = None) -> None:
    if not (1 <= min_lvl <= default_lvl <= max_lvl <= 4):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"密级区间非法，必须满足 1 <= min({min_lvl}) <= default({default_lvl}) <= max({max_lvl}) <= 4",
        )
    if visibility == "PUBLIC" and (min_lvl, default_lvl, max_lvl) != (1, 1, 1):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="全员公开知识库 (PUBLIC) 仅允许 L1 文档，密级区间必须为 [1, 1, 1]",
        )


async def _commit_policy_change(
    db: AsyncSession,
    user: EffectiveAuthzContext,
    action: str,
    resource_id: str,
    details: Dict[str, Any],
    conflict_msg: Optional[str] = None,
) -> int:
    """统一原子提交策略变更：flush -> 递增 policy_epoch -> 强审计落库 -> commit -> 推 Redis（审计失败必回滚）"""
    try:
        await db.flush()
        row = (
            await db.execute(
                text(
                    "INSERT INTO sys_tenant_policy_epochs (tenant_id, policy_epoch, updated_at) "
                    "VALUES (:tid, 2, NOW()) "
                    "ON CONFLICT (tenant_id) DO UPDATE "
                    "SET policy_epoch = sys_tenant_policy_epochs.policy_epoch + 1, updated_at = NOW() "
                    "RETURNING policy_epoch"
                ),
                {"tid": user.tenant_id},
            )
        ).first()
        new_epoch = int(row[0])
        await record_security_audit_in_tx(
            db=db,
            action=action,
            resource_id=resource_id,
            status="SUCCESS",
            details={**details, "operator": user.username, "policy_epoch": new_epoch},
            tenant_id=user.tenant_id,
            user_id=user.user_id,
        )
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=conflict_msg or "记录已存在，违反唯一约束")
    except AuditWriteError as ae:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(ae))
    except HTTPException:
        await db.rollback()
        raise
    except Exception:
        await db.rollback()
        raise

    await publish_tenant_policy_epoch_to_redis(user.tenant_id, new_epoch)
    return new_epoch


def _serialize_kb(kb: KnowledgeBase) -> Dict[str, Any]:
    return {
        "id": str(kb.id), "tenantId": kb.tenant_id, "name": kb.name, "description": kb.description,
        "ownerDepartmentId": str(kb.owner_department_id) if kb.owner_department_id else None,
        "ownerDeptPath": kb.owner_dept_path, "visibility": kb.visibility,
        "minClassificationLevel": int(kb.min_classification_level),
        "defaultDocLevel": int(kb.default_doc_level), "maxDocLevel": int(kb.max_doc_level),
        "status": kb.status, "createdBy": kb.created_by,
        "createdAt": _iso(kb.created_at), "updatedAt": _iso(kb.updated_at),
    }


def _serialize_grant(g: KbGrant) -> Dict[str, Any]:
    return {
        "id": str(g.id), "tenantId": g.tenant_id, "kbId": str(g.kb_id),
        "subjectType": g.subject_type, "subjectId": g.subject_id, "subjectDeptPath": g.subject_dept_path,
        "includeSubDepts": bool(g.include_sub_depts), "action": g.action, "effect": g.effect,
        "expiresAt": _iso(g.expires_at), "reason": g.reason, "grantedBy": g.granted_by,
        "createdAt": _iso(g.created_at),
    }


def _serialize_position(p: Position) -> Dict[str, Any]:
    return {
        "tenantId": p.tenant_id, "positionCode": p.position_code, "positionName": p.position_name,
        "rankOrder": int(p.rank_order), "defaultClearanceLevel": int(p.default_clearance_level),
        "description": p.description, "updatedAt": _iso(p.updated_at),
    }


def _serialize_role(r: Role) -> Dict[str, Any]:
    return {
        "tenantId": r.tenant_id, "roleCode": r.role_code, "roleName": r.role_name,
        "dataScope": r.data_scope, "permissionsJson": json.dumps(r.permissions or [], ensure_ascii=False),
    }


# ==================== 1. 企业同步部门目录（只读） ====================


@router.get("/departments")
async def list_admin_departments(
    db: AsyncSession = Depends(get_db),
    user: EffectiveAuthzContext = Depends(get_current_user_with_db),
):
    _require_any_perm(user, Permissions.KB_MANAGE, Permissions.KB_GRANT, Permissions.SYSTEM_ADMIN)
    rows = (
        await db.execute(
            select(Department).where(Department.tenant_id == user.tenant_id)
            .order_by(Department.sort_order.asc(), Department.path.asc())
        )
    ).scalars().all()
    return [
        {
            "id": str(d.id), "tenantId": d.tenant_id,
            "parentId": str(d.parent_id) if d.parent_id else None,
            "name": d.name, "path": d.path, "sortOrder": int(d.sort_order or 0),
            "status": d.status, "createdAt": _iso(d.created_at),
        }
        for d in rows
    ]


# ==================== 2. 企业职级 -> L1-L4 密级策略映射 ====================


@router.get("/positions")
async def list_positions(
    db: AsyncSession = Depends(get_db),
    user: EffectiveAuthzContext = Depends(get_current_user_with_db),
):
    _require_any_perm(user, Permissions.SYSTEM_ADMIN)
    rows = (
        await db.execute(
            select(Position).where(Position.tenant_id == user.tenant_id)
            .order_by(Position.rank_order.asc(), Position.position_code.asc())
        )
    ).scalars().all()
    return [_serialize_position(p) for p in rows]


@router.put("/positions")
async def upsert_position(
    req: UpsertPositionReq,
    db: AsyncSession = Depends(get_db),
    user: EffectiveAuthzContext = Depends(get_current_user_with_db),
):
    _require_any_perm(user, Permissions.SYSTEM_ADMIN)
    code, name = req.positionCode.strip().upper(), req.positionName.strip()
    existing = await db.get(Position, (user.tenant_id, code))
    old_level = int(existing.default_clearance_level) if existing else None

    # 防间接自提权：禁止管理员上调自身所属职级的默认密级
    if code == user.position_code.upper():
        baseline = old_level if old_level is not None else int(user.effective_clearance)
        if req.defaultClearanceLevel > baseline:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"【防间接自提权拦截】禁止将自身所属职级 [{code}] 的默认密级从 L{baseline} 上调至 L{req.defaultClearanceLevel}",
            )

    now_utc = datetime.now(timezone.utc)
    if existing:
        existing.position_name, existing.rank_order = name, req.rankOrder
        existing.default_clearance_level, existing.description, existing.updated_at = (
            req.defaultClearanceLevel, req.description, now_utc,
        )
        pos = existing
    else:
        pos = Position(
            tenant_id=user.tenant_id, position_code=code, position_name=name,
            rank_order=req.rankOrder, default_clearance_level=req.defaultClearanceLevel,
            description=req.description, updated_at=now_utc,
        )
        db.add(pos)

    await _commit_policy_change(
        db, user, "ADMIN_POSITION_UPSERT", code,
        {"position_code": code, "previous_clearance": old_level, "new_clearance": req.defaultClearanceLevel, "reason": req.reason},
    )
    return _serialize_position(pos)


# ==================== 3. 企业角色编码 -> RAG 权限与数据范围映射 ====================


@router.get("/roles")
async def list_roles(
    db: AsyncSession = Depends(get_db),
    user: EffectiveAuthzContext = Depends(get_current_user_with_db),
):
    _require_any_perm(user, Permissions.KB_MANAGE, Permissions.KB_GRANT, Permissions.SYSTEM_ADMIN)
    rows = (
        await db.execute(select(Role).where(Role.tenant_id == user.tenant_id).order_by(Role.role_code.asc()))
    ).scalars().all()
    return [_serialize_role(r) for r in rows]


@router.put("/roles")
async def upsert_role(
    req: UpsertRoleReq,
    db: AsyncSession = Depends(get_db),
    user: EffectiveAuthzContext = Depends(get_current_user_with_db),
):
    _require_any_perm(user, Permissions.SYSTEM_ADMIN)
    code, name, scope = req.roleCode.strip(), req.roleName.strip(), req.dataScope.strip().upper()
    if scope not in VALID_DATA_SCOPES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"无效的 dataScope: {scope}")

    clean_perms = sorted({str(p).strip() for p in (req.permissions or []) if str(p).strip()})
    if Permissions.ALL.value not in (user.perms or []):
        missing = [p for p in clean_perms if p not in (user.perms or [])]
        if missing:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"【防越权授信拦截】禁止授予自身未持有的权限点: {missing}")

    existing = await db.get(Role, (user.tenant_id, code))
    if code in (user.roles or []) and existing is not None:
        if DATA_SCOPE_WEIGHTS.get(scope, 0) > DATA_SCOPE_WEIGHTS.get(existing.data_scope, 0):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"【防间接自提权拦截】禁止扩大自身持有角色 [{code}] 的数据范围 ({existing.data_scope} -> {scope})",
            )

    prev_scope = existing.data_scope if existing else None
    prev_perms = list(existing.permissions or []) if existing else []
    if existing:
        existing.role_name, existing.data_scope, existing.permissions = name, scope, clean_perms
        role = existing
    else:
        role = Role(tenant_id=user.tenant_id, role_code=code, role_name=name, data_scope=scope, permissions=clean_perms)
        db.add(role)

    await _commit_policy_change(
        db, user, "ADMIN_ROLE_UPSERT", code,
        {"role_code": code, "previous_scope": prev_scope, "new_scope": scope, "previous_perms": prev_perms, "new_perms": clean_perms, "reason": req.reason},
    )
    return _serialize_role(role)


# ==================== 4. 已同步员工只读列表 & 应急离职封禁 ====================


@router.get("/users")
async def list_synced_users(
    db: AsyncSession = Depends(get_db),
    user: EffectiveAuthzContext = Depends(get_current_user_with_db),
):
    _require_any_perm(user, Permissions.KB_MANAGE, Permissions.KB_GRANT, Permissions.SYSTEM_ADMIN)
    pos_map = {
        p.position_code: p
        for p in (await db.execute(select(Position).where(Position.tenant_id == user.tenant_id))).scalars().all()
    }
    users = (
        await db.execute(select(SysUser).where(SysUser.tenant_id == user.tenant_id).order_by(SysUser.created_at.desc()))
    ).scalars().all()
    dept_rows = (
        await db.execute(
            select(UserDepartment.user_id, Department.path)
            .select_from(UserDepartment)
            .join(
                Department,
                and_(
                    Department.tenant_id == UserDepartment.tenant_id,
                    Department.id == UserDepartment.department_id,
                ),
            )
            .where(
                UserDepartment.tenant_id == user.tenant_id,
                Department.status == "ACTIVE",
                or_(UserDepartment.expires_at.is_(None), UserDepartment.expires_at > func.now()),
            )
            .order_by(UserDepartment.is_primary.desc(), Department.path.asc())
        )
    ).all()
    depts_by_user: Dict[uuid.UUID, List[str]] = {}
    for uid, raw_path in dept_rows:
        norm = normalize_dept_path(raw_path)
        if not norm:
            continue
        bucket = depts_by_user.setdefault(uid, [])
        if norm not in bucket:
            bucket.append(norm)

    return [
        {
            "userId": str(u.id), "tenantId": u.tenant_id, "externalId": u.external_id, "username": u.real_name,
            "isActive": bool(u.is_active), "positionCode": u.position_code,
            "positionName": pos_map[u.position_code].position_name if u.position_code in pos_map else u.position_code,
            "effectiveClearanceLevel": int(pos_map[u.position_code].default_clearance_level) if u.position_code in pos_map else 2,
            "authzVersion": int(u.authz_version),
            "primaryDeptPath": (depts_by_user.get(u.id) or [""])[0],
            "effectiveDeptPaths": depts_by_user.get(u.id, []),
            "lastLoginAt": _iso(u.last_login_at), "createdAt": _iso(u.created_at),
        }
        for u in users
    ]


class RevokeUserRequest(BaseModel):
    ttl_seconds: Optional[int] = Field(default=None, ge=600, le=604800)
    reason: Optional[str] = Field(default=None, max_length=200)


@router.post("/users/{user_id}/revoke")
async def admin_revoke_user(
    user_id: str,
    req: Optional[RevokeUserRequest] = None,
    db: AsyncSession = Depends(get_db),
    user: EffectiveAuthzContext = Depends(get_current_user_with_db),
):
    """同租户账号停用与秒级吊销闭环接口（DB + authz_version + 同事务安全审计 + Redis 同步）"""
    _require_any_perm(user, Permissions.SYSTEM_ADMIN)
    try:
        target_uuid = uuid.UUID(user_id.strip())
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="无效的员工 user_id UUID 格式")

    if str(target_uuid) == str(user.user_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="禁止停用当前操作者本人账号")

    try:
        res = await db.execute(
            text(
                "UPDATE sys_users SET is_active = FALSE, authz_version = authz_version + 1 "
                "WHERE id = :uid AND tenant_id = :tid RETURNING real_name, authz_version"
            ),
            {"uid": target_uuid, "tid": user.tenant_id},
        )
        row = res.first()
        if row is None:
            await db.rollback()
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"未在当前租户中找到员工账号 [{user_id}]",
            )

        target_name, new_ver = row[0], int(row[1])
        ttl = req.ttl_seconds if req else None
        await record_security_audit_in_tx(
            db=db,
            action="AUTH_USER_REVOKE",
            resource_id=str(target_uuid),
            status="SUCCESS",
            details={
                "operator": user.username,
                "target_username": target_name,
                "db_is_active": False,
                "authz_version": new_ver,
                "ttl_seconds": ttl,
                "reason": req.reason if req else None,
            },
            tenant_id=user.tenant_id,
            user_id=user.user_id,
        )
        await db.commit()
    except HTTPException:
        raise
    except AuditWriteError as ae:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(ae))
    except Exception:
        await db.rollback()
        raise

    await revoke_user_access(str(target_uuid), ttl_seconds=ttl, authz_version=new_ver)
    return {
        "status": "revoked",
        "user_id": str(target_uuid),
        "is_active": False,
        "authz_version": new_ver,
        "message": f"账号 [{target_name}] 已停用并执行秒级令牌吊销",
    }


@router.delete("/users/{user_id}/revoke")
async def admin_restore_user(
    user_id: str,
    db: AsyncSession = Depends(get_db),
    user: EffectiveAuthzContext = Depends(get_current_user_with_db),
):
    """恢复同租户指定员工的 sys_users.is_active=TRUE、递增 authz_version 并同步清除 Redis 吊销拦截标记"""
    _require_any_perm(user, Permissions.SYSTEM_ADMIN)
    try:
        target_uuid = uuid.UUID(user_id.strip())
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="无效的员工 user_id UUID 格式")

    try:
        res = await db.execute(
            text(
                "UPDATE sys_users SET is_active = TRUE, authz_version = authz_version + 1 "
                "WHERE id = :uid AND tenant_id = :tid RETURNING real_name, authz_version"
            ),
            {"uid": target_uuid, "tid": user.tenant_id},
        )
        row = res.first()
        if row is None:
            await db.rollback()
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"未在当前租户中找到员工账号 [{user_id}]",
            )

        target_name, new_ver = row[0], int(row[1])
        await record_security_audit_in_tx(
            db=db,
            action="AUTH_USER_RESTORE",
            resource_id=str(target_uuid),
            status="SUCCESS",
            details={
                "operator": user.username,
                "target_username": target_name,
                "db_is_active": True,
                "authz_version": new_ver,
            },
            tenant_id=user.tenant_id,
            user_id=user.user_id,
        )
        await db.commit()
    except HTTPException:
        raise
    except AuditWriteError as ae:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(ae))
    except Exception:
        await db.rollback()
        raise

    removed = await restore_user_access(str(target_uuid), authz_version=new_ver)
    return {
        "status": "restored",
        "user_id": str(target_uuid),
        "is_active": True,
        "authz_version": new_ver,
        "removed": removed,
    }


# ==================== 5. 知识库 CRUD 与精细化 ACL ====================


def _is_org_wide_kb_operator(user: EffectiveAuthzContext) -> bool:
    """判断操作者是否具备全公司级知识库治理范围（system:admin 或 MANAGE=ALL_ORG）"""
    return user.has_permission(Permissions.SYSTEM_ADMIN) or (
        user.scope_for_action(KbAction.MANAGE.value) == DataScope.ALL_ORG.value
    )


def _is_dept_in_manage_scope(user: EffectiveAuthzContext, target_dept_path: Optional[str]) -> bool:
    """校验目标部门路径是否落在操作者 MANAGE 组织范围内（严格按路径段前缀匹配）"""
    if _is_org_wide_kb_operator(user):
        return True
    norm_target = normalize_dept_path(target_dept_path)
    if not norm_target:
        return False
    manage_scope = user.scope_for_action(KbAction.MANAGE.value)
    user_depts = [normalize_dept_path(p) for p in (user.dept_paths or []) if normalize_dept_path(p)]
    if not user_depts:
        return False
    if manage_scope == DataScope.DEPT_AND_SUB.value:
        return any(is_path_under_prefix(norm_target, p) for p in user_depts)
    return False


async def _load_user_effective_dept_paths(
    db: AsyncSession, tenant_id: str, target_user: SysUser
) -> List[str]:
    """加载目标员工当前有效的全部归属部门路径（与正式鉴权严格一致：仅认 ACTIVE 部门的未过期归属，不做展示字段兜底）"""
    rows = (
        await db.execute(
            select(Department.path)
            .select_from(UserDepartment)
            .join(
                Department,
                and_(
                    Department.tenant_id == UserDepartment.tenant_id,
                    Department.id == UserDepartment.department_id,
                ),
            )
            .where(
                UserDepartment.tenant_id == tenant_id,
                UserDepartment.user_id == target_user.id,
                Department.status == "ACTIVE",
                or_(UserDepartment.expires_at.is_(None), UserDepartment.expires_at > func.now()),
            )
            .order_by(UserDepartment.is_primary.desc(), UserDepartment.created_at.asc())
        )
    ).scalars().all()
    paths: List[str] = []
    for raw in rows:
        norm = normalize_dept_path(raw)
        if norm and norm not in paths:
            paths.append(norm)
    return paths


def _assert_can_govern_grant_target(
    user: EffectiveAuthzContext,
    *,
    subject_type: str,
    action: str,
    operation_verb: str,
    subject_dept_path: Optional[str] = None,
    target_user_dept_paths: Optional[List[str]] = None,
) -> None:
    """校验非全公司管理范围的操作者是否有权新增或撤销该主体与动作对应的 ACL 规则"""
    if _is_org_wide_kb_operator(user):
        return
    if subject_type == "ROLE":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"【越权拦截】仅全公司管理范围或系统管理员可{operation_verb}按角色配置的批量授权规则",
        )
    if subject_type == "DEPARTMENT":
        if not _is_dept_in_manage_scope(user, subject_dept_path):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"【越权拦截】无权{operation_verb}管辖范围外部门 [{subject_dept_path or '-'}] 的授权规则",
            )
        return
    if subject_type == "USER" and action == KbAction.MANAGE.value:
        in_dept_scope = any(_is_dept_in_manage_scope(user, p) for p in (target_user_dept_paths or []))
        if not in_dept_scope:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"【越权拦截】仅可向管辖范围内员工{operation_verb}知识库管理权 (MANAGE)，禁止跨部门操作管理权",
            )


async def _assert_operator_retains_manage_access(
    db: AsyncSession,
    user: EffectiveAuthzContext,
    kb: KnowledgeBase,
    *,
    detail: str,
) -> None:
    """在当前事务已 flush 的 ACL 变更状态下，复核非系统管理员仍具备该知识库的有效 MANAGE 权限"""
    if user.has_permission(Permissions.SYSTEM_ADMIN):
        return
    still_can_manage = (
        await db.execute(
            select(KnowledgeBase.id).where(
                KnowledgeBase.id == kb.id,
                build_kb_access_condition(ctx=user, action=KbAction.MANAGE.value),
            )
        )
    ).scalar_one_or_none()
    if still_can_manage is None:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


async def _load_managed_kb(
    db: AsyncSession,
    user: EffectiveAuthzContext,
    kb_id: str,
    *,
    for_update: bool = False,
) -> KnowledgeBase:
    kb_uuid = _parse_uuid(kb_id, "kb_id")
    stmt = select(KnowledgeBase).where(KnowledgeBase.tenant_id == user.tenant_id, KnowledgeBase.id == kb_uuid)
    if for_update:
        stmt = stmt.with_for_update()
    kb = (await db.execute(stmt)).scalar_one_or_none()
    if kb is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="指定的知识库不存在或不属于当前租户")
    # 职责分离：SYSTEM_ADMIN 可治理租户内知识库元信息与 ACL（不含阅读高密文档内容）
    if not user.has_permission(Permissions.SYSTEM_ADMIN):
        await assert_kb_access(db, user, kb, KbAction.MANAGE.value, audit_denial=True)
    return kb


@router.get("/knowledge-bases")
async def list_admin_knowledge_bases(
    db: AsyncSession = Depends(get_db),
    user: EffectiveAuthzContext = Depends(get_current_user_with_db),
):
    _require_any_perm(user, Permissions.KB_MANAGE, Permissions.KB_GRANT, Permissions.SYSTEM_ADMIN)
    stmt = select(KnowledgeBase).where(KnowledgeBase.tenant_id == user.tenant_id)
    if not user.has_permission(Permissions.SYSTEM_ADMIN):
        stmt = stmt.where(build_kb_access_condition(ctx=user, action=KbAction.MANAGE.value))
    rows = (await db.execute(stmt.order_by(KnowledgeBase.visibility.asc(), KnowledgeBase.name.asc()))).scalars().all()
    return [_serialize_kb(kb) for kb in rows]


@router.post("/knowledge-bases", status_code=status.HTTP_201_CREATED)
async def create_knowledge_base(
    req: CreateKbReq,
    db: AsyncSession = Depends(get_db),
    user: EffectiveAuthzContext = Depends(get_current_user_with_db),
):
    _require_any_perm(user, Permissions.KB_MANAGE, Permissions.SYSTEM_ADMIN)
    vis = req.visibility.strip().upper()
    if vis not in VALID_VISIBILITIES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"无效的可见性 visibility: {vis}")
    _validate_levels(req.minClassificationLevel, req.defaultDocLevel, req.maxDocLevel, vis)

    if req.minClassificationLevel > int(user.effective_clearance) and not user.has_permission(Permissions.SYSTEM_ADMIN):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"【密级越权拦截】知识库最低密级 L{req.minClassificationLevel} 高于当前访问等级 L{user.effective_clearance}",
        )

    dept: Optional[Department] = None
    if req.ownerDepartmentId and req.ownerDepartmentId.strip():
        dept = (
            await db.execute(
                select(Department).where(
                    Department.tenant_id == user.tenant_id,
                    Department.id == _parse_uuid(req.ownerDepartmentId, "ownerDepartmentId"),
                    Department.status == "ACTIVE",
                )
            )
        ).scalar_one_or_none()
    elif req.ownerDeptPath and req.ownerDeptPath.strip():
        dept = (
            await db.execute(
                select(Department).where(
                    Department.tenant_id == user.tenant_id,
                    Department.path == normalize_dept_path(req.ownerDeptPath),
                    Department.status == "ACTIVE",
                )
            )
        ).scalar_one_or_none()
    elif user.dept_path:
        dept = (
            await db.execute(
                select(Department).where(
                    Department.tenant_id == user.tenant_id,
                    Department.path == user.dept_path,
                    Department.status == "ACTIVE",
                )
            )
        ).scalar_one_or_none()
    if dept is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="未在当前租户中找到启用状态的有效归属部门")

    if not _is_org_wide_kb_operator(user):
        if vis == "PUBLIC":
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="仅全公司管理范围的管理员可创建全员公共知识库")
        if not _is_dept_in_manage_scope(user, dept.path):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"【越权拦截】无权在部门 [{dept.path}] 下创建知识库")

    kb = KnowledgeBase(
        id=uuid.uuid4(), tenant_id=user.tenant_id, name=req.name.strip(), description=req.description,
        owner_department_id=dept.id, owner_dept_path=dept.path, visibility=vis,
        min_classification_level=req.minClassificationLevel, default_doc_level=req.defaultDocLevel,
        max_doc_level=req.maxDocLevel, status="ACTIVE", created_by=user.user_id,
    )
    db.add(kb)
    if vis == "RESTRICTED":
        # 受限库创建者默认获授 MANAGE 及自身 RBAC 已具备的操作权限，避免自建后无法上传/检索
        default_actions = [KbAction.MANAGE.value]
        for act_enum in (KbAction.READ, KbAction.UPLOAD, KbAction.REVIEW, KbAction.DELETE):
            if user.has_permission(ACTION_TO_PERMISSION[act_enum.value]):
                default_actions.append(act_enum.value)
        for act in default_actions:
            db.add(
                KbGrant(
                    id=uuid.uuid4(), tenant_id=user.tenant_id, kb_id=kb.id,
                    subject_type="USER", subject_id=user.user_id, action=act,
                    effect="ALLOW", reason="受限库创建者默认权限", granted_by=user.user_id,
                )
            )

    await _commit_policy_change(
        db, user, "ADMIN_KB_CREATE", str(kb.id),
        {"kb_name": kb.name, "owner_dept_path": kb.owner_dept_path, "visibility": kb.visibility, "levels": [kb.min_classification_level, kb.default_doc_level, kb.max_doc_level], "reason": req.reason},
        conflict_msg=f"知识库名称 [{req.name.strip()}] 在当前租户下已存在",
    )
    return _serialize_kb(kb)


@router.patch("/knowledge-bases/{kb_id}")
async def update_knowledge_base(
    kb_id: str,
    req: UpdateKbReq,
    db: AsyncSession = Depends(get_db),
    user: EffectiveAuthzContext = Depends(get_current_user_with_db),
):
    _require_any_perm(user, Permissions.KB_MANAGE, Permissions.SYSTEM_ADMIN)
    kb = await _load_managed_kb(db, user, kb_id)

    next_vis = req.visibility.strip().upper() if req.visibility else kb.visibility
    next_status = req.status.strip().upper() if req.status else kb.status
    if next_vis not in VALID_VISIBILITIES or next_status not in VALID_KB_STATUSES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="无效的 visibility 或 status")
    next_min = req.minClassificationLevel if req.minClassificationLevel is not None else int(kb.min_classification_level)
    next_def = req.defaultDocLevel if req.defaultDocLevel is not None else int(kb.default_doc_level)
    next_max = req.maxDocLevel if req.maxDocLevel is not None else int(kb.max_doc_level)
    _validate_levels(next_min, next_def, next_max, next_vis)

    if next_vis == "PUBLIC" and kb.visibility != "PUBLIC" and not _is_org_wide_kb_operator(user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="仅全公司管理范围的管理员可将知识库转为全员公共知识库 (PUBLIC)",
        )

    if next_min > int(user.effective_clearance) and not user.has_permission(Permissions.SYSTEM_ADMIN):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"【密级越权拦截】知识库最低密级 L{next_min} 高于当前访问等级 L{user.effective_clearance}",
        )

    out_of_range_cnt = (
        await db.execute(
            select(func.count(Document.id)).where(
                Document.tenant_id == user.tenant_id,
                Document.kb_id == kb.id,
                (Document.classification_level < next_min) | (Document.classification_level > next_max),
            )
        )
    ).scalar_one()
    if int(out_of_range_cnt or 0) > 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"该知识库下存在 {out_of_range_cnt} 篇文档（含排队/处理中文档）的密级不在新区间 [L{next_min}, L{next_max}] 内，请先调整存量文档密级或清理文档",
        )

    if req.name and req.name.strip():
        kb.name = req.name.strip()
    if req.description is not None:
        kb.description = req.description
    kb.visibility, kb.status = next_vis, next_status
    kb.min_classification_level, kb.default_doc_level, kb.max_doc_level = next_min, next_def, next_max
    kb.updated_at = datetime.now(timezone.utc)

    await _commit_policy_change(
        db, user, "ADMIN_KB_UPDATE", str(kb.id),
        {"kb_name": kb.name, "visibility": kb.visibility, "levels": [next_min, next_def, next_max], "status": kb.status, "reason": req.reason},
        conflict_msg="知识库名称在当前租户下已存在",
    )
    return _serialize_kb(kb)


@router.delete("/knowledge-bases/{kb_id}")
async def delete_knowledge_base(
    kb_id: str,
    db: AsyncSession = Depends(get_db),
    user: EffectiveAuthzContext = Depends(get_current_user_with_db),
):
    _require_any_perm(user, Permissions.KB_MANAGE, Permissions.SYSTEM_ADMIN)
    kb = await _load_managed_kb(db, user, kb_id)
    doc_count = (
        await db.execute(
            select(func.count(Document.id)).where(
                Document.tenant_id == user.tenant_id, Document.kb_id == kb.id
            )
        )
    ).scalar_one()
    if int(doc_count or 0) > 0:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"该知识库下仍有 {doc_count} 篇文档（含排队/处理中文档），请先清理文档或将其归档")

    kb_name = kb.name
    await db.delete(kb)
    new_epoch = await _commit_policy_change(db, user, "ADMIN_KB_DELETE", str(kb.id), {"kb_name": kb_name})
    return {"status": "deleted", "kbId": str(kb.id), "policyEpoch": new_epoch}


@router.get("/knowledge-bases/{kb_id}/grants")
async def list_kb_grants(
    kb_id: str,
    db: AsyncSession = Depends(get_db),
    user: EffectiveAuthzContext = Depends(get_current_user_with_db),
):
    _require_any_perm(user, Permissions.KB_MANAGE, Permissions.KB_GRANT, Permissions.SYSTEM_ADMIN)
    kb = await _load_managed_kb(db, user, kb_id)
    rows = (
        await db.execute(
            select(KbGrant).where(KbGrant.tenant_id == user.tenant_id, KbGrant.kb_id == kb.id)
            .order_by(KbGrant.created_at.desc())
        )
    ).scalars().all()
    return [_serialize_grant(g) for g in rows]


@router.post("/knowledge-bases/{kb_id}/grants", status_code=status.HTTP_201_CREATED)
async def create_kb_grant(
    kb_id: str,
    req: CreateKbGrantReq,
    db: AsyncSession = Depends(get_db),
    user: EffectiveAuthzContext = Depends(get_current_user_with_db),
):
    _require_any_perm(user, Permissions.KB_GRANT, Permissions.SYSTEM_ADMIN)
    kb = await _load_managed_kb(db, user, kb_id, for_update=True)

    stype, action, effect, sid = (
        req.subjectType.strip().upper(), req.action.strip().upper(),
        req.effect.strip().upper(), req.subjectId.strip(),
    )
    if stype not in VALID_SUBJECT_TYPES or action not in VALID_KB_ACTIONS or effect not in VALID_GRANT_EFFECTS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="无效的 ACL 主体类型、动作或效果")

    subject_dept_path: Optional[str] = None
    target_user_dept_paths: List[str] = []
    if stype == "USER":
        target_user = (
            await db.execute(
                select(SysUser).where(SysUser.tenant_id == user.tenant_id, SysUser.id == _parse_uuid(sid, "subjectId"))
            )
        ).scalar_one_or_none()
        if target_user is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="目标员工不存在或不属于当前租户")
        target_user_dept_paths = await _load_user_effective_dept_paths(db, user.tenant_id, target_user)
    elif stype == "DEPARTMENT":
        dept = (
            await db.execute(
                select(Department).where(
                    Department.tenant_id == user.tenant_id,
                    Department.id == _parse_uuid(sid, "subjectId"),
                    Department.status == "ACTIVE",
                )
            )
        ).scalar_one_or_none()
        if dept is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="目标部门不存在、已停用或不属于当前租户")
        subject_dept_path = dept.path
    elif await db.get(Role, (user.tenant_id, sid)) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"目标角色 [{sid}] 不存在或不属于当前租户")

    _assert_can_govern_grant_target(
        user,
        subject_type=stype,
        action=action,
        operation_verb="配置",
        subject_dept_path=subject_dept_path,
        target_user_dept_paths=target_user_dept_paths,
    )

    grant = KbGrant(
        id=uuid.uuid4(), tenant_id=user.tenant_id, kb_id=kb.id,
        subject_type=stype, subject_id=sid, subject_dept_path=subject_dept_path,
        include_sub_depts=bool(req.includeSubDepts) if stype == "DEPARTMENT" else False,
        action=action, effect=effect, expires_at=req.expiresAt, reason=req.reason, granted_by=user.user_id,
    )
    db.add(grant)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="相同主体、动作与效果的 ACL 规则已存在")

    await _assert_operator_retains_manage_access(
        db,
        user,
        kb,
        detail="禁止新增会导致当前操作者失去该知识库有效管理权限 (MANAGE) 的规则，避免自锁",
    )

    await _commit_policy_change(
        db, user, "ADMIN_KB_GRANT_CREATE", str(grant.id),
        {"kb_id": str(kb.id), "kb_name": kb.name, "subject_type": stype, "subject_id": sid, "action": action, "effect": effect, "expires_at": _iso(req.expiresAt), "reason": req.reason},
        conflict_msg="相同主体、动作与效果的 ACL 规则已存在",
    )
    return _serialize_grant(grant)


@router.delete("/knowledge-bases/{kb_id}/grants/{grant_id}")
async def delete_kb_grant(
    kb_id: str,
    grant_id: str,
    db: AsyncSession = Depends(get_db),
    user: EffectiveAuthzContext = Depends(get_current_user_with_db),
):
    _require_any_perm(user, Permissions.KB_GRANT, Permissions.SYSTEM_ADMIN)
    kb = await _load_managed_kb(db, user, kb_id, for_update=True)
    guid = _parse_uuid(grant_id, "grant_id")
    grant = (
        await db.execute(select(KbGrant).where(KbGrant.tenant_id == user.tenant_id, KbGrant.kb_id == kb.id, KbGrant.id == guid))
    ).scalar_one_or_none()
    if grant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="指定的 ACL 授权记录不存在")

    subject_dept_path: Optional[str] = grant.subject_dept_path
    target_user_dept_paths: List[str] = []
    if not _is_org_wide_kb_operator(user):
        if grant.subject_type == "DEPARTMENT":
            try:
                dept_uuid = uuid.UUID(str(grant.subject_id).strip())
            except ValueError:
                dept_uuid = None
            if dept_uuid is not None:
                dept_row = (
                    await db.execute(
                        select(Department).where(Department.tenant_id == user.tenant_id, Department.id == dept_uuid)
                    )
                ).scalar_one_or_none()
                if dept_row is not None:
                    subject_dept_path = dept_row.path
        elif grant.subject_type == "USER" and grant.action == KbAction.MANAGE.value:
            try:
                target_uid = uuid.UUID(str(grant.subject_id).strip())
            except ValueError:
                target_uid = None
            if target_uid is not None:
                target_user = (
                    await db.execute(
                        select(SysUser).where(SysUser.tenant_id == user.tenant_id, SysUser.id == target_uid)
                    )
                ).scalar_one_or_none()
                if target_user is not None:
                    target_user_dept_paths = await _load_user_effective_dept_paths(db, user.tenant_id, target_user)

    _assert_can_govern_grant_target(
        user,
        subject_type=grant.subject_type,
        action=grant.action,
        operation_verb="撤销",
        subject_dept_path=subject_dept_path,
        target_user_dept_paths=target_user_dept_paths,
    )

    snapshot = _serialize_grant(grant)
    await db.delete(grant)
    await db.flush()
    await _assert_operator_retains_manage_access(
        db,
        user,
        kb,
        detail="禁止撤销会导致当前操作者失去该知识库唯一有效管理权限 (MANAGE) 的授权规则，避免自锁或产生无主孤库",
    )
    new_epoch = await _commit_policy_change(db, user, "ADMIN_KB_GRANT_DELETE", str(guid), {"deleted_grant": snapshot})
    return {"status": "deleted", "grantId": str(guid), "policyEpoch": new_epoch}


# ==================== 6. 安全审计看板 ====================


@router.get("/audit-logs")
async def list_audit_logs(
    action: Optional[str] = Query(default=None),
    status_filter: Optional[str] = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    user: EffectiveAuthzContext = Depends(get_current_user_with_db),
):
    _require_any_perm(user, Permissions.SYSTEM_ADMIN)
    stmt = select(AuditLog).where(AuditLog.tenant_id == user.tenant_id)
    if action and action.strip():
        stmt = stmt.where(AuditLog.action.ilike(f"%{action.strip()}%"))
    if status_filter and status_filter.strip():
        normalized_status = status_filter.strip().upper()
        if normalized_status == "DENIED":
            stmt = stmt.where(
                AuditLog.status.in_(["FORBIDDEN", "BLOCKED", "UNAUTHORIZED", "REVOKED"])
            )
        else:
            stmt = stmt.where(AuditLog.status == normalized_status)
    rows = (await db.execute(stmt.order_by(desc(AuditLog.created_at)).limit(limit))).scalars().all()
    return [
        {
            "id": str(a.id), "requestId": a.request_id, "tenantId": a.tenant_id, "userId": a.user_id,
            "action": a.action, "resourceId": a.resource_id, "durationMs": int(a.duration_ms or 0),
            "status": a.status, "errorMsg": a.error_msg,
            "detailsJson": json.dumps(a.details or {}, ensure_ascii=False), "createdAt": _iso(a.created_at),
        }
        for a in rows
    ]
