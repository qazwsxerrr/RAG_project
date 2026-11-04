package com.enterprise.rag.admin.mapper;

import org.springframework.dao.DuplicateKeyException;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.core.RowMapper;
import org.springframework.stereotype.Repository;

import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.Timestamp;
import java.time.Instant;
import java.util.*;

@Repository
public class IdentityMapper {

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
            String deptPath,
            boolean isActive,
            Instant lastLoginAt,
            Instant createdAt
    ) {}

    public record RoleRecord(
            String roleCode,
            String roleName,
            String dataScope,
            String permissionsJson
    ) {}

    private final RowMapper<UserRecord> userRowMapper = (rs, rowNum) -> new UserRecord(
            (UUID) rs.getObject("id"),
            rs.getString("tenant_id"),
            rs.getString("external_id"),
            rs.getString("id_source"),
            rs.getString("real_name"),
            rs.getString("dept_path"),
            rs.getBoolean("is_active"),
            toInstant(rs.getTimestamp("last_login_at")),
            toInstant(rs.getTimestamp("created_at"))
    );

    private final RowMapper<RoleRecord> roleRowMapper = (rs, rowNum) -> new RoleRecord(
            rs.getString("role_code"),
            rs.getString("role_name"),
            rs.getString("data_scope"),
            rs.getString("permissions")
    );

    private static Instant toInstant(Timestamp ts) {
        return ts != null ? ts.toInstant() : null;
    }

    public Optional<UserRecord> findUser(String idSource, String externalId) {
        String sql = "SELECT id, tenant_id, external_id, id_source, real_name, dept_path, is_active, last_login_at, created_at " +
                     "FROM sys_users WHERE id_source = ? AND external_id = ?";
        List<UserRecord> list = jdbcTemplate.query(sql, userRowMapper, idSource, externalId);
        return list.isEmpty() ? Optional.empty() : Optional.of(list.getFirst());
    }

    public UserRecord upsertUser(String idSource, String externalId, String realName, String deptPath) {
        Optional<UserRecord> existing = findUser(idSource, externalId);
        if (existing.isPresent()) {
            UserRecord u = existing.get();
            String updateSql = "UPDATE sys_users SET real_name = ?, dept_path = ?, last_login_at = NOW() WHERE id = ?";
            jdbcTemplate.update(updateSql, realName, deptPath, u.id());
            return findUser(idSource, externalId).orElse(u);
        }

        UUID newId = UUID.randomUUID();
        String insertSql = "INSERT INTO sys_users (id, tenant_id, external_id, id_source, real_name, dept_path, is_active, last_login_at, created_at) " +
                           "VALUES (?, 'default_tenant', ?, ?, ?, ?, TRUE, NOW(), NOW())";
        try {
            jdbcTemplate.update(insertSql, newId, externalId, idSource, realName, deptPath);
            return findUser(idSource, externalId).orElseThrow();
        } catch (DuplicateKeyException e) {
            // 并发冲突重查
            return findUser(idSource, externalId).orElseThrow(() -> new IllegalStateException("用户并发建档失败", e));
        }
    }

    public Optional<RoleRecord> findRole(String roleCode) {
        String sql = "SELECT role_code, role_name, data_scope, permissions::text as permissions FROM sys_roles WHERE role_code = ?";
        List<RoleRecord> list = jdbcTemplate.query(sql, roleRowMapper, roleCode);
        return list.isEmpty() ? Optional.empty() : Optional.of(list.getFirst());
    }

    public List<RoleRecord> listRoles(Collection<String> roleCodes) {
        if (roleCodes == null || roleCodes.isEmpty()) {
            return Collections.emptyList();
        }
        String placeholders = String.join(",", Collections.nCopies(roleCodes.size(), "?"));
        String sql = "SELECT role_code, role_name, data_scope, permissions::text as permissions FROM sys_roles WHERE role_code IN (" + placeholders + ")";
        return jdbcTemplate.query(sql, roleRowMapper, roleCodes.toArray());
    }
}
