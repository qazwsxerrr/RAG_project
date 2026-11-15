package com.enterprise.rag.admin.web;

import com.enterprise.rag.admin.identity.PermissionResolver;
import com.enterprise.rag.admin.identity.PermissionResolver.ResolvedPermissions;
import com.enterprise.rag.admin.identity.RedisRevocationSync;
import com.enterprise.rag.admin.identity.UserMappingService;
import com.enterprise.rag.admin.identity.UserMappingService.MappedIdentity;
import com.nimbusds.jose.jwk.JWKSet;
import com.nimbusds.jose.jwk.RSAKey;
import jakarta.servlet.http.Cookie;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import jakarta.servlet.http.HttpSession;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.core.env.Environment;
import org.springframework.http.HttpStatus;
import org.springframework.security.core.Authentication;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.security.core.context.SecurityContext;
import org.springframework.security.core.context.SecurityContextHolder;
import org.springframework.security.oauth2.client.OAuth2AuthorizeRequest;
import org.springframework.security.oauth2.client.OAuth2AuthorizedClient;
import org.springframework.security.oauth2.client.OAuth2AuthorizedClientManager;
import org.springframework.security.oauth2.client.authentication.OAuth2AuthenticationToken;
import org.springframework.security.oauth2.client.oidc.userinfo.OidcUserRequest;
import org.springframework.security.oauth2.client.oidc.userinfo.OidcUserService;
import org.springframework.security.oauth2.core.OAuth2AuthenticationException;
import org.springframework.security.oauth2.core.OAuth2AuthorizationException;
import org.springframework.security.oauth2.core.oidc.user.OidcUser;
import org.springframework.security.oauth2.jwt.JwsHeader;
import org.springframework.security.oauth2.jwt.JwtClaimsSet;
import org.springframework.security.oauth2.jwt.JwtEncoder;
import org.springframework.security.oauth2.jwt.JwtEncoderParameters;
import org.springframework.security.web.context.HttpSessionSecurityContextRepository;
import org.springframework.web.bind.annotation.*;
import org.springframework.web.server.ResponseStatusException;

import java.io.IOException;
import java.time.Instant;
import java.time.temporal.ChronoUnit;
import java.util.*;

@RestController
public class AuthController {

    private static final Logger log = LoggerFactory.getLogger(AuthController.class);
    private static final Set<String> ALLOWED_SCOPES = Set.of("model:llm", "model:embedding", "model:rerank", "model:vlm");

    private final UserMappingService userMappingService;
    private final PermissionResolver permissionResolver;
    private final JwtEncoder jwtEncoder;
    private final RSAKey rsaKey;
    private final JWKSet publicJwkSet;
    private final Environment env;
    private final OAuth2AuthorizedClientManager authorizedClientManager;
    private final OidcUserService oidcUserService;
    private final RedisRevocationSync redisRevocationSync;

    @Value("${rag.model-vault.allowed-providers:kedaya,siliconflow,deepseek,dashscope,zhipu,openai,volces}")
    private String allowedProvidersConfig;

    @Value("${rag.jwt.issuer:rag-admin}")
    private String jwtIssuer;

    @Value("${rag.jwt.audience:rag-engine}")
    private String jwtAudience;

    @Value("${rag.jwt.ttl-minutes:10}")
    private int jwtTtlMinutes;

    @Value("${rag.internal.service-id:rag-engine}")
    private String internalServiceId;

    @Value("${rag.internal.service-token:rag-engine-service-secret}")
    private String internalServiceToken;

    public AuthController(
            UserMappingService userMappingService,
            PermissionResolver permissionResolver,
            JwtEncoder jwtEncoder,
            RSAKey rsaKey,
            JWKSet publicJwkSet,
            Environment env,
            OAuth2AuthorizedClientManager authorizedClientManager,
            OidcUserService oidcUserService,
            RedisRevocationSync redisRevocationSync
    ) {
        this.userMappingService = Objects.requireNonNull(userMappingService, "userMappingService 不能为空");
        this.permissionResolver = Objects.requireNonNull(permissionResolver, "permissionResolver 不能为空");
        this.jwtEncoder = Objects.requireNonNull(jwtEncoder, "jwtEncoder 不能为空");
        this.rsaKey = Objects.requireNonNull(rsaKey, "rsaKey 不能为空");
        this.publicJwkSet = Objects.requireNonNull(publicJwkSet, "publicJwkSet 不能为空");
        this.env = Objects.requireNonNull(env, "env 不能为空");
        this.authorizedClientManager = Objects.requireNonNull(authorizedClientManager, "authorizedClientManager 不能为空");
        this.oidcUserService = Objects.requireNonNull(oidcUserService, "oidcUserService 不能为空");
        this.redisRevocationSync = Objects.requireNonNull(redisRevocationSync, "redisRevocationSync 不能为空");
    }

    /**
     * Phase-6 §2.1 & §3.1: JWKS 公钥端点（支持平滑密钥轮换期间同时暴露新旧公钥）
     */
    @GetMapping("/.well-known/jwks.json")
    public Map<String, Object> getJwks() {
        return publicJwkSet.toJSONObject();
    }

    private record RefreshedOidcSession(
            OidcUser oidcUser,
            boolean deptChangedInIdp,
            boolean rolesChangedInIdp
    ) {}

    /**
     * 标准 OIDC 会话实时身份刷新：
     * 严格 Fail-Fast，移除一切静默跳过旧身份回退分支。
     * 若员工在 IdP 侧已被禁用或会话吊销，同步写入真实 Redis 秒级吊销其存量业务 JWT，并立即销毁本地 Session 返回 401。
     */
    private RefreshedOidcSession refreshOidcIdentityStrict(
            OidcUser currentOidcUser,
            Authentication authentication,
            HttpServletRequest request,
            HttpServletResponse response
    ) {
        if (currentOidcUser == null
                || !(authentication instanceof OAuth2AuthenticationToken oauthToken)
                || request == null
                || response == null) {
            invalidateLocalSession(request, response);
            throw new ResponseStatusException(HttpStatus.UNAUTHORIZED, "【鉴权失败】缺少 OIDC 会话身份");
        }

        String previousIdpDept = UserMappingService.extractDeptPath(currentOidcUser);
        Set<String> previousRoles = new LinkedHashSet<>(UserMappingService.extractRoleCodes(currentOidcUser));
        try {
            OAuth2AuthorizeRequest authorizeRequest = OAuth2AuthorizeRequest
                    .withClientRegistrationId(oauthToken.getAuthorizedClientRegistrationId())
                    .principal(oauthToken)
                    .attribute(HttpServletRequest.class.getName(), request)
                    .attribute(HttpServletResponse.class.getName(), response)
                    .build();

            OAuth2AuthorizedClient authorizedClient = authorizedClientManager.authorize(authorizeRequest);
            if (authorizedClient == null || authorizedClient.getAccessToken() == null) {
                markExistingUserPermissionChanged(currentOidcUser.getSubject());
                invalidateLocalSession(request, response);
                throw new ResponseStatusException(HttpStatus.UNAUTHORIZED, "【会话失效】企业 IdP 授权已过期或被撤销，请重新登录");
            }

            OidcUser refreshedUser = oidcUserService.loadUser(new OidcUserRequest(
                    authorizedClient.getClientRegistration(),
                    authorizedClient.getAccessToken(),
                    currentOidcUser.getIdToken()
            ));

            OAuth2AuthenticationToken updatedAuth = new OAuth2AuthenticationToken(
                    refreshedUser,
                    refreshedUser.getAuthorities(),
                    oauthToken.getAuthorizedClientRegistrationId()
            );
            SecurityContext context = SecurityContextHolder.createEmptyContext();
            context.setAuthentication(updatedAuth);
            SecurityContextHolder.setContext(context);
            HttpSession session = request.getSession(false);
            if (session != null) {
                session.setAttribute(HttpSessionSecurityContextRepository.SPRING_SECURITY_CONTEXT_KEY, context);
            }

            String latestIdpDept = UserMappingService.extractDeptPath(refreshedUser);
            boolean deptChanged = !Objects.equals(previousIdpDept, latestIdpDept);
            boolean rolesChanged = !Objects.equals(previousRoles, new LinkedHashSet<>(UserMappingService.extractRoleCodes(refreshedUser)));
            return new RefreshedOidcSession(refreshedUser, deptChanged, rolesChanged);
        } catch (ResponseStatusException e) {
            throw e;
        } catch (OAuth2AuthorizationException | OAuth2AuthenticationException ex) {
            String errorCode = (ex instanceof OAuth2AuthorizationException authzEx)
                    ? authzEx.getError().getErrorCode()
                    : ((OAuth2AuthenticationException) ex).getError().getErrorCode();
            if (isTransientIdpError(ex, errorCode)) {
                log.warn("[OIDC_SESSION_REFRESH] IdP 临时通信或服务异常 (code={})，Fail-Fast 拒绝本次换票但不写入全账号封禁: {}",
                        errorCode, ex.getMessage());
                throw new ResponseStatusException(HttpStatus.SERVICE_UNAVAILABLE, "【鉴权中台暂时不可用】企业 IdP 通信异常，请稍后重试", ex);
            }
            log.warn("[OIDC_SESSION_REFRESH] IdP 明确拒绝或撤销当前会话 (code={})，主动销毁本地会话并淘汰当前用户既有权限票据: {}",
                    errorCode, ex.getMessage());
            markExistingUserPermissionChanged(currentOidcUser.getSubject());
            invalidateLocalSession(request, response);
            throw new ResponseStatusException(HttpStatus.UNAUTHORIZED, "【会话失效】企业身份授权已过期或被撤销，请重新登录", ex);
        } catch (Exception ex) {
            if (isTransientIdpError(ex, null)) {
                log.warn("[OIDC_SESSION_REFRESH] IdP 临时网络或服务端异常，Fail-Fast 拒绝本次换票但不封禁账号: {}", ex.getMessage());
                throw new ResponseStatusException(HttpStatus.SERVICE_UNAVAILABLE, "【鉴权中台暂时不可用】企业 IdP 通信异常，请稍后重试", ex);
            }
            log.warn("[OIDC_SESSION_REFRESH] IdP 实时刷新身份失败，主动销毁本地会话: {}", ex.getMessage());
            markExistingUserPermissionChanged(currentOidcUser.getSubject());
            invalidateLocalSession(request, response);
            throw new ResponseStatusException(HttpStatus.UNAUTHORIZED, "【会话失效】企业身份状态已变更或停用，请重新登录", ex);
        }
    }

    private void markExistingUserPermissionChanged(String externalId) {
        if (externalId == null || externalId.isBlank()) {
            return;
        }
        userMappingService.findExistingUserId(externalId)
                .ifPresent(uid -> redisRevocationSync.markPermissionChanged(uid, System.currentTimeMillis()));
    }

    private static boolean isTransientIdpError(Throwable ex, String errorCode) {
        if ("server_error".equalsIgnoreCase(errorCode) || "temporarily_unavailable".equalsIgnoreCase(errorCode)) {
            return true;
        }
        Throwable cursor = ex;
        int depth = 0;
        while (cursor != null && depth < 10) {
            if (cursor instanceof IOException) {
                return true;
            }
            String msg = cursor.getMessage();
            if (msg != null) {
                String lower = msg.toLowerCase(Locale.ROOT);
                if (lower.contains("connection refused")
                        || lower.contains("timed out")
                        || lower.contains("timeout")
                        || lower.contains("502")
                        || lower.contains("503")
                        || lower.contains("504")) {
                    return true;
                }
            }
            cursor = cursor.getCause();
            depth++;
        }
        return false;
    }

    private void invalidateLocalSession(HttpServletRequest request, HttpServletResponse response) {
        if (request != null) {
            HttpSession session = request.getSession(false);
            if (session != null) {
                session.invalidate();
            }
        }
        SecurityContextHolder.clearContext();
        if (response != null) {
            Cookie cookie = new Cookie("JSESSIONID", null);
            cookie.setPath("/");
            cookie.setMaxAge(0);
            cookie.setHttpOnly(true);
            response.addCookie(cookie);
        }
    }

    /**
     * Phase-6 §2.3 & §3.1: 会话换短期业务 JWT (默认 10min，含标准 OIDC 实时身份刷新与权限展开)
     */
    @PostMapping("/api/admin/auth/token")
    public Map<String, Object> exchangeToken(
            @AuthenticationPrincipal OidcUser oidcUser,
            Authentication authentication,
            HttpServletRequest request,
            HttpServletResponse response
    ) {
        RefreshedOidcSession refreshed = refreshOidcIdentityStrict(oidcUser, authentication, request, response);
        MappedIdentity mapped = userMappingService.mapAndProvision(refreshed.oidcUser(), refreshed.deptChangedInIdp());
        ResolvedPermissions perms = permissionResolver.resolve(mapped.roleCodes());

        Instant now = Instant.now();
        String userIdStr = mapped.user().id().toString();
        if (refreshed.deptChangedInIdp() || refreshed.rolesChangedInIdp()) {
            redisRevocationSync.markPermissionChanged(userIdStr, System.currentTimeMillis());
        }
        long lastPermChangedMs = redisRevocationSync.getPermissionChangedAtMs(userIdStr);
        long issuedAtMs = Math.max(System.currentTimeMillis(), lastPermChangedMs + 1);
        Instant exp = now.plus(jwtTtlMinutes, ChronoUnit.MINUTES);

        JwtClaimsSet claims = JwtClaimsSet.builder()
                .issuer(jwtIssuer)
                .audience(List.of(jwtAudience))
                .subject(userIdStr)
                .claim("username", mapped.user().realName())
                .claim("tenant_id", mapped.user().tenantId())
                .claim("roles", mapped.roleCodes())
                .claim("perms", new ArrayList<>(perms.permissions()))
                .claim("dept_path", mapped.user().deptPath())
                .claim("data_scope", perms.dataScope())
                .claim("is_to_c", false)
                .claim("cv", 1)
                .claim("typ", "access")
                .claim("iat_ms", issuedAtMs)
                .issuedAt(now)
                .expiresAt(exp)
                .build();

        JwsHeader header = JwsHeader.with(org.springframework.security.oauth2.jose.jws.SignatureAlgorithm.RS256)
                .keyId(rsaKey.getKeyID())
                .build();

        String token = jwtEncoder.encode(JwtEncoderParameters.from(header, claims)).getTokenValue();

        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("access_token", token);
        resp.put("token_type", "bearer");
        resp.put("expires_in", jwtTtlMinutes * 60L);
        return resp;
    }

    /**
     * Phase-6 §2.5: 回显当前登录员工基本信息与权限范围
     */
    @GetMapping("/api/admin/users/me")
    public Map<String, Object> getMyProfile(
            @AuthenticationPrincipal OidcUser oidcUser,
            Authentication authentication,
            HttpServletRequest request,
            HttpServletResponse response
    ) {
        RefreshedOidcSession refreshed = refreshOidcIdentityStrict(oidcUser, authentication, request, response);
        MappedIdentity mapped = userMappingService.mapAndProvision(refreshed.oidcUser(), refreshed.deptChangedInIdp());
        ResolvedPermissions perms = permissionResolver.resolve(mapped.roleCodes());
        if (refreshed.deptChangedInIdp() || refreshed.rolesChangedInIdp()) {
            redisRevocationSync.markPermissionChanged(mapped.user().id().toString(), System.currentTimeMillis());
        }

        Map<String, Object> me = new LinkedHashMap<>();
        me.put("user_id", mapped.user().id().toString());
        me.put("tenant_id", mapped.user().tenantId());
        me.put("username", mapped.user().realName());
        me.put("dept_path", mapped.user().deptPath());
        me.put("data_scope", perms.dataScope());
        me.put("roles", mapped.roleCodes());
        me.put("perms", perms.permissions());
        me.put("is_to_c", false);
        me.put("cv", 1);
        return me;
    }

    /**
     * Phase-6 §2.5: 会话登出端点（清理 Session 与 JSESSIONID Cookie）
     */
    @PostMapping("/api/admin/auth/logout")
    public Map<String, Object> logout(HttpServletRequest request, HttpServletResponse response) {
        invalidateLocalSession(request, response);
        return Map.of("code", 200, "message", "Logged out successfully");
    }

    public record ModelTokenRequest(String provider, List<String> scopes) {}

    /**
     * Phase-6 §7.3 & §7.4: 内部服务短期凭据租约发放端点 (仅允许容器网络内部访问)
     * 每次取用均记录结构化审计日志 (service_id + provider + scopes + masked + expires_at)，严禁明文 Key 落日志。
     */
    @PostMapping("/api/admin/internal/model-token")
    public Map<String, Object> issueModelToken(
            @RequestHeader(value = "X-Service-Id", required = false) String serviceId,
            @RequestHeader(value = "X-Service-Token", required = false) String serviceToken,
            @RequestBody ModelTokenRequest req
    ) {
        if (serviceId == null || serviceToken == null
                || !internalServiceId.equals(serviceId)
                || !internalServiceToken.equals(serviceToken)) {
            log.warn("[MODEL_KEY_AUDIT] status=DENIED reason=INVALID_SERVICE_TOKEN service_id={}", serviceId);
            throw new ResponseStatusException(HttpStatus.FORBIDDEN, "【安全拒绝】服务身份校验失败 (Invalid Service Token)");
        }

        String provider = req != null && req.provider() != null ? req.provider().toLowerCase().trim() : "";
        Set<String> allowedProviders = Arrays.stream(
                (allowedProvidersConfig != null ? allowedProvidersConfig : "kedaya,siliconflow,deepseek,dashscope,zhipu,openai,volces").split(","))
                .map(String::trim)
                .map(String::toLowerCase)
                .filter(s -> !s.isEmpty())
                .collect(java.util.stream.Collectors.toSet());
        if (!allowedProviders.contains(provider)) {
            log.warn("[MODEL_KEY_AUDIT] status=DENIED reason=UNSUPPORTED_PROVIDER service_id={} provider={}", serviceId, provider);
            throw new ResponseStatusException(HttpStatus.FORBIDDEN, "【安全拒绝】未知或未授权的模型供应商: " + provider);
        }

        if (req.scopes() == null || req.scopes().isEmpty()) {
            log.warn("[MODEL_KEY_AUDIT] status=DENIED reason=EMPTY_SCOPES service_id={} provider={}", serviceId, provider);
            throw new ResponseStatusException(HttpStatus.FORBIDDEN, "【安全拒绝】未声明模型调用 scopes");
        }

        for (String scope : req.scopes()) {
            if (!ALLOWED_SCOPES.contains(scope)) {
                log.warn("[MODEL_KEY_AUDIT] status=DENIED reason=UNAUTHORIZED_SCOPE service_id={} provider={} scope={}", serviceId, provider, scope);
                throw new ResponseStatusException(HttpStatus.FORBIDDEN, "【安全拒绝】未授权的调用 scope: " + scope);
            }
        }

        String baseUrl = env.getProperty("rag.model-vault." + provider + ".base-url");
        String apiKey = env.getProperty("rag.model-vault." + provider + ".api-key");

        if (baseUrl == null || baseUrl.isBlank() || apiKey == null || apiKey.isBlank()) {
            log.error("[MODEL_KEY_AUDIT] status=ERROR reason=VAULT_KEY_MISSING service_id={} provider={}", serviceId, provider);
            throw new ResponseStatusException(HttpStatus.INTERNAL_SERVER_ERROR, "【鉴权中台错误】Vault 中未配置供应商 [" + provider + "] 密钥");
        }

        long nowSec = Instant.now().getEpochSecond();
        long expSec = nowSec + 600; // 10分钟租约

        String masked = apiKey.length() > 8
                ? apiKey.substring(0, 4) + "****" + apiKey.substring(apiKey.length() - 4)
                : "sk-****";

        log.info(
                "[MODEL_KEY_AUDIT] status=ISSUED service_id={} provider={} scopes={} masked={} issued_at={} expires_at={}",
                serviceId, provider, req.scopes(), masked, nowSec, expSec
        );

        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("provider", provider);
        resp.put("base_url", baseUrl.trim());
        resp.put("api_key", apiKey.trim());
        resp.put("expires_at", expSec);
        resp.put("masked", masked);
        return resp;
    }
}
