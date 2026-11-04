package com.enterprise.rag.admin.config;

import org.springframework.beans.factory.ObjectProvider;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.http.HttpStatus;
import org.springframework.security.config.annotation.web.builders.HttpSecurity;
import org.springframework.security.config.annotation.web.configuration.EnableWebSecurity;
import org.springframework.security.config.annotation.web.configurers.AbstractHttpConfigurer;
import org.springframework.security.oauth2.client.registration.ClientRegistrationRepository;
import org.springframework.security.oauth2.client.web.DefaultOAuth2AuthorizationRequestResolver;
import org.springframework.security.oauth2.client.web.OAuth2AuthorizationRequestCustomizers;
import org.springframework.security.oauth2.client.web.OAuth2AuthorizationRequestResolver;
import org.springframework.security.web.SecurityFilterChain;
import org.springframework.security.web.authentication.HttpStatusEntryPoint;
import org.springframework.security.web.authentication.logout.HttpStatusReturningLogoutSuccessHandler;

@Configuration
@EnableWebSecurity
public class SecurityConfig {

    @Bean
    public SecurityFilterChain securityFilterChain(
            HttpSecurity http,
            ObjectProvider<ClientRegistrationRepository> clientRegistrationRepositoryProvider
    ) throws Exception {
        ClientRegistrationRepository repo = clientRegistrationRepositoryProvider.getIfAvailable();

        http
                // 彻底关闭表单登录与 HttpBasic，杜绝自研登录页 (§2.2)
                .formLogin(AbstractHttpConfigurer::disable)
                .httpBasic(AbstractHttpConfigurer::disable)
                // 仅针对 API 换票端点、登出与内部服务端点关闭 CSRF
                .csrf(csrf -> csrf.ignoringRequestMatchers(
                        "/api/admin/auth/token",
                        "/api/admin/internal/**",
                        "/api/admin/auth/logout"
                ))
                // 接入企业 OIDC 客户端流程：强制开启 S256 PKCE，认证成功后跳回前端 /auth/callback (§2.2)
                .oauth2Login(oauth2 -> {
                    if (repo != null) {
                        oauth2.authorizationEndpoint(authEndpoint ->
                                authEndpoint.authorizationRequestResolver(pkceAuthorizationRequestResolver(repo))
                        );
                    }
                    oauth2.defaultSuccessUrl("/auth/callback", true);
                })
                // 会话登出端点 (§2.5)
                .logout(logout -> logout
                        .logoutUrl("/api/admin/auth/logout")
                        .invalidateHttpSession(true)
                        .clearAuthentication(true)
                        .deleteCookies("JSESSIONID")
                        .logoutSuccessHandler(new HttpStatusReturningLogoutSuccessHandler(HttpStatus.OK))
                )
                // API 请求未认证时返回 401 状态码，避免重定向至 SSO 登录页
                .exceptionHandling(ex -> ex
                        .defaultAuthenticationEntryPointFor(
                                new HttpStatusEntryPoint(HttpStatus.UNAUTHORIZED),
                                request -> request.getServletPath().startsWith("/api/")
                        )
                )
                // 显式白名单与 denyAll 铁律 (§2.6)
                .authorizeHttpRequests(auth -> auth
                        .requestMatchers("/.well-known/jwks.json").permitAll()
                        .requestMatchers("/oauth2/**", "/login/oauth2/**").permitAll()
                        .requestMatchers("/api/admin/internal/**").permitAll()
                        .requestMatchers("/api/admin/auth/logout").permitAll()
                        .requestMatchers("/api/admin/auth/token", "/api/admin/users/me").authenticated()
                        .anyRequest().denyAll()
                );

        return http.build();
    }

    private OAuth2AuthorizationRequestResolver pkceAuthorizationRequestResolver(ClientRegistrationRepository repo) {
        DefaultOAuth2AuthorizationRequestResolver resolver =
                new DefaultOAuth2AuthorizationRequestResolver(repo, "/oauth2/authorization");
        resolver.setAuthorizationRequestCustomizer(OAuth2AuthorizationRequestCustomizers.withPkce());
        return new OAuth2AuthorizationRequestResolver() {
            @Override
            public org.springframework.security.oauth2.core.endpoint.OAuth2AuthorizationRequest resolve(
                    jakarta.servlet.http.HttpServletRequest request) {
                return customizePrompt(resolver.resolve(request), request);
            }

            @Override
            public org.springframework.security.oauth2.core.endpoint.OAuth2AuthorizationRequest resolve(
                    jakarta.servlet.http.HttpServletRequest request, String clientRegistrationId) {
                return customizePrompt(resolver.resolve(request, clientRegistrationId), request);
            }

            private org.springframework.security.oauth2.core.endpoint.OAuth2AuthorizationRequest customizePrompt(
                    org.springframework.security.oauth2.core.endpoint.OAuth2AuthorizationRequest req,
                    jakarta.servlet.http.HttpServletRequest httpReq) {
                if (req == null) {
                    return null;
                }
                String prompt = httpReq.getParameter("prompt");
                if (prompt != null && !prompt.isBlank()) {
                    return org.springframework.security.oauth2.core.endpoint.OAuth2AuthorizationRequest.from(req)
                            .additionalParameters(params -> params.put("prompt", prompt.trim()))
                            .build();
                }
                return req;
            }
        };
    }
}
