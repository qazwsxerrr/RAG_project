package com.enterprise.rag.admin.web;

import com.enterprise.rag.admin.identity.PermissionResolver;
import com.enterprise.rag.admin.identity.PermissionResolver.ResolvedPermissions;
import com.enterprise.rag.admin.identity.UserMappingService;
import com.enterprise.rag.admin.identity.UserMappingService.MappedIdentity;
import com.nimbusds.jose.jwk.JWKSet;
import com.nimbusds.jose.jwk.RSAKey;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.core.env.Environment;
import org.springframework.http.HttpStatus;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.security.oauth2.core.oidc.user.OidcUser;
import org.springframework.security.oauth2.jwt.JwsHeader;
import org.springframework.security.oauth2.jwt.JwtClaimsSet;
import org.springframework.security.oauth2.jwt.JwtEncoder;
import org.springframework.security.oauth2.jwt.JwtEncoderParameters;
import org.springframework.web.bind.annotation.*;
import org.springframework.web.server.ResponseStatusException;

import java.time.Instant;
import java.time.temporal.ChronoUnit;
import java.util.*;

@RestController
public class AuthController {

    private static final Logger log = LoggerFactory.getLogger(AuthController.class);
    private static final Set<String> ALLOWED_PROVIDERS = Set.of("siliconflow", "kedaya");
    private static final Set<String> ALLOWED_SCOPES = Set.of("model:llm", "model:embedding", "model:rerank", "model:vlm");

    private final UserMappingService userMappingService;
    private final PermissionResolver permissionResolver;
    private final JwtEncoder jwtEncoder;
    private final RSAKey rsaKey;
    private final Environment env;

    @Value("${rag.jwt.issuer:rag-admin}")
    private String jwtIssuer;

    @Value("${rag.jwt.audience:rag-engine}")
    private String jwtAudience;

    @Value("${rag.jwt.ttl-minutes:30}")
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
            Environment env
    ) {
        this.userMappingService = userMappingService;
        this.permissionResolver = permissionResolver;
        this.jwtEncoder = jwtEncoder;
        this.rsaKey = rsaKey;
        this.env = env;
    }

    /**
     * Phase-6 §2.1 & §3.1: JWKS 公钥端点
     */
    @GetMapping("/.well-known/jwks.json")
    public Map<String, Object> getJwks() {
        return new JWKSet(rsaKey.toPublicJWK()).toJSONObject();
    }

    /**
     * Phase-6 §2.3 & §3.1: 会话换 30min 业务 JWT (含 JIT 建档与权限展开)
     */
    @PostMapping("/api/admin/auth/token")
    public Map<String, Object> exchangeToken(@AuthenticationPrincipal OidcUser oidcUser) {
        MappedIdentity mapped = userMappingService.mapAndProvision(oidcUser);
        ResolvedPermissions perms = permissionResolver.resolve(mapped.roleCodes());

        Instant now = Instant.now();
        Instant exp = now.plus(jwtTtlMinutes, ChronoUnit.MINUTES);

        JwtClaimsSet claims = JwtClaimsSet.builder()
                .issuer(jwtIssuer)
                .audience(List.of(jwtAudience))
                .subject(mapped.user().id().toString())
                .claim("username", mapped.user().realName())
                .claim("tenant_id", mapped.user().tenantId())
                .claim("roles", mapped.roleCodes())
                .claim("perms", new ArrayList<>(perms.permissions()))
                .claim("dept_path", mapped.user().deptPath())
                .claim("data_scope", perms.dataScope())
                .claim("is_to_c", false)
                .claim("cv", 1)
                .claim("typ", "access")
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
    public Map<String, Object> getMyProfile(@AuthenticationPrincipal OidcUser oidcUser) {
        MappedIdentity mapped = userMappingService.mapAndProvision(oidcUser);
        ResolvedPermissions perms = permissionResolver.resolve(mapped.roleCodes());

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
        if (!ALLOWED_PROVIDERS.contains(provider)) {
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
