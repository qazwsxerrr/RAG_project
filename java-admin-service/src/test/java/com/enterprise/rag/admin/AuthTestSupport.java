package com.enterprise.rag.admin;

import com.enterprise.rag.admin.config.JwtConfig;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.DriverManagerDataSource;
import org.springframework.security.core.authority.SimpleGrantedAuthority;
import org.springframework.security.oauth2.core.oidc.OidcIdToken;
import org.springframework.security.oauth2.core.oidc.user.DefaultOidcUser;
import org.springframework.security.oauth2.core.oidc.user.OidcUser;
import org.springframework.test.util.ReflectionTestUtils;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.time.Instant;
import java.util.*;

/** 测试配置与纯构造工具，各测试组显式选择所需组件。 */
final class AuthTestSupport {
    private AuthTestSupport() {}

    static JdbcTemplate database() throws Exception {
        Map<String, String> dotEnv = loadWorkspaceDotEnv();
        String pgHost = dotEnv.getOrDefault("POSTGRES_HOST", "121.41.118.56");
        String pgPort = dotEnv.getOrDefault("POSTGRES_PORT", "5432");
        String pgDb = dotEnv.getOrDefault("POSTGRES_DB", "rag_enterprise");
        String pgUser = dotEnv.getOrDefault("SPRING_DATASOURCE_USERNAME", dotEnv.getOrDefault("POSTGRES_USER", "postgres"));
        String pgPass = dotEnv.getOrDefault("SPRING_DATASOURCE_PASSWORD", dotEnv.getOrDefault("POSTGRES_PASSWORD", "postgres_password"));
        String jdbcUrl = dotEnv.getOrDefault(
                "SPRING_DATASOURCE_URL",
                "jdbc:postgresql://" + pgHost + ":" + pgPort + "/" + pgDb
        );
        DriverManagerDataSource dataSource = new DriverManagerDataSource(jdbcUrl, pgUser, pgPass);
        dataSource.setDriverClassName("org.postgresql.Driver");
        return new JdbcTemplate(dataSource);
    }

    static JwtConfig jwtConfig() {
        JwtConfig jwtConfig = new JwtConfig();
        Path privPath = Paths.get("../keys/rsa_private.pem").toAbsolutePath().normalize();
        Path pubPath = Paths.get("../keys/rsa_public.pem").toAbsolutePath().normalize();
        ReflectionTestUtils.setField(jwtConfig, "keyId", "rag-admin-key-1");
        ReflectionTestUtils.setField(jwtConfig, "privateKeyPath", privPath.toString());
        ReflectionTestUtils.setField(jwtConfig, "publicKeyPath", pubPath.toString());
        ReflectionTestUtils.setField(jwtConfig, "previousKeyId", "");
        ReflectionTestUtils.setField(jwtConfig, "previousPublicKeyPath", "");

        return jwtConfig;
    }

    static Map<String, String> loadWorkspaceDotEnv() throws Exception {
        Map<String, String> map = new HashMap<>();
        Path envFile = Paths.get("../.env").toAbsolutePath().normalize();
        if (Files.isRegularFile(envFile)) {
            for (String rawLine : Files.readAllLines(envFile, StandardCharsets.UTF_8)) {
                String line = rawLine.trim();
                if (line.isEmpty() || line.startsWith("#") || !line.contains("=")) {
                    continue;
                }
                int idx = line.indexOf('=');
                String k = line.substring(0, idx).trim();
                String v = line.substring(idx + 1).trim();
                if ((v.startsWith("\"") && v.endsWith("\"")) || (v.startsWith("'") && v.endsWith("'"))) {
                    v = v.substring(1, v.length() - 1);
                }
                map.put(k, v);
            }
        }
        map.putAll(System.getenv());
        return map;
    }

    static OidcUser buildOidcUser(String sub, String username, String deptPath, List<String> groups, List<String> roles) {
        Map<String, Object> claims = new HashMap<>();
        claims.put("sub", sub);
        claims.put("preferred_username", username);
        if (deptPath != null) {
            claims.put("dept_path", deptPath);
        }
        if (groups != null) {
            claims.put("groups", groups);
        }
        if (roles != null) {
            claims.put("roles", roles);
        }
        OidcIdToken idToken = new OidcIdToken("mapping-test-input", Instant.now(), Instant.now().plusSeconds(3600), claims);
        return new DefaultOidcUser(List.of(new SimpleGrantedAuthority("ROLE_USER")), idToken, "preferred_username");
    }
}
