package com.enterprise.rag.admin;

import com.enterprise.rag.admin.config.JwtConfig;
import com.nimbusds.jose.jwk.JWKSet;
import com.nimbusds.jose.jwk.RSAKey;
import org.junit.jupiter.api.Test;
import org.springframework.security.oauth2.jwt.JwsHeader;
import org.springframework.security.oauth2.jwt.Jwt;
import org.springframework.security.oauth2.jwt.JwtClaimsSet;
import org.springframework.security.oauth2.jwt.JwtEncoder;
import org.springframework.security.oauth2.jwt.JwtEncoderParameters;
import org.springframework.security.oauth2.jwt.NimbusJwtDecoder;
import org.springframework.test.util.ReflectionTestUtils;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.security.KeyPair;
import java.security.KeyPairGenerator;
import java.security.interfaces.RSAPublicKey;
import java.time.Instant;
import java.util.*;

import static org.junit.jupiter.api.Assertions.*;

/** Java 签名与 Python 消费 cv=2 字段契约检查，仅加载本地 RSA 密钥。 */
class CrossLanguageContractTest {
    @Test
    void jwksPublishesCurrentAndPreviousPublicKeys() throws Exception {
        KeyPairGenerator kpg = KeyPairGenerator.getInstance("RSA");
        kpg.initialize(2048);
        KeyPair prevPair = kpg.generateKeyPair();
        String pem = "-----BEGIN PUBLIC KEY-----\n"
                + Base64.getMimeEncoder(64, "\n".getBytes(StandardCharsets.UTF_8))
                .encodeToString(prevPair.getPublic().getEncoded())
                + "\n-----END PUBLIC KEY-----\n";
        Path tempPub = Files.createTempFile("prev_rsa_pub_", ".pem");
        try {
            Files.writeString(tempPub, pem, StandardCharsets.UTF_8);
            JwtConfig rotCfg = new JwtConfig();
            Path privPath = Paths.get("../keys/rsa_private.pem").toAbsolutePath().normalize();
            Path pubPath = Paths.get("../keys/rsa_public.pem").toAbsolutePath().normalize();
            ReflectionTestUtils.setField(rotCfg, "keyId", "rag-admin-key-2");
            ReflectionTestUtils.setField(rotCfg, "privateKeyPath", privPath.toString());
            ReflectionTestUtils.setField(rotCfg, "publicKeyPath", pubPath.toString());
            ReflectionTestUtils.setField(rotCfg, "previousKeyId", "rag-admin-key-1");
            ReflectionTestUtils.setField(rotCfg, "previousPublicKeyPath", tempPub.toString());

            RSAKey activeKey = rotCfg.rsaKey();
            JWKSet jwkSet = rotCfg.publicJwkSet(activeKey);
            assertEquals(2, jwkSet.getKeys().size());
            assertNotNull(jwkSet.getKeyByKeyId("rag-admin-key-2"));
            assertNotNull(jwkSet.getKeyByKeyId("rag-admin-key-1"));
            assertTrue(((RSAKey) jwkSet.getKeyByKeyId("rag-admin-key-1")).toRSAPublicKey() instanceof RSAPublicKey);
        } finally {
            Files.deleteIfExists(tempPub);
        }
    }

    @Test
    void signedCv2TokenPreservesConsumerClaims() throws Exception {
        JwtConfig config = AuthTestSupport.jwtConfig();
        RSAKey key = config.rsaKey();
        JwtEncoder encoder = config.jwtEncoder(config.jwkSource(key));
        Instant now = Instant.now().truncatedTo(java.time.temporal.ChronoUnit.SECONDS);

        Map<String, String> scopeByAction = Map.of(
                "READ", "ALL_ORG",
                "UPLOAD", "DEPT_AND_SUB",
                "DELETE", "DEPT_AND_SUB"
        );
        List<String> deptPaths = List.of("/总公司/研发中心/技术部", "/总公司/职能中心/人事行政部");

        JwtClaimsSet claims = JwtClaimsSet.builder()
                .issuer("rag-admin")
                .audience(List.of("rag-engine"))
                .subject(UUID.randomUUID().toString())
                .claim("username", "多维契约测试员工")
                .claim("tenant_id", "default_tenant")
                .claim("roles", List.of("developer", "executive"))
                .claim("idp_roles", List.of("developer"))
                .claim("perms", List.of("kb:retrieve", "doc:upload", "doc:delete"))
                .claim("dept_path", "/总公司/研发中心/技术部")
                .claim("dept_paths", deptPaths)
                .claim("scope_by_action", scopeByAction)
                .claim("position_code", "MANAGER")
                .claim("clearance_level", 3)
                .claim("authz_ver", 7L)
                .claim("policy_epoch", 12L)
                .claim("is_to_c", false)
                .claim("cv", 2)
                .claim("typ", "access")
                .claim("iat_ms", now.toEpochMilli())
                .issuedAt(now)
                .expiresAt(now.plusSeconds(600))
                .build();

        JwsHeader header = JwsHeader.with(() -> "RS256").keyId(key.getKeyID()).type("JWT").build();
        String token = encoder.encode(JwtEncoderParameters.from(header, claims)).getTokenValue();
        Jwt decoded = NimbusJwtDecoder.withPublicKey(key.toRSAPublicKey()).build().decode(token);

        assertEquals(key.getKeyID(), decoded.getHeaders().get("kid"));
        assertEquals(claims.getClaims().keySet(), decoded.getClaims().keySet());
        for (String name : List.of("sub", "iss", "username", "tenant_id", "dept_path", "position_code", "typ")) {
            assertEquals(claims.getClaim(name), decoded.getClaimAsString(name));
        }
        assertEquals(List.of("rag-engine"), decoded.getAudience());
        assertEquals(List.of("developer", "executive"), decoded.getClaimAsStringList("roles"));
        assertEquals(List.of("developer"), decoded.getClaimAsStringList("idp_roles"));
        assertEquals(deptPaths, decoded.getClaimAsStringList("dept_paths"));
        assertEquals(scopeByAction, decoded.getClaim("scope_by_action"));
        assertEquals(3, ((Number) decoded.getClaim("clearance_level")).intValue());
        assertEquals(7L, ((Number) decoded.getClaim("authz_ver")).longValue());
        assertEquals(12L, ((Number) decoded.getClaim("policy_epoch")).longValue());
        assertEquals(false, decoded.getClaimAsBoolean("is_to_c"));
        assertEquals(2, ((Number) decoded.getClaim("cv")).intValue());
        assertEquals(now.toEpochMilli(), ((Number) decoded.getClaim("iat_ms")).longValue());
        assertEquals(now, decoded.getIssuedAt());
        assertEquals(now.plusSeconds(600), decoded.getExpiresAt());
    }

    @Test
    void generateCv2CrossLanguageFixtureForPython() throws Exception {
        JwtConfig config = AuthTestSupport.jwtConfig();
        RSAKey key = config.rsaKey();
        JwtEncoder encoder = config.jwtEncoder(config.jwkSource(key));
        JwsHeader header = JwsHeader.with(() -> "RS256").keyId(key.getKeyID()).type("JWT").build();
        Instant now = Instant.now().truncatedTo(java.time.temporal.ChronoUnit.SECONDS);

        Map<String, String> fixture = new LinkedHashMap<>();
        fixture.put("developer_token", encodeFixtureToken(
                encoder, header, now, 600,
                UUID.nameUUIDFromBytes("java-fixture-dev".getBytes(StandardCharsets.UTF_8)).toString(),
                "张工", "default_tenant", "/总公司/研发中心/技术部",
                List.of("developer"), List.of("kb:retrieve", "doc:upload", "doc:delete", "doc:review"),
                Map.of("READ", "DEPT_AND_SUB", "UPLOAD", "DEPT_AND_SUB", "DELETE", "DEPT_AND_SUB", "REVIEW", "DEPT_AND_SUB"),
                "STAFF", 2, false
        ));
        fixture.put("accountant_token", encodeFixtureToken(
                encoder, header, now, 600,
                UUID.nameUUIDFromBytes("java-fixture-acc".getBytes(StandardCharsets.UTF_8)).toString(),
                "王会计", "default_tenant", "/总公司/职能中心/财务部",
                List.of("employee"), List.of("kb:retrieve"),
                Map.of("READ", "DEPT_AND_SUB"),
                "STAFF", 2, false
        ));
        fixture.put("admin_token", encodeFixtureToken(
                encoder, header, now, 600,
                UUID.nameUUIDFromBytes("java-fixture-adm".getBytes(StandardCharsets.UTF_8)).toString(),
                "系统管理员", "default_tenant", "/总公司/研发中心/技术部",
                List.of("admin"), List.of("kb:retrieve", "kb:manage", "kb:grant", "user:authz:manage", "system:admin"),
                Map.of("READ", "ALL_ORG", "MANAGE", "ALL_ORG"),
                "EXECUTIVE", 4, false
        ));
        fixture.put("to_c_token", encodeFixtureToken(
                encoder, header, now, 600,
                UUID.nameUUIDFromBytes("java-fixture-toc".getBytes(StandardCharsets.UTF_8)).toString(),
                "外部ToC用户", "default_tenant", "/public",
                List.of("employee"), List.of("kb:retrieve"),
                Map.of("READ", "CUSTOM_ACL"),
                "INTERN_EXT", 1, true
        ));
        fixture.put("expired_token", encodeFixtureToken(
                encoder, header, now.minusSeconds(1200), 600,
                UUID.nameUUIDFromBytes("java-fixture-exp".getBytes(StandardCharsets.UTF_8)).toString(),
                "过期测试员工", "default_tenant", "/public",
                List.of("employee"), List.of("kb:retrieve"),
                Map.of("READ", "DEPT_AND_SUB"),
                "STAFF", 2, false
        ));

        Path outPath = Paths.get("../backend/tests/fixtures/java_issued_tokens.json").toAbsolutePath().normalize();
        Files.createDirectories(outPath.getParent());
        tools.jackson.databind.ObjectMapper om = new tools.jackson.databind.ObjectMapper();
        Files.writeString(outPath, om.writerWithDefaultPrettyPrinter().writeValueAsString(fixture), StandardCharsets.UTF_8);
        assertTrue(Files.isRegularFile(outPath));
    }

    private static String encodeFixtureToken(
            JwtEncoder encoder,
            JwsHeader header,
            Instant issuedAt,
            long ttlSec,
            String sub,
            String username,
            String tenantId,
            String deptPath,
            List<String> roles,
            List<String> perms,
            Map<String, String> scopeByAction,
            String positionCode,
            int clearanceLevel,
            boolean isToC
    ) {
        JwtClaimsSet claims = JwtClaimsSet.builder()
                .issuer("rag-admin")
                .audience(List.of("rag-engine"))
                .subject(sub)
                .claim("username", username)
                .claim("tenant_id", tenantId)
                .claim("roles", roles)
                .claim("idp_roles", roles)
                .claim("perms", perms)
                .claim("dept_path", deptPath)
                .claim("dept_paths", List.of(deptPath))
                .claim("scope_by_action", scopeByAction)
                .claim("position_code", positionCode)
                .claim("clearance_level", clearanceLevel)
                .claim("authz_ver", 1L)
                .claim("policy_epoch", 1L)
                .claim("is_to_c", isToC)
                .claim("cv", 2)
                .claim("typ", "access")
                .claim("iat_ms", issuedAt.toEpochMilli())
                .issuedAt(issuedAt)
                .expiresAt(issuedAt.plusSeconds(ttlSec))
                .build();
        return encoder.encode(JwtEncoderParameters.from(header, claims)).getTokenValue();
    }
}
