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
            Map<String, String> scopeByAction,
            Map<String, List<String>> deptPathsByAction
    ) {
        public String scopeForAction(String action) {
            return scopeByAction.get(action);
        }

        public List<String> deptPathsForAction(String action) {
            return deptPathsByAction.getOrDefault(action, Collections.emptyList());
        }
    }

    private record RoleDeptCandidate(String roleScope, List<String> roleDepts) {}

    public static boolean isValidDataScope(String scope) {
        return scope != null && DATA_SCOPE_WEIGHTS.containsKey(scope.trim().toUpperCase(Locale.ROOT));
    }

    public static int scopeWeight(String scope) {
        if (scope == null) {
            return -1;
        }
        return DATA_SCOPE_WEIGHTS.getOrDefault(scope.trim().toUpperCase(Locale.ROOT), -1);
    }

    public static boolean isPathUnderPrefix(String path, String prefix) {
        String normPath = IdentityMapper.normalizeDeptPath(path);
        String normPrefix = IdentityMapper.normalizeDeptPath(prefix);
        if (normPath == null || normPrefix == null || "/".equals(normPath) || "/".equals(normPrefix)) {
            return false;
        }
        return normPath.equals(normPrefix) || normPath.startsWith(normPrefix + "/");
    }

    /**
     * 多角色合并与 ABAC 职级/主部门/部门前缀三维准入判定 + 按动作计算组织范围与可及部门集合:
     * 1. 校验 position_code ∈ role.allowed_positions（或 "*"）；
     * 2. 若 role.primary_dept_only = true，校验 primaryDeptPath 非空；
     * 3. 计算角色可及部门集合 D_r（B_r 按 allowed_dept_prefixes 过滤），若 data_scope = DEPT_AND_SUB 且 D_r 为空则拒绝 (403)；
     * 4. 按动作聚合最宽 data_scope 及对应的 dept_paths_by_action[action]。
     */
    public ResolvedPermissions resolve(
            String tenantId,
            Collection<String> roleCodes,
            String positionCode,
            String primaryDeptPath,
            Collection<String> effectiveDeptPaths
    ) {
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

        String cleanPosition = positionCode == null ? "" : positionCode.trim().toUpperCase(Locale.ROOT);
        if (cleanPosition.isBlank()) {
            throw new ResponseStatusException(HttpStatus.FORBIDDEN, "【拒绝签发】员工职级 position_code 缺失");
        }
        String normPrimary = IdentityMapper.normalizeDeptPath(primaryDeptPath);
        if ("/".equals(normPrimary)) {
            normPrimary = null;
        }
        List<String> normEffective = new ArrayList<>();
        Set<String> seenDepts = new LinkedHashSet<>();
        if (effectiveDeptPaths != null) {
            for (String p : effectiveDeptPaths) {
                String np = IdentityMapper.normalizeDeptPath(p);
                if (np != null && !"/".equals(np) && seenDepts.add(np)) {
                    normEffective.add(np);
                }
            }
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
        Map<String, List<RoleDeptCandidate>> candidatesByAction = new LinkedHashMap<>();

        for (RoleRecord r : roles) {
            String normalizedScope = r.dataScope() == null ? "" : r.dataScope().trim().toUpperCase(Locale.ROOT);
            int roleScopeWeight = scopeWeight(normalizedScope);
            if (roleScopeWeight < 0) {
                throw new ResponseStatusException(
                        HttpStatus.INTERNAL_SERVER_ERROR,
                        "【策略配置错误】角色 [" + r.roleCode() + "] 存在非法 data_scope: " + r.dataScope()
                );
            }

            List<String> allowedPositions = parseJsonArray(r.allowedPositionsJson());
            boolean positionAllowed = allowedPositions.stream()
                    .anyMatch(p -> "*".equals(p) || p.equalsIgnoreCase(cleanPosition));
            if (!positionAllowed) {
                throw new ResponseStatusException(
                        HttpStatus.FORBIDDEN,
                        "【拒绝签发】职级 [" + cleanPosition + "] 无权持有角色 [" + r.roleCode() + "]"
                );
            }

            if (r.primaryDeptOnly() && (normPrimary == null || normPrimary.isBlank())) {
                throw new ResponseStatusException(
                        HttpStatus.FORBIDDEN,
                        "【拒绝签发】角色 [" + r.roleCode() + "] 要求明确主部门 (primary_dept_only)"
                );
            }

            List<String> baseDepts = r.primaryDeptOnly() ? List.of(normPrimary) : normEffective;
            List<String> allowedPrefixes = parseJsonArray(r.allowedDeptPrefixesJson());
            List<String> roleDepts;
            if (allowedPrefixes.contains("*")) {
                roleDepts = List.copyOf(baseDepts);
            } else {
                List<String> filtered = new ArrayList<>();
                for (String dept : baseDepts) {
                    boolean matches = allowedPrefixes.stream().anyMatch(pref -> isPathUnderPrefix(dept, pref));
                    if (matches) {
                        filtered.add(dept);
                    }
                }
                roleDepts = List.copyOf(filtered);
            }

            if ("DEPT_AND_SUB".equals(normalizedScope) && roleDepts.isEmpty()) {
                throw new ResponseStatusException(
                        HttpStatus.FORBIDDEN,
                        "【拒绝签发】角色 [" + r.roleCode() + "] 部门前缀约束不匹配当前有效归属部门"
                );
            }

            List<String> parsedPerms = parseJsonArray(r.permissionsJson());
            Set<String> rolePerms = new LinkedHashSet<>();
            for (String p : parsedPerms) {
                if (!"user:authz:manage".equals(p)) {
                    rolePerms.add(p);
                }
            }
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
                    candidatesByAction.computeIfAbsent(action, k -> new ArrayList<>())
                            .add(new RoleDeptCandidate(normalizedScope, roleDepts));
                }
            }
        }

        if (permsUnion.contains("*")) {
            permsUnion = Set.of("*");
        } else {
            permsUnion = Collections.unmodifiableSet(permsUnion);
        }

        Map<String, List<String>> deptPathsByAction = new LinkedHashMap<>();
        for (Map.Entry<String, String> entry : scopeByAction.entrySet()) {
            String action = entry.getKey();
            String bestScope = entry.getValue();
            if ("DEPT_AND_SUB".equals(bestScope)) {
                Set<String> merged = new TreeSet<>();
                for (RoleDeptCandidate cand : candidatesByAction.getOrDefault(action, Collections.emptyList())) {
                    if ("DEPT_AND_SUB".equals(cand.roleScope())) {
                        merged.addAll(cand.roleDepts());
                    }
                }
                deptPathsByAction.put(action, List.copyOf(merged));
            } else {
                deptPathsByAction.put(action, Collections.emptyList());
            }
        }

        return new ResolvedPermissions(
                permsUnion,
                Collections.unmodifiableMap(scopeByAction),
                Collections.unmodifiableMap(deptPathsByAction)
        );
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
