package com.enterprise.rag.admin;

import com.enterprise.rag.admin.config.JwtConfig;
import com.enterprise.rag.admin.identity.PermissionResolver;
import com.enterprise.rag.admin.identity.PermissionResolver.ResolvedPermissions;
import com.enterprise.rag.admin.identity.UserMappingService;
import com.enterprise.rag.admin.identity.UserMappingService.MappedIdentity;
import com.enterprise.rag.admin.mapper.IdentityMapper;
import com.enterprise.rag.admin.mapper.IdentityMapper.RoleRecord;
import com.enterprise.rag.admin.mapper.IdentityMapper.UserRecord;
import com.enterprise.rag.admin.web.AuthController;
import com.enterprise.rag.admin.web.AuthController.ModelTokenRequest;
import com.nimbusds.jose.jwk.RSAKey;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.mock.env.MockEnvironment;
import org.springframework.security.core.authority.SimpleGrantedAuthority;
import org.springframework.security.oauth2.core.oidc.OidcIdToken;
import org.springframework.security.oauth2.core.oidc.user.DefaultOidcUser;
import org.springframework.security.oauth2.core.oidc.user.OidcUser;
import org.springframework.security.oauth2.jwt.JwsHeader;
import org.springframework.security.oauth2.jwt.JwtClaimsSet;
import org.springframework.security.oauth2.jwt.JwtEncoder;
import org.springframework.security.oauth2.jwt.JwtEncoderParameters;
import org.springframework.test.util.ReflectionTestUtils;
import org.springframework.web.server.ResponseStatusException;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.time.Instant;
import java.time.temporal.ChronoUnit;
import java.util.*;

import static org.junit.jupiter.api.Assertions.*;

/**
 * Phase-6 Java 鉴权中心核心单测与跨语言 JWT 契约 Fixture 生成器 (§8.1 / §8.2 / §8.6)
 */
public class AuthAndContractTest {

    private JwtEncoder jwtEncoder;
    private RSAKey rsaKey;
    private InMemoryIdentityMapper fakeMapper;
    private UserMappingService userMappingService;
    private PermissionResolver permissionResolver;
    private AuthController authController;
    private MockEnvironment mockEnv;

    static class InMemoryIdentityMapper extends IdentityMapper {
        private final Map<String, UserRecord> users = new HashMap<>();
        private final Map<String, RoleRecord> roles = new HashMap<>();
        private boolean nextUserActive = true;

        public InMemoryIdentityMapper() {
            super(null);
            roles.put("admin", new RoleRecord("admin", "超级管理员", "ALL", "[\"*\"]"));
            roles.put("developer", new RoleRecord("developer", "研发工程师", "DEPT_TREE",
                    "[\"kb:retrieve\",\"doc:upload\",\"doc:delete\",\"doc:review\"]"));
            roles.put("accountant", new RoleRecord("accountant", "财务主管", "DEPT_TREE",
                    "[\"kb:retrieve\"]"));
            roles.put("employee", new RoleRecord("employee", "普通员工", "DEPT_TREE",
                    "[\"kb:retrieve\"]"));
        }

        public void setNextUserActive(boolean active) {
            this.nextUserActive = active;
        }

        @Override
        public UserRecord upsertUser(String idSource, String externalId, String realName, String deptPath) {
            String key = idSource + ":" + externalId;
            UserRecord existing = users.get(key);
            UUID id = existing != null ? existing.id() : UUID.nameUUIDFromBytes(key.getBytes(StandardCharsets.UTF_8));
            UserRecord record = new UserRecord(
                    id,
                    "default_tenant",
                    externalId,
                    idSource,
                    realName,
                    deptPath,
                    nextUserActive,
                    Instant.now(),
                    Instant.now()
            );
            users.put(key, record);
            return record;
        }

        @Override
        public List<RoleRecord> listRoles(Collection<String> roleCodes) {
            List<RoleRecord> result = new ArrayList<>();
            for (String code : roleCodes) {
                RoleRecord r = roles.get(code);
                if (r != null) {
                    result.add(r);
                }
            }
            return result;
        }
    }

    @BeforeEach
    void setUp() throws Exception {
        JwtConfig jwtConfig = new JwtConfig();
        Path privPath = Paths.get("../keys/rsa_private.pem").toAbsolutePath().normalize();
        Path pubPath = Paths.get("../keys/rsa_public.pem").toAbsolutePath().normalize();
        ReflectionTestUtils.setField(jwtConfig, "privateKeyPath", privPath.toString());
        ReflectionTestUtils.setField(jwtConfig, "publicKeyPath", pubPath.toString());

        this.rsaKey = jwtConfig.rsaKey();
        this.jwtEncoder = jwtConfig.jwtEncoder(jwtConfig.jwkSource(this.rsaKey));
        this.fakeMapper = new InMemoryIdentityMapper();
        this.userMappingService = new UserMappingService(this.fakeMapper);
        this.permissionResolver = new PermissionResolver(this.fakeMapper);

        this.mockEnv = new MockEnvironment()
                .withProperty("rag.model-vault.kedaya.base-url", "https://kedaya.ai/v1")
                .withProperty("rag.model-vault.kedaya.api-key", "sk-vault-kedaya-12345678")
                .withProperty("rag.model-vault.siliconflow.base-url", "https://api.siliconflow.cn/v1")
                .withProperty("rag.model-vault.siliconflow.api-key", "sk-vault-silicon-87654321");

        this.authController = new AuthController(
                userMappingService,
                permissionResolver,
                jwtEncoder,
                rsaKey,
                mockEnv
        );
        ReflectionTestUtils.setField(authController, "jwtIssuer", "rag-admin");
        ReflectionTestUtils.setField(authController, "jwtAudience", "rag-engine");
        ReflectionTestUtils.setField(authController, "jwtTtlMinutes", 30);
        ReflectionTestUtils.setField(authController, "internalServiceId", "rag-engine");
        ReflectionTestUtils.setField(authController, "internalServiceToken", "rag-engine-service-secret");
    }

    private OidcUser buildOidcUser(String sub, String username, String deptPath, List<String> groups, List<String> roles) {
        Map<String, Object> claims = new HashMap<>();
        claims.put("sub", sub);
        claims.put("preferred_username", username);
        if (deptPath != null) {
            claims.put("dept_path", deptPath);
        }
        if (groups != null) {
            claims.put("groups", groups);
        }
        if (roles != null) {
            claims.put("roles", roles);
        }
        OidcIdToken idToken = new OidcIdToken("mock-id-token", Instant.now(), Instant.now().plusSeconds(3600), claims);
        return new DefaultOidcUser(List.of(new SimpleGrantedAuthority("ROLE_USER")), idToken, "preferred_username");
    }

    @Test
    void test01_jitProvisionAndGroupMapping() {
        // 仅通过 /corp/ 前缀映射部门，且通过 /roles/ 映射角色
        OidcUser oidcUser = buildOidcUser(
                "emp-dev-001",
                "张研发",
                null,
                List.of("/corp/总公司/研发中心/技术部", "/roles/developer"),
                null
        );
        MappedIdentity mapped = userMappingService.mapAndProvision(oidcUser);
        assertEquals("/总公司/研发中心/技术部", mapped.user().deptPath());
        assertEquals(List.of("developer"), mapped.roleCodes());
    }

    @Test
    void test02_missingDeptPathOrNonCorpGroupRejected403() {
        // 含非 /corp/ 普通安全组（如 /vpn-users）但缺真实部门时，坚决抛 403 拒绝建档 (§8.1 用例 3)
        OidcUser oidcUser = buildOidcUser(
                "emp-nodept-002",
                "无部门员工",
                null,
                List.of("/vpn-users", "/roles/employee"),
                null
        );
        ResponseStatusException ex = assertThrows(ResponseStatusException.class,
                () -> userMappingService.mapAndProvision(oidcUser));
        assertEquals(403, ex.getStatusCode().value());
    }

    @Test
    void test03_disabledUserRejected403() {
        // 停用账号换票坚决抛 403 (§8.5 用例 22)
        fakeMapper.setNextUserActive(false);
        OidcUser oidcUser = buildOidcUser(
                "emp-disabled-003",
                "已离职员工",
                "/总公司/研发中心/技术部",
                null,
                List.of("developer")
        );
        ResponseStatusException ex = assertThrows(ResponseStatusException.class,
                () -> userMappingService.mapAndProvision(oidcUser));
        assertEquals(403, ex.getStatusCode().value());
    }

    @Test
    void test04_unknownRoleRejected403AndMultiRoleMerge() {
        // 未知角色码 -> 403 (§8.1 用例 4)
        ResponseStatusException ex = assertThrows(ResponseStatusException.class,
                () -> permissionResolver.resolve(List.of("unknown_hacker_role")));
        assertEquals(403, ex.getStatusCode().value());

        // 多角色合并：ALL > DEPT_TREE，权限取并集 (§2.3)
        ResolvedPermissions merged = permissionResolver.resolve(List.of("accountant", "developer"));
        assertEquals("DEPT_TREE", merged.dataScope());
        assertTrue(merged.permissions().containsAll(List.of("kb:retrieve", "doc:upload", "doc:delete", "doc:review")));

        ResolvedPermissions adminMerged = permissionResolver.resolve(List.of("employee", "admin"));
        assertEquals("ALL", adminMerged.dataScope());
        assertEquals(Set.of("*"), adminMerged.permissions());
    }

    @Test
    void test05_modelTokenVaultLeaseAndSecurityBoundaries() {
        // 合法换取 Kedaya 租约 (§8.6 用例 23)
        Map<String, Object> lease = authController.issueModelToken(
                "rag-engine",
                "rag-engine-service-secret",
                new ModelTokenRequest("kedaya", List.of("model:llm"))
        );
        assertEquals("kedaya", lease.get("provider"));
        assertEquals("https://kedaya.ai/v1", lease.get("base_url"));
        assertEquals("sk-vault-kedaya-12345678", lease.get("api_key"));
        assertEquals("sk-v****5678", lease.get("masked"));

        // 错误的 Service Token -> 403 (§8.6 用例 26)
        ResponseStatusException badTokenEx = assertThrows(ResponseStatusException.class,
                () -> authController.issueModelToken(
                        "rag-engine",
                        "wrong-secret",
                        new ModelTokenRequest("kedaya", List.of("model:llm"))
                ));
        assertEquals(403, badTokenEx.getStatusCode().value());

        // 未授权的 scope (如 oss:write) -> 403 (§8.6 用例 27)
        ResponseStatusException badScopeEx = assertThrows(ResponseStatusException.class,
                () -> authController.issueModelToken(
                        "rag-engine",
                        "rag-engine-service-secret",
                        new ModelTokenRequest("kedaya", List.of("oss:write"))
                ));
        assertEquals(403, badScopeEx.getStatusCode().value());
    }

    @Test
    void test06_generateCrossLanguageJwtFixturesForPython() throws Exception {
        // 由 Java NimbusJwtEncoder 真实签发 Token Fixture 供 Python pytest 消费 (§8.2 用例 10)
        OidcUser devOidc = buildOidcUser(
                "user_dev_01",
                "张工",
                "/总公司/研发中心/技术部",
                null,
                List.of("developer")
        );
        Map<String, Object> devTokenResp = authController.exchangeToken(devOidc);
        String developerToken = (String) devTokenResp.get("access_token");

        OidcUser accOidc = buildOidcUser(
                "user_acc_01",
                "王会计",
                "/总公司/财务部",
                null,
                List.of("accountant")
        );
        String accountantToken = (String) authController.exchangeToken(accOidc).get("access_token");

        OidcUser adminOidc = buildOidcUser(
                "user_admin_01",
                "系统管理员",
                "/总公司",
                null,
                List.of("admin")
        );
        String adminToken = (String) authController.exchangeToken(adminOidc).get("access_token");

        // 构造 To C 测试令牌与过期令牌 (同样由 Java NimbusJwtEncoder 签发)
        JwsHeader header = JwsHeader.with(() -> "RS256").keyId(rsaKey.getKeyID()).type("JWT").build();
        Instant now = Instant.now();

        JwtClaimsSet toCClaims = JwtClaimsSet.builder()
                .issuer("rag-admin")
                .audience(List.of("rag-engine"))
                .subject("c_user_999")
                .claim("username", "外部C端访客")
                .claim("tenant_id", "default_tenant")
                .claim("roles", List.of("c_user"))
                .claim("perms", List.of("kb:retrieve"))
                .claim("dept_path", "/public")
                .claim("data_scope", "DEPT_TREE")
                .claim("is_to_c", true)
                .claim("cv", 1)
                .claim("typ", "access")
                .issuedAt(now)
                .expiresAt(now.plus(30, ChronoUnit.MINUTES))
                .build();
        String toCToken = jwtEncoder.encode(JwtEncoderParameters.from(header, toCClaims)).getTokenValue();

        JwtClaimsSet expiredClaims = JwtClaimsSet.builder()
                .issuer("rag-admin")
                .audience(List.of("rag-engine"))
                .subject("user_expired_01")
                .claim("username", "过期员工")
                .claim("tenant_id", "default_tenant")
                .claim("roles", List.of("developer"))
                .claim("perms", List.of("kb:retrieve"))
                .claim("dept_path", "/总公司/研发中心/技术部")
                .claim("data_scope", "DEPT_TREE")
                .claim("is_to_c", false)
                .claim("cv", 1)
                .claim("typ", "access")
                .issuedAt(now.minus(120, ChronoUnit.MINUTES))
                .expiresAt(now.minus(60, ChronoUnit.MINUTES))
                .build();
        String expiredToken = jwtEncoder.encode(JwtEncoderParameters.from(header, expiredClaims)).getTokenValue();

        Path fixtureDir = Paths.get("../backend/tests/fixtures").toAbsolutePath().normalize();
        Files.createDirectories(fixtureDir);
        Path fixtureFile = fixtureDir.resolve("java_issued_tokens.json");

        String json = "{\n" +
                "  \"issuer\": \"rag-admin\",\n" +
                "  \"audience\": \"rag-engine\",\n" +
                "  \"kid\": \"" + rsaKey.getKeyID() + "\",\n" +
                "  \"developer_token\": \"" + developerToken + "\",\n" +
                "  \"accountant_token\": \"" + accountantToken + "\",\n" +
                "  \"admin_token\": \"" + adminToken + "\",\n" +
                "  \"to_c_token\": \"" + toCToken + "\",\n" +
                "  \"expired_token\": \"" + expiredToken + "\"\n" +
                "}\n";
        Files.writeString(fixtureFile, json, StandardCharsets.UTF_8);
        assertTrue(Files.exists(fixtureFile));
    }
}
