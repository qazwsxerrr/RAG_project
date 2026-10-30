import axios from 'axios';

export const apiClient = axios.create({
  baseURL: '/api/v1',
  timeout: 60000,
  headers: {
    'Content-Type': 'application/json',
  },
});

// 自动注入当前 RBAC 模拟凭据 (Bearer Token)
apiClient.interceptors.request.use((config) => {
  const token = localStorage.getItem('rag_auth_token');
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

// Fail-Fast: 拦截并抛出真实错误信息，绝不吞掉异常或返回默认假数据
apiClient.interceptors.response.use(
  (response) => response.data,
  (error) => {
    const errorDetail = error.response?.data?.detail || error.message || '网络请求失败';
    console.error(`[API 错误] ${error.config?.url}:`, errorDetail);
    return Promise.reject(new Error(errorDetail));
  }
);
