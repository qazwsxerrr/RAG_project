package com.enterprise.rag.admin;

import com.enterprise.rag.admin.identity.RedisRevocationSync;
import org.springframework.data.redis.connection.RedisStandaloneConfiguration;
import org.springframework.data.redis.connection.lettuce.LettuceConnectionFactory;
import org.springframework.data.redis.core.StringRedisTemplate;
import java.net.URI;
import java.util.*;

/** 真实 Redis 资源，仅 Redis、建档及 OIDC 测试显式创建。 */
final class RedisTestSupport implements AutoCloseable {
    final LettuceConnectionFactory connectionFactory;
    final StringRedisTemplate client;
    final RedisRevocationSync revocations;
    final Set<String> userIds = new LinkedHashSet<>();

    RedisTestSupport() throws Exception {
        Map<String, String> dotEnv = AuthTestSupport.loadWorkspaceDotEnv();
        URI redisUri = URI.create(dotEnv.getOrDefault("REDIS_URL", "redis://127.0.0.1:6379/0"));
        RedisStandaloneConfiguration redisCfg = new RedisStandaloneConfiguration(
                redisUri.getHost() != null ? redisUri.getHost() : "127.0.0.1",
                redisUri.getPort() > 0 ? redisUri.getPort() : 6379
        );
        if (redisUri.getPath() != null && redisUri.getPath().length() > 1) {
            redisCfg.setDatabase(Integer.parseInt(redisUri.getPath().substring(1)));
        }
        connectionFactory = new LettuceConnectionFactory(redisCfg);
        connectionFactory.afterPropertiesSet();
        client = new StringRedisTemplate(connectionFactory);
        client.afterPropertiesSet();

        revocations = new RedisRevocationSync(client, 86400);
    }

    @Override
    public void close() {
        try {
            for (String uid : userIds) {
                client.delete(List.of(
                        RedisRevocationSync.REVOKED_KEY_PREFIX + uid,
                        RedisRevocationSync.PERM_CHANGED_KEY_PREFIX + uid
                ));
            }
        } finally {
            connectionFactory.destroy();
        }
    }
}
