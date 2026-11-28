package com.enterprise.rag.admin;

import com.enterprise.rag.admin.identity.RedisRevocationSync;
import com.enterprise.rag.admin.identity.UserMappingService;
import com.enterprise.rag.admin.identity.UserMappingService.MappedIdentity;
import com.enterprise.rag.admin.mapper.IdentityMapper;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.security.oauth2.core.oidc.user.OidcUser;
import org.springframework.security.oauth2.core.oidc.OidcUserInfo;
import org.springframework.security.oauth2.core.oidc.user.DefaultOidcUser;
import org.springframework.web.server.ResponseStatusException;
import java.util.*;

import static org.junit.jupiter.api.Assertions.*;

class DatabaseProvisioningTest {
    private JdbcTemplate jdbcTemplate;
    private IdentityMapper identityMapper;
    private UserMappingService userMappingService;
    private RedisRevocationSync redisRevocationSync;
    private RedisTestSupport redis;
    private final List<String> createdExternalIds = new ArrayList<>();
    private final List<String> touchedRedisUserIds = new ArrayList<>();

    @BeforeEach
    void setUp() throws Exception {
        jdbcTemplate = AuthTestSupport.database();
        identityMapper = new IdentityMapper(jdbcTemplate);
        redis = new RedisTestSupport();
        redisRevocationSync = redis.revocations;
        userMappingService = new UserMappingService(identityMapper, redisRevocationSync);
    }

    @AfterEach
    void tearDown() {
        if (redis == null) return;
        try {
            for (String extId : createdExternalIds) {
                identityMapper.findUser(IdentityMapper.DEFAULT_TENANT_ID, "oidc:corp-sso", extId)
                        .ifPresent(u -> redis.userIds.add(u.id().toString()));
                jdbcTemplate.update("DELETE FROM sys_users WHERE id_source = 'oidc:corp-sso' AND external_id = ?", extId);
            }
        } finally {
            redis.userIds.addAll(touchedRedisUserIds);
            redis.close();
        }
    }

    @Test
    void jitProvisionAndGroupMapping() {
        String extId = "emp-dev-jit-" + UUID.randomUUID();
        createdExternalIds.add(extId);
        OidcUser oidcUser = AuthTestSupport.buildOidcUser(
                extId,
                "张研发",
                null,
                List.of("/corp/总公司/研发中心/技术部", "/roles/developer"),
                null,
                "STAFF"
        );
        MappedIdentity mapped = userMappingService.mapAndProvision(oidcUser, false);
        assertEquals("/总公司/研发中心/技术部", mapped.primaryDeptPath());
        assertEquals(List.of("developer"), mapped.roleCodes());
        assertEquals(List.of("/总公司/研发中心/技术部"), mapped.deptPathsByAction().get("READ"));
        assertTrue(identityMapper.findUser(IdentityMapper.DEFAULT_TENANT_ID, "oidc:corp-sso", extId).isPresent());
    }

    @Test
    void missingDeptPathOrNonCorpGroupRejected403() {
        String extId = "emp-nodept-" + UUID.randomUUID();
        createdExternalIds.add(extId);
        OidcUser oidcUser = AuthTestSupport.buildOidcUser(
                extId,
                "无部门员工",
                null,
                List.of("/vpn-users", "/roles/employee"),
                null,
                "STAFF"
        );
        ResponseStatusException ex = assertThrows(ResponseStatusException.class,
                () -> userMappingService.mapAndProvision(oidcUser, false));
        assertEquals(403, ex.getStatusCode().value());
        assertTrue(identityMapper.findUser(IdentityMapper.DEFAULT_TENANT_ID, "oidc:corp-sso", extId).isEmpty());
    }

    @Test
    void missingPositionCodeRejected403WithoutStaffFallback() {
        String extId = "emp-nopos-" + UUID.randomUUID();
        createdExternalIds.add(extId);
        OidcUser noPosUser = AuthTestSupport.buildOidcUser(
                extId, "无职级员工", "/总公司/研发中心/技术部", null, List.of("developer"), null
        );
        ResponseStatusException newEx = assertThrows(ResponseStatusException.class,
                () -> userMappingService.mapAndProvision(noPosUser, false));
        assertEquals(403, newEx.getStatusCode().value());
        assertTrue(identityMapper.findUser(IdentityMapper.DEFAULT_TENANT_ID, "oidc:corp-sso", extId).isEmpty());

        OidcUser validUser = AuthTestSupport.buildOidcUser(
                extId, "转岗缺职级员工", "/总公司/研发中心/技术部", null, List.of("developer"), "STAFF"
        );
        MappedIdentity created = userMappingService.mapAndProvision(validUser, false);
        touchedRedisUserIds.add(created.user().id().toString());
        ResponseStatusException existingEx = assertThrows(ResponseStatusException.class,
                () -> userMappingService.mapAndProvision(noPosUser, false));
        assertEquals(403, existingEx.getStatusCode().value());
        assertTrue(redisRevocationSync.isUserRevoked(created.user().id().toString()));
    }

    @Test
    void disabledUserRejected403AndSyncedToRealRedis() {
        String extId = "emp-disabled-" + UUID.randomUUID();
        createdExternalIds.add(extId);
        OidcUser oidcUser = AuthTestSupport.buildOidcUser(
                extId,
                "已离职员工",
                "/总公司/研发中心/技术部",
                null,
                List.of("developer"),
                "STAFF"
        );
        MappedIdentity initial = userMappingService.mapAndProvision(oidcUser, false);
        UUID userId = initial.user().id();
        touchedRedisUserIds.add(userId.toString());

        // 在真实 PostgreSQL 将 sys_users.is_active 置为 FALSE，验证再次换票抛 403 并自动向真实 Redis 写入吊销标记
        jdbcTemplate.update("UPDATE sys_users SET is_active = FALSE WHERE id = ?", userId);
        ResponseStatusException ex = assertThrows(ResponseStatusException.class,
                () -> userMappingService.mapAndProvision(oidcUser, false));
        assertEquals(403, ex.getStatusCode().value());
        assertTrue(redisRevocationSync.isUserRevoked(userId.toString()),
                "PostgreSQL 停用账号触发换票拦截时必须同步在真实 Redis 写入 auth:revoked:{user_id}");
    }

    @ParameterizedTest
    @ValueSource(strings = {"missing", "empty", "null"})
    void latestUserInfoWithoutDepartmentRevokesExistingUser(String departmentState) {
        String extId = "emp-dept-removed-" + UUID.randomUUID();
        createdExternalIds.add(extId);
        OidcUser original = AuthTestSupport.buildOidcUser(
                extId, "部门变更员工", "/总公司/研发中心/技术部",
                List.of("/corp/总公司/研发中心/技术部"), List.of("developer"), "STAFF");
        MappedIdentity initial = userMappingService.mapAndProvision(original, false);
        String userId = initial.user().id().toString();
        touchedRedisUserIds.add(userId);

        Map<String, Object> latestClaims = new HashMap<>();
        latestClaims.put("sub", extId);
        latestClaims.put("roles", List.of("developer"));
        latestClaims.put("position_code", "STAFF");
        if (departmentState.equals("empty")) {
            latestClaims.put("dept_path", "");
        } else if (departmentState.equals("null")) {
            latestClaims.put("dept_path", null);
        }
        OidcUser refreshed = withUserInfo(original, latestClaims);

        ResponseStatusException ex = assertThrows(ResponseStatusException.class,
                () -> userMappingService.mapAndProvision(refreshed, true));
        assertEquals(403, ex.getStatusCode().value());
        assertTrue(redisRevocationSync.isUserRevoked(userId));
        assertNotNull(redis.client.opsForValue().get(RedisRevocationSync.PERM_CHANGED_KEY_PREFIX + userId));
        assertEquals(List.of("/总公司/研发中心/技术部"),
                identityMapper.listActiveUserDepartments(IdentityMapper.DEFAULT_TENANT_ID, initial.user().id())
                        .stream().map(IdentityMapper.UserDepartmentRecord::deptPath).toList());
    }

    @Test
    void latestUserInfoWithoutDepartmentRejectsNewUser() {
        String extId = "emp-new-nodept-" + UUID.randomUUID();
        createdExternalIds.add(extId);
        OidcUser original = AuthTestSupport.buildOidcUser(
                extId, "新员工", "/总公司/研发中心/技术部", null, List.of("developer"), "STAFF");
        OidcUser refreshed = withUserInfo(original, Map.of(
                "sub", extId, "roles", List.of("developer"), "position_code", "STAFF"));

        ResponseStatusException ex = assertThrows(ResponseStatusException.class,
                () -> userMappingService.mapAndProvision(refreshed, false));
        assertEquals(403, ex.getStatusCode().value());
        assertTrue(identityMapper.findUser(IdentityMapper.DEFAULT_TENANT_ID, "oidc:corp-sso", extId).isEmpty());
    }

    @ParameterizedTest
    @ValueSource(booleans = {false, true})
    void latestUserInfoDepartmentAndGroupsOverrideOldToken(boolean explicitDepartment) {
        String extId = "emp-new-dept-" + UUID.randomUUID();
        createdExternalIds.add(extId);
        OidcUser original = AuthTestSupport.buildOidcUser(
                extId, "调岗员工", "/总公司/研发中心/技术部", null, List.of("developer"), "STAFF");
        userMappingService.mapAndProvision(original, false);
        Map<String, Object> latestClaims = new HashMap<>();
        latestClaims.put("sub", extId);
        latestClaims.put("roles", List.of("accountant"));
        latestClaims.put("position_code", "STAFF");
        if (explicitDepartment) {
            latestClaims.put("dept_path", "/总公司/职能中心/财务部");
        } else {
            latestClaims.put("groups", List.of("/corp/总公司/职能中心/财务部"));
        }
        MappedIdentity mapped = userMappingService.mapAndProvision(withUserInfo(original, latestClaims), true);
        assertEquals(List.of("/总公司/职能中心/财务部"), mapped.effectiveDeptPaths());
        assertEquals("/总公司/职能中心/财务部", mapped.primaryDeptPath());
        assertEquals(List.of("accountant"), mapped.roleCodes());
        assertFalse(redisRevocationSync.isUserRevoked(mapped.user().id().toString()));
    }

    @Test
    void latestUserInfoWithoutRolesRevokesExistingUser() {
        String extId = "emp-roles-removed-" + UUID.randomUUID();
        createdExternalIds.add(extId);
        OidcUser original = AuthTestSupport.buildOidcUser(
                extId, "角色移除员工", "/总公司/研发中心/技术部",
                List.of("/roles/developer"), List.of("developer"), "STAFF");
        MappedIdentity initial = userMappingService.mapAndProvision(original, false);
        String userId = initial.user().id().toString();
        touchedRedisUserIds.add(userId);
        OidcUser refreshed = withUserInfo(original, Map.of(
                "sub", extId, "dept_path", "/总公司/研发中心/技术部", "position_code", "STAFF"));

        ResponseStatusException ex = assertThrows(ResponseStatusException.class,
                () -> userMappingService.mapAndProvision(refreshed, true));
        assertEquals(403, ex.getStatusCode().value());
        assertTrue(redisRevocationSync.isUserRevoked(userId));
    }

    @Test
    void offlineDepartmentTransferSyncsIdpAsSingleSourceOfTruth() {
        String extId = "emp-offline-transfer-" + UUID.randomUUID();
        createdExternalIds.add(extId);
        OidcUser loginA = AuthTestSupport.buildOidcUser(
                extId, "调岗员工", "/总公司/研发中心/技术部",
                List.of("/roles/developer"), List.of("developer"), "STAFF");
        MappedIdentity first = userMappingService.mapAndProvision(loginA, false);
        UUID userId = first.user().id();
        touchedRedisUserIds.add(userId.toString());
        long verBefore = first.user().authzVersion();

        // 离线期间 IdP 将员工从技术部调至产品部（同属研发中心），重新登录同步单真源部门并递增 authz_version
        OidcUser loginB = AuthTestSupport.buildOidcUser(
                extId, "调岗员工", "/总公司/研发中心/产品部",
                List.of("/roles/developer"), List.of("developer"), "STAFF");
        MappedIdentity second = userMappingService.mapAndProvision(loginB, false);
        assertTrue(second.idpDepartmentsMutated(), "离线调岗重新登录必须检测到 IdP 部门变更");
        assertEquals("/总公司/研发中心/产品部", second.primaryDeptPath());
        assertEquals(List.of("/总公司/研发中心/产品部"), second.effectiveDeptPaths());
        assertTrue(second.user().authzVersion() > verBefore, "调岗后必须递增 authz_version");
    }

    @Test
    void primaryDeptValidationContractsEnforced() {
        String extId = "emp-primary-contract-" + UUID.randomUUID();
        createdExternalIds.add(extId);
        String tech = "/总公司/研发中心/技术部";
        String hr = "/总公司/职能中心/人事行政部";
        String fin = "/总公司/职能中心/财务部";

        // 1. 多部门 + primary_dept_only 角色 (dept_manager) 缺失 dept_path -> 403，禁止按顺序猜测
        OidcUser baseMgr = AuthTestSupport.buildOidcUser(
                extId, "双部门经理", null,
                List.of("/corp" + tech, "/corp" + hr), List.of("dept_manager"), "MANAGER");
        ResponseStatusException missingPrimaryEx = assertThrows(ResponseStatusException.class,
                () -> userMappingService.mapAndProvision(baseMgr, true));
        assertEquals(403, missingPrimaryEx.getStatusCode().value());

        // 2. dept_path 指向未归属部门 财务部（与 dept_paths 冲突）-> 403
        OidcUser mismatchUser = withUserInfo(baseMgr, Map.of(
                "sub", extId, "dept_paths", List.of(tech, hr), "dept_path", fin,
                "roles", List.of("dept_manager"), "position_code", "MANAGER"));
        ResponseStatusException mismatchEx = assertThrows(ResponseStatusException.class,
                () -> userMappingService.mapAndProvision(mismatchUser, true));
        assertEquals(403, mismatchEx.getStatusCode().value());

        // 3. 多值冲突 dept_path -> 403
        OidcUser multiPrimaryUser = withUserInfo(baseMgr, Map.of(
                "sub", extId, "dept_paths", List.of(tech, hr), "dept_path", List.of(tech, hr),
                "roles", List.of("dept_manager"), "position_code", "MANAGER"));
        ResponseStatusException multiEx = assertThrows(ResponseStatusException.class,
                () -> userMappingService.mapAndProvision(multiPrimaryUser, true));
        assertEquals(403, multiEx.getStatusCode().value());

        // 4. 多部门普通员工（无 primary_dept_only 角色）缺省 dept_path 允许登录，且 primaryDeptPath=""
        OidcUser multiNormalNoPrimary = AuthTestSupport.buildOidcUser(
                extId, "双部门普通员工", null,
                List.of("/corp" + tech, "/corp" + hr), List.of("developer", "employee"), "STAFF");
        MappedIdentity okNoPrimary = userMappingService.mapAndProvision(multiNormalNoPrimary, true);
        touchedRedisUserIds.add(okNoPrimary.user().id().toString());
        assertEquals("", okNoPrimary.primaryDeptPath());
        assertEquals(List.of(tech, hr), okNoPrimary.deptPathsByAction().get("READ"));
        assertEquals(List.of(tech), okNoPrimary.deptPathsByAction().get("UPLOAD"));

        // 5. 所属部门集合不变但主部门从 空 -> 技术部 -> 人事行政部，必须检测到主部门切换并递增 authz_version
        long v1 = okNoPrimary.user().authzVersion();
        OidcUser setPrimaryTech = withUserInfo(baseMgr, Map.of(
                "sub", extId, "dept_paths", List.of(tech, hr), "dept_path", tech,
                "roles", List.of("developer", "employee"), "position_code", "STAFF"));
        MappedIdentity switched = userMappingService.mapAndProvision(setPrimaryTech, true);
        assertTrue(switched.idpDepartmentsMutated());
        assertEquals(tech, switched.primaryDeptPath());
        assertTrue(switched.user().authzVersion() > v1);
    }

    @Test
    void unknownPositionCodeIsRejectedInsteadOfKeepingExecutive() {
        String extId = "emp-unknown-pos-" + UUID.randomUUID();
        createdExternalIds.add(extId);
        OidcUser execUser = AuthTestSupport.buildOidcUser(
                extId, "高管调岗测试", "/总公司/总裁办",
                List.of("/roles/executive"), List.of("executive"), "EXECUTIVE");
        OidcUser execWithPos = withUserInfo(execUser, Map.of(
                "sub", extId,
                "dept_path", "/总公司/总裁办",
                "roles", List.of("executive"),
                "position_code", "EXECUTIVE"));
        MappedIdentity initial = userMappingService.mapAndProvision(execWithPos, false);
        touchedRedisUserIds.add(initial.user().id().toString());
        assertEquals("EXECUTIVE", initial.user().positionCode());
        assertEquals(4, initial.effectiveClearance());

        OidcUser unmappedPosUser = withUserInfo(execUser, Map.of(
                "sub", extId,
                "dept_path", "/总公司/总裁办",
                "roles", List.of("executive"),
                "position_code", "NEW_VP_UNMAPPED_999"));
        ResponseStatusException ex = assertThrows(ResponseStatusException.class,
                () -> userMappingService.mapAndProvision(unmappedPosUser, false));
        assertEquals(403, ex.getStatusCode().value());
    }

    @Test
    void positionDowngradeAtomicallyIncrementsAuthzVersion() {
        String extId = "emp-pos-downgrade-" + UUID.randomUUID();
        createdExternalIds.add(extId);
        OidcUser baseUser = AuthTestSupport.buildOidcUser(
                extId, "降级测试高管", "/总公司/研发中心/技术部",
                List.of("/roles/developer"), List.of("developer"), "EXECUTIVE");
        OidcUser execLogin = withUserInfo(baseUser, Map.of(
                "sub", extId,
                "dept_path", "/总公司/研发中心/技术部",
                "roles", List.of("executive"),
                "position_code", "EXECUTIVE"));
        MappedIdentity first = userMappingService.mapAndProvision(execLogin, false);
        touchedRedisUserIds.add(first.user().id().toString());
        long verBefore = first.user().authzVersion();
        assertEquals(4, first.effectiveClearance());
        assertFalse(first.positionMutated());

        OidcUser staffLogin = withUserInfo(baseUser, Map.of(
                "sub", extId,
                "dept_path", "/总公司/研发中心/技术部",
                "roles", List.of("developer"),
                "position_code", "STAFF"));
        MappedIdentity second = userMappingService.mapAndProvision(staffLogin, false);
        assertEquals("STAFF", second.user().positionCode());
        assertEquals(2, second.effectiveClearance());
        assertTrue(second.positionMutated(), "职级降级必须标记 positionMutated=true");
        assertEquals(verBefore + 1, second.user().authzVersion(), "职级变化必须原子递增 authz_version");
    }

    @Test
    void idpRemovalDuringDepartmentDisabledDoesNotReviveAfterReEnable() {
        String extId = "emp-disabled-dept-" + UUID.randomUUID();
        createdExternalIds.add(extId);
        String deptA = "/总公司/研发中心/技术部";
        String deptB = "/总公司/研发中心/临时停用验证部_" + UUID.randomUUID().toString().substring(0, 6);

        try {
            OidcUser baseUser = AuthTestSupport.buildOidcUser(
                    extId, "停用部门同步验证员工", deptA,
                    List.of("/roles/developer"), List.of("developer"), "STAFF");
            OidcUser initialLogin = withUserInfo(baseUser, Map.of(
                    "sub", extId,
                    "dept_paths", List.of(deptA, deptB),
                    "dept_path", deptA,
                    "roles", List.of("developer"),
                    "position_code", "STAFF"));
            MappedIdentity first = userMappingService.mapAndProvision(initialLogin, true);
            UUID uid = first.user().id();
            touchedRedisUserIds.add(uid.toString());
            assertTrue(first.effectiveDeptPaths().contains(deptB));

            // 停用部门 B
            jdbcTemplate.update(
                    "UPDATE sys_departments SET status = 'DISABLED' WHERE tenant_id = ? AND path = ?",
                    IdentityMapper.DEFAULT_TENANT_ID, deptB
            );

            // 部门 B 停用期间，IdP 移除员工对 B 的归属
            OidcUser removedBLogin = withUserInfo(baseUser, Map.of(
                    "sub", extId,
                    "dept_paths", List.of(deptA),
                    "dept_path", deptA,
                    "roles", List.of("developer"),
                    "position_code", "STAFF"));
            MappedIdentity afterRemoval = userMappingService.mapAndProvision(removedBLogin, true);
            assertTrue(afterRemoval.idpDepartmentsMutated(), "停用部门 B 被 IdP 移除时必须识别出差异并清理持久化关系");

            // 重新启用部门 B，再次登录验证员工绝不复活 B 的权限
            jdbcTemplate.update(
                    "UPDATE sys_departments SET status = 'ACTIVE' WHERE tenant_id = ? AND path = ?",
                    IdentityMapper.DEFAULT_TENANT_ID, deptB
            );
            MappedIdentity afterReEnable = userMappingService.mapAndProvision(removedBLogin, true);
            assertFalse(afterReEnable.effectiveDeptPaths().contains(deptB), "部门 B 重新启用后绝不能复活已被 IdP 移除的权限");
            assertTrue(afterReEnable.effectiveDeptPaths().contains(deptA));
        } finally {
            jdbcTemplate.update(
                    "DELETE FROM sys_departments WHERE tenant_id = ? AND path = ?",
                    IdentityMapper.DEFAULT_TENANT_ID, deptB
            );
        }
    }

    @Test
    void concurrentFirstLoginSucceedsWithoutPostgresTransactionAbort() throws Exception {
        String extId = "emp-concurrent-login-" + UUID.randomUUID();
        createdExternalIds.add(extId);
        String newDeptPath = "/总公司/研发中心/并发首登部_" + UUID.randomUUID().toString().substring(0, 6);
        int threads = 8;
        java.util.concurrent.ExecutorService pool = java.util.concurrent.Executors.newFixedThreadPool(threads);
        java.util.concurrent.CountDownLatch ready = new java.util.concurrent.CountDownLatch(threads);
        java.util.concurrent.CountDownLatch start = new java.util.concurrent.CountDownLatch(1);
        List<java.util.concurrent.Future<MappedIdentity>> futures = new ArrayList<>();

        try {
            for (int i = 0; i < threads; i++) {
                futures.add(pool.submit(() -> {
                    OidcUser oidcUser = AuthTestSupport.buildOidcUser(
                            extId, "并发首登员工", newDeptPath,
                            List.of("/roles/developer"), List.of("developer"), "STAFF");
                    ready.countDown();
                    start.await();
                    return userMappingService.mapAndProvision(oidcUser, true);
                }));
            }
            ready.await();
            start.countDown();

            Set<UUID> seenUserIds = new HashSet<>();
            for (var f : futures) {
                MappedIdentity res = f.get();
                assertNotNull(res);
                seenUserIds.add(res.user().id());
                touchedRedisUserIds.add(res.user().id().toString());
            }
            assertEquals(1, seenUserIds.size(), "并发首登必须落到同一个 sys_users 主键");
            Integer deptCount = jdbcTemplate.queryForObject(
                    "SELECT COUNT(*) FROM sys_departments WHERE tenant_id = ? AND path = ?",
                    Integer.class, IdentityMapper.DEFAULT_TENANT_ID, newDeptPath
            );
            assertEquals(1, deptCount, "并发首登必须只创建 1 条部门记录");
        } finally {
            pool.shutdownNow();
            jdbcTemplate.update(
                    "DELETE FROM sys_users WHERE tenant_id = ? AND external_id = ?",
                    IdentityMapper.DEFAULT_TENANT_ID, extId
            );
            jdbcTemplate.update(
                    "DELETE FROM sys_departments WHERE tenant_id = ? AND path = ?",
                    IdentityMapper.DEFAULT_TENANT_ID, newDeptPath
            );
        }
    }

    private static OidcUser withUserInfo(OidcUser original, Map<String, Object> latestClaims) {
        return new DefaultOidcUser(original.getAuthorities(), original.getIdToken(),
                new OidcUserInfo(latestClaims), "preferred_username");
    }
}
