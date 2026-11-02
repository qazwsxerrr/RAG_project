import axios from 'axios';
import { useAuthStore, isTokenExpired } from '../stores/auth';

export const apiClient = axios.create({
  baseURL: '/api/v1',
  timeout: 60000,
  headers: {
    'Content-Type': 'application/json',
  },
});

// 自动注入当前 RBAC 模拟凭据 (Bearer Token)，前置预判并按需静默续签
apiClient.interceptors.request.use(async (config) => {
  let token = localStorage.getItem('rag_auth_token');
  if (token && isTokenExpired(token)) {
    try {
      const authStore = useAuthStore();
      token = await authStore.refreshToken();
    } catch (e) {
      console.warn('[API 请求拦截] 前置换签异常:', e);
    }
  }

  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

// Fail-Fast: 拦截并抛出真实错误信息，若遇到 401 凭据失效则自动静默换票重试一次
apiClient.interceptors.response.use(
  (response) => response.data,
  async (error) => {
    const originalRequest = error.config;
    const status = error.response?.status;
    const errorDetail = error.response?.data?.detail || error.message || '网络请求失败';

    // 遇到 401 且尚未重试过时，触发静默换票并透明重试一次
    if (status === 401 && originalRequest && !originalRequest._retry) {
      originalRequest._retry = true;
      try {
        console.warn(`[API 鉴权拦截] 检测到 401 凭据失效 (${originalRequest.url})，执行静默换票重试...`);
        const authStore = useAuthStore();
        const newToken = await authStore.refreshToken();
        originalRequest.headers.Authorization = `Bearer ${newToken}`;
        return apiClient(originalRequest);
      } catch (refreshErr) {
        console.error('[API 鉴权拦截] 静默换票失败:', refreshErr);
      }
    }

    console.error(`[API 错误] ${error.config?.url}:`, errorDetail);
    return Promise.reject(new Error(errorDetail));
  }
);
