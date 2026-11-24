package com.enterprise.rag.admin;

import com.enterprise.rag.admin.identity.PermissionResolver;
import com.enterprise.rag.admin.identity.PermissionResolver.ResolvedPermissions;
import com.enterprise.rag.admin.mapper.IdentityMapper;
import org.junit.jupiter.api.Test;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.web.server.ResponseStatusException;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.*;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

import static org.junit.jupiter.api.Assertions.*;

class PermissionRulesTest {

    @Test
    void unknownRoleRejectedAndPerActionScopeNotCrossBorrowed() throws Exception {
        JdbcTemplate jdbc = AuthTestSupport.database();
        IdentityMapper mapper = new IdentityMapper(jdbc);
        PermissionResolver resolver = new PermissionResolver(mapper);

        // 1. 未知角色码 -> 403
        ResponseStatusException ex = assertThrows(ResponseStatusException.class,
                () -> resolver.resolve("default_tenant", List.of("unknown_hacker_role")));
        assertEquals(403, ex.getStatusCode().value());

        // 2. 多角色合并：验证「全公司只读 + 仅显式授权可删除 + 含子部门可审核 = READ=ALL_ORG, DELETE=CUSTOM_ACL, REVIEW=DEPT_AND_SUB」不交叉借用范围
        String testTenant = "perm_rule_test_" + UUID.randomUUID().toString().substring(0, 8);
        try {
            jdbc.update(
                    "INSERT INTO sys_roles (tenant_id, role_code, role_name, data_scope, permissions) VALUES " +
                    "(?, 'org_reader', '全公司只读', 'ALL_ORG', '[\"kb:retrieve\"]'::jsonb), " +
                    "(?, 'acl_dev', '显式授权研发', 'CUSTOM_ACL', '[\"kb:retrieve\",\"doc:upload\",\"doc:delete\"]'::jsonb), " +
                    "(?, 'sub_reviewer', '含子部门审核', 'DEPT_AND_SUB', '[\"doc:review\"]'::jsonb)",
                    testTenant, testTenant, testTenant
            );

            ResolvedPermissions merged = resolver.resolve(testTenant, List.of("org_reader", "acl_dev", "sub_reviewer"));
            assertTrue(merged.permissions().containsAll(List.of("kb:retrieve", "doc:upload", "doc:delete", "doc:review")));
            assertEquals("ALL_ORG", merged.scopeByAction().get("READ"));
            assertEquals("CUSTOM_ACL", merged.scopeByAction().get("UPLOAD"));
            assertEquals("CUSTOM_ACL", merged.scopeByAction().get("DELETE"));
            assertEquals("DEPT_AND_SUB", merged.scopeByAction().get("REVIEW"));
            assertNull(merged.scopeByAction().get("MANAGE"), "无角色授予 kb:manage 时，scope_by_action 不得包含 MANAGE");

            // 3. 租户隔离：其他租户的角色码在 default_tenant 必须被 403 拒绝
            ResponseStatusException crossTenantEx = assertThrows(ResponseStatusException.class,
                    () -> resolver.resolve("default_tenant", List.of("org_reader")));
            assertEquals(403, crossTenantEx.getStatusCode().value());
        } finally {
            jdbc.update("DELETE FROM sys_roles WHERE tenant_id = ?", testTenant);
        }

        // 4. 管理员通配符 '*' 展开至全部动作的对应角色范围
        ResolvedPermissions adminMerged = resolver.resolve("default_tenant", List.of("employee", "admin"));
        assertEquals(Set.of("*"), adminMerged.permissions());
        assertEquals("ALL_ORG", adminMerged.scopeByAction().get("READ"));
        assertEquals("ALL_ORG", adminMerged.scopeByAction().get("DELETE"));
        assertEquals("ALL_ORG", adminMerged.scopeByAction().get("MANAGE"));
    }

    @Test
    void seedPermissionsMatchKnownPermissions() throws Exception {
        for (String migrationFile : List.of(
                "src/main/resources/db/migration/V1__rbac.sql",
                "src/main/resources/db/migration/V2__enterprise_multidim_authz.sql"
        )) {
            Path migrationSql = Paths.get(migrationFile).toAbsolutePath().normalize();
            String sqlText = Files.readString(migrationSql, StandardCharsets.UTF_8);
            Matcher m = Pattern.compile("\"(\\*|[a-z]+:[a-z_:]+)\"").matcher(sqlText);
            Set<String> sqlPerms = new HashSet<>();
            while (m.find()) {
                sqlPerms.add(m.group(1));
            }
            assertFalse(sqlPerms.isEmpty(), migrationFile + " 未匹配到权限点");
            assertTrue(PermissionResolver.KNOWN_PERMISSIONS.containsAll(sqlPerms),
                    migrationFile + " 存在未纳入 KNOWN_PERMISSIONS 的权限字面量: " + sqlPerms);
        }
    }
}
