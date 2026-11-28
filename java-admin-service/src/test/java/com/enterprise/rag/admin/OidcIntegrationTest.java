package com.enterprise.rag.admin;

import com.enterprise.rag.admin.identity.RedisRevocationSync;
import com.enterprise.rag.admin.identity.UserMappingService.MappedIdentity;
import com.enterprise.rag.admin.mapper.IdentityMapper;
import com.enterprise.rag.admin.mapper.IdentityMapper.UserRecord;
import com.enterprise.rag.admin.web.AuthController;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.mock.web.MockHttpServletRequest;
import org.springframework.mock.web.MockHttpServletResponse;
import org.springframework.security.oauth2.client.OAuth2AuthorizedClient;
import org.springframework.security.oauth2.client.OAuth2AuthorizedClientManager;
import org.springframework.security.oauth2.client.authentication.OAuth2AuthenticationToken;
import org.springframework.security.oauth2.client.oidc.userinfo.OidcUserService;
import org.springframework.security.oauth2.core.OAuth2AccessToken;
import org.springframework.security.oauth2.core.OAuth2AuthenticationException;
import org.springframework.security.oauth2.core.OAuth2Error;
import org.springframework.security.oauth2.core.oidc.user.OidcUser;
import org.springframework.security.oauth2.jwt.Jwt;
import org.springframework.security.oauth2.jwt.NimbusJwtDecoder;
import org.springframework.test.util.ReflectionTestUtils;
import org.springframework.web.server.ResponseStatusException;
import java.time.Instant;
import java.util.*;

import static org.junit.jupiter.api.Assertions.*;

/** 会话刷新、吊销和故障注入边界用例。 */
class OidcIntegrationTest {
    private OidcTestSupport support;

    @BeforeEach
    void setUp() throws Exception {
        support = new OidcTestSupport();
    }

    @AfterEach
    void tearDown() {
        if (support != null) support.close();
    }

    @Test
    void realKeycloakPkceLiveRefreshPreventsDeptOverwriteAndRevokesSessionInRedis() throws Exception {
        // 1. 直连真实 Keycloak 执行 PKCE 授权码登录并完成首次实时 UserInfo 校验与建档
        OidcTestSupport.RealKeycloakSession session = support.loginViaRealKeycloakPkce("developer", "Rag@123456");
        String externalSub = session.oidcUser().getSubject();
        OidcTestSupport.UserStateSnapshot snapshot = support.snapshotUserState(externalSub);
        UserRecord beforeRecord = snapshot.existed()
                ? support.identityMapper.findUser(IdentityMapper.DEFAULT_TENANT_ID, "oidc:corp-sso", externalSub).orElseThrow()
                : support.userMappingService.mapAndProvision(session.oidcUser(), false).user();
        String userIdStr = beforeRecord.id().toString();

        try {
            support.jdbcTemplate.update("UPDATE sys_users SET is_active = TRUE WHERE id = ?", beforeRecord.id());
            support.redisRevocationSync.clearUserRevoked(userIdStr);

            Map<String, Object> me = support.authController.getMyProfile(
                    session.oidcUser(), session.authToken(), session.request(), session.response()
            );
            assertEquals(List.of("developer"), me.get("roles"));
            assertEquals(1, ((Number) me.get("cv")).intValue());
            assertEquals("/总公司/研发中心/技术部", me.get("dept_path"));
            @SuppressWarnings("unchecked")
            List<String> deptPaths = (List<String>) me.get("dept_paths");
            assertEquals(List.of("/总公司/研发中心/技术部"), deptPaths, "必须仅包含 IdP 同步部门");
            @SuppressWarnings("unchecked")
            Map<String, List<String>> dpa = (Map<String, List<String>>) me.get("dept_paths_by_action");
            assertEquals(List.of("/总公司/研发中心/技术部"), dpa.get("READ"));
            assertEquals(List.of("/总公司/研发中心/技术部"), dpa.get("UPLOAD"));

            // 2. 调用真实 Keycloak OIDC /protocol/openid-connect/revoke 吊销该会话的 AccessToken 与 RefreshToken
            OidcTestSupport.revokeTokenInRealKeycloak(session.accessToken(), "access_token");
            OidcTestSupport.revokeTokenInRealKeycloak(session.refreshToken(), "refresh_token");

            // 3. 再次请求换票：真实 OidcUserService 请求 Keycloak /userinfo 收到 401，
            //    AuthController 立即销毁本地 HttpSession、写入 auth:perm_changed_at:{user_id} 淘汰既有票据而不误伤全账号封禁
            ResponseStatusException ex = assertThrows(ResponseStatusException.class,
                    () -> support.authController.exchangeToken(
                            session.oidcUser(), session.authToken(), session.request(), session.response()
                    ));
            assertEquals(401, ex.getStatusCode().value());
            assertNull(session.request().getSession(false), "IdP 会话吊销后本地 HttpSession 必须被立即销毁");
            assertFalse(support.redisRevocationSync.isUserRevoked(userIdStr),
                    "单个会话撤销不应写入用户级全账号封禁 auth:revoked:{user_id}");
            assertNotNull(support.stringRedisTemplate.opsForValue().get(RedisRevocationSync.PERM_CHANGED_KEY_PREFIX + userIdStr),
                    "IdP 会话撤销后必须同步写入 auth:perm_changed_at:{user_id} 淘汰既有权限票据");

            // 4. R7 恢复路径验证：若存在历史残留的 auth:revoked 键且数据库账号仍启用，重新通过真实 Keycloak 登录后自动清除 auth:revoked 键
            long preRevokeMs = System.currentTimeMillis() - 5;
            support.redisRevocationSync.markUserRevoked(userIdStr, 600);
            assertTrue(support.redisRevocationSync.isUserRevoked(userIdStr));
            OidcTestSupport.RealKeycloakSession reloginSession = support.loginViaRealKeycloakPkce("developer", "Rag@123456");
            Map<String, Object> reloginTokenResp = support.authController.exchangeToken(
                    reloginSession.oidcUser(), reloginSession.authToken(), reloginSession.request(), reloginSession.response()
            );
            String reloginJwtStr = (String) reloginTokenResp.get("access_token");
            assertNotNull(reloginJwtStr);
            assertFalse(support.redisRevocationSync.isUserRevoked(userIdStr),
                    "有效登录且 DB is_active=TRUE 时，必须自动清理残留的 auth:revoked:{user_id}");
            long permChangedAfterClear = support.redisRevocationSync.getPermissionChangedAtMs(userIdStr);
            assertTrue(permChangedAfterClear > preRevokeMs,
                    "清理 auth:revoked 时必须同步推进 auth:perm_changed_at，严防吊销前旧凭据复活");
            Jwt reloginDecoded = NimbusJwtDecoder.withPublicKey(support.rsaKey.toRSAPublicKey()).build().decode(reloginJwtStr);
            assertEquals(1, ((Number) reloginDecoded.getClaim("cv")).intValue());
            assertNull(reloginDecoded.getClaim("idp_roles"));
            Long reloginIatMs = reloginDecoded.getClaim("iat_ms");
            assertNotNull(reloginIatMs);
            assertTrue(reloginIatMs > permChangedAfterClear,
                    "重登录签发的新 JWT iat_ms 必须严格大于清理吊销时推进的 perm_changed_at");
        } finally {
            support.restoreUserState(snapshot);
        }
    }

    @Test
    void multiDeptStaffRealKeycloakPkceResolvesPrimaryDeptAndScopedActions() throws Exception {
        OidcTestSupport.RealKeycloakSession session = support.loginViaRealKeycloakPkce("multi_dept_staff", "Rag@123456");
        String externalSub = session.oidcUser().getSubject();
        OidcTestSupport.UserStateSnapshot snapshot = support.snapshotUserState(externalSub);
        try {
            Map<String, Object> me = support.authController.getMyProfile(
                    session.oidcUser(), session.authToken(), session.request(), session.response()
            );
            assertEquals(1, ((Number) me.get("cv")).intValue());
            assertEquals("/总公司/研发中心/技术部", me.get("dept_path"));
            @SuppressWarnings("unchecked")
            List<String> deptPaths = (List<String>) me.get("dept_paths");
            assertEquals(Set.of("/总公司/研发中心/技术部", "/总公司/职能中心/人事行政部"), Set.copyOf(deptPaths));
            @SuppressWarnings("unchecked")
            Map<String, List<String>> dpa = (Map<String, List<String>>) me.get("dept_paths_by_action");
            assertEquals(List.of("/总公司/研发中心/技术部", "/总公司/职能中心/人事行政部"), dpa.get("READ"));
            assertEquals(List.of("/总公司/研发中心/技术部"), dpa.get("UPLOAD"));
            assertEquals(List.of("/总公司/研发中心/技术部"), dpa.get("REVIEW"));
            assertEquals(List.of("/总公司/研发中心/技术部"), dpa.get("DELETE"));

            Map<String, Object> tokResp = support.authController.exchangeToken(
                    session.oidcUser(), session.authToken(), session.request(), session.response()
            );
            Jwt decoded = NimbusJwtDecoder.withPublicKey(support.rsaKey.toRSAPublicKey()).build()
                    .decode((String) tokResp.get("access_token"));
            assertEquals(1, ((Number) decoded.getClaim("cv")).intValue());
            assertNull(decoded.getClaim("idp_roles"));
            assertEquals("/总公司/研发中心/技术部", decoded.getClaimAsString("dept_path"));
            @SuppressWarnings("unchecked")
            Map<String, List<String>> jwtDpa = decoded.getClaim("dept_paths_by_action");
            assertEquals(List.of("/总公司/研发中心/技术部", "/总公司/职能中心/人事行政部"), jwtDpa.get("READ"));
            assertEquals(List.of("/总公司/研发中心/技术部"), jwtDpa.get("UPLOAD"));
        } finally {
            support.restoreUserState(snapshot);
        }
    }

    @Test
    void transientIdpErrorReturns503WithoutRevokingUser() {
        String extId = "emp-transient-" + UUID.randomUUID();
        support.createdExternalIds.add(extId);
        OidcUser oidcUser = AuthTestSupport.buildOidcUser(
                extId, "瞬时异常测试员", "/总公司/研发中心/技术部", null, List.of("developer"), "STAFF");
        MappedIdentity initial = support.userMappingService.mapAndProvision(oidcUser, false);
        String userIdStr = initial.user().id().toString();
        support.touchedRedisUserIds.add(userIdStr);

        OAuth2AuthorizedClientManager failingManager = authorizeRequest -> {
            throw new OAuth2AuthenticationException(new OAuth2Error("temporarily_unavailable", "503 Service Unavailable", null));
        };
        AuthController transientController = new AuthController(
                support.userMappingService,
                support.permissionResolver,
                support.jwtEncoder,
                support.rsaKey,
                support.publicJwkSet,
                support.mockEnv,
                failingManager,
                support.oidcUserService,
                support.redisRevocationSync
        );
        OAuth2AuthenticationToken authToken = new OAuth2AuthenticationToken(
                oidcUser, oidcUser.getAuthorities(), "corp-sso"
        );
        MockHttpServletRequest req = new MockHttpServletRequest();
        req.getSession(true);
        MockHttpServletResponse resp = new MockHttpServletResponse();

        ResponseStatusException ex = assertThrows(ResponseStatusException.class,
                () -> transientController.exchangeToken(oidcUser, authToken, req, resp));
        assertEquals(503, ex.getStatusCode().value());
        assertFalse(support.redisRevocationSync.isUserRevoked(userIdStr),
                "IdP 瞬时故障时严禁写入用户级 auth:revoked 吊销键");
        assertNull(support.stringRedisTemplate.opsForValue().get(RedisRevocationSync.PERM_CHANGED_KEY_PREFIX + userIdStr),
                "IdP 瞬时故障时严禁写入 auth:perm_changed_at");
    }

    @Test
    void roleChangeWritesMillisecondPermChangedAndNewTokenIatMsExceedsIt() throws Exception {
        String extId = "emp-perm-ms-" + UUID.randomUUID();
        support.createdExternalIds.add(extId);
        OidcUser initialUser = AuthTestSupport.buildOidcUser(
                extId, "毫秒边界测试员", "/总公司/研发中心/技术部", null, List.of("developer"), "STAFF");
        MappedIdentity initial = support.userMappingService.mapAndProvision(initialUser, false);
        String userIdStr = initial.user().id().toString();
        support.touchedRedisUserIds.add(userIdStr);

        OidcUser downgradedUser = AuthTestSupport.buildOidcUser(
                extId, "毫秒边界测试员", "/总公司/研发中心/技术部", null, List.of("employee"), "STAFF");
        OAuth2AuthorizedClientManager passthroughManager = authorizeRequest -> new OAuth2AuthorizedClient(
                support.corpSsoRegistration,
                extId,
                new OAuth2AccessToken(OAuth2AccessToken.TokenType.BEARER, "fake-access", Instant.now(), Instant.now().plusSeconds(300)),
                null
        );
        OidcUserService fixedUserService = new OidcUserService() {
            @Override
            public OidcUser loadUser(org.springframework.security.oauth2.client.oidc.userinfo.OidcUserRequest userRequest) {
                return downgradedUser;
            }
        };
        AuthController controller = new AuthController(
                support.userMappingService,
                support.permissionResolver,
                support.jwtEncoder,
                support.rsaKey,
                support.publicJwkSet,
                support.mockEnv,
                passthroughManager,
                fixedUserService,
                support.redisRevocationSync
        );
        ReflectionTestUtils.setField(controller, "jwtIssuer", "rag-admin");
        ReflectionTestUtils.setField(controller, "jwtAudience", "rag-engine");
        ReflectionTestUtils.setField(controller, "jwtTtlMinutes", 10);

        OAuth2AuthenticationToken authToken = new OAuth2AuthenticationToken(
                initialUser, initialUser.getAuthorities(), "corp-sso"
        );
        MockHttpServletRequest req = new MockHttpServletRequest();
        req.getSession(true);
        MockHttpServletResponse resp = new MockHttpServletResponse();

        Map<String, Object> tokenResp = controller.exchangeToken(initialUser, authToken, req, resp);
        String jwtStr = (String) tokenResp.get("access_token");
        Jwt decoded = NimbusJwtDecoder.withPublicKey(support.rsaKey.toRSAPublicKey()).build().decode(jwtStr);

        String rawChanged = support.stringRedisTemplate.opsForValue().get(RedisRevocationSync.PERM_CHANGED_KEY_PREFIX + userIdStr);
        assertNotNull(rawChanged, "角色变更后必须写入 auth:perm_changed_at:{user_id}");
        long changedMs = Long.parseLong(rawChanged);
        assertTrue(changedMs >= 10_000_000_000L, "写入的 auth:perm_changed_at 必须为 13 位毫秒级时间戳");
        Long iatMs = decoded.getClaim("iat_ms");
        assertNotNull(iatMs, "换票生成的新 JWT 必须包含毫秒级 iat_ms 声明");
        assertTrue(iatMs > changedMs, "新签发 JWT 的 iat_ms 必须严格大于本次变更写入的 changedMs");
    }

    @Test
    void missingOidcSessionFailsFastWithoutFallback() {
        MockHttpServletRequest req = new MockHttpServletRequest();
        req.getSession(true);
        MockHttpServletResponse resp = new MockHttpServletResponse();
        OidcUser dummy = AuthTestSupport.buildOidcUser("emp-no-auth", "测试", "/总公司/研发中心/技术部", null, List.of("developer"), "STAFF");

        // 未提供合法的 OAuth2AuthenticationToken 会话时，严禁回退旧身份签发，必须直接 401 并销毁 Session
        ResponseStatusException ex = assertThrows(ResponseStatusException.class,
                () -> support.authController.exchangeToken(dummy, null, req, resp));
        assertEquals(401, ex.getStatusCode().value());
        assertNull(req.getSession(false));
    }
}
