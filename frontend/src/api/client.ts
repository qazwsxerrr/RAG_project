import axios from 'axios';
import { useAuthStore, isTokenExpired } from '../stores/auth';

export const apiClient = axios.create({
  baseURL: '/api/v1',
  timeout: 60000,
  headers: {
    'Content-Type': 'application/json',
  },
});

// 自动注入当前内存态业务 JWT 凭据 (Bearer Token)，前置预判并按需静默续签
apiClient.interceptors.request.use(async (config) => {
  const authStore = useAuthStore();
  let currentToken = authStore.token;

  if (currentToken && isTokenExpired(currentToken)) {
    try {
      currentToken = await authStore.refreshToken();
    } catch (e) {
      console.warn('[API 请求拦截] 前置换签异常:', e);
    }
  }

  if (currentToken) {
    config.headers.Authorization = `Bearer ${currentToken}`;
  }
  return config;
});

// Fail-Fast: 拦截并抛出真实错误信息；遇到 401 透明重试一次，遇到 403 明确标记无权限拒绝重试
apiClient.interceptors.response.use(
  (response) => response.data,
  async (error) => {
    const originalRequest = error.config;
    const status = error.response?.status;
    const errorDetail = error.response?.data?.detail || error.message || '网络请求失败';
    const authStore = useAuthStore();

    // 遇到 429 高并发限流或并发护栏拦截：透传真实 Retry-After 冷却提示并弹顶栏提醒
    if (status === 429) {
      const retryAfter = error.response?.headers?.['retry-after'];
      const msg = retryAfter && !String(errorDetail).includes('秒后重试')
        ? `${errorDetail}（建议等待 ${retryAfter} 秒后重试）`
        : errorDetail;
      authStore.interceptAlert = msg;
      setTimeout(() => {
        if (authStore.interceptAlert === msg) {
          authStore.interceptAlert = '';
        }
      }, 4500);
      return Promise.reject(new Error(msg));
    }

    // 遇到 403 权限不足：标记 isPermissionDenied，绝不重试与换票 (§2.4 规范)
    if (status === 403) {
      authStore.isPermissionDenied = true;
      authStore.interceptAlert = errorDetail;
      console.warn(`[API 权限拦截 403] 越权或缺乏权限: ${errorDetail}`);
      return Promise.reject(new Error(errorDetail));
    }

    // 遇到 401 且尚未重试过时，触发静默换票并透明重试一次
    if (status === 401 && originalRequest && !originalRequest._retry) {
      originalRequest._retry = true;
      try {
        console.warn(`[API 鉴权拦截 401] 凭据失效 (${originalRequest.url})，执行静默换票重试...`);
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
