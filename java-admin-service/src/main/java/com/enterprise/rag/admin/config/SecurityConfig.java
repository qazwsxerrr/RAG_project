package com.enterprise.rag.admin.config;

import org.springframework.beans.factory.ObjectProvider;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.http.HttpStatus;
import org.springframework.security.config.annotation.web.builders.HttpSecurity;
import org.springframework.security.config.annotation.web.configuration.EnableWebSecurity;
import org.springframework.security.config.annotation.web.configurers.AbstractHttpConfigurer;
import org.springframework.security.oauth2.client.OAuth2AuthorizedClientManager;
import org.springframework.security.oauth2.client.OAuth2AuthorizedClientProvider;
import org.springframework.security.oauth2.client.OAuth2AuthorizedClientProviderBuilder;
import org.springframework.security.oauth2.client.oidc.userinfo.OidcUserService;
import org.springframework.security.oauth2.client.registration.ClientRegistrationRepository;
import org.springframework.security.oauth2.client.web.DefaultOAuth2AuthorizedClientManager;
import org.springframework.security.oauth2.client.web.DefaultOAuth2AuthorizationRequestResolver;
import org.springframework.security.oauth2.client.web.OAuth2AuthorizationRequestCustomizers;
import org.springframework.security.oauth2.client.web.OAuth2AuthorizationRequestResolver;
import org.springframework.security.oauth2.client.web.OAuth2AuthorizedClientRepository;
import org.springframework.security.web.SecurityFilterChain;
import org.springframework.security.web.authentication.HttpStatusEntryPoint;
import org.springframework.security.web.authentication.logout.HttpStatusReturningLogoutSuccessHandler;
import org.springframework.web.util.UriComponentsBuilder;

import java.time.Duration;

/**
 * 企业 OIDC 认证网关安全配置：
 * 仅负责 OIDC 登录回调、RS256 cv=2 令牌签发、JWKS 公钥发布与内部模型密钥租约；
 * 知识库与 ACL 治理控制面统一收拢至 Python FastAPI 单一策略引擎。
 */
@Configuration
@EnableWebSecurity
public class SecurityConfig {

    @Bean
    public OAuth2AuthorizedClientManager authorizedClientManager(
            ClientRegistrationRepository clientRegistrationRepository,
            OAuth2AuthorizedClientRepository authorizedClientRepository
    ) {
        OAuth2AuthorizedClientProvider provider = OAuth2AuthorizedClientProviderBuilder.builder()
                .authorizationCode()
                .refreshToken(builder -> builder.clockSkew(Duration.ofSeconds(60)))
                .build();
        DefaultOAuth2AuthorizedClientManager manager = new DefaultOAuth2AuthorizedClientManager(
                clientRegistrationRepository,
                authorizedClientRepository
        );
        manager.setAuthorizedClientProvider(provider);
        return manager;
    }

    @Bean
    public OidcUserService oidcUserService() {
        OidcUserService service = new OidcUserService();
        // 换票刷新时强制从标准 OIDC UserInfo 端点拉取最新多部门与角色组声明
        service.setRetrieveUserInfo(req -> {
            String uri = req.getClientRegistration().getProviderDetails().getUserInfoEndpoint().getUri();
            return uri != null && !uri.isBlank();
        });
        return service;
    }

    /**
     * OIDC Session 登录、换票、登出、JWKS 与内部密钥租约安全链
     */
    @Bean
    public SecurityFilterChain securityFilterChain(
            HttpSecurity http,
            ObjectProvider<ClientRegistrationRepository> clientRegistrationRepositoryProvider
    ) throws Exception {
        ClientRegistrationRepository repo = clientRegistrationRepositoryProvider.getIfAvailable();

        http
                .formLogin(AbstractHttpConfigurer::disable)
                .httpBasic(AbstractHttpConfigurer::disable)
                .csrf(csrf -> csrf.ignoringRequestMatchers(
                        "/api/admin/auth/token",
                        "/api/admin/internal/**",
                        "/api/admin/auth/logout"
                ))
                .oauth2Login(oauth2 -> {
                    if (repo != null) {
                        oauth2.authorizationEndpoint(authEndpoint ->
                                authEndpoint.authorizationRequestResolver(pkceAuthorizationRequestResolver(repo))
                        );
                    }
                    oauth2.defaultSuccessUrl("/auth/callback", true);
                })
                .logout(logout -> logout
                        .logoutUrl("/api/admin/auth/logout")
                        .invalidateHttpSession(true)
                        .clearAuthentication(true)
                        .deleteCookies("JSESSIONID")
                        .logoutSuccessHandler(new HttpStatusReturningLogoutSuccessHandler(HttpStatus.OK))
                )
                .exceptionHandling(ex -> ex
                        .defaultAuthenticationEntryPointFor(
                                new HttpStatusEntryPoint(HttpStatus.UNAUTHORIZED),
                                request -> request.getServletPath().startsWith("/api/")
                        )
                )
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
                    String existingUri = req.getAuthorizationRequestUri();
                    String newUri = UriComponentsBuilder
                            .fromUriString(existingUri)
                            .queryParam("prompt", prompt.trim())
                            .build()
                            .toUriString();
                    return org.springframework.security.oauth2.core.endpoint.OAuth2AuthorizationRequest.from(req)
                            .authorizationRequestUri(newUri)
                            .additionalParameters(params -> params.put("prompt", prompt.trim()))
                            .build();
                }
                return req;
            }
        };
    }
}
