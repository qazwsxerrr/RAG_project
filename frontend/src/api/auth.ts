import axios from 'axios';

export const adminClient = axios.create({
  baseURL: '/api/admin',
  timeout: 15000,
  withCredentials: true,
  headers: {
    'Content-Type': 'application/json',
  },
});

export interface TokenResponse {
  access_token: string;
  token_type: string;
  expires_in: number;
}

export interface UserProfile {
  user_id: string;
  tenant_id: string;
  username: string;
  dept_path: string;
  data_scope: string;
  roles: string[];
  perms: string[];
  is_to_c: boolean;
  cv: number;
}

/** 凭会话 Cookie 换取业务 JWT (30min) */
export async function fetchToken(): Promise<TokenResponse> {
  const resp = await adminClient.post<TokenResponse>('/auth/token');
  return resp.data;
}

/** 获取当前登录用户画像与权限范围 */
export async function getMe(): Promise<UserProfile> {
  const resp = await adminClient.get<UserProfile>('/users/me');
  return resp.data;
}

/** 登出会话 */
export async function logout(): Promise<void> {
  await adminClient.post('/auth/logout');
}
