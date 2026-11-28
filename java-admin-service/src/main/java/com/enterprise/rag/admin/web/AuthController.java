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
import org.springframework.web.util.UriComponentsBuilder;

import java.io.IOException;
import java.time.Instant;
import java.time.temporal.ChronoUnit;
import java.util.*;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.locks.ReentrantLock;

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
    private final ConcurrentHashMap<String, ReentrantLock> sessionRefreshLocks = new ConcurrentHashMap<>();

    @Value("${rag.oidc.end-session-uri:http://localhost:8180/realms/enterprise-rag/protocol/openid-connect/logout}")
    private String oidcEndSessionUri;

    @Value("${spring.security.oauth2.client.registration.corp-sso.client-id:rag-admin-service}")
    private String oidcClientId;

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

    @GetMapping("/.well-known/jwks.json")
    public Map<String, Object> getJwks() {
        return publicJwkSet.toJSONObject();
    }

    private record RefreshedOidcSession(
            OidcUser oidcUser,
            boolean deptsChangedInIdp,
            boolean rolesChangedInIdp
    ) {}

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

        HttpSession currentSession = request.getSession(false);
        String lockKey = currentSession != null ? currentSession.getId() : null;
        ReentrantLock sessionLock = lockKey != null
                ? sessionRefreshLocks.computeIfAbsent(lockKey, k -> new ReentrantLock())
                : null;
        if (sessionLock != null) {
            sessionLock.lock();
        }
        try {
            OidcUser effectiveOidcUser = currentOidcUser;
            OAuth2AuthenticationToken effectiveOauthToken = oauthToken;
            HttpSession activeSession = request.getSession(false);
            if (activeSession != null) {
                Object ctxObj = activeSession.getAttribute(HttpSessionSecurityContextRepository.SPRING_SECURITY_CONTEXT_KEY);
                if (ctxObj instanceof SecurityContext sc
                        && sc.getAuthentication() instanceof OAuth2AuthenticationToken latestToken
                        && latestToken.getPrincipal() instanceof OidcUser latestOidcUser) {
                    effectiveOauthToken = latestToken;
                    effectiveOidcUser = latestOidcUser;
                }
            }

            Set<String> previousIdpDepts = new LinkedHashSet<>(UserMappingService.extractDeptPaths(effectiveOidcUser));
            Set<String> previousRoles = new LinkedHashSet<>(UserMappingService.extractRoleCodes(effectiveOidcUser));
            try {
                OAuth2AuthorizeRequest authorizeRequest = OAuth2AuthorizeRequest
                        .withClientRegistrationId(effectiveOauthToken.getAuthorizedClientRegistrationId())
                        .principal(effectiveOauthToken)
                        .attribute(HttpServletRequest.class.getName(), request)
                        .attribute(HttpServletResponse.class.getName(), response)
                        .build();

                OAuth2AuthorizedClient authorizedClient = authorizedClientManager.authorize(authorizeRequest);
                if (authorizedClient == null || authorizedClient.getAccessToken() == null) {
                    markExistingUserPermissionChanged(UserMappingService.extractTenantId(effectiveOidcUser), effectiveOidcUser.getSubject());
                    invalidateLocalSession(request, response);
                    throw new ResponseStatusException(HttpStatus.UNAUTHORIZED, "【会话失效】企业 IdP 授权已过期或被撤销，请重新登录");
                }

                OidcUser refreshedUser = oidcUserService.loadUser(new OidcUserRequest(
                        authorizedClient.getClientRegistration(),
                        authorizedClient.getAccessToken(),
                        effectiveOidcUser.getIdToken()
                ));

                OAuth2AuthenticationToken updatedAuth = new OAuth2AuthenticationToken(
                        refreshedUser,
                        refreshedUser.getAuthorities(),
                        effectiveOauthToken.getAuthorizedClientRegistrationId()
                );
                SecurityContext context = SecurityContextHolder.createEmptyContext();
                context.setAuthentication(updatedAuth);
                SecurityContextHolder.setContext(context);
                HttpSession session = request.getSession(false);
                if (session != null) {
                    session.setAttribute(HttpSessionSecurityContextRepository.SPRING_SECURITY_CONTEXT_KEY, context);
                }

                Set<String> latestIdpDepts = new LinkedHashSet<>(UserMappingService.extractDeptPaths(refreshedUser));
                boolean deptsChanged = !Objects.equals(previousIdpDepts, latestIdpDepts);
                boolean rolesChanged = !Objects.equals(previousRoles, new LinkedHashSet<>(UserMappingService.extractRoleCodes(refreshedUser)));
                return new RefreshedOidcSession(refreshedUser, deptsChanged, rolesChanged);
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
                markExistingUserPermissionChanged(UserMappingService.extractTenantId(effectiveOidcUser), effectiveOidcUser.getSubject());
                invalidateLocalSession(request, response);
                throw new ResponseStatusException(HttpStatus.UNAUTHORIZED, "【会话失效】企业身份授权已过期或被撤销，请重新登录", ex);
            } catch (Exception ex) {
                if (isTransientIdpError(ex, null)) {
                    log.warn("[OIDC_SESSION_REFRESH] IdP 临时网络或服务端异常，Fail-Fast 拒绝本次换票但不封禁账号: {}", ex.getMessage());
                    throw new ResponseStatusException(HttpStatus.SERVICE_UNAVAILABLE, "【鉴权中台暂时不可用】企业 IdP 通信异常，请稍后重试", ex);
                }
                log.warn("[OIDC_SESSION_REFRESH] IdP 实时刷新身份失败，主动销毁本地会话: {}", ex.getMessage());
                markExistingUserPermissionChanged(UserMappingService.extractTenantId(effectiveOidcUser), effectiveOidcUser.getSubject());
                invalidateLocalSession(request, response);
                throw new ResponseStatusException(HttpStatus.UNAUTHORIZED, "【会话失效】企业身份状态已变更或停用，请重新登录", ex);
            }
        } finally {
            if (sessionLock != null) {
                sessionLock.unlock();
            }
        }
    }

    private void markExistingUserPermissionChanged(String tenantId, String externalId) {
        if (externalId == null || externalId.isBlank()) {
            return;
        }
        userMappingService.findExistingUserId(tenantId, externalId)
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
                sessionRefreshLocks.remove(session.getId());
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
     * 会话换短期 cv=1 业务 JWT（含多部门、按动作范围 scope_by_action、按动作部门集合 dept_paths_by_action、职级、有效密级、双版本号）
     */
    @PostMapping("/api/admin/auth/token")
    public Map<String, Object> exchangeToken(
            @AuthenticationPrincipal OidcUser oidcUser,
            Authentication authentication,
            HttpServletRequest request,
            HttpServletResponse response
    ) {
        RefreshedOidcSession refreshed = refreshOidcIdentityStrict(oidcUser, authentication, request, response);
        MappedIdentity mapped = userMappingService.mapAndProvision(refreshed.oidcUser(), refreshed.deptsChangedInIdp());

        Instant now = Instant.now();
        String userIdStr = mapped.user().id().toString();
        if (refreshed.deptsChangedInIdp() || refreshed.rolesChangedInIdp()
                || mapped.idpDepartmentsMutated() || mapped.positionMutated()) {
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
                .claim("perms", new ArrayList<>(mapped.permissions()))
                .claim("dept_path", mapped.primaryDeptPath())
                .claim("dept_paths", mapped.effectiveDeptPaths())
                .claim("dept_paths_by_action", mapped.deptPathsByAction())
                .claim("scope_by_action", mapped.scopeByAction())
                .claim("position_code", mapped.user().positionCode())
                .claim("clearance_level", mapped.effectiveClearance())
                .claim("authz_ver", mapped.user().authzVersion())
                .claim("policy_epoch", mapped.policyEpoch())
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
     * 回显当前 OIDC 登录员工完整多维权限画像 (cv=1)
     */
    @GetMapping("/api/admin/users/me")
    public Map<String, Object> getMyProfile(
            @AuthenticationPrincipal OidcUser oidcUser,
            Authentication authentication,
            HttpServletRequest request,
            HttpServletResponse response
    ) {
        RefreshedOidcSession refreshed = refreshOidcIdentityStrict(oidcUser, authentication, request, response);
        MappedIdentity mapped = userMappingService.mapAndProvision(refreshed.oidcUser(), refreshed.deptsChangedInIdp());
        if (refreshed.deptsChangedInIdp() || refreshed.rolesChangedInIdp()
                || mapped.idpDepartmentsMutated() || mapped.positionMutated()) {
            redisRevocationSync.markPermissionChanged(mapped.user().id().toString(), System.currentTimeMillis());
        }

        Map<String, Object> me = new LinkedHashMap<>();
        me.put("user_id", mapped.user().id().toString());
        me.put("tenant_id", mapped.user().tenantId());
        me.put("username", mapped.user().realName());
        me.put("dept_path", mapped.primaryDeptPath());
        me.put("dept_paths", mapped.effectiveDeptPaths());
        me.put("dept_paths_by_action", mapped.deptPathsByAction());
        me.put("scope_by_action", mapped.scopeByAction());
        me.put("position_code", mapped.user().positionCode());
        me.put("clearance_level", mapped.effectiveClearance());
        me.put("authz_ver", mapped.user().authzVersion());
        me.put("policy_epoch", mapped.policyEpoch());
        me.put("roles", mapped.roleCodes());
        me.put("perms", mapped.permissions());
        me.put("is_to_c", false);
        me.put("cv", 1);
        return me;
    }

    private static String resolveClientOrigin(HttpServletRequest request) {
        String origin = request != null ? request.getHeader("Origin") : null;
        if (origin != null && (origin.startsWith("http://") || origin.startsWith("https://"))) {
            return origin.endsWith("/") ? origin.substring(0, origin.length() - 1) : origin;
        }
        String proto = request != null ? request.getHeader("X-Forwarded-Proto") : null;
        String host = request != null ? request.getHeader("X-Forwarded-Host") : null;
        if (proto != null && !proto.isBlank() && host != null && !host.isBlank()) {
            return proto.split(",")[0].trim() + "://" + host.split(",")[0].trim();
        }
        if (request == null) {
            return "http://localhost:5173";
        }
        String scheme = request.getScheme();
        int port = request.getServerPort();
        boolean isDefault = ("http".equalsIgnoreCase(scheme) && port == 80)
                || ("https".equalsIgnoreCase(scheme) && port == 443);
        return scheme + "://" + request.getServerName() + (isDefault ? "" : ":" + port);
    }

    @PostMapping("/api/admin/auth/logout")
    public Map<String, Object> logout(
            @AuthenticationPrincipal OidcUser oidcUser,
            Authentication authentication,
            HttpServletRequest request,
            HttpServletResponse response
    ) {
        if (oidcEndSessionUri == null || oidcEndSessionUri.isBlank()) {
            throw new ResponseStatusException(HttpStatus.INTERNAL_SERVER_ERROR, "【鉴权配置错误】缺少 rag.oidc.end-session-uri");
        }
        OidcUser effectiveUser = oidcUser != null
                ? oidcUser
                : (authentication != null && authentication.getPrincipal() instanceof OidcUser u ? u : null);
        String idToken = (effectiveUser != null && effectiveUser.getIdToken() != null)
                ? effectiveUser.getIdToken().getTokenValue()
                : null;

        String postLogoutRedirectUri = resolveClientOrigin(request) + "/oauth2/authorization/corp-sso";
        UriComponentsBuilder builder = UriComponentsBuilder.fromUriString(oidcEndSessionUri.trim())
                .queryParam("post_logout_redirect_uri", postLogoutRedirectUri);
        if (idToken != null && !idToken.isBlank()) {
            builder.queryParam("id_token_hint", idToken);
        } else {
            builder.queryParam("client_id", oidcClientId);
        }
        String logoutUrl = builder.build().encode().toUriString();

        invalidateLocalSession(request, response);
        return Map.of("code", 200, "message", "Logged out successfully", "logout_url", logoutUrl);
    }

    public record ModelTokenRequest(String provider, List<String> scopes) {}

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
        long expSec = nowSec + 600;

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
