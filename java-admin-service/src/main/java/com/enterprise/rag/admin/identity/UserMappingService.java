package com.enterprise.rag.admin.identity;

import com.enterprise.rag.admin.mapper.IdentityMapper;
import com.enterprise.rag.admin.mapper.IdentityMapper.UserRecord;
import org.springframework.http.HttpStatus;
import org.springframework.security.oauth2.core.oidc.OidcUserInfo;
import org.springframework.security.oauth2.core.oidc.user.OidcUser;
import org.springframework.stereotype.Service;
import org.springframework.web.server.ResponseStatusException;

import java.util.*;

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
            List<String> roleCodes
    ) {}

    /**
     * Phase-6 §2.3: JIT 建档与属性映射
     * 1. 提取 sub -> external_id
     * 2. 提取 preferred_username / name -> real_name
     * 3. 提取 dept_path 或 groups 路径映射；缺失坚决抛 403 拒绝建档
     * 4. 提取 roles 角色码；缺失坚决抛 403 拒绝签发
     */
    public MappedIdentity mapAndProvision(OidcUser oidcUser, boolean syncDeptPath) {
        if (oidcUser == null) {
            throw new ResponseStatusException(HttpStatus.UNAUTHORIZED, "【鉴权失败】缺少 OIDC 会话身份");
        }

        String externalId = oidcUser.getSubject();
        if (externalId == null || externalId.isBlank()) {
            throw new ResponseStatusException(HttpStatus.UNAUTHORIZED, "【鉴权失败】OIDC id_token 缺少 sub Claim");
        }

        String realName = oidcUser.getPreferredUsername();
        if (realName == null || realName.isBlank()) {
            realName = oidcUser.getFullName();
        }
        if (realName == null || realName.isBlank()) {
            realName = oidcUser.getName();
        }
        if (realName == null || realName.isBlank()) {
            realName = "employee_" + externalId.substring(0, Math.min(8, externalId.length()));
        }

        // 提取部门路径
        String deptPath = extractDeptPath(oidcUser);

        if (deptPath == null || deptPath.isBlank() || !deptPath.startsWith("/")) {
            revokeExistingUserIfPresent(externalId);
            throw new ResponseStatusException(HttpStatus.FORBIDDEN, "【拒绝建档】员工缺少部门路径 dept_path 属性且 groups 无法映射，禁止建档");
        }

        // 提取角色列表
        List<String> roleCodes = extractRoleCodes(oidcUser);

        if (roleCodes.isEmpty()) {
            revokeExistingUserIfPresent(externalId);
            throw new ResponseStatusException(HttpStatus.FORBIDDEN, "【拒绝签发】员工未分配任何有效角色，权限体系未对齐");
        }

        UserRecord user = identityMapper.upsertUser("oidc:corp-sso", externalId, realName, deptPath, syncDeptPath);
        if (!user.isActive()) {
            redisRevocationSync.markUserRevoked(user.id().toString());
            throw new ResponseStatusException(HttpStatus.FORBIDDEN, "【拒绝签发】该员工账号已被停用，禁止签发业务令牌");
        }
        // 员工已通过真实 IdP 校验且数据库处于启用状态，清理历史误伤/会话过期残留的 Redis 吊销键，
        // 同时不触碰 auth:perm_changed_at，旧凭据仍受权限时间戳边界约束。
        redisRevocationSync.clearUserRevoked(user.id().toString());
        return new MappedIdentity(user, roleCodes);
    }

    /**
     * 当员工在 IdP 侧被禁用、吊销或移除全部角色/部门时，若本地已建档，同步写入 Redis 秒级吊销其存量业务 JWT
     */
    public void revokeExistingUserIfPresent(String externalId) {
        if (externalId == null || externalId.isBlank()) {
            return;
        }
        identityMapper.findUser("oidc:corp-sso", externalId.trim())
                .ifPresent(u -> redisRevocationSync.markUserRevoked(u.id().toString()));
    }

    public Optional<String> findExistingUserId(String externalId) {
        if (externalId == null || externalId.isBlank()) {
            return Optional.empty();
        }
        return identityMapper.findUser("oidc:corp-sso", externalId.trim())
                .map(u -> u.id().toString());
    }

    public static String extractDeptPath(OidcUser oidcUser) {
        if (oidcUser == null) {
            return null;
        }
        String deptPath = readStringClaim(oidcUser, "dept_path");
        List<String> groups = readStringListClaim(oidcUser, "groups");
        if ((deptPath == null || deptPath.isBlank()) && groups != null) {
            for (String g : groups) {
                if (g != null && g.startsWith("/corp/")) {
                    String mapped = g.substring("/corp".length()).trim();
                    if (!mapped.isBlank()) {
                        return mapped;
                    }
                }
            }
        }
        return deptPath;
    }

    public static List<String> extractRoleCodes(OidcUser oidcUser) {
        if (oidcUser == null) {
            return Collections.emptyList();
        }
        Set<String> roleCodes = new LinkedHashSet<>();
        for (String role : readStringListClaim(oidcUser, "roles")) {
            if (role != null && !role.isBlank()) {
                roleCodes.add(role);
            }
        }
        for (String g : readStringListClaim(oidcUser, "groups")) {
            if (g != null && g.startsWith("/roles/")) {
                String code = g.substring("/roles/".length()).trim();
                if (!code.isEmpty()) {
                    roleCodes.add(code);
                }
            }
        }
        return List.copyOf(roleCodes);
    }

    /**
     * 存在实时 UserInfo 时，字段缺失也以该快照为准，防止已移除的角色或部门继续生效。
     */
    private static Object readFreshClaim(OidcUser oidcUser, String claimName) {
        OidcUserInfo userInfo = oidcUser.getUserInfo();
        if (userInfo != null) {
            return userInfo.getClaim(claimName);
        }
        return oidcUser.getAttribute(claimName);
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
                if (item != null) {
                    list.add(String.valueOf(item));
                }
            }
            return list;
        }
        return Collections.emptyList();
    }
}
