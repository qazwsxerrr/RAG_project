import { defineStore } from 'pinia';
import { ref } from 'vue';
import { fetchToken, logout as apiLogout, type UserProfile } from '../api/auth';
import type { DataScope, KbAction } from '../types/authz';

export const PERMISSIONS = {
  ALL: '*',
  KB_RETRIEVE: 'kb:retrieve',
  DOC_UPLOAD: 'doc:upload',
  DOC_DELETE: 'doc:delete',
  DOC_REVIEW: 'doc:review',
  SYSTEM_ADMIN: 'system:admin',
  KB_MANAGE: 'kb:manage',
  KB_GRANT: 'kb:grant',
  USER_AUTHZ_MANAGE: 'user:authz:manage',
  SECURITY_CLASSIFY: 'security:classify',
  SECURITY_DECLASSIFY: 'security:declassify',
} as const;

export const GOVERNANCE_PERMISSIONS = [
  PERMISSIONS.KB_MANAGE,
  PERMISSIONS.KB_GRANT,
  PERMISSIONS.USER_AUTHZ_MANAGE,
  PERMISSIONS.SYSTEM_ADMIN,
] as const;

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

export function isTokenExpired(token: string, offsetSeconds = 60): boolean {
  if (!token || !token.trim()) return true;
  const payload = parseJwtPayload(token);
  if (!payload || !payload.exp) return true;
  const now = Math.floor(Date.now() / 1000);
  return payload.exp <= now + offsetSeconds;
}

let refreshPromise: Promise<string> | null = null;

const SSO_LOOP_STORAGE_KEY = 'rag_sso_redirect_timestamps';
const SSO_LOOP_WINDOW_MS = 15000;
const SSO_MAX_REDIRECTS_IN_WINDOW = 2;

export interface AuthErrorState {
  status: number | null;
  message: string;
  isLoopBlocked?: boolean;
}

export interface RefreshTokenOptions {
  redirectOnUnauthorized?: boolean;
}

function readRecentSsoRedirects(): number[] {
  try {
    const raw = sessionStorage.getItem(SSO_LOOP_STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    const cutoff = Date.now() - SSO_LOOP_WINDOW_MS;
    return parsed.map(Number).filter((ts) => Number.isFinite(ts) && ts >= cutoff);
  } catch {
    return [];
  }
}

function extractAuthErrorInfo(err: any): { status: number | null; message: string } {
  const status = typeof err?.response?.status === 'number' ? err.response.status : null;
  const serverMsg =
    err?.response?.data?.message ||
    err?.response?.data?.detail ||
    err?.response?.data?.error;
  const rawMsg = typeof serverMsg === 'string' && serverMsg.trim() ? serverMsg.trim() : err?.message;
  const message =
    rawMsg ||
    (status ? `鉴权服务返回异常状态 (HTTP ${status})` : '无法连接鉴权服务，请检查网络或服务状态');
  return { status, message };
}

export const useAuthStore = defineStore('auth', () => {
  // 业务 JWT 仅存在 Pinia 内存态，严禁持久化落 localStorage
  const token = ref<string>('');
  const currentUser = ref<UserProfile | null>(null);
  const interceptAlert = ref<string>('');
  const isPermissionDenied = ref<boolean>(false);
  const authError = ref<AuthErrorState | null>(null);

  const clearSsoLoopGuard = () => {
    try {
      sessionStorage.removeItem(SSO_LOOP_STORAGE_KEY);
    } catch {
      // ignore storage access errors
    }
  };

  const redirectToSso = (options?: { redirectPath?: string }): boolean => {
    if (options?.redirectPath && options.redirectPath !== '/auth/callback') {
      sessionStorage.setItem('rag_auth_redirect', options.redirectPath);
    }

    const recent = readRecentSsoRedirects();
    if (recent.length >= SSO_MAX_REDIRECTS_IN_WINDOW) {
      authError.value = {
        status: authError.value?.status ?? 401,
        message: '检测到连续 SSO 登录重定向循环，已自动停止跳转。请点击“重新登录 / 切换账号”或检查浏览器 Cookie 与鉴权服务状态。',
        isLoopBlocked: true,
      };
      console.error('[RBAC] 检测到 SSO 重定向循环，已熔断自动跳转');
      return false;
    }

    try {
      sessionStorage.setItem(SSO_LOOP_STORAGE_KEY, JSON.stringify([...recent, Date.now()]));
    } catch {
      // ignore storage access errors
    }
    window.location.href = '/oauth2/authorization/corp-sso';
    return true;
  };

  const refreshToken = async (options?: RefreshTokenOptions): Promise<string> => {
    if (refreshPromise) {
      return refreshPromise;
    }

    const shouldRedirectOn401 =
      options?.redirectOnUnauthorized !== false &&
      window.location.pathname !== '/auth/callback';

    refreshPromise = (async () => {
      try {
        const resp = await fetchToken();
        if (resp && resp.access_token) {
          token.value = resp.access_token;
          isPermissionDenied.value = false;
          authError.value = null;
          clearSsoLoopGuard();
          const payload = parseJwtPayload(resp.access_token);
          if (!payload || !payload.sub) {
            throw new Error('鉴权中心返回的 JWT 载荷缺少必要身份字段');
          }
          const rawDeptPaths = Array.isArray(payload.dept_paths) ? payload.dept_paths.map(String) : [];
          const primaryDept = typeof payload.dept_path === 'string' ? payload.dept_path : (rawDeptPaths[0] || '');
          const rawScopeMap =
            payload.scope_by_action && typeof payload.scope_by_action === 'object'
              ? (payload.scope_by_action as Partial<Record<KbAction, DataScope>>)
              : {};

          currentUser.value = {
            user_id: String(payload.sub),
            tenant_id: String(payload.tenant_id || 'default_tenant'),
            username: String(payload.username || ''),
            dept_path: primaryDept,
            dept_paths: rawDeptPaths,
            scope_by_action: rawScopeMap,
            position_code: String(payload.position_code || 'STAFF'),
            clearance_level: Number(payload.clearance_level ?? 2),
            authz_ver: Number(payload.authz_ver ?? 1),
            policy_epoch: Number(payload.policy_epoch ?? 1),
            roles: Array.isArray(payload.roles) ? payload.roles.map(String) : [],
            perms: Array.isArray(payload.perms) ? payload.perms.map(String) : [],
            is_to_c: Boolean(payload.is_to_c),
            cv: Number(payload.cv ?? 2),
          };
          return resp.access_token;
        }
        throw new Error('鉴权中心未返回有效 access_token');
      } catch (err: any) {
        const { status, message } = extractAuthErrorInfo(err);
        authError.value = { status, message };
        // 仅当明确为 401/403 认证或授权失效时清空内存态登录信息；
        // 网络超时 (status === null) 或 5xx 临时故障保留当前用户视图，避免清空侧边栏与菜单。
        if (status === 401 || status === 403) {
          token.value = '';
          currentUser.value = null;
        }
        // 仅当明确为 401 会话缺失/失效且不在回调页时才跳向 SSO；
        // 403/5xx/网络异常绝不静默跳回 SSO，避免在 IdP 已登录态下触发毫秒级死循环。
        if (shouldRedirectOn401 && status === 401) {
          console.warn('[RBAC] 会话已失效 (401)，重定向至企业 IdP SSO...');
          redirectToSso({ redirectPath: window.location.pathname + window.location.search });
        } else {
          console.error(`[RBAC] 换票失败 (status=${status ?? 'network'}):`, message);
        }
        throw err;
      } finally {
        refreshPromise = null;
      }
    })();

    return refreshPromise;
  };

  const logout = async () => {
    let rpLogoutUrl = '';
    try {
      const resp = await apiLogout();
      if (resp && typeof resp.logout_url === 'string' && resp.logout_url.trim()) {
        rpLogoutUrl = resp.logout_url.trim();
      }
    } catch {
      // ignore
    } finally {
      token.value = '';
      currentUser.value = null;
      interceptAlert.value = '';
      isPermissionDenied.value = false;
      localStorage.removeItem('rag_active_session_id');
      sessionStorage.removeItem('rag_auth_redirect');
      clearSsoLoopGuard();
      if (rpLogoutUrl) {
        window.location.href = rpLogoutUrl;
      } else {
        window.location.href = '/oauth2/authorization/corp-sso';
      }
    }
  };

  const hasPermission = (perm: string): boolean => {
    const perms = currentUser.value?.perms || [];
    return perms.includes(PERMISSIONS.ALL) || perms.includes(perm);
  };

  const hasAnyPermission = (permsToCheck: readonly string[]): boolean => {
    return permsToCheck.some((p) => hasPermission(p));
  };

  const canAccessGovernance = (): boolean => hasAnyPermission(GOVERNANCE_PERMISSIONS);

  return {
    token,
    currentUser,
    interceptAlert,
    isPermissionDenied,
    authError,
    hasPermission,
    hasAnyPermission,
    canAccessGovernance,
    refreshToken,
    logout,
    redirectToSso,
  };
});
