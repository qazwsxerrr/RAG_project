package com.enterprise.rag.admin.config;

import org.springframework.beans.factory.ObjectProvider;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.http.HttpStatus;
import org.springframework.http.client.SimpleClientHttpRequestFactory;
import org.springframework.http.converter.FormHttpMessageConverter;
import org.springframework.security.config.annotation.web.builders.HttpSecurity;
import org.springframework.security.config.annotation.web.configuration.EnableWebSecurity;
import org.springframework.security.config.annotation.web.configurers.AbstractHttpConfigurer;
import org.springframework.security.oauth2.client.OAuth2AuthorizedClientManager;
import org.springframework.security.oauth2.client.OAuth2AuthorizedClientProvider;
import org.springframework.security.oauth2.client.OAuth2AuthorizedClientProviderBuilder;
import org.springframework.security.oauth2.client.endpoint.RestClientRefreshTokenTokenResponseClient;
import org.springframework.security.oauth2.client.http.OAuth2ErrorResponseErrorHandler;
import org.springframework.security.oauth2.client.oidc.userinfo.OidcUserService;
import org.springframework.security.oauth2.client.registration.ClientRegistrationRepository;
import org.springframework.security.oauth2.client.userinfo.DefaultOAuth2UserService;
import org.springframework.security.oauth2.client.web.DefaultOAuth2AuthorizedClientManager;
import org.springframework.security.oauth2.client.web.DefaultOAuth2AuthorizationRequestResolver;
import org.springframework.security.oauth2.client.web.OAuth2AuthorizationRequestCustomizers;
import org.springframework.security.oauth2.client.web.OAuth2AuthorizationRequestResolver;
import org.springframework.security.oauth2.client.web.OAuth2AuthorizedClientRepository;
import org.springframework.security.oauth2.core.http.converter.OAuth2AccessTokenResponseHttpMessageConverter;
import org.springframework.security.web.SecurityFilterChain;
import org.springframework.security.web.authentication.HttpStatusEntryPoint;
import org.springframework.web.client.RestClient;
import org.springframework.web.client.RestTemplate;

import java.time.Duration;

/**
 * 企业 OIDC 认证网关安全配置：
 * 仅负责 OIDC 登录回调、RS256 cv=2 令牌签发、JWKS 公钥发布与内部模型密钥租约；
 * 知识库与 ACL 治理控制面统一收拢至 Python FastAPI 单一策略引擎。
 */
@Configuration
@EnableWebSecurity
public class SecurityConfig {

    private static SimpleClientHttpRequestFactory createOidcRequestFactory() {
        SimpleClientHttpRequestFactory factory = new SimpleClientHttpRequestFactory();
        factory.setConnectTimeout(Duration.ofSeconds(5));
        factory.setReadTimeout(Duration.ofSeconds(8));
        return factory;
    }

    @Bean
    public OAuth2AuthorizedClientManager authorizedClientManager(
            ClientRegistrationRepository clientRegistrationRepository,
            OAuth2AuthorizedClientRepository authorizedClientRepository
    ) {
        RestClient refreshRestClient = RestClient.builder()
                .requestFactory(createOidcRequestFactory())
                .configureMessageConverters(converters -> {
                    converters.addCustomConverter(new FormHttpMessageConverter());
                    converters.addCustomConverter(new OAuth2AccessTokenResponseHttpMessageConverter());
                })
                .defaultStatusHandler(new OAuth2ErrorResponseErrorHandler())
                .build();

        RestClientRefreshTokenTokenResponseClient refreshTokenResponseClient = new RestClientRefreshTokenTokenResponseClient();
        refreshTokenResponseClient.setRestClient(refreshRestClient);

        OAuth2AuthorizedClientProvider provider = OAuth2AuthorizedClientProviderBuilder.builder()
                .authorizationCode()
                .refreshToken(builder -> builder
                        .clockSkew(Duration.ofSeconds(60))
                        .accessTokenResponseClient(refreshTokenResponseClient))
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
        RestTemplate userInfoRestTemplate = new RestTemplate(createOidcRequestFactory());
        userInfoRestTemplate.setErrorHandler(new OAuth2ErrorResponseErrorHandler());

        DefaultOAuth2UserService delegateUserService = new DefaultOAuth2UserService();
        delegateUserService.setRestOperations(userInfoRestTemplate);

        OidcUserService service = new OidcUserService();
        service.setOauth2UserService(delegateUserService);
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
                // 由 AuthController#logout 接管登出，提取 id_token 构造 OIDC RP-Initiated Logout 地址后再销毁本地 Session
                .logout(AbstractHttpConfigurer::disable)
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
        return resolver;
    }
}
