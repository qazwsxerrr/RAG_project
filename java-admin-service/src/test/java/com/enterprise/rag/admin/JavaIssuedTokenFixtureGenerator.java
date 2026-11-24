package com.enterprise.rag.admin;

import com.enterprise.rag.admin.identity.RedisRevocationSync;
import com.enterprise.rag.admin.mapper.IdentityMapper;
import org.junit.jupiter.api.Test;
import org.springframework.security.oauth2.jwt.JwsHeader;
import org.springframework.security.oauth2.jwt.JwtClaimsSet;
import org.springframework.security.oauth2.jwt.JwtEncoderParameters;
import org.springframework.test.util.ReflectionTestUtils;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.nio.file.StandardCopyOption;
import java.time.Instant;
import java.time.temporal.ChronoUnit;
import java.util.*;

import static org.junit.jupiter.api.Assertions.*;

/** 显式执行 mvn -Dtest=JavaIssuedTokenFixtureGenerator test 生成短有效期 JWT。 */
class JavaIssuedTokenFixtureGenerator {
    @Test
    void generateFixtures() throws Exception {
        try (OidcTestSupport support = new OidcTestSupport()) {
            // 通过真实 Keycloak PKCE 登录 + 真实 PostgreSQL 建档 + Java NimbusJwtEncoder 签发短有效期 (30min) 跨语言契约 Token Fixture
            ReflectionTestUtils.setField(support.authController, "jwtTtlMinutes", 30);
            List<OidcTestSupport.UserStateSnapshot> snapshots = new ArrayList<>();
            String json;
            try {
                OidcTestSupport.RealKeycloakSession devSession = support.loginViaRealKeycloakPkce("developer", "Rag@123456");
                String devSub = devSession.oidcUser().getSubject();
                snapshots.add(support.snapshotUserState(devSub));
                support.identityMapper.findUser(IdentityMapper.DEFAULT_TENANT_ID, "oidc:corp-sso", devSub).ifPresent(u -> {
                    if (!u.isActive()) {
                        support.jdbcTemplate.update("UPDATE sys_users SET is_active = TRUE WHERE id = ?", u.id());
                    }
                });
                Map<String, Object> devProfile = support.authController.getMyProfile(
                        devSession.oidcUser(), devSession.authToken(), devSession.request(), devSession.response()
                );
                support.redisRevocationSync.clearUserRevoked(String.valueOf(devProfile.get("user_id")));
                support.stringRedisTemplate.delete(RedisRevocationSync.PERM_CHANGED_KEY_PREFIX + devProfile.get("user_id"));
                Map<String, Object> devTokenResp = support.authController.exchangeToken(
                        devSession.oidcUser(), devSession.authToken(), devSession.request(), devSession.response()
                );
                String developerToken = (String) devTokenResp.get("access_token");

                OidcTestSupport.RealKeycloakSession accSession = support.loginViaRealKeycloakPkce("accountant", "Rag@123456");
                String accSub = accSession.oidcUser().getSubject();
                snapshots.add(support.snapshotUserState(accSub));
                support.identityMapper.findUser(IdentityMapper.DEFAULT_TENANT_ID, "oidc:corp-sso", accSub).ifPresent(u -> {
                    if (!u.isActive()) {
                        support.jdbcTemplate.update("UPDATE sys_users SET is_active = TRUE WHERE id = ?", u.id());
                    }
                });
                Map<String, Object> accProfile = support.authController.getMyProfile(
                        accSession.oidcUser(), accSession.authToken(), accSession.request(), accSession.response()
                );
                support.redisRevocationSync.clearUserRevoked(String.valueOf(accProfile.get("user_id")));
                support.stringRedisTemplate.delete(RedisRevocationSync.PERM_CHANGED_KEY_PREFIX + accProfile.get("user_id"));
                String accountantToken = (String) support.authController.exchangeToken(
                        accSession.oidcUser(), accSession.authToken(), accSession.request(), accSession.response()
                ).get("access_token");

                OidcTestSupport.RealKeycloakSession adminSession = support.loginViaRealKeycloakPkce("admin", "Rag@123456");
                String admSub = adminSession.oidcUser().getSubject();
                snapshots.add(support.snapshotUserState(admSub));
                support.identityMapper.findUser(IdentityMapper.DEFAULT_TENANT_ID, "oidc:corp-sso", admSub).ifPresent(u -> {
                    if (!u.isActive()) {
                        support.jdbcTemplate.update("UPDATE sys_users SET is_active = TRUE WHERE id = ?", u.id());
                    }
                });
                Map<String, Object> admProfile = support.authController.getMyProfile(
                        adminSession.oidcUser(), adminSession.authToken(), adminSession.request(), adminSession.response()
                );
                support.redisRevocationSync.clearUserRevoked(String.valueOf(admProfile.get("user_id")));
                support.stringRedisTemplate.delete(RedisRevocationSync.PERM_CHANGED_KEY_PREFIX + admProfile.get("user_id"));
                String adminToken = (String) support.authController.exchangeToken(
                        adminSession.oidcUser(), adminSession.authToken(), adminSession.request(), adminSession.response()
                ).get("access_token");

                // 构造 To C 测试令牌与过期令牌 (同样由 Java NimbusJwtEncoder 签发)
                JwsHeader header = JwsHeader.with(() -> "RS256").keyId(support.rsaKey.getKeyID()).type("JWT").build();
                Instant now = Instant.now();
                long nowMs = now.toEpochMilli();

                JwtClaimsSet toCClaims = JwtClaimsSet.builder()
                        .issuer("rag-admin")
                        .audience(List.of("rag-engine"))
                        .subject("c_user_999")
                        .claim("username", "外部C端访客")
                        .claim("tenant_id", "default_tenant")
                        .claim("roles", List.of("c_user"))
                        .claim("idp_roles", List.of("c_user"))
                        .claim("perms", List.of("kb:retrieve"))
                        .claim("dept_path", "/public")
                        .claim("dept_paths", List.of("/public"))
                        .claim("scope_by_action", Map.of("READ", "CUSTOM_ACL"))
                        .claim("position_code", "INTERN_EXT")
                        .claim("clearance_level", 1)
                        .claim("authz_ver", 1L)
                        .claim("policy_epoch", 1L)
                        .claim("is_to_c", true)
                        .claim("cv", 2)
                        .claim("typ", "access")
                        .claim("iat_ms", nowMs)
                        .issuedAt(now)
                        .expiresAt(now.plus(30, ChronoUnit.MINUTES))
                        .build();
                String toCToken = support.jwtEncoder.encode(JwtEncoderParameters.from(header, toCClaims)).getTokenValue();

                JwtClaimsSet expiredClaims = JwtClaimsSet.builder()
                        .issuer("rag-admin")
                        .audience(List.of("rag-engine"))
                        .subject("user_expired_01")
                        .claim("username", "过期员工")
                        .claim("tenant_id", "default_tenant")
                        .claim("roles", List.of("developer"))
                        .claim("idp_roles", List.of("developer"))
                        .claim("perms", List.of("kb:retrieve"))
                        .claim("dept_path", "/总公司/研发中心/技术部")
                        .claim("dept_paths", List.of("/总公司/研发中心/技术部"))
                        .claim("scope_by_action", Map.of("READ", "DEPT_AND_SUB"))
                        .claim("position_code", "STAFF")
                        .claim("clearance_level", 2)
                        .claim("authz_ver", 1L)
                        .claim("policy_epoch", 1L)
                        .claim("is_to_c", false)
                        .claim("cv", 2)
                        .claim("typ", "access")
                        .claim("iat_ms", now.minus(120, ChronoUnit.MINUTES).toEpochMilli())
                        .issuedAt(now.minus(120, ChronoUnit.MINUTES))
                        .expiresAt(now.minus(60, ChronoUnit.MINUTES))
                        .build();
                String expiredToken = support.jwtEncoder.encode(JwtEncoderParameters.from(header, expiredClaims)).getTokenValue();

                json = "{\n" +
                        "  \"issuer\": \"rag-admin\",\n" +
                        "  \"audience\": \"rag-engine\",\n" +
                        "  \"kid\": \"" + support.rsaKey.getKeyID() + "\",\n" +
                        "  \"developer_token\": \"" + developerToken + "\",\n" +
                        "  \"accountant_token\": \"" + accountantToken + "\",\n" +
                        "  \"admin_token\": \"" + adminToken + "\",\n" +
                        "  \"to_c_token\": \"" + toCToken + "\",\n" +
                        "  \"expired_token\": \"" + expiredToken + "\"\n" +
                        "}\n";
            } finally {
                for (int i = snapshots.size() - 1; i >= 0; i--) {
                    support.restoreUserState(snapshots.get(i));
                }
                ReflectionTestUtils.setField(support.authController, "jwtTtlMinutes", 10);
            }
            Path fixtureDir = Paths.get("../backend/tests/fixtures").toAbsolutePath().normalize();
            Files.createDirectories(fixtureDir);
            Path fixtureFile = fixtureDir.resolve("java_issued_tokens.json");
            Path temporaryFile = Files.createTempFile(fixtureDir, "java_issued_tokens-", ".json");
            try {
                Files.writeString(temporaryFile, json, StandardCharsets.UTF_8);
                Files.move(temporaryFile, fixtureFile, StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING);
                assertTrue(Files.exists(fixtureFile));
            } finally {
                Files.deleteIfExists(temporaryFile);
            }
        }
    }
}
