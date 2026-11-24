package com.enterprise.rag.admin;

import com.enterprise.rag.admin.web.AuthController.ModelTokenRequest;
import org.junit.jupiter.api.Test;
import org.springframework.web.server.ResponseStatusException;
import java.util.*;

import static org.junit.jupiter.api.Assertions.*;

class ModelLeaseContractTest {
    @Test
    void modelTokenVaultLeaseAndSecurityBoundaries() throws Exception {
        try (OidcTestSupport support = new OidcTestSupport()) {
            // 合法换取 Kedaya 租约 (§8.6 用例 23)
            Map<String, Object> lease = support.authController.issueModelToken(
                    "rag-engine",
                    "rag-engine-service-secret",
                    new ModelTokenRequest("kedaya", List.of("model:llm"))
            );
            assertEquals("kedaya", lease.get("provider"));
            assertEquals("https://kedaya.ai/v1", lease.get("base_url"));
            assertEquals("sk-vault-kedaya-12345678", lease.get("api_key"));
            assertEquals("sk-v****5678", lease.get("masked"));

            // 错误的 Service Token -> 403 (§8.6 用例 26)
            ResponseStatusException badTokenEx = assertThrows(ResponseStatusException.class,
                    () -> support.authController.issueModelToken(
                            "rag-engine",
                            "wrong-secret",
                            new ModelTokenRequest("kedaya", List.of("model:llm"))
                    ));
            assertEquals(403, badTokenEx.getStatusCode().value());

            // 未授权的 scope (如 oss:write) -> 403 (§8.6 用例 27)
            ResponseStatusException badScopeEx = assertThrows(ResponseStatusException.class,
                    () -> support.authController.issueModelToken(
                            "rag-engine",
                            "rag-engine-service-secret",
                            new ModelTokenRequest("kedaya", List.of("oss:write"))
                    ));
            assertEquals(403, badScopeEx.getStatusCode().value());
        }
    }
}
