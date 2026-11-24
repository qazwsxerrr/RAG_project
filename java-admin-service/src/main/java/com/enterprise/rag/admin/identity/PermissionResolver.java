package com.enterprise.rag.admin.identity;

import com.enterprise.rag.admin.mapper.IdentityMapper;
import com.enterprise.rag.admin.mapper.IdentityMapper.RoleRecord;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;
import org.springframework.web.server.ResponseStatusException;

import java.util.*;

@Service
public class PermissionResolver {

    public static final Set<String> KNOWN_PERMISSIONS = Set.of(
            "*",
            "kb:retrieve",
            "doc:upload",
            "doc:delete",
            "doc:review",
            "system:admin",
            "kb:manage",
            "kb:grant",
            "user:authz:manage",
            "security:classify",
            "security:declassify"
    );

    public static final Map<String, Integer> DATA_SCOPE_WEIGHTS = Map.of(
            "CUSTOM_ACL", 20,
            "DEPT_AND_SUB", 40,
            "ALL_ORG", 50
    );

    /**
     * 动作到操作权限点的唯一映射（按方案 §2.2）：
     * READ -> kb:retrieve, UPLOAD -> doc:upload, REVIEW -> doc:review, DELETE -> doc:delete, MANAGE -> kb:manage
     */
    public static final Map<String, String> ACTION_TO_PERMISSION;

    static {
        Map<String, String> m = new LinkedHashMap<>();
        m.put("READ", "kb:retrieve");
        m.put("UPLOAD", "doc:upload");
        m.put("REVIEW", "doc:review");
        m.put("DELETE", "doc:delete");
        m.put("MANAGE", "kb:manage");
        ACTION_TO_PERMISSION = Collections.unmodifiableMap(m);
    }

    private final IdentityMapper identityMapper;

    public PermissionResolver(IdentityMapper identityMapper) {
        this.identityMapper = identityMapper;
    }

    public record ResolvedPermissions(
            Set<String> permissions,
            Map<String, String> scopeByAction
    ) {
        public String scopeForAction(String action) {
            return scopeByAction.get(action);
        }
    }

    public static boolean isValidDataScope(String scope) {
        return scope != null && DATA_SCOPE_WEIGHTS.containsKey(scope.trim().toUpperCase(Locale.ROOT));
    }

    public static int scopeWeight(String scope) {
        if (scope == null) {
            return -1;
        }
        return DATA_SCOPE_WEIGHTS.getOrDefault(scope.trim().toUpperCase(Locale.ROOT), -1);
    }

    public ResolvedPermissions resolve(Collection<String> roleCodes) {
        return resolve(IdentityMapper.DEFAULT_TENANT_ID, roleCodes);
    }

    /**
     * 多角色合并与按动作计算组织范围 (§2.2 & §3.2):
     * 1. 所有有效角色的 permissions 取并集（任一角色含 "*" 则为 ["*"]）；
     * 2. 对每个动作 (READ/UPLOAD/REVIEW/DELETE/MANAGE)，仅在授予对应权限点（含 "*"）的有效角色中取最宽 data_scope；
     *    若没有任何角色授予该动作对应权限，则 scope_by_action 中不包含该动作（直接拒绝，ACL 不补发操作权限）；
     * 3. 未在当前租户策略字典中对齐的角色码坚决报 403 拒绝签发。
     */
    public ResolvedPermissions resolve(String tenantId, Collection<String> roleCodes) {
        if (roleCodes == null || roleCodes.isEmpty()) {
            throw new ResponseStatusException(HttpStatus.FORBIDDEN, "【拒绝签发】有效角色列表为空，拒绝签发凭据");
        }

        Set<String> distinctRoleCodes = new LinkedHashSet<>();
        for (String code : roleCodes) {
            if (code != null && !code.isBlank()) {
                distinctRoleCodes.add(code.trim());
            }
        }
        if (distinctRoleCodes.isEmpty()) {
            throw new ResponseStatusException(HttpStatus.FORBIDDEN, "【拒绝签发】有效角色列表为空，拒绝签发凭据");
        }

        String safeTenant = (tenantId == null || tenantId.isBlank()) ? IdentityMapper.DEFAULT_TENANT_ID : tenantId.trim();
        List<RoleRecord> roles = identityMapper.listRoles(safeTenant, distinctRoleCodes);
        if (roles.size() != distinctRoleCodes.size()) {
            Set<String> foundCodes = new HashSet<>();
            for (RoleRecord r : roles) {
                foundCodes.add(r.roleCode());
            }
            Set<String> missing = new LinkedHashSet<>(distinctRoleCodes);
            missing.removeAll(foundCodes);
            throw new ResponseStatusException(HttpStatus.FORBIDDEN, "【拒绝签发】未知角色码未在租户策略字典对齐: " + missing);
        }

        Set<String> permsUnion = new LinkedHashSet<>();
        Map<String, String> scopeByAction = new LinkedHashMap<>();

        for (RoleRecord r : roles) {
            String normalizedScope = r.dataScope() == null ? "" : r.dataScope().trim().toUpperCase(Locale.ROOT);
            int roleScopeWeight = scopeWeight(normalizedScope);
            if (roleScopeWeight < 0) {
                throw new ResponseStatusException(
                        HttpStatus.INTERNAL_SERVER_ERROR,
                        "【策略配置错误】角色 [" + r.roleCode() + "] 存在非法 data_scope: " + r.dataScope()
                );
            }

            Set<String> rolePerms = new LinkedHashSet<>(parseJsonArray(r.permissionsJson()));
            permsUnion.addAll(rolePerms);

            boolean roleHasWildcard = rolePerms.contains("*");
            for (Map.Entry<String, String> entry : ACTION_TO_PERMISSION.entrySet()) {
                String action = entry.getKey();
                String requiredPerm = entry.getValue();
                if (roleHasWildcard || rolePerms.contains(requiredPerm)) {
                    String currentBest = scopeByAction.get(action);
                    if (currentBest == null || roleScopeWeight > scopeWeight(currentBest)) {
                        scopeByAction.put(action, normalizedScope);
                    }
                }
            }
        }

        if (permsUnion.contains("*")) {
            permsUnion = Set.of("*");
        } else {
            permsUnion = Collections.unmodifiableSet(permsUnion);
        }

        return new ResolvedPermissions(permsUnion, Collections.unmodifiableMap(scopeByAction));
    }

    public static List<String> parseJsonArray(String json) {
        if (json == null || json.isBlank()) {
            return Collections.emptyList();
        }
        String trimmed = json.trim();
        if (trimmed.startsWith("[") && trimmed.endsWith("]")) {
            trimmed = trimmed.substring(1, trimmed.length() - 1).trim();
        }
        if (trimmed.isEmpty()) {
            return Collections.emptyList();
        }
        List<String> result = new ArrayList<>();
        for (String item : trimmed.split(",")) {
            String clean = item.trim().replace("\"", "").replace("'", "");
            if (!clean.isEmpty()) {
                result.add(clean);
            }
        }
        return result;
    }
}
