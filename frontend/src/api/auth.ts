import axios from 'axios';
import type { DataScope, KbAction } from '../types/authz';

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
  dept_paths: string[];
  scope_by_action: Partial<Record<KbAction, DataScope>>;
  position_code: string;
  clearance_level: number;
  authz_ver: number;
  policy_epoch: number;
  roles: string[];
  perms: string[];
  is_to_c: boolean;
  cv: number;
}

/** 凭会话 Cookie 换取 cv=2 业务 JWT (10min) */
export async function fetchToken(): Promise<TokenResponse> {
  const resp = await adminClient.post<TokenResponse>('/auth/token');
  return resp.data;
}

/** 登出会话 */
export async function logout(): Promise<void> {
  await adminClient.post('/auth/logout');
}
