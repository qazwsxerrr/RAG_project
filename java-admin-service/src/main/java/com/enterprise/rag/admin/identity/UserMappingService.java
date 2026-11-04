package com.enterprise.rag.admin.identity;

import com.enterprise.rag.admin.mapper.IdentityMapper;
import com.enterprise.rag.admin.mapper.IdentityMapper.UserRecord;
import org.springframework.http.HttpStatus;
import org.springframework.security.oauth2.core.oidc.user.OidcUser;
import org.springframework.stereotype.Service;
import org.springframework.web.server.ResponseStatusException;

import java.util.*;

@Service
public class UserMappingService {

    private final IdentityMapper identityMapper;

    public UserMappingService(IdentityMapper identityMapper) {
        this.identityMapper = identityMapper;
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
    public MappedIdentity mapAndProvision(OidcUser oidcUser) {
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
        String deptPath = oidcUser.getAttribute("dept_path");
        List<String> groups = oidcUser.getAttribute("groups");
        if (groups == null) {
            groups = Collections.emptyList();
        }

        if ((deptPath == null || deptPath.isBlank()) && !groups.isEmpty()) {
            for (String g : groups) {
                if (g != null && g.startsWith("/corp/")) {
                    String mapped = g.substring("/corp".length()).trim();
                    if (!mapped.isBlank()) {
                        deptPath = mapped;
                        break;
                    }
                }
            }
        }

        if (deptPath == null || deptPath.isBlank() || !deptPath.startsWith("/")) {
            throw new ResponseStatusException(HttpStatus.FORBIDDEN, "【拒绝建档】员工缺少部门路径 dept_path 属性且 groups 无法映射，禁止建档");
        }

        // 提取角色列表
        List<String> roleCodes = new ArrayList<>();
        List<String> rolesAttr = oidcUser.getAttribute("roles");
        if (rolesAttr != null) {
            roleCodes.addAll(rolesAttr);
        }
        for (String g : groups) {
            if (g != null && g.startsWith("/roles/")) {
                String code = g.substring("/roles/".length()).trim();
                if (!code.isEmpty() && !roleCodes.contains(code)) {
                    roleCodes.add(code);
                }
            }
        }

        if (roleCodes.isEmpty()) {
            throw new ResponseStatusException(HttpStatus.FORBIDDEN, "【拒绝签发】员工未分配任何有效角色，权限体系未对齐");
        }

        UserRecord user = identityMapper.upsertUser("oidc:corp-sso", externalId, realName, deptPath);
        if (!user.isActive()) {
            throw new ResponseStatusException(HttpStatus.FORBIDDEN, "【拒绝签发】该员工账号已被停用，禁止签发业务令牌");
        }
        return new MappedIdentity(user, roleCodes);
    }
}
