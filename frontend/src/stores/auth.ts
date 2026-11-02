import { defineStore } from 'pinia';
import { ref } from 'vue';
import axios from 'axios';

export interface MockRoleProfile {
  id: string;
  name: string;
  userId: string;
  deptName: string;
  deptPath: string;
  roles: string[];
  description: string;
}

export const MOCK_ROLES: MockRoleProfile[] = [
  {
    id: 'developer',
    name: '研发工程师',
    userId: 'dev_user_01',
    deptName: '技术部',
    deptPath: '/总公司/研发中心/技术部',
    roles: ['developer'],
    description: '仅允许检索技术部及公共文档',
  },
  {
    id: 'accountant',
    name: '财务主管',
    userId: 'fin_user_01',
    deptName: '财务部',
    deptPath: '/总公司/职能中心/财务部',
    roles: ['accountant'],
    description: '仅允许检索财务部及公共文档',
  },
  {
    id: 'admin',
    name: '超级管理员',
    userId: 'admin_user_01',
    deptName: '总经办',
    deptPath: '/',
    roles: ['admin'],
    description: '拥有全公司所有部门的穿透权限',
  },
];

const TOKEN_KEY = 'rag_auth_token';
const ROLE_KEY = 'rag_mock_role_id';

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
  const currentRoleId = ref<string>(localStorage.getItem(ROLE_KEY) || 'developer');
  const token = ref<string>(localStorage.getItem(TOKEN_KEY) || '');
  const interceptAlert = ref<string>('');

  const currentRole = () => MOCK_ROLES.find((r) => r.id === currentRoleId.value) || MOCK_ROLES[0];

  /**
   * 静默续签当前角色的 JWT 访问凭据 (自带互斥锁与并发去重)
   */
  const refreshToken = async (): Promise<string> => {
    if (refreshPromise) {
      return refreshPromise;
    }

    refreshPromise = (async () => {
      try {
        const target = currentRole();
        const resp = await axios.post('/api/v1/auth/dev-token', {
          user_id: target.userId,
          tenant_id: 'default_tenant',
          department_name: target.deptName,
          department_path: target.deptPath !== '/' ? target.deptPath : undefined,
          roles: target.roles,
        });

        if (resp.data && resp.data.access_token) {
          const newToken = resp.data.access_token;
          token.value = newToken;
          localStorage.setItem(TOKEN_KEY, newToken);
          console.log(`[RBAC] 已成功静默签发/续期身份凭据: ${target.name}`);
          return newToken;
        }
        throw new Error('未返回有效 access_token');
      } finally {
        refreshPromise = null;
      }
    })();

    return refreshPromise;
  };

  /**
   * 切换当前模拟身份并立即签发对应的 JWT 凭据
   */
  const switchRole = async (roleId: string) => {
    const target = MOCK_ROLES.find((r) => r.id === roleId);
    if (!target) return;

    currentRoleId.value = roleId;
    localStorage.setItem(ROLE_KEY, roleId);
    interceptAlert.value = '';

    try {
      await refreshToken();
    } catch (err) {
      console.error('[RBAC] 获取模拟身份 Token 失败:', err);
    }
  };

  /**
   * 初始化应用鉴权：若本地 Token 缺失或已过期，自动触发静默续签
   */
  const initAuth = async () => {
    if (!token.value || isTokenExpired(token.value)) {
      console.log('[RBAC] 本地 Token 缺失或已接近过期，自动执行静默签发...');
      try {
        await refreshToken();
      } catch (err) {
        console.error('[RBAC] 初始化鉴权静默换票失败:', err);
      }
    }
  };

  return {
    currentRoleId,
    token,
    interceptAlert,
    MOCK_ROLES,
    currentRole,
    refreshToken,
    switchRole,
    initAuth,
  };
});
