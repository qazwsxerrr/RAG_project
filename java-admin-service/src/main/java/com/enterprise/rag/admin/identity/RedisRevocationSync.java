package com.enterprise.rag.admin.identity;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.stereotype.Component;

import java.time.Duration;
import java.util.Objects;

/**
 * 基于 Spring Data Redis (StringRedisTemplate) 的员工访问吊销与权限变更同步服务。
 * 严格遵循 AGENTS.md Fail-Fast 红线：直连真实 Redis 实例，异常直接抛出，绝不吞异常降级。
 */
@Component
public class RedisRevocationSync {

    public static final String REVOKED_KEY_PREFIX = "auth:revoked:";
    public static final String PERM_CHANGED_KEY_PREFIX = "auth:perm_changed_at:";

    private final StringRedisTemplate redisTemplate;
    private final int defaultTtlSeconds;

    public RedisRevocationSync(
            StringRedisTemplate redisTemplate,
            @Value("${rag.redis.revocation-ttl-seconds:86400}") int defaultTtlSeconds
    ) {
        this.redisTemplate = Objects.requireNonNull(redisTemplate, "redisTemplate 不能为空");
        this.defaultTtlSeconds = Math.max(60, defaultTtlSeconds);
    }

    /**
     * 立即在真实 Redis 中标记该员工账号已吊销，并同步推进毫秒级权限变更边界（阻断存量业务 JWT、短票与活跃 SSE 连接）
     */
    public void markUserRevoked(String userId) {
        markUserRevoked(userId, defaultTtlSeconds);
    }

    public void markUserRevoked(String userId, int ttlSeconds) {
        if (userId == null || userId.isBlank()) {
            throw new IllegalArgumentException("userId 不能为空");
        }
        String uid = userId.trim();
        int effTtl = Math.max(60, ttlSeconds);
        long nowMs = System.currentTimeMillis();
        redisTemplate.opsForValue().set(REVOKED_KEY_PREFIX + uid, "1", Duration.ofSeconds(effTtl));
        redisTemplate.opsForValue().set(
                PERM_CHANGED_KEY_PREFIX + uid,
                String.valueOf(nowMs),
                Duration.ofSeconds(Math.max(effTtl, defaultTtlSeconds))
        );
    }

    /**
     * 解除指定员工的 Redis 吊销标记；若此前存在吊销标记，同步推进毫秒级权限边界，严防吊销前旧凭据复活
     */
    public void clearUserRevoked(String userId) {
        if (userId == null || userId.isBlank()) {
            return;
        }
        String uid = userId.trim();
        Boolean removed = redisTemplate.delete(REVOKED_KEY_PREFIX + uid);
        if (Boolean.TRUE.equals(removed)) {
            markPermissionChanged(uid, System.currentTimeMillis());
        }
    }

    /**
     * 当员工在 IdP 侧的角色或部门发生变更时，记录最新权限变更 13 位毫秒级时间戳，使不晚于该时间签发的旧票立即失效
     */
    public void markPermissionChanged(String userId, long changedEpochMilli) {
        if (userId == null || userId.isBlank()) {
            throw new IllegalArgumentException("userId 不能为空");
        }
        redisTemplate.opsForValue().set(
                PERM_CHANGED_KEY_PREFIX + userId.trim(),
                String.valueOf(changedEpochMilli),
                Duration.ofSeconds(defaultTtlSeconds)
        );
    }

    /**
     * 读取指定员工最近一次权限变更的毫秒级时间戳（若无记录则返回 0L）
     */
    public long getPermissionChangedAtMs(String userId) {
        if (userId == null || userId.isBlank()) {
            return 0L;
        }
        String raw = redisTemplate.opsForValue().get(PERM_CHANGED_KEY_PREFIX + userId.trim());
        if (raw == null || raw.isBlank()) {
            return 0L;
        }
        return Long.parseLong(raw.trim());
    }

    /**
     * 查询指定员工是否处于 Redis 吊销状态（供契约与集成测试直连核验）
     */
    public boolean isUserRevoked(String userId) {
        if (userId == null || userId.isBlank()) {
            return true;
        }
        return Boolean.TRUE.equals(redisTemplate.hasKey(REVOKED_KEY_PREFIX + userId.trim()));
    }
}
