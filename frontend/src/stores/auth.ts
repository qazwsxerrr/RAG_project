import { defineStore } from 'pinia';
import { ref } from 'vue';
import { fetchToken, getMe, logout as apiLogout, type UserProfile } from '../api/auth';

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
   * 凭会话 Cookie 向 Java 鉴权中心换取业务 JWT (30min)，支持并发互斥锁
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
          try {
            currentUser.value = await getMe();
          } catch {
            // me 接口非阻塞
          }
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

  return {
    token,
    currentUser,
    interceptAlert,
    isPermissionDenied,
    refreshToken,
    initAuth,
    logout,
  };
});

/**
 * 为原生浏览器标签 (<img> / <a> / EventSource) 受控注入当前内存态 JWT
 * 仅对本系统后端代理路径 (/api/v1/...) 追加 ?token=，外部绝对链接保持原样
 */
export function withTokenUrl(url?: string | null): string {
  if (!url) return '';
  if (!url.startsWith('/api/')) return url;
  const authStore = useAuthStore();
  const tk = authStore.token;
  if (!tk) return url;

  const [pathAndQuery, hash] = url.split('#');
  const sep = pathAndQuery.includes('?') ? '&' : '?';
  const withParam = `${pathAndQuery}${sep}token=${encodeURIComponent(tk)}`;
  return hash ? `${withParam}#${hash}` : withParam;
}
