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
                () -> resolver.resolve(
                        "default_tenant",
                        List.of("unknown_hacker_role"),
                        "STAFF",
                        "/总公司/研发中心/技术部",
                        List.of("/总公司/研发中心/技术部")
                ));
        assertEquals(403, ex.getStatusCode().value());

        // 2. 多角色合并：验证「全公司只读 + 仅显式授权可删除 + 含子部门可审核 = READ=ALL_ORG, DELETE=CUSTOM_ACL, REVIEW=DEPT_AND_SUB」不交叉借用范围
        String testTenant = "perm_rule_test_" + UUID.randomUUID().toString().substring(0, 8);
        List<String> depts = List.of("/总公司/研发中心/技术部", "/总公司/职能中心/人事行政部");
        try {
            jdbc.update(
                    "INSERT INTO sys_roles (tenant_id, role_code, role_name, data_scope, permissions) VALUES " +
                    "(?, 'org_reader', '全公司只读', 'ALL_ORG', '[\"kb:retrieve\"]'::jsonb), " +
                    "(?, 'acl_dev', '显式授权研发', 'CUSTOM_ACL', '[\"kb:retrieve\",\"doc:upload\",\"doc:delete\"]'::jsonb), " +
                    "(?, 'sub_reviewer', '含子部门审核', 'DEPT_AND_SUB', '[\"doc:review\"]'::jsonb)",
                    testTenant, testTenant, testTenant
            );

            ResolvedPermissions merged = resolver.resolve(
                    testTenant,
                    List.of("org_reader", "acl_dev", "sub_reviewer"),
                    "STAFF",
                    "/总公司/研发中心/技术部",
                    depts
            );
            assertTrue(merged.permissions().containsAll(List.of("kb:retrieve", "doc:upload", "doc:delete", "doc:review")));
            assertEquals("ALL_ORG", merged.scopeByAction().get("READ"));
            assertEquals("CUSTOM_ACL", merged.scopeByAction().get("UPLOAD"));
            assertEquals("CUSTOM_ACL", merged.scopeByAction().get("DELETE"));
            assertEquals("DEPT_AND_SUB", merged.scopeByAction().get("REVIEW"));
            assertNull(merged.scopeByAction().get("MANAGE"), "无角色授予 kb:manage 时，scope_by_action 不得包含 MANAGE");
            assertEquals(List.of(), merged.deptPathsByAction().get("READ"));
            assertEquals(List.of(), merged.deptPathsByAction().get("UPLOAD"));
            assertEquals(
                    List.of("/总公司/研发中心/技术部", "/总公司/职能中心/人事行政部"),
                    merged.deptPathsByAction().get("REVIEW")
            );
            assertFalse(merged.deptPathsByAction().containsKey("MANAGE"));

            // 3. 租户隔离：其他租户的角色码在 default_tenant 必须被 403 拒绝
            ResponseStatusException crossTenantEx = assertThrows(ResponseStatusException.class,
                    () -> resolver.resolve("default_tenant", List.of("org_reader"), "STAFF", "/总公司/研发中心/技术部", depts));
            assertEquals(403, crossTenantEx.getStatusCode().value());
        } finally {
            jdbc.update("DELETE FROM sys_roles WHERE tenant_id = ?", testTenant);
        }

        // 4. 管理员通配符 '*' 展开至全部动作的对应角色范围（高管持 admin 角色通过，普通员工持 admin 角色被 403 拦截）
        ResolvedPermissions adminMerged = resolver.resolve(
                "default_tenant",
                List.of("employee", "admin"),
                "EXECUTIVE",
                "/总公司/研发中心/技术部",
                List.of("/总公司/研发中心/技术部")
        );
        assertEquals(Set.of("*"), adminMerged.permissions());
        assertEquals("ALL_ORG", adminMerged.scopeByAction().get("READ"));
        assertEquals("ALL_ORG", adminMerged.scopeByAction().get("DELETE"));
        assertEquals("ALL_ORG", adminMerged.scopeByAction().get("MANAGE"));
        assertEquals(List.of(), adminMerged.deptPathsByAction().get("READ"));
    }

    @Test
    void abacPositionPrimaryAndDeptPrefixConstraints() throws Exception {
        JdbcTemplate jdbc = AuthTestSupport.database();
        IdentityMapper mapper = new IdentityMapper(jdbc);
        PermissionResolver resolver = new PermissionResolver(mapper);

        List<String> multiDepts = List.of("/总公司/研发中心/技术部", "/总公司/职能中心/人事行政部");

        // 1. 职级准入拦截：STAFF 持有 admin 或 dept_manager 必须报 403
        ResponseStatusException staffAdminEx = assertThrows(ResponseStatusException.class, () -> resolver.resolve(
                "default_tenant", List.of("admin"), "STAFF", "/总公司/研发中心/技术部", List.of("/总公司/研发中心/技术部")));
        assertEquals(403, staffAdminEx.getStatusCode().value());

        ResponseStatusException staffMgrEx = assertThrows(ResponseStatusException.class, () -> resolver.resolve(
                "default_tenant", List.of("dept_manager"), "STAFF", "/总公司/研发中心/技术部", List.of("/总公司/研发中心/技术部")));
        assertEquals(403, staffMgrEx.getStatusCode().value());

        // 2. 跨部门多角色隔离：multi_dept_staff (developer + employee, STAFF)
        ResolvedPermissions multiStaff = resolver.resolve(
                "default_tenant",
                List.of("developer", "employee"),
                "STAFF",
                "/总公司/研发中心/技术部",
                multiDepts
        );
        assertEquals(
                List.of("/总公司/研发中心/技术部", "/总公司/职能中心/人事行政部"),
                multiStaff.deptPathsByAction().get("READ")
        );
        assertEquals(List.of("/总公司/研发中心/技术部"), multiStaff.deptPathsByAction().get("UPLOAD"));
        assertEquals(List.of("/总公司/研发中心/技术部"), multiStaff.deptPathsByAction().get("DELETE"));

        // 3. 部门前缀不匹配且 data_scope=DEPT_AND_SUB -> D_r 为空 -> 403
        ResponseStatusException wrongDeptDev = assertThrows(ResponseStatusException.class, () -> resolver.resolve(
                "default_tenant", List.of("developer"), "STAFF", "/总公司/职能中心/财务部", List.of("/总公司/职能中心/财务部")));
        assertEquals(403, wrongDeptDev.getStatusCode().value());

        // 4. primary_dept_only = TRUE：dept_manager 仅对主部门授予 MANAGE，不扩展至副部门 人事行政部；缺失主部门时 403
        ResolvedPermissions dualMgr = resolver.resolve(
                "default_tenant",
                List.of("dept_manager", "employee"),
                "MANAGER",
                "/总公司/研发中心/技术部",
                multiDepts
        );
        assertEquals(
                List.of("/总公司/研发中心/技术部", "/总公司/职能中心/人事行政部"),
                dualMgr.deptPathsByAction().get("READ")
        );
        assertEquals(List.of("/总公司/研发中心/技术部"), dualMgr.deptPathsByAction().get("MANAGE"));
        assertEquals(List.of("/总公司/研发中心/技术部"), dualMgr.deptPathsByAction().get("UPLOAD"));

        ResponseStatusException noPrimaryMgr = assertThrows(ResponseStatusException.class, () -> resolver.resolve(
                "default_tenant", List.of("dept_manager"), "MANAGER", "", multiDepts));
        assertEquals(403, noPrimaryMgr.getStatusCode().value());
    }

    @Test
    void seedPermissionsMatchKnownPermissions() throws Exception {
        assertFalse(PermissionResolver.KNOWN_PERMISSIONS.contains("user:authz:manage"),
                "user:authz:manage 已在去 V1 改造中下线");

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
            sqlPerms.remove("user:authz:manage");
            assertTrue(PermissionResolver.KNOWN_PERMISSIONS.containsAll(sqlPerms),
                    migrationFile + " 存在未纳入 KNOWN_PERMISSIONS 的权限字面量: " + sqlPerms);
        }

        JdbcTemplate jdbc = AuthTestSupport.database();
        Integer kbAdminCount = jdbc.queryForObject(
                "SELECT COUNT(*) FROM sys_roles WHERE role_code = 'kb_admin'", Integer.class);
        assertEquals(0, kbAdminCount, "V5 迁移后 sys_roles 中不得残留 kb_admin");

        List<String> rolePermJsons = jdbc.queryForList(
                "SELECT permissions::text FROM sys_roles WHERE tenant_id = 'default_tenant'", String.class);
        for (String permJson : rolePermJsons) {
            List<String> perms = PermissionResolver.parseJsonArray(permJson);
            assertFalse(perms.contains("user:authz:manage"), "数据库角色仍残留已下线权限 user:authz:manage: " + permJson);
            assertTrue(PermissionResolver.KNOWN_PERMISSIONS.containsAll(perms),
                    "数据库角色存在未知权限点: " + perms);
        }
    }
}
