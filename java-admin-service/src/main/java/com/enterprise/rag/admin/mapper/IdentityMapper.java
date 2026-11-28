package com.enterprise.rag.admin.mapper;

import org.springframework.http.HttpStatus;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.core.RowMapper;
import org.springframework.stereotype.Repository;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.web.server.ResponseStatusException;

import java.sql.Timestamp;
import java.time.Duration;
import java.time.Instant;
import java.util.*;

/**
 * 外部企业 IdP/HR 身份影子映射与策略字典读取仓储。
 * 员工身份、多部门与职级均由企业 IdP/HR 同步；本方仅维护策略字典 (sys_roles / sys_positions) 与版本纪元。
 */
@Repository
public class IdentityMapper {

    public static final String DEFAULT_TENANT_ID = "default_tenant";

    private final JdbcTemplate jdbcTemplate;

    public IdentityMapper(JdbcTemplate jdbcTemplate) {
        this.jdbcTemplate = jdbcTemplate;
    }

    public record UserRecord(
            UUID id,
            String tenantId,
            String externalId,
            String idSource,
            String realName,
            boolean isActive,
            String positionCode,
            long authzVersion,
            Instant lastLoginAt,
            Instant createdAt
    ) {}

    public record RoleRecord(
            String tenantId,
            String roleCode,
            String roleName,
            String dataScope,
            String permissionsJson,
            String allowedPositionsJson,
            String allowedDeptPrefixesJson,
            boolean primaryDeptOnly
    ) {}

    public record DepartmentRecord(
            UUID id,
            String tenantId,
            UUID parentId,
            String name,
            String path,
            int sortOrder,
            String status,
            Instant createdAt
    ) {}

    public record PositionRecord(
            String tenantId,
            String positionCode,
            String positionName,
            int rankOrder,
            int defaultClearanceLevel,
            int minAllowedClearance,
            int maxAllowedClearance,
            String description,
            Instant updatedAt
    ) {}

    public record UserDepartmentRecord(
            UUID id,
            String tenantId,
            UUID userId,
            UUID departmentId,
            String deptPath,
            boolean isPrimary,
            Instant createdAt
    ) {}

    private final RowMapper<UserRecord> userRowMapper = (rs, rowNum) -> new UserRecord(
            (UUID) rs.getObject("id"),
            rs.getString("tenant_id"),
            rs.getString("external_id"),
            rs.getString("id_source"),
            rs.getString("real_name"),
            rs.getBoolean("is_active"),
            rs.getString("position_code"),
            rs.getLong("authz_version"),
            toInstant(rs.getTimestamp("last_login_at")),
            toInstant(rs.getTimestamp("created_at"))
    );

    private final RowMapper<RoleRecord> roleRowMapper = (rs, rowNum) -> new RoleRecord(
            rs.getString("tenant_id"),
            rs.getString("role_code"),
            rs.getString("role_name"),
            rs.getString("data_scope"),
            rs.getString("permissions"),
            rs.getString("allowed_positions"),
            rs.getString("allowed_dept_prefixes"),
            rs.getBoolean("primary_dept_only")
    );

    private final RowMapper<DepartmentRecord> deptRowMapper = (rs, rowNum) -> new DepartmentRecord(
            (UUID) rs.getObject("id"),
            rs.getString("tenant_id"),
            (UUID) rs.getObject("parent_id"),
            rs.getString("name"),
            rs.getString("path"),
            rs.getInt("sort_order"),
            rs.getString("status"),
            toInstant(rs.getTimestamp("created_at"))
    );

    private final RowMapper<PositionRecord> positionRowMapper = (rs, rowNum) -> new PositionRecord(
            rs.getString("tenant_id"),
            rs.getString("position_code"),
            rs.getString("position_name"),
            rs.getInt("rank_order"),
            rs.getInt("default_clearance_level"),
            rs.getInt("min_allowed_clearance"),
            rs.getInt("max_allowed_clearance"),
            rs.getString("description"),
            toInstant(rs.getTimestamp("updated_at"))
    );

    private final RowMapper<UserDepartmentRecord> userDeptRowMapper = (rs, rowNum) -> new UserDepartmentRecord(
            (UUID) rs.getObject("id"),
            rs.getString("tenant_id"),
            (UUID) rs.getObject("user_id"),
            (UUID) rs.getObject("department_id"),
            rs.getString("dept_path"),
            rs.getBoolean("is_primary"),
            toInstant(rs.getTimestamp("created_at"))
    );

    private static Instant toInstant(Timestamp ts) {
        return ts != null ? ts.toInstant() : null;
    }

    public Optional<UserRecord> findUser(String tenantId, String idSource, String externalId) {
        String sql = "SELECT id, tenant_id, external_id, id_source, real_name, is_active, " +
                     "position_code, authz_version, last_login_at, created_at " +
                     "FROM sys_users WHERE tenant_id = ? AND id_source = ? AND external_id = ?";
        List<UserRecord> list = jdbcTemplate.query(sql, userRowMapper, tenantId, idSource, externalId);
        return list.isEmpty() ? Optional.empty() : Optional.of(list.getFirst());
    }

    public Optional<UserRecord> findUserById(String tenantId, UUID userId) {
        String sql = "SELECT id, tenant_id, external_id, id_source, real_name, is_active, " +
                     "position_code, authz_version, last_login_at, created_at " +
                     "FROM sys_users WHERE tenant_id = ? AND id = ?";
        List<UserRecord> list = jdbcTemplate.query(sql, userRowMapper, tenantId, userId);
        return list.isEmpty() ? Optional.empty() : Optional.of(list.getFirst());
    }

    public Optional<UserRecord> findUserByRealName(String tenantId, String idSource, String realName) {
        if (realName == null || realName.isBlank()) {
            return Optional.empty();
        }
        String sql = "SELECT id, tenant_id, external_id, id_source, real_name, is_active, " +
                     "position_code, authz_version, last_login_at, created_at " +
                     "FROM sys_users WHERE tenant_id = ? AND id_source = ? AND LOWER(real_name) = LOWER(?) " +
                     "ORDER BY last_login_at DESC NULLS LAST, created_at DESC LIMIT 1";
        List<UserRecord> list = jdbcTemplate.query(sql, userRowMapper, tenantId, idSource, realName.trim());
        return list.isEmpty() ? Optional.empty() : Optional.of(list.getFirst());
    }

    /**
     * 企业 IdP 登录时自动建档并对齐姓名与职级（严禁缺失 position_code 或历史职级/STAFF 兜底）。
     */
    public UserRecord upsertUser(
            String tenantId,
            String idSource,
            String externalId,
            String realName,
            String positionCode
    ) {
        String safeTenant = (tenantId == null || tenantId.isBlank()) ? DEFAULT_TENANT_ID : tenantId.trim();
        ensureDefaultPositionsIfMissing(safeTenant);

        if (positionCode == null || positionCode.isBlank()) {
            throw new ResponseStatusException(
                    HttpStatus.FORBIDDEN,
                    "【拒绝签发】企业 IdP 未下发有效职级 position_code"
            );
        }
        String validIdpPosition = positionCode.trim().toUpperCase(Locale.ROOT);
        if (findPosition(safeTenant, validIdpPosition).isEmpty()) {
            throw new ResponseStatusException(
                    HttpStatus.FORBIDDEN,
                    "【拒绝签发】企业 IdP 下发的职级 [" + validIdpPosition + "] 未在 sys_positions 中配置映射，请先在权限治理中心配置"
            );
        }

        Optional<UserRecord> existing = findUser(safeTenant, idSource, externalId);
        if (existing.isPresent()) {
            UserRecord u = existing.get();
            boolean nameChanged = !Objects.equals(u.realName(), realName);
            boolean positionChanged = !Objects.equals(u.positionCode(), validIdpPosition);
            boolean lastLoginStale = u.lastLoginAt() == null
                    || Duration.between(u.lastLoginAt(), Instant.now()).compareTo(Duration.ofMinutes(5)) >= 0;
            if (!nameChanged && !positionChanged && !lastLoginStale) {
                return u;
            }
            jdbcTemplate.update(
                    "UPDATE sys_users SET real_name = ?, position_code = ?, " +
                    "authz_version = authz_version + ?, last_login_at = NOW() " +
                    "WHERE tenant_id = ? AND id = ?",
                    realName, validIdpPosition, positionChanged ? 1 : 0, safeTenant, u.id()
            );
            return findUserById(safeTenant, u.id()).orElse(u);
        }

        // IdP 重建导致 sub 变化时按同源同用户名收敛重绑，防止产生同名影子账号
        Optional<UserRecord> sameNameExisting = findUserByRealName(safeTenant, idSource, realName);
        if (sameNameExisting.isPresent()) {
            UserRecord u = sameNameExisting.get();
            boolean positionChanged = !Objects.equals(u.positionCode(), validIdpPosition);
            jdbcTemplate.update(
                    "UPDATE sys_users SET external_id = ?, real_name = ?, position_code = ?, " +
                    "authz_version = authz_version + ?, last_login_at = NOW() " +
                    "WHERE tenant_id = ? AND id = ?",
                    externalId, realName, validIdpPosition, positionChanged ? 1 : 0, safeTenant, u.id()
            );
            return findUserById(safeTenant, u.id()).orElse(u);
        }

        UUID newId = UUID.randomUUID();
        String insertSql = "INSERT INTO sys_users (id, tenant_id, external_id, id_source, real_name, " +
                           "is_active, position_code, authz_version, last_login_at, created_at) " +
                           "VALUES (?, ?, ?, ?, ?, TRUE, ?, 1, NOW(), NOW()) " +
                           "ON CONFLICT (tenant_id, id_source, external_id) DO NOTHING";
        int inserted = jdbcTemplate.update(
                insertSql, newId, safeTenant, externalId, idSource, realName, validIdpPosition
        );
        if (inserted > 0) {
            return findUserById(safeTenant, newId).orElseThrow();
        }
        return findUser(safeTenant, idSource, externalId)
                .orElseThrow(() -> new IllegalStateException("用户并发建档后读取失败: " + externalId));
    }

    public long incrementUserAuthzVersion(String tenantId, UUID userId) {
        Long ver = jdbcTemplate.queryForObject(
                "UPDATE sys_users SET authz_version = authz_version + 1 WHERE tenant_id = ? AND id = ? RETURNING authz_version",
                Long.class, tenantId, userId
        );
        if (ver == null) {
            throw new IllegalStateException("递增用户 authz_version 失败，用户不存在: " + userId);
        }
        return ver;
    }

    public long getTenantPolicyEpoch(String tenantId) {
        String safeTenant = (tenantId == null || tenantId.isBlank()) ? DEFAULT_TENANT_ID : tenantId.trim();
        List<Long> rows = jdbcTemplate.query(
                "SELECT policy_epoch FROM sys_tenant_policy_epochs WHERE tenant_id = ?",
                (rs, rowNum) -> rs.getLong("policy_epoch"), safeTenant
        );
        if (!rows.isEmpty()) {
            return rows.getFirst();
        }
        jdbcTemplate.update(
                "INSERT INTO sys_tenant_policy_epochs (tenant_id, policy_epoch, updated_at) VALUES (?, 1, NOW()) ON CONFLICT (tenant_id) DO NOTHING",
                safeTenant
        );
        return jdbcTemplate.queryForObject(
                "SELECT policy_epoch FROM sys_tenant_policy_epochs WHERE tenant_id = ?",
                Long.class, safeTenant
        );
    }

    public long incrementTenantPolicyEpoch(String tenantId) {
        String safeTenant = (tenantId == null || tenantId.isBlank()) ? DEFAULT_TENANT_ID : tenantId.trim();
        Long epoch = jdbcTemplate.queryForObject(
                "INSERT INTO sys_tenant_policy_epochs (tenant_id, policy_epoch, updated_at) VALUES (?, 2, NOW()) " +
                "ON CONFLICT (tenant_id) DO UPDATE SET policy_epoch = sys_tenant_policy_epochs.policy_epoch + 1, updated_at = NOW() " +
                "RETURNING policy_epoch",
                Long.class, safeTenant
        );
        if (epoch == null) {
            throw new IllegalStateException("递增租户 policy_epoch 失败: " + safeTenant);
        }
        return epoch;
    }

    public DepartmentRecord ensureDepartmentPath(String tenantId, String rawPath) {
        String normalized = normalizeDeptPath(rawPath);
        if (normalized == null || normalized.equals("/")) {
            throw new IllegalArgumentException("非法部门路径: " + rawPath);
        }
        Optional<DepartmentRecord> direct = findDepartmentByPath(tenantId, normalized);
        if (direct.isPresent()) {
            return direct.get();
        }
        String[] segments = normalized.substring(1).split("/");
        UUID parentId = null;
        StringBuilder pathBuilder = new StringBuilder();
        DepartmentRecord current = null;
        for (String seg : segments) {
            if (seg.isBlank()) continue;
            pathBuilder.append('/').append(seg);
            String curPath = pathBuilder.toString();
            Optional<DepartmentRecord> existing = findDepartmentByPath(tenantId, curPath);
            if (existing.isPresent()) {
                current = existing.get();
                parentId = current.id();
                continue;
            }
            UUID newId = UUID.randomUUID();
            String displayName = "/public".equals(curPath) ? "公共知识库" : seg;
            jdbcTemplate.update(
                    "INSERT INTO sys_departments (id, tenant_id, parent_id, name, path, sort_order, status, created_at) " +
                    "VALUES (?, ?, ?, ?, ?, 0, 'ACTIVE', NOW()) " +
                    "ON CONFLICT (tenant_id, path) DO NOTHING",
                    newId, tenantId, parentId, displayName, curPath
            );
            current = findDepartmentByPath(tenantId, curPath)
                    .orElseThrow(() -> new IllegalStateException("并发创建部门失败: " + curPath));
            parentId = current.id();
        }
        if (current == null) {
            throw new IllegalArgumentException("无法解析部门路径: " + rawPath);
        }
        return current;
    }

    public Optional<DepartmentRecord> findDepartmentByPath(String tenantId, String path) {
        List<DepartmentRecord> list = jdbcTemplate.query(
                "SELECT id, tenant_id, parent_id, name, path, sort_order, status, created_at " +
                "FROM sys_departments WHERE tenant_id = ? AND path = ?",
                deptRowMapper, tenantId, path
        );
        return list.isEmpty() ? Optional.empty() : Optional.of(list.getFirst());
    }

    /** 查询员工全部持久化部门关系（含已停用部门，用于与上游 IdP 声明做精准差量比对） */
    public List<UserDepartmentRecord> listPersistedUserDepartments(String tenantId, UUID userId) {
        String sql = "SELECT ud.id, ud.tenant_id, ud.user_id, ud.department_id, " +
                     "COALESCE(d.path, ud.dept_path) AS dept_path, " +
                     "ud.is_primary, ud.created_at " +
                     "FROM sys_user_departments ud " +
                     "LEFT JOIN sys_departments d ON d.tenant_id = ud.tenant_id AND d.id = ud.department_id " +
                     "WHERE ud.tenant_id = ? AND ud.user_id = ? " +
                     "ORDER BY ud.is_primary DESC, ud.created_at ASC";
        return jdbcTemplate.query(sql, userDeptRowMapper, tenantId, userId);
    }

    /** 同步企业 IdP 下发的员工多部门归属与主部门标记（变更部门集合或主部门切换均触发重建） */
    @Transactional
    public boolean syncIdpUserDepartments(
            String tenantId,
            UUID userId,
            List<String> idpDeptPaths,
            String primaryDeptPath
    ) {
        List<String> normalizedPaths = new ArrayList<>();
        Set<String> seen = new LinkedHashSet<>();
        if (idpDeptPaths != null) {
            for (String p : idpDeptPaths) {
                String norm = normalizeDeptPath(p);
                if (norm != null && !norm.equals("/") && seen.add(norm)) {
                    normalizedPaths.add(norm);
                }
            }
        }
        String normPrimary = normalizeDeptPath(primaryDeptPath);
        if ("/".equals(normPrimary)) {
            normPrimary = null;
        }

        List<UserDepartmentRecord> existing = listPersistedUserDepartments(tenantId, userId);
        boolean unchanged = existing.size() == normalizedPaths.size();
        if (unchanged) {
            Map<String, Boolean> existingPrimaryByPath = new LinkedHashMap<>();
            for (UserDepartmentRecord rec : existing) {
                String existingNorm = normalizeDeptPath(rec.deptPath());
                if (existingNorm != null) {
                    existingPrimaryByPath.put(existingNorm, rec.isPrimary());
                }
            }
            for (String path : normalizedPaths) {
                Boolean wasPrimary = existingPrimaryByPath.get(path);
                boolean expectPrimary = normPrimary != null && path.equals(normPrimary);
                if (wasPrimary == null || wasPrimary != expectPrimary) {
                    unchanged = false;
                    break;
                }
            }
        }
        if (unchanged) {
            return false;
        }

        jdbcTemplate.update(
                "DELETE FROM sys_user_departments WHERE tenant_id = ? AND user_id = ?",
                tenantId, userId
        );
        for (String path : normalizedPaths) {
            DepartmentRecord dept = ensureDepartmentPath(tenantId, path);
            boolean isPrimary = normPrimary != null && dept.path().equals(normPrimary);
            jdbcTemplate.update(
                    "INSERT INTO sys_user_departments (id, tenant_id, user_id, department_id, dept_path, is_primary, created_at) " +
                    "VALUES (?, ?, ?, ?, ?, ?, NOW()) " +
                    "ON CONFLICT (tenant_id, user_id, department_id) DO UPDATE " +
                    "SET dept_path = EXCLUDED.dept_path, is_primary = EXCLUDED.is_primary",
                    UUID.randomUUID(), tenantId, userId, dept.id(), dept.path(), isPrimary
            );
        }
        return true;
    }

    public List<UserDepartmentRecord> listActiveUserDepartments(String tenantId, UUID userId) {
        String sql = "SELECT ud.id, ud.tenant_id, ud.user_id, ud.department_id, d.path AS dept_path, " +
                     "ud.is_primary, ud.created_at " +
                     "FROM sys_user_departments ud " +
                     "JOIN sys_departments d ON d.tenant_id = ud.tenant_id AND d.id = ud.department_id " +
                     "WHERE ud.tenant_id = ? AND ud.user_id = ? " +
                     "  AND d.status = 'ACTIVE' " +
                     "ORDER BY ud.is_primary DESC, ud.created_at ASC";
        return jdbcTemplate.query(sql, userDeptRowMapper, tenantId, userId);
    }

    public Optional<PositionRecord> findPosition(String tenantId, String positionCode) {
        List<PositionRecord> list = jdbcTemplate.query(
                "SELECT tenant_id, position_code, position_name, rank_order, default_clearance_level, " +
                "min_allowed_clearance, max_allowed_clearance, description, updated_at " +
                "FROM sys_positions WHERE tenant_id = ? AND position_code = ?",
                positionRowMapper, tenantId, positionCode
        );
        return list.isEmpty() ? Optional.empty() : Optional.of(list.getFirst());
    }

    /** 有效密级严格由企业人事同步的职级查 sys_positions 策略映射表决定 */
    public int resolveEffectiveClearance(UserRecord user) {
        return findPosition(user.tenantId(), user.positionCode())
                .map(PositionRecord::defaultClearanceLevel)
                .orElseThrow(() -> new IllegalStateException(
                        "员工职级在 sys_positions 中缺失: tenant=" + user.tenantId() + ", position=" + user.positionCode()
                ));
    }

    public Optional<RoleRecord> findRole(String tenantId, String roleCode) {
        String sql = "SELECT tenant_id, role_code, role_name, data_scope, permissions::text AS permissions, " +
                     "allowed_positions::text AS allowed_positions, " +
                     "allowed_dept_prefixes::text AS allowed_dept_prefixes, " +
                     "primary_dept_only " +
                     "FROM sys_roles WHERE tenant_id = ? AND role_code = ?";
        List<RoleRecord> list = jdbcTemplate.query(sql, roleRowMapper, tenantId, roleCode);
        return list.isEmpty() ? Optional.empty() : Optional.of(list.getFirst());
    }

    public List<RoleRecord> listRoles(String tenantId, Collection<String> roleCodes) {
        if (roleCodes == null || roleCodes.isEmpty()) {
            return Collections.emptyList();
        }
        String safeTenant = (tenantId == null || tenantId.isBlank()) ? DEFAULT_TENANT_ID : tenantId.trim();
        List<String> distinctCodes = new ArrayList<>(new LinkedHashSet<>(roleCodes));
        String placeholders = String.join(",", Collections.nCopies(distinctCodes.size(), "?"));
        String sql = "SELECT tenant_id, role_code, role_name, data_scope, permissions::text AS permissions, " +
                     "allowed_positions::text AS allowed_positions, " +
                     "allowed_dept_prefixes::text AS allowed_dept_prefixes, " +
                     "primary_dept_only " +
                     "FROM sys_roles WHERE tenant_id = ? AND role_code IN (" + placeholders + ")";
        Object[] args = new Object[distinctCodes.size() + 1];
        args[0] = safeTenant;
        for (int i = 0; i < distinctCodes.size(); i++) {
            args[i + 1] = distinctCodes.get(i);
        }
        return jdbcTemplate.query(sql, roleRowMapper, args);
    }

    public void ensureDefaultPositionsIfMissing(String tenantId) {
        if (findPosition(tenantId, "STAFF").isPresent()) {
            return;
        }
        jdbcTemplate.update(
                "INSERT INTO sys_positions (tenant_id, position_code, position_name, rank_order, default_clearance_level, min_allowed_clearance, max_allowed_clearance, description) VALUES " +
                "(?, 'INTERN_EXT', '实习 / 外部协作', 10, 1, 1, 1, '全员公开 L1'), " +
                "(?, 'STAFF',      '普通员工',       20, 2, 1, 2, '部门内部 L1-L2'), " +
                "(?, 'SUPERVISOR', '部门主管',       30, 2, 2, 3, '默认 L2，可按职级动态配置'), " +
                "(?, 'MANAGER',    '部门经理',       40, 3, 3, 3, '管理层机密 L1-L3'), " +
                "(?, 'DIRECTOR',   '总监',           50, 3, 3, 3, '中心及下属部门 L1-L3'), " +
                "(?, 'EXECUTIVE',  '公司高管',       60, 4, 4, 4, '高层核心机密 L1-L4') " +
                "ON CONFLICT (tenant_id, position_code) DO NOTHING",
                tenantId, tenantId, tenantId, tenantId, tenantId, tenantId
        );
    }

    public static String normalizeDeptPath(String rawPath) {
        if (rawPath == null) return null;
        String trimmed = rawPath.trim();
        if (trimmed.isEmpty()) return null;
        if ("/".equals(trimmed)) return "/";
        if (!trimmed.startsWith("/")) return null;
        String cleaned = trimmed.replaceAll("/+", "/");
        if (cleaned.length() > 1 && cleaned.endsWith("/")) {
            cleaned = cleaned.substring(0, cleaned.length() - 1);
        }
        return cleaned;
    }
}
