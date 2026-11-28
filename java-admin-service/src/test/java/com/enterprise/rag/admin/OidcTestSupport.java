package com.enterprise.rag.admin;

import com.enterprise.rag.admin.config.JwtConfig;
import com.enterprise.rag.admin.config.SecurityConfig;
import com.enterprise.rag.admin.identity.PermissionResolver;
import com.enterprise.rag.admin.identity.RedisRevocationSync;
import com.enterprise.rag.admin.identity.UserMappingService;
import com.enterprise.rag.admin.mapper.IdentityMapper;
import com.enterprise.rag.admin.mapper.IdentityMapper.UserRecord;
import com.enterprise.rag.admin.web.AuthController;
import com.nimbusds.jose.jwk.JWKSet;
import com.nimbusds.jose.jwk.RSAKey;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.mock.env.MockEnvironment;
import org.springframework.mock.web.MockHttpServletRequest;
import org.springframework.mock.web.MockHttpServletResponse;
import org.springframework.security.core.authority.SimpleGrantedAuthority;
import org.springframework.security.oauth2.client.OAuth2AuthorizedClient;
import org.springframework.security.oauth2.client.OAuth2AuthorizedClientManager;
import org.springframework.security.oauth2.client.authentication.OAuth2AuthenticationToken;
import org.springframework.security.oauth2.client.oidc.userinfo.OidcUserService;
import org.springframework.security.oauth2.client.registration.ClientRegistration;
import org.springframework.security.oauth2.client.registration.ClientRegistrationRepository;
import org.springframework.security.oauth2.client.registration.InMemoryClientRegistrationRepository;
import org.springframework.security.oauth2.client.web.HttpSessionOAuth2AuthorizedClientRepository;
import org.springframework.security.oauth2.client.web.OAuth2AuthorizedClientRepository;
import org.springframework.security.oauth2.core.AuthorizationGrantType;
import org.springframework.security.oauth2.core.ClientAuthenticationMethod;
import org.springframework.security.oauth2.core.OAuth2AccessToken;
import org.springframework.security.oauth2.core.OAuth2RefreshToken;
import org.springframework.security.oauth2.core.oidc.OidcIdToken;
import org.springframework.security.oauth2.core.oidc.user.DefaultOidcUser;
import org.springframework.security.oauth2.core.oidc.user.OidcUser;
import org.springframework.security.oauth2.jwt.Jwt;
import org.springframework.security.oauth2.jwt.JwtEncoder;
import org.springframework.security.oauth2.jwt.NimbusJwtDecoder;
import org.springframework.test.util.ReflectionTestUtils;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import java.net.URI;
import java.net.URLEncoder;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.SecureRandom;
import java.time.Instant;
import java.util.*;
import java.util.concurrent.TimeUnit;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import org.springframework.security.core.context.SecurityContextHolder;

import static org.junit.jupiter.api.Assertions.*;

/** 真实 OIDC 联调所需组件与登录流程，按需创建并释放。 */
final class OidcTestSupport implements AutoCloseable {
    static final String KEYCLOAK_BASE = "http://localhost:8180/realms/enterprise-rag/protocol/openid-connect";
    static final String CLIENT_ID = "rag-admin-service";
    static final String CLIENT_SECRET = "rag-admin-secret";
    static final String REDIRECT_URI = "http://localhost:8080/login/oauth2/code/corp-sso";

    JwtEncoder jwtEncoder;
    RSAKey rsaKey;
    JWKSet publicJwkSet;
    JdbcTemplate jdbcTemplate;
    StringRedisTemplate stringRedisTemplate;
    RedisRevocationSync redisRevocationSync;
    IdentityMapper identityMapper;
    UserMappingService userMappingService;
    PermissionResolver permissionResolver;
    ClientRegistration corpSsoRegistration;
    OAuth2AuthorizedClientRepository authorizedClientRepository;
    OAuth2AuthorizedClientManager authorizedClientManager;
    OidcUserService oidcUserService;
    AuthController authController;
    MockEnvironment mockEnv;
    final List<String> createdExternalIds = new ArrayList<>();
    final List<String> touchedRedisUserIds;
    final RedisTestSupport redis;

    OidcTestSupport() throws Exception {
        JwtConfig jwtConfig = AuthTestSupport.jwtConfig();
        rsaKey = jwtConfig.rsaKey();
        publicJwkSet = jwtConfig.publicJwkSet(rsaKey);
        jwtEncoder = jwtConfig.jwtEncoder(jwtConfig.jwkSource(rsaKey));
        jdbcTemplate = AuthTestSupport.database();
        redis = new RedisTestSupport();
        stringRedisTemplate = redis.client;
        redisRevocationSync = redis.revocations;
        touchedRedisUserIds = new ArrayList<>();
        try {
            this.identityMapper = new IdentityMapper(this.jdbcTemplate);
            this.userMappingService = new UserMappingService(this.identityMapper, this.redisRevocationSync);
            this.permissionResolver = new PermissionResolver(this.identityMapper);

            // 3. 绑定真实 Keycloak OIDC Provider 与 Spring Security 标准组件
            this.corpSsoRegistration = ClientRegistration.withRegistrationId("corp-sso")
                    .clientId(CLIENT_ID)
                    .clientSecret(CLIENT_SECRET)
                    .clientAuthenticationMethod(ClientAuthenticationMethod.CLIENT_SECRET_BASIC)
                    .authorizationGrantType(AuthorizationGrantType.AUTHORIZATION_CODE)
                    .redirectUri(REDIRECT_URI)
                    .scope("openid", "profile", "email")
                    .authorizationUri(KEYCLOAK_BASE + "/auth")
                    .tokenUri(KEYCLOAK_BASE + "/token")
                    .userInfoUri(KEYCLOAK_BASE + "/userinfo")
                    .jwkSetUri(KEYCLOAK_BASE + "/certs")
                    .userNameAttributeName("preferred_username")
                    .build();

            SecurityConfig securityConfig = new SecurityConfig();
            ClientRegistrationRepository clientRepo = new InMemoryClientRegistrationRepository(this.corpSsoRegistration);
            this.authorizedClientRepository = new HttpSessionOAuth2AuthorizedClientRepository();
            this.authorizedClientManager = securityConfig.authorizedClientManager(clientRepo, this.authorizedClientRepository);
            this.oidcUserService = securityConfig.oidcUserService();

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
                    publicJwkSet,
                    mockEnv,
                    authorizedClientManager,
                    oidcUserService,
                    redisRevocationSync
            );
            ReflectionTestUtils.setField(authController, "jwtIssuer", "rag-admin");
            ReflectionTestUtils.setField(authController, "jwtAudience", "rag-engine");
            ReflectionTestUtils.setField(authController, "jwtTtlMinutes", 10);
            ReflectionTestUtils.setField(authController, "internalServiceId", "rag-engine");
            ReflectionTestUtils.setField(authController, "internalServiceToken", "rag-engine-service-secret");
            ReflectionTestUtils.setField(authController, "allowedProvidersConfig",
                    "kedaya,siliconflow,deepseek,dashscope,zhipu,openai,volces");
        } catch (Exception | Error ex) {
            redis.close();
            throw ex;
        }
    }

    @Override
    public void close() {
        try {
            for (String extId : createdExternalIds) {
                identityMapper.findUser(IdentityMapper.DEFAULT_TENANT_ID, "oidc:corp-sso", extId)
                        .ifPresent(u -> redis.userIds.add(u.id().toString()));
                jdbcTemplate.update("DELETE FROM sys_users WHERE id_source = 'oidc:corp-sso' AND external_id = ?", extId);
            }
        } finally {
            redis.userIds.addAll(touchedRedisUserIds);
            try {
                redis.close();
            } finally {
                SecurityContextHolder.clearContext();
            }
        }
    }

    record UserStateSnapshot(
            String externalId,
            boolean existed,
            UUID userId,
            boolean isActive,
            String realName,
            String prevRevokedVal,
            long prevRevokedTtlSec,
            String prevPermChangedVal,
            long prevPermChangedTtlSec
    ) {}

    UserStateSnapshot snapshotUserState(String externalId) {
        Optional<UserRecord> existing = identityMapper.findUser(IdentityMapper.DEFAULT_TENANT_ID, "oidc:corp-sso", externalId);
        if (existing.isEmpty()) {
            return new UserStateSnapshot(externalId, false, null, true, null, null, -1L, null, -1L);
        }
        UserRecord rec = existing.get();
        String uid = rec.id().toString();
        String revKey = RedisRevocationSync.REVOKED_KEY_PREFIX + uid;
        String permKey = RedisRevocationSync.PERM_CHANGED_KEY_PREFIX + uid;
        String revVal = stringRedisTemplate.opsForValue().get(revKey);
        Long revTtl = revVal != null ? stringRedisTemplate.getExpire(revKey, TimeUnit.SECONDS) : -1L;
        String permVal = stringRedisTemplate.opsForValue().get(permKey);
        Long permTtl = permVal != null ? stringRedisTemplate.getExpire(permKey, TimeUnit.SECONDS) : -1L;
        return new UserStateSnapshot(
                externalId,
                true,
                rec.id(),
                rec.isActive(),
                rec.realName(),
                revVal,
                revTtl != null ? revTtl : -1L,
                permVal,
                permTtl != null ? permTtl : -1L
        );
    }

    void restoreUserState(UserStateSnapshot snap) {
        if (snap == null) {
            return;
        }
        if (!snap.existed()) {
            identityMapper.findUser(IdentityMapper.DEFAULT_TENANT_ID, "oidc:corp-sso", snap.externalId()).ifPresent(u -> {
                stringRedisTemplate.delete(RedisRevocationSync.REVOKED_KEY_PREFIX + u.id());
                stringRedisTemplate.delete(RedisRevocationSync.PERM_CHANGED_KEY_PREFIX + u.id());
                jdbcTemplate.update("DELETE FROM sys_users WHERE id = ?", u.id());
            });
            return;
        }
        jdbcTemplate.update(
                "UPDATE sys_users SET is_active = ?, real_name = ? WHERE id = ?",
                snap.isActive(),
                snap.realName(),
                snap.userId()
        );
        String uid = snap.userId().toString();
        String revKey = RedisRevocationSync.REVOKED_KEY_PREFIX + uid;
        String permKey = RedisRevocationSync.PERM_CHANGED_KEY_PREFIX + uid;
        if (snap.prevRevokedVal() != null) {
            long ttl = snap.prevRevokedTtlSec() > 0 ? snap.prevRevokedTtlSec() : 86400L;
            stringRedisTemplate.opsForValue().set(revKey, snap.prevRevokedVal(), ttl, TimeUnit.SECONDS);
        } else {
            stringRedisTemplate.delete(revKey);
        }
        if (snap.prevPermChangedVal() != null) {
            long ttl = snap.prevPermChangedTtlSec() > 0 ? snap.prevPermChangedTtlSec() : 86400L;
            stringRedisTemplate.opsForValue().set(permKey, snap.prevPermChangedVal(), ttl, TimeUnit.SECONDS);
        } else {
            stringRedisTemplate.delete(permKey);
        }
    }


    record RealKeycloakSession(
            OidcUser oidcUser,
            OAuth2AuthenticationToken authToken,
            MockHttpServletRequest request,
            MockHttpServletResponse response,
            String accessToken,
            String refreshToken
    ) {}

    /**
     * 执行真实 Keycloak OIDC Authorization Code + PKCE (S256) 登录，换取真实 IdToken / AccessToken / RefreshToken。
     */
    RealKeycloakSession loginViaRealKeycloakPkce(String username, String password) throws Exception {
        byte[] verifierBytes = new byte[36];
        new SecureRandom().nextBytes(verifierBytes);
        String codeVerifier = Base64.getUrlEncoder().withoutPadding().encodeToString(verifierBytes);
        String codeChallenge = Base64.getUrlEncoder().withoutPadding().encodeToString(
                MessageDigest.getInstance("SHA-256").digest(codeVerifier.getBytes(StandardCharsets.US_ASCII))
        );
        String state = UUID.randomUUID().toString();

        HttpClient http = HttpClient.newBuilder()
                .followRedirects(HttpClient.Redirect.NEVER)
                .build();

        String authUrl = KEYCLOAK_BASE + "/auth"
                + "?client_id=" + urlEncode(CLIENT_ID)
                + "&response_type=code"
                + "&scope=" + urlEncode("openid profile email")
                + "&redirect_uri=" + urlEncode(REDIRECT_URI)
                + "&state=" + urlEncode(state)
                + "&code_challenge=" + urlEncode(codeChallenge)
                + "&code_challenge_method=S256";

        HttpResponse<String> loginPageResp = http.send(
                HttpRequest.newBuilder(URI.create(authUrl)).GET().build(),
                HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8)
        );
        assertEquals(200, loginPageResp.statusCode(), "Keycloak 授权页返回非 200");

        List<String> setCookies = loginPageResp.headers().allValues("set-cookie");
        String cookieHeader = setCookies.stream()
                .map(c -> c.split(";", 2)[0])
                .reduce((a, b) -> a + "; " + b)
                .orElse("");

        Matcher actionMatcher = Pattern.compile("action=\"([^\"]+)\"").matcher(loginPageResp.body());
        assertTrue(actionMatcher.find(), "未在 Keycloak 登录页找到表单提交地址");
        String formAction = actionMatcher.group(1).replace("&amp;", "&");

        String formBody = "username=" + urlEncode(username)
                + "&password=" + urlEncode(password)
                + "&credentialId=";
        HttpResponse<String> submitResp = http.send(
                HttpRequest.newBuilder(URI.create(formAction))
                        .header("Content-Type", "application/x-www-form-urlencoded")
                        .header("Cookie", cookieHeader)
                        .POST(HttpRequest.BodyPublishers.ofString(formBody, StandardCharsets.UTF_8))
                        .build(),
                HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8)
        );
        assertEquals(302, submitResp.statusCode(), "Keycloak 账号密码认证未返回 302 授权码重定向");
        String redirectLocation = submitResp.headers().firstValue("location").orElseThrow();
        String authCode = extractQueryParam(URI.create(redirectLocation).getRawQuery(), "code");
        assertNotNull(authCode, "重定向 URL 缺少授权码 code");

        String basicAuth = Base64.getEncoder().encodeToString(
                (CLIENT_ID + ":" + CLIENT_SECRET).getBytes(StandardCharsets.UTF_8)
        );
        String tokenForm = "grant_type=authorization_code"
                + "&code=" + urlEncode(authCode)
                + "&redirect_uri=" + urlEncode(REDIRECT_URI)
                + "&code_verifier=" + urlEncode(codeVerifier);
        HttpResponse<String> tokenResp = http.send(
                HttpRequest.newBuilder(URI.create(KEYCLOAK_BASE + "/token"))
                        .header("Content-Type", "application/x-www-form-urlencoded")
                        .header("Authorization", "Basic " + basicAuth)
                        .POST(HttpRequest.BodyPublishers.ofString(tokenForm, StandardCharsets.UTF_8))
                        .build(),
                HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8)
        );
        assertEquals(200, tokenResp.statusCode(), "Keycloak PKCE 换票失败: " + tokenResp.body());

        ObjectMapper om = new ObjectMapper();
        JsonNode tokJson = om.readTree(tokenResp.body());
        String accessTokenVal = tokJson.get("access_token").asText();
        String refreshTokenVal = tokJson.get("refresh_token").asText();
        String idTokenVal = tokJson.get("id_token").asText();
        long expiresIn = tokJson.get("expires_in").asLong(1800L);

        NimbusJwtDecoder idTokenDecoder = NimbusJwtDecoder.withJwkSetUri(KEYCLOAK_BASE + "/certs").build();
        Jwt decodedIdToken = idTokenDecoder.decode(idTokenVal);
        OidcIdToken oidcIdToken = new OidcIdToken(
                idTokenVal,
                decodedIdToken.getIssuedAt(),
                decodedIdToken.getExpiresAt(),
                decodedIdToken.getClaims()
        );
        OidcUser initialOidcUser = new DefaultOidcUser(
                List.of(new SimpleGrantedAuthority("ROLE_USER")),
                oidcIdToken,
                "preferred_username"
        );
        OAuth2AuthenticationToken authToken = new OAuth2AuthenticationToken(
                initialOidcUser,
                initialOidcUser.getAuthorities(),
                "corp-sso"
        );

        Instant now = Instant.now();
        OAuth2AccessToken accessToken = new OAuth2AccessToken(
                OAuth2AccessToken.TokenType.BEARER,
                accessTokenVal,
                now,
                now.plusSeconds(expiresIn),
                Set.of("openid", "profile", "email")
        );
        OAuth2RefreshToken refreshToken = new OAuth2RefreshToken(refreshTokenVal, now);
        OAuth2AuthorizedClient authorizedClient = new OAuth2AuthorizedClient(
                corpSsoRegistration,
                authToken.getName(),
                accessToken,
                refreshToken
        );

        MockHttpServletRequest req = new MockHttpServletRequest();
        req.getSession(true);
        MockHttpServletResponse resp = new MockHttpServletResponse();
        authorizedClientRepository.saveAuthorizedClient(authorizedClient, authToken, req, resp);

        return new RealKeycloakSession(initialOidcUser, authToken, req, resp, accessTokenVal, refreshTokenVal);
    }

    static void revokeTokenInRealKeycloak(String token, String tokenTypeHint) throws Exception {
        HttpClient http = HttpClient.newHttpClient();
        String basicAuth = Base64.getEncoder().encodeToString(
                (CLIENT_ID + ":" + CLIENT_SECRET).getBytes(StandardCharsets.UTF_8)
        );
        String body = "token=" + urlEncode(token) + "&token_type_hint=" + urlEncode(tokenTypeHint);
        HttpResponse<String> resp = http.send(
                HttpRequest.newBuilder(URI.create(KEYCLOAK_BASE + "/revoke"))
                        .header("Content-Type", "application/x-www-form-urlencoded")
                        .header("Authorization", "Basic " + basicAuth)
                        .POST(HttpRequest.BodyPublishers.ofString(body, StandardCharsets.UTF_8))
                        .build(),
                HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8)
        );
        assertEquals(200, resp.statusCode(), "调用真实 Keycloak OIDC 吊销端点失败: " + resp.body());
    }

    private static String urlEncode(String val) {
        return URLEncoder.encode(val, StandardCharsets.UTF_8);
    }

    private static String extractQueryParam(String rawQuery, String key) {
        if (rawQuery == null) {
            return null;
        }
        for (String pair : rawQuery.split("&")) {
            int idx = pair.indexOf('=');
            if (idx > 0 && pair.substring(0, idx).equals(key)) {
                return java.net.URLDecoder.decode(pair.substring(idx + 1), StandardCharsets.UTF_8);
            }
        }
        return null;
    }


}
