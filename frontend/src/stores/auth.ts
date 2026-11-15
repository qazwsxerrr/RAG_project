import { defineStore } from 'pinia';
import { ref } from 'vue';
import { fetchToken, logout as apiLogout, type UserProfile } from '../api/auth';

export const PERMISSIONS = {
  ALL: '*',
  KB_RETRIEVE: 'kb:retrieve',
  DOC_UPLOAD: 'doc:upload',
  DOC_DELETE: 'doc:delete',
  DOC_REVIEW: 'doc:review',
  SYSTEM_ADMIN: 'system:admin',
} as const;

/** 解析 JWT 载荷信息 */
export function parseJwtPayload(token: string): Record<string, any> | null {
  try {
    const parts = token.split('.');
    if (parts.length !== 3) return null;
    const base64Url = parts[1];
    const base64 = base64Url.replace(/-/g, '+').replace(/_/g, '/');
    const jsonPayload = decodeURIComponent(
      atob(base64)
        .split('')
        .map((c) => '%' + ('00' + c.charCodeAt(0).toString(16)).slice(-2))
        .join('')
    );
    return JSON.parse(jsonPayload);
  } catch {
    return null;
  }
}

/**
 * 校验 JWT 是否过期 (默认预留 offsetSeconds 秒缓冲时间进行提前静默续期)
 */
export function isTokenExpired(token: string, offsetSeconds = 60): boolean {
  if (!token || !token.trim()) return true;
  const payload = parseJwtPayload(token);
  if (!payload || !payload.exp) return true;
  const now = Math.floor(Date.now() / 1000);
  return payload.exp <= now + offsetSeconds;
}

// 全局静默换票单例互斥锁，防止多请求并发同时打穿换票接口
let refreshPromise: Promise<string> | null = null;

export const useAuthStore = defineStore('auth', () => {
  // Phase-6 核心要求：业务 JWT 仅存在 Pinia 内存态，严禁持久化落 localStorage (防御 XSS)
  const token = ref<string>('');
  const currentUser = ref<UserProfile | null>(null);
  const interceptAlert = ref<string>('');
  const isPermissionDenied = ref<boolean>(false);

  /**
   * 凭会话 Cookie 向 Java 鉴权中心换取业务 JWT (10min)，直接从已签名 JWT 载荷同步当前员工画像
   */
  const refreshToken = async (): Promise<string> => {
    if (refreshPromise) {
      return refreshPromise;
    }

    refreshPromise = (async () => {
      try {
        const resp = await fetchToken();
        if (resp && resp.access_token) {
          token.value = resp.access_token;
          isPermissionDenied.value = false;
          const payload = parseJwtPayload(resp.access_token);
          if (!payload || !payload.sub) {
            throw new Error('鉴权中心返回的 JWT 载荷缺少必要身份字段');
          }
          currentUser.value = {
            user_id: String(payload.sub),
            tenant_id: String(payload.tenant_id || 'default_tenant'),
            username: String(payload.username || ''),
            dept_path: String(payload.dept_path || ''),
            data_scope: String(payload.data_scope || 'DEPT_TREE'),
            roles: Array.isArray(payload.roles) ? payload.roles : [],
            perms: Array.isArray(payload.perms) ? payload.perms : [],
            is_to_c: Boolean(payload.is_to_c),
            cv: Number(payload.cv ?? 1),
          };
          return resp.access_token;
        }
        throw new Error('鉴权中心未返回有效 access_token');
      } catch (err: any) {
        token.value = '';
        currentUser.value = null;
        // 会话失效，跳转至企业 IdP 单点登录
        console.warn('[RBAC] 会话已失效或换票失败，重定向至企业 IdP SSO...');
        window.location.href = '/oauth2/authorization/corp-sso';
        throw err;
      } finally {
        refreshPromise = null;
      }
    })();

    return refreshPromise;
  };

  /**
   * 初始化应用鉴权：若内存中无票或已接近过期，自动触发静默换票
   */
  const initAuth = async () => {
    if (!token.value || isTokenExpired(token.value)) {
      try {
        await refreshToken();
      } catch (err) {
        console.error('[RBAC] 初始化鉴权静默换票失败:', err);
      }
    }
  };

  /**
   * 登出当前员工会话并强制跳转企业 IdP 重新认证 (prompt=login 强制弹出账号密码页切换账号)
   */
  const logout = async () => {
    try {
      await apiLogout();
    } catch {
      // 忽略登出网络错误
    } finally {
      token.value = '';
      currentUser.value = null;
      interceptAlert.value = '';
      isPermissionDenied.value = false;
      localStorage.removeItem('rag_active_session_id');
      sessionStorage.setItem('rag_auth_redirect', window.location.pathname || '/documents');
      window.location.href = '/oauth2/authorization/corp-sso?prompt=login';
    }
  };

  const hasPermission = (perm: string): boolean => {
    const perms = currentUser.value?.perms || [];
    return perms.includes(PERMISSIONS.ALL) || perms.includes(perm);
  };

  return {
    token,
    currentUser,
    interceptAlert,
    isPermissionDenied,
    hasPermission,
    refreshToken,
    initAuth,
    logout,
  };
});
