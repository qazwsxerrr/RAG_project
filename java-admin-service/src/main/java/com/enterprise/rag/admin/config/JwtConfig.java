package com.enterprise.rag.admin.config;

import com.nimbusds.jose.jwk.JWK;
import com.nimbusds.jose.jwk.JWKSet;
import com.nimbusds.jose.jwk.RSAKey;
import com.nimbusds.jose.jwk.source.ImmutableJWKSet;
import com.nimbusds.jose.jwk.source.JWKSource;
import com.nimbusds.jose.proc.SecurityContext;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.security.oauth2.jwt.JwtEncoder;
import org.springframework.security.oauth2.jwt.NimbusJwtEncoder;

import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.security.KeyFactory;
import java.security.interfaces.RSAPrivateKey;
import java.security.interfaces.RSAPublicKey;
import java.security.spec.PKCS8EncodedKeySpec;
import java.security.spec.X509EncodedKeySpec;
import java.util.ArrayList;
import java.util.Base64;
import java.util.List;

@Configuration
public class JwtConfig {

    @Value("${rag.jwt.kid:rag-admin-key-1}")
    private String keyId;

    @Value("${rag.jwt.private-key-path:../keys/rsa_private.pem}")
    private String privateKeyPath;

    @Value("${rag.jwt.public-key-path:../keys/rsa_public.pem}")
    private String publicKeyPath;

    @Value("${rag.jwt.previous-kid:}")
    private String previousKeyId;

    @Value("${rag.jwt.previous-public-key-path:}")
    private String previousPublicKeyPath;

    @Bean
    public RSAKey rsaKey() throws Exception {
        if (keyId == null || keyId.isBlank()) {
            throw new IllegalStateException("rag.jwt.kid 不能为空");
        }
        RSAPrivateKey privateKey = loadPrivateKey(privateKeyPath);
        RSAPublicKey publicKey = loadPublicKey(publicKeyPath);

        return new RSAKey.Builder(publicKey)
                .privateKey(privateKey)
                .keyID(keyId.trim())
                .build();
    }

    @Bean
    public JWKSet publicJwkSet(RSAKey rsaKey) throws Exception {
        List<JWK> keys = new ArrayList<>();
        keys.add(rsaKey.toPublicJWK());

        if (previousPublicKeyPath != null && !previousPublicKeyPath.isBlank()) {
            String prevKid = (previousKeyId != null && !previousKeyId.isBlank())
                    ? previousKeyId.trim()
                    : rsaKey.getKeyID() + "-prev";
            if (prevKid.equals(rsaKey.getKeyID())) {
                throw new IllegalStateException("rag.jwt.previous-kid 不能与当前生效的 rag.jwt.kid 相同");
            }
            RSAPublicKey prevPub = loadPublicKey(previousPublicKeyPath.trim());
            keys.add(new RSAKey.Builder(prevPub).keyID(prevKid).build());
        }
        return new JWKSet(keys);
    }

    @Bean
    public JWKSource<SecurityContext> jwkSource(RSAKey rsaKey) {
        JWKSet jwkSet = new JWKSet(rsaKey);
        return new ImmutableJWKSet<>(jwkSet);
    }

    @Bean
    public JwtEncoder jwtEncoder(JWKSource<SecurityContext> jwkSource) {
        return new NimbusJwtEncoder(jwkSource);
    }

    private static RSAPrivateKey loadPrivateKey(String pathStr) throws Exception {
        Path path = Paths.get(pathStr);
        String key = Files.readString(path)
                .replace("-----BEGIN PRIVATE KEY-----", "")
                .replace("-----END PRIVATE KEY-----", "")
                .replaceAll("\\s+", "");
        byte[] keyBytes = Base64.getDecoder().decode(key);
        PKCS8EncodedKeySpec spec = new PKCS8EncodedKeySpec(keyBytes);
        return (RSAPrivateKey) KeyFactory.getInstance("RSA").generatePrivate(spec);
    }

    private static RSAPublicKey loadPublicKey(String pathStr) throws Exception {
        Path path = Paths.get(pathStr);
        String key = Files.readString(path)
                .replace("-----BEGIN PUBLIC KEY-----", "")
                .replace("-----END PUBLIC KEY-----", "")
                .replaceAll("\\s+", "");
        byte[] keyBytes = Base64.getDecoder().decode(key);
        X509EncodedKeySpec spec = new X509EncodedKeySpec(keyBytes);
        return (RSAPublicKey) KeyFactory.getInstance("RSA").generatePublic(spec);
    }
}
