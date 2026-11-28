package com.enterprise.rag.admin.identity;

import com.enterprise.rag.admin.identity.PermissionResolver.ResolvedPermissions;
import com.enterprise.rag.admin.mapper.IdentityMapper;
import com.enterprise.rag.admin.mapper.IdentityMapper.PositionRecord;
import com.enterprise.rag.admin.mapper.IdentityMapper.RoleRecord;
import com.enterprise.rag.admin.mapper.IdentityMapper.UserDepartmentRecord;
import com.enterprise.rag.admin.mapper.IdentityMapper.UserRecord;
import org.springframework.beans.factory.annotation.Autowired;
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
    private final PermissionResolver permissionResolver;

    @Autowired
    public UserMappingService(
            IdentityMapper identityMapper,
            RedisRevocationSync redisRevocationSync,
            PermissionResolver permissionResolver
    ) {
        this.identityMapper = Objects.requireNonNull(identityMapper, "identityMapper 不能为空");
        this.redisRevocationSync = Objects.requireNonNull(redisRevocationSync, "redisRevocationSync 不能为空");
        this.permissionResolver = Objects.requireNonNull(permissionResolver, "permissionResolver 不能为空");
    }

    public UserMappingService(IdentityMapper identityMapper, RedisRevocationSync redisRevocationSync) {
        this(identityMapper, redisRevocationSync, new PermissionResolver(identityMapper));
    }

    public record MappedIdentity(
            UserRecord user,
            List<String> roleCodes,
            Set<String> permissions,
            Map<String, String> scopeByAction,
            Map<String, List<String>> deptPathsByAction,
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

        String positionCode = extractPositionCode(oidcUser);
        if (positionCode == null || positionCode.isBlank()) {
            revokeExistingUserIfPresent(tenantId, externalId);
            throw new ResponseStatusException(HttpStatus.FORBIDDEN, "【拒绝建档】企业 IdP 未下发有效职级 position_code");
        }
        identityMapper.ensureDefaultPositionsIfMissing(tenantId);
        Optional<PositionRecord> posOpt = identityMapper.findPosition(tenantId, positionCode);
        if (posOpt.isEmpty()) {
            revokeExistingUserIfPresent(tenantId, externalId);
            throw new ResponseStatusException(
                    HttpStatus.FORBIDDEN,
                    "【拒绝建档】企业 IdP 下发的职级 [" + positionCode + "] 未在 sys_positions 注册"
            );
        }

        List<String> roleCodes = extractRoleCodes(oidcUser);
        if (roleCodes.isEmpty()) {
            revokeExistingUserIfPresent(tenantId, externalId);
            throw new ResponseStatusException(HttpStatus.FORBIDDEN, "【拒绝签发】企业 IdP 未分配任何有效角色");
        }
        List<RoleRecord> roleRecords = identityMapper.listRoles(tenantId, roleCodes);
        if (roleRecords.size() != new LinkedHashSet<>(roleCodes).size()) {
            Set<String> found = new LinkedHashSet<>();
            for (RoleRecord rr : roleRecords) {
                found.add(rr.roleCode());
            }
            Set<String> missing = new LinkedHashSet<>(roleCodes);
            missing.removeAll(found);
            revokeExistingUserIfPresent(tenantId, externalId);
            throw new ResponseStatusException(HttpStatus.FORBIDDEN, "【拒绝签发】未知角色码未在租户策略字典对齐: " + missing);
        }

        List<String> belongingPaths = extractBelongingDeptPaths(oidcUser);
        boolean hasRootDeptMarker = belongingPaths.contains("/");
        List<String> concreteIdpDepts = new ArrayList<>(
                belongingPaths.stream().filter(p -> p != null && !p.isBlank() && !"/".equals(p)).toList()
        );

        String explicitPrimary;
        try {
            explicitPrimary = extractExplicitPrimaryDeptPath(oidcUser);
        } catch (ResponseStatusException ex) {
            revokeExistingUserIfPresent(tenantId, externalId);
            throw ex;
        }
        if ("/".equals(explicitPrimary)) {
            if (!concreteIdpDepts.isEmpty()) {
                revokeExistingUserIfPresent(tenantId, externalId);
                throw new ResponseStatusException(HttpStatus.FORBIDDEN, "【拒绝建档】IdP 主部门属性 dept_path 不能为根路径 /");
            }
            hasRootDeptMarker = true;
            explicitPrimary = "";
        }

        if (!concreteIdpDepts.isEmpty() && !explicitPrimary.isBlank() && !concreteIdpDepts.contains(explicitPrimary)) {
            revokeExistingUserIfPresent(tenantId, externalId);
            throw new ResponseStatusException(
                    HttpStatus.FORBIDDEN,
                    "【拒绝建档】IdP 主部门 dept_path [" + explicitPrimary + "] 不在员工所属部门集合 " + concreteIdpDepts + " 中"
            );
        }
        if (concreteIdpDepts.isEmpty() && !hasRootDeptMarker && !explicitPrimary.isBlank()) {
            concreteIdpDepts.add(explicitPrimary);
        }

        if (concreteIdpDepts.isEmpty() && !hasRootDeptMarker) {
            revokeExistingUserIfPresent(tenantId, externalId);
            throw new ResponseStatusException(HttpStatus.FORBIDDEN, "【拒绝建档】企业 IdP 未下发有效部门路径 (dept_paths/dept_path/groups)");
        }

        boolean requiresPrimaryDept = roleRecords.stream().anyMatch(RoleRecord::primaryDeptOnly);
        if (concreteIdpDepts.size() >= 2 && requiresPrimaryDept && explicitPrimary.isBlank()) {
            revokeExistingUserIfPresent(tenantId, externalId);
            throw new ResponseStatusException(
                    HttpStatus.FORBIDDEN,
                    "【拒绝建档】多部门员工持有要求明确主部门的角色 (primary_dept_only)，IdP 必须显式下发单值 dept_path"
            );
        }

        String desiredPrimaryDeptPath;
        if (concreteIdpDepts.size() == 1) {
            desiredPrimaryDeptPath = concreteIdpDepts.getFirst();
        } else if (!explicitPrimary.isBlank()) {
            desiredPrimaryDeptPath = explicitPrimary;
        } else {
            desiredPrimaryDeptPath = "";
        }

        // 前置校验 ABAC 三维策略，未通过则拒绝建档/登录并吊销存量用户
        try {
            permissionResolver.resolve(tenantId, roleCodes, positionCode, desiredPrimaryDeptPath, concreteIdpDepts);
        } catch (ResponseStatusException ex) {
            revokeExistingUserIfPresent(tenantId, externalId);
            throw ex;
        }

        Optional<UserRecord> existingBefore = identityMapper.findUser(tenantId, "oidc:corp-sso", externalId);
        UserRecord user = identityMapper.upsertUser(tenantId, "oidc:corp-sso", externalId, realName, positionCode);
        boolean positionMutated = existingBefore.isPresent()
                && !Objects.equals(existingBefore.get().positionCode(), user.positionCode());

        if (!user.isActive()) {
            redisRevocationSync.markUserRevoked(user.id().toString());
            throw new ResponseStatusException(HttpStatus.FORBIDDEN, "【拒绝签发】该员工账号已被停用，禁止签发业务令牌");
        }

        boolean idpDeptsMutated = identityMapper.syncIdpUserDepartments(
                tenantId, user.id(), concreteIdpDepts, desiredPrimaryDeptPath
        );

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

        ResolvedPermissions resolved;
        try {
            resolved = permissionResolver.resolve(tenantId, roleCodes, user.positionCode(), primaryDeptPath, effectiveDeptPaths);
        } catch (ResponseStatusException ex) {
            redisRevocationSync.markUserRevoked(user.id().toString());
            throw ex;
        }

        if (idpDeptsMutated) {
            identityMapper.incrementUserAuthzVersion(tenantId, user.id());
            user = identityMapper.findUserById(tenantId, user.id()).orElse(user);
        }

        int effectiveClearance = identityMapper.resolveEffectiveClearance(user);
        long policyEpoch = identityMapper.getTenantPolicyEpoch(tenantId);
        redisRevocationSync.clearUserRevoked(user.id().toString());

        List<String> rolesCopy = List.copyOf(roleCodes);
        return new MappedIdentity(
                user,
                rolesCopy,
                resolved.permissions(),
                resolved.scopeByAction(),
                resolved.deptPathsByAction(),
                primaryDeptPath,
                effectiveDeptPaths,
                effectiveClearance,
                policyEpoch,
                idpDeptsMutated,
                positionMutated
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
        Set<String> paths = new LinkedHashSet<>(extractBelongingDeptPaths(oidcUser));
        Object rawSingle = readFreshClaim(oidcUser, "dept_path");
        if (rawSingle instanceof Collection<?> col) {
            for (Object item : col) {
                if (item != null) {
                    String norm = IdentityMapper.normalizeDeptPath(String.valueOf(item));
                    if (norm != null) paths.add(norm);
                }
            }
        } else if (rawSingle != null) {
            String normSingle = IdentityMapper.normalizeDeptPath(String.valueOf(rawSingle));
            if (normSingle != null) paths.add(normSingle);
        }
        return List.copyOf(paths);
    }

    /** 仅从 dept_paths 与 /corp/* 组提取员工实际归属部门集合（严禁与主部门 dept_path 混并前跳过归属校验） */
    public static List<String> extractBelongingDeptPaths(OidcUser oidcUser) {
        if (oidcUser == null) return Collections.emptyList();
        Set<String> paths = new LinkedHashSet<>();
        for (String item : readStringListClaim(oidcUser, "dept_paths")) {
            String norm = IdentityMapper.normalizeDeptPath(item);
            if (norm != null) paths.add(norm);
        }
        for (String g : readStringListClaim(oidcUser, "groups")) {
            if (g != null && g.startsWith("/corp/")) {
                String mapped = IdentityMapper.normalizeDeptPath(g.substring("/corp".length()));
                if (mapped != null) paths.add(mapped);
            }
        }
        return List.copyOf(paths);
    }

    /** 严格提取单值主部门属性 dept_path，若为多值冲突或非法格式直接抛 403 */
    public static String extractExplicitPrimaryDeptPath(OidcUser oidcUser) {
        if (oidcUser == null) return "";
        Object raw = readFreshClaim(oidcUser, "dept_path");
        if (raw == null) {
            return "";
        }
        if (raw instanceof Collection<?> col) {
            Set<String> distinct = new LinkedHashSet<>();
            for (Object item : col) {
                if (item == null) continue;
                String trimmed = String.valueOf(item).trim();
                if (trimmed.isEmpty()) continue;
                String norm = IdentityMapper.normalizeDeptPath(trimmed);
                if (norm == null) {
                    throw new ResponseStatusException(
                            HttpStatus.FORBIDDEN,
                            "【拒绝建档】IdP 主部门属性 dept_path 格式非法: " + trimmed
                    );
                }
                distinct.add(norm);
            }
            if (distinct.size() > 1) {
                throw new ResponseStatusException(
                        HttpStatus.FORBIDDEN,
                        "【拒绝建档】IdP 主部门属性 dept_path 存在多值冲突: " + distinct
                );
            }
            return distinct.isEmpty() ? "" : distinct.iterator().next();
        }
        String trimmed = String.valueOf(raw).trim();
        if (trimmed.isEmpty()) {
            return "";
        }
        String norm = IdentityMapper.normalizeDeptPath(trimmed);
        if (norm == null) {
            throw new ResponseStatusException(
                    HttpStatus.FORBIDDEN,
                    "【拒绝建档】IdP 主部门属性 dept_path 格式非法: " + trimmed
            );
        }
        return norm;
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
