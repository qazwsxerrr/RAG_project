package com.enterprise.rag.admin.identity;

import com.enterprise.rag.admin.mapper.IdentityMapper;
import com.enterprise.rag.admin.mapper.IdentityMapper.UserDepartmentRecord;
import com.enterprise.rag.admin.mapper.IdentityMapper.UserRecord;
import org.springframework.http.HttpStatus;
import org.springframework.security.oauth2.core.oidc.OidcUserInfo;
import org.springframework.security.oauth2.core.oidc.user.OidcUser;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.web.server.ResponseStatusException;

import java.util.*;

/**
 * 外部企业 IdP/HR 身份同步服务：
 * 员工多部门、多角色与职级均以企业账号系统下发为准，自动对齐本地影子表 (sys_users / sys_user_departments)。
 */
@Service
public class UserMappingService {

    private final IdentityMapper identityMapper;
    private final RedisRevocationSync redisRevocationSync;

    public UserMappingService(IdentityMapper identityMapper, RedisRevocationSync redisRevocationSync) {
        this.identityMapper = Objects.requireNonNull(identityMapper, "identityMapper 不能为空");
        this.redisRevocationSync = Objects.requireNonNull(redisRevocationSync, "redisRevocationSync 不能为空");
    }

    public record MappedIdentity(
            UserRecord user,
            List<String> idpRoleCodes,
            List<String> roleCodes,
            String primaryDeptPath,
            List<String> effectiveDeptPaths,
            int effectiveClearance,
            long policyEpoch,
            boolean idpDepartmentsMutated,
            boolean positionMutated
    ) {}

    @Transactional
    public MappedIdentity mapAndProvision(OidcUser oidcUser, boolean syncIdpDepartments) {
        if (oidcUser == null) {
            throw new ResponseStatusException(HttpStatus.UNAUTHORIZED, "【鉴权失败】缺少 OIDC 会话身份");
        }
        String externalId = oidcUser.getSubject();
        if (externalId == null || externalId.isBlank()) {
            throw new ResponseStatusException(HttpStatus.UNAUTHORIZED, "【鉴权失败】OIDC id_token 缺少 sub Claim");
        }

        String tenantId = extractTenantId(oidcUser);
        String realName = firstNonBlank(
                oidcUser.getPreferredUsername(),
                oidcUser.getFullName(),
                oidcUser.getName(),
                "employee_" + externalId.substring(0, Math.min(8, externalId.length()))
        );

        List<String> idpDeptPaths = extractDeptPaths(oidcUser);
        boolean hasRootDeptMarker = idpDeptPaths.contains("/");
        List<String> concreteIdpDepts = idpDeptPaths.stream()
                .filter(p -> p != null && !p.isBlank() && !"/".equals(p))
                .toList();
        List<String> idpRoleCodes = extractRoleCodes(oidcUser);
        String optionalPositionCode = extractPositionCode(oidcUser);

        if (concreteIdpDepts.isEmpty() && !hasRootDeptMarker) {
            revokeExistingUserIfPresent(tenantId, externalId);
            throw new ResponseStatusException(HttpStatus.FORBIDDEN, "【拒绝建档】企业 IdP 未下发有效部门路径 (dept_paths/dept_path/groups)");
        }
        if (idpRoleCodes.isEmpty()) {
            revokeExistingUserIfPresent(tenantId, externalId);
            throw new ResponseStatusException(HttpStatus.FORBIDDEN, "【拒绝签发】企业 IdP 未分配任何有效角色");
        }

        String initialDisplayDept = !concreteIdpDepts.isEmpty() ? concreteIdpDepts.getFirst() : "";
        Optional<UserRecord> existingBefore = identityMapper.findUser(tenantId, "oidc:corp-sso", externalId);
        UserRecord user = identityMapper.upsertUser(
                tenantId, "oidc:corp-sso", externalId, realName, initialDisplayDept, optionalPositionCode, false
        );
        boolean positionMutated = existingBefore.isPresent()
                && !Objects.equals(existingBefore.get().positionCode(), user.positionCode());

        if (!user.isActive()) {
            redisRevocationSync.markUserRevoked(user.id().toString());
            throw new ResponseStatusException(HttpStatus.FORBIDDEN, "【拒绝签发】该员工账号已被停用，禁止签发业务令牌");
        }

        // 每次换票均比对最新 IdP 部门与数据库 source='IDP' 记录，防止离线调岗后仍保留原部门权限
        boolean idpDeptsMutated = identityMapper.syncIdpUserDepartments(tenantId, user.id(), concreteIdpDepts);

        List<UserDepartmentRecord> effectiveDepts = identityMapper.listActiveUserDepartments(tenantId, user.id());
        LinkedHashMap<UUID, String> dedupDepts = new LinkedHashMap<>();
        String primaryDeptPath = "";
        for (UserDepartmentRecord rec : effectiveDepts) {
            dedupDepts.putIfAbsent(rec.departmentId(), rec.deptPath());
            if (rec.isPrimary() && primaryDeptPath.isEmpty()) {
                primaryDeptPath = rec.deptPath();
            }
        }
        List<String> effectiveDeptPaths = List.copyOf(dedupDepts.values());

        if (!Objects.equals(user.deptPath(), primaryDeptPath)) {
            identityMapper.updateUserDisplayDeptPath(tenantId, user.id(), primaryDeptPath);
            user = identityMapper.findUserById(tenantId, user.id()).orElse(user);
        }

        if (idpDeptsMutated) {
            identityMapper.incrementUserAuthzVersion(tenantId, user.id());
            user = identityMapper.findUserById(tenantId, user.id()).orElse(user);
        }

        int effectiveClearance = identityMapper.resolveEffectiveClearance(user);
        long policyEpoch = identityMapper.getTenantPolicyEpoch(tenantId);
        redisRevocationSync.clearUserRevoked(user.id().toString());

        List<String> rolesCopy = List.copyOf(idpRoleCodes);
        return new MappedIdentity(
                user, rolesCopy, rolesCopy, primaryDeptPath, effectiveDeptPaths,
                effectiveClearance, policyEpoch, idpDeptsMutated, positionMutated
        );
    }

    public void revokeExistingUserIfPresent(String tenantId, String externalId) {
        if (externalId == null || externalId.isBlank()) return;
        String safeTenant = (tenantId == null || tenantId.isBlank()) ? IdentityMapper.DEFAULT_TENANT_ID : tenantId.trim();
        identityMapper.findUser(safeTenant, "oidc:corp-sso", externalId.trim())
                .ifPresent(u -> redisRevocationSync.markUserRevoked(u.id().toString()));
    }

    public Optional<String> findExistingUserId(String tenantId, String externalId) {
        if (externalId == null || externalId.isBlank()) return Optional.empty();
        String safeTenant = (tenantId == null || tenantId.isBlank()) ? IdentityMapper.DEFAULT_TENANT_ID : tenantId.trim();
        return identityMapper.findUser(safeTenant, "oidc:corp-sso", externalId.trim()).map(u -> u.id().toString());
    }

    public static List<String> extractDeptPaths(OidcUser oidcUser) {
        if (oidcUser == null) return Collections.emptyList();
        Set<String> paths = new LinkedHashSet<>();
        for (String item : readStringListClaim(oidcUser, "dept_paths")) {
            String norm = IdentityMapper.normalizeDeptPath(item);
            if (norm != null) paths.add(norm);
        }
        String normSingle = IdentityMapper.normalizeDeptPath(readStringClaim(oidcUser, "dept_path"));
        if (normSingle != null) paths.add(normSingle);
        for (String g : readStringListClaim(oidcUser, "groups")) {
            if (g != null && g.startsWith("/corp/")) {
                String mapped = IdentityMapper.normalizeDeptPath(g.substring("/corp".length()));
                if (mapped != null) paths.add(mapped);
            }
        }
        return List.copyOf(paths);
    }

    public static String extractPositionCode(OidcUser oidcUser) {
        String pos = oidcUser == null ? null : readStringClaim(oidcUser, "position_code");
        return (pos == null || pos.isBlank()) ? null : pos.trim().toUpperCase(Locale.ROOT);
    }

    public static String extractTenantId(OidcUser oidcUser) {
        String tid = oidcUser == null ? null : readStringClaim(oidcUser, "tenant_id");
        return (tid == null || tid.isBlank()) ? IdentityMapper.DEFAULT_TENANT_ID : tid.trim();
    }

    public static List<String> extractRoleCodes(OidcUser oidcUser) {
        if (oidcUser == null) return Collections.emptyList();
        Set<String> roleCodes = new LinkedHashSet<>();
        for (String role : readStringListClaim(oidcUser, "roles")) {
            if (role != null && !role.isBlank()) roleCodes.add(role.trim());
        }
        for (String g : readStringListClaim(oidcUser, "groups")) {
            if (g != null && g.startsWith("/roles/")) {
                String code = g.substring("/roles/".length()).trim();
                if (!code.isEmpty()) roleCodes.add(code);
            }
        }
        return List.copyOf(roleCodes);
    }

    private static String firstNonBlank(String... candidates) {
        for (String c : candidates) {
            if (c != null && !c.isBlank()) return c;
        }
        return "employee";
    }

    private static Object readFreshClaim(OidcUser oidcUser, String claimName) {
        OidcUserInfo userInfo = oidcUser.getUserInfo();
        return userInfo != null ? userInfo.getClaim(claimName) : oidcUser.getAttribute(claimName);
    }

    private static String readStringClaim(OidcUser oidcUser, String claimName) {
        Object val = readFreshClaim(oidcUser, claimName);
        return val != null ? String.valueOf(val) : null;
    }

    private static List<String> readStringListClaim(OidcUser oidcUser, String claimName) {
        Object raw = readFreshClaim(oidcUser, claimName);
        if (raw instanceof Collection<?> col) {
            List<String> list = new ArrayList<>(col.size());
            for (Object item : col) {
                if (item != null) list.add(String.valueOf(item));
            }
            return list;
        }
        return Collections.emptyList();
    }
}
