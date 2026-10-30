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

export const useAuthStore = defineStore('auth', () => {
  const currentRoleId = ref<string>(localStorage.getItem(ROLE_KEY) || 'developer');
  const token = ref<string>(localStorage.getItem(TOKEN_KEY) || '');
  const interceptAlert = ref<string>('');

  const currentRole = () => MOCK_ROLES.find((r) => r.id === currentRoleId.value) || MOCK_ROLES[0];

  const switchRole = async (roleId: string) => {
    const target = MOCK_ROLES.find((r) => r.id === roleId);
    if (!target) return;

    currentRoleId.value = roleId;
    localStorage.setItem(ROLE_KEY, roleId);
    interceptAlert.value = '';

    try {
      const resp = await axios.post('/api/v1/auth/dev-token', {
        user_id: target.userId,
        tenant_id: 'default_tenant',
        department_name: target.deptName,
        department_path: target.deptPath !== '/' ? target.deptPath : undefined,
        roles: target.roles,
      });

      if (resp.data && resp.data.access_token) {
        token.value = resp.data.access_token;
        localStorage.setItem(TOKEN_KEY, resp.data.access_token);
        console.log(`[RBAC] 已成功切换至模拟身份: ${target.name}`);
      }
    } catch (err) {
      console.error('[RBAC] 获取模拟身份 Token 失败:', err);
    }
  };

  const initAuth = async () => {
    if (!token.value) {
      await switchRole(currentRoleId.value);
    }
  };

  return {
    currentRoleId,
    token,
    interceptAlert,
    MOCK_ROLES,
    currentRole,
    switchRole,
    initAuth,
  };
});
