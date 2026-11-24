package com.enterprise.rag.admin;

import com.enterprise.rag.admin.identity.RedisRevocationSync;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import java.util.*;

import static org.junit.jupiter.api.Assertions.*;

class RedisRevocationTest {
    private RedisTestSupport redis;
    private String uid;

    @BeforeEach
    void setUp() throws Exception {
        redis = new RedisTestSupport();
        uid = "revocation-test-" + UUID.randomUUID();
        redis.userIds.add(uid);
    }

    @AfterEach
    void tearDown() {
        if (redis != null) redis.close();
    }

    @Test
    void revocationWritesBothKeysAndTheirLifetimes() {
        redis.revocations.markUserRevoked(uid, 600);
        assertTrue(redis.revocations.isUserRevoked(uid));
        assertTrue(redis.revocations.getPermissionChangedAtMs(uid) > 0);
        assertTrue(redis.client.getExpire(RedisRevocationSync.REVOKED_KEY_PREFIX + uid) > 0);
        assertTrue(redis.client.getExpire(RedisRevocationSync.PERM_CHANGED_KEY_PREFIX + uid) > 0);
    }

    @Test
    void restoringRevokedUserInvalidatesEarlierCredentials() {
        redis.revocations.markUserRevoked(uid);
        long beforeClear = redis.revocations.getPermissionChangedAtMs(uid);
        redis.revocations.clearUserRevoked(uid);
        assertFalse(redis.revocations.isUserRevoked(uid));
        assertTrue(redis.revocations.getPermissionChangedAtMs(uid) >= beforeClear);
    }

    @Test
    void permissionChangePersistsMillisecondBoundary() {
        long changedAt = System.currentTimeMillis();
        redis.revocations.markPermissionChanged(uid, changedAt);
        assertEquals(changedAt, redis.revocations.getPermissionChangedAtMs(uid));
        assertFalse(redis.revocations.isUserRevoked(uid));
    }
}
