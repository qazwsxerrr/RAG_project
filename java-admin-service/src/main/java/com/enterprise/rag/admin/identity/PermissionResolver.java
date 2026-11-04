package com.enterprise.rag.admin.identity;

import com.enterprise.rag.admin.mapper.IdentityMapper;
import com.enterprise.rag.admin.mapper.IdentityMapper.RoleRecord;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;
import org.springframework.web.server.ResponseStatusException;

import java.util.*;

@Service
public class PermissionResolver {

    private final IdentityMapper identityMapper;

    public PermissionResolver(IdentityMapper identityMapper) {
        this.identityMapper = identityMapper;
    }

    public record ResolvedPermissions(
            String dataScope,
            Set<String> permissions
    ) {}

    /**
     * Phase-6 §2.3 & §2.7: 多角色合并与权限展开
     * 1. data_scope 取最宽：ALL > DEPT_TREE
     * 2. permissions 取并集；任一角色含 "*" 则结果为 ["*"]
     * 3. 未匹配到已知角色码坚决报 403 拒绝签发 (杜绝隐式降级或赋予默认角色)
     */
    public ResolvedPermissions resolve(Collection<String> roleCodes) {
        if (roleCodes == null || roleCodes.isEmpty()) {
            throw new ResponseStatusException(HttpStatus.FORBIDDEN, "【拒绝签发】IdP 下发角色列表为空，拒绝签发凭据");
        }

        List<RoleRecord> roles = identityMapper.listRoles(roleCodes);
        if (roles.size() != roleCodes.size()) {
            Set<String> foundCodes = new HashSet<>();
            for (RoleRecord r : roles) {
                foundCodes.add(r.roleCode());
            }
            Set<String> missing = new HashSet<>(roleCodes);
            missing.removeAll(foundCodes);
            throw new ResponseStatusException(HttpStatus.FORBIDDEN, "【拒绝签发】未知角色码未在策略字典对齐: " + missing);
        }

        boolean hasAll = false;
        Set<String> permsUnion = new LinkedHashSet<>();

        for (RoleRecord r : roles) {
            if ("ALL".equalsIgnoreCase(r.dataScope())) {
                hasAll = true;
            }
            // 解析 JSONB 数组，例如 ["kb:retrieve", "doc:upload"]
            String json = r.permissionsJson();
            if (json != null && !json.isBlank()) {
                cleanJsonArray(json).forEach(permsUnion::add);
            }
        }

        String finalScope = hasAll ? "ALL" : "DEPT_TREE";
        if (permsUnion.contains("*")) {
            permsUnion = Set.of("*");
        }

        return new ResolvedPermissions(finalScope, permsUnion);
    }

    private List<String> cleanJsonArray(String json) {
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
