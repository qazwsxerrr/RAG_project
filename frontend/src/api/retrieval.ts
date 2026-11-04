import { apiClient } from './client';
import type { ThinkingProcess, CitationSource } from '../types';
import { useAuthStore, isTokenExpired } from '../stores/auth';

export interface ChatQueryPayload {
  query: string;
  conversation_id?: string;
  department_scope?: string[];
  category_scope?: string;
  tags_scope?: string[];
  scene_type?: string;
  enable_hyde?: boolean;
  stream?: boolean;
  history?: Array<{ role: string; content: string }>;
}

export const retrievalApi = {
  /** 纯检索接口 (阶段 1 ~ 阶段 6)：获取经过多阶段漏斗精选的切片与思考流 */
  async search(payload: ChatQueryPayload) {
    return apiClient.post('/retrieval/search', {
      ...payload,
      stream: false,
    });
  },

  /** 阶段 1 ~ 阶段 8 全流程智能问答流式打字机接口 (POST SSE 协议) */
  async streamChat(
    payload: ChatQueryPayload,
    callbacks: {
      onThinking?: (thinking: ThinkingProcess) => void;
      onCitations?: (citations: CitationSource[]) => void;
      onReasoning?: (token: string) => void;
      onMessage?: (token: string) => void;
      onDone?: (meta: { total_overall_ms: number; total_retrieve_ms: number }) => void;
      onError?: (error: Error) => void;
    },
    abortSignal?: AbortSignal
  ) {
    try {
      const authStore = useAuthStore();
      let token = authStore.token;
      // 前置检查：若内存中 Token 缺失或已接近过期，提前自动静默续期
      if (!token || isTokenExpired(token)) {
        try {
          token = await authStore.refreshToken();
        } catch (e) {
          console.warn('[Retrieval] 前置静默换签异常:', e);
        }
      }

      const headers: Record<string, string> = {
        'Content-Type': 'application/json',
      };
      if (token) {
        headers['Authorization'] = `Bearer ${token}`;
      }

      let response = await fetch('/api/v1/retrieval/chat', {
        method: 'POST',
        headers,
        body: JSON.stringify({
          ...payload,
          stream: true,
        }),
        signal: abortSignal,
      });

      // 遇到 401 凭据失效（例如长时间休眠后被掐断），自动静默换卡并透明重发一次
      if (response.status === 401) {
        console.warn('[Retrieval] 检测到 401 凭据失效，自动触发静默续签并重试...');
        try {
          const newToken = await authStore.refreshToken();
          headers['Authorization'] = `Bearer ${newToken}`;
          response = await fetch('/api/v1/retrieval/chat', {
            method: 'POST',
            headers,
            body: JSON.stringify({
              ...payload,
              stream: true,
            }),
            signal: abortSignal,
          });
        } catch (retryErr) {
          console.error('[Retrieval] 401 静默重试失败:', retryErr);
        }
      }

      if (response.status === 403) {
        let errorMsg = '您无权访问所选部门或执行该检索操作';
        try {
          const errJson = await response.json();
          errorMsg = errJson.detail || errorMsg;
        } catch {
          // ignore
        }
        authStore.interceptAlert = errorMsg;
        setTimeout(() => {
          if (authStore.interceptAlert === errorMsg) {
            authStore.interceptAlert = '';
          }
        }, 5000);
        throw new Error(errorMsg);
      }

      if (!response.ok) {
        let errorMsg = `HTTP ${response.status}`;
        try {
          const errJson = await response.json();
          errorMsg = errJson.detail || errorMsg;
        } catch {
          errorMsg = await response.text();
        }
        throw new Error(errorMsg);
      }

      if (!response.body) {
        throw new Error('问答服务未返回数据流');
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder('utf-8');
      let buffer = '';
      let currentEvent = 'message';

      while (true) {
        const { value, done } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop() || '';

        for (let i = 0; i < lines.length; i++) {
          const line = lines[i].trim();
          if (!line) continue;

          if (line.startsWith('event:')) {
            currentEvent = line.slice(6).trim();
            continue;
          }

          if (line.startsWith('data:')) {
            const dataStr = line.slice(5).trim();
            try {
              const data = JSON.parse(dataStr);
              if (currentEvent === 'thinking') {
                callbacks.onThinking?.(data);
              } else if (currentEvent === 'citations') {
                callbacks.onCitations?.(data);
              } else if (currentEvent === 'reasoning') {
                if (data.content) {
                  callbacks.onReasoning?.(data.content);
                }
              } else if (currentEvent === 'message') {
                if (data.content) {
                  callbacks.onMessage?.(data.content);
                }
              } else if (currentEvent === 'done') {
                callbacks.onDone?.(data);
              } else if (currentEvent === 'error') {
                callbacks.onError?.(new Error(data.error || '大模型推理失败'));
              }
            } catch (err) {
              console.warn('[SSE 解析异常]', line, err);
            }
          }
        }
      }
    } catch (err: any) {
      if (err.name === 'AbortError') {
        console.log('[用户终止生成]');
        return;
      }
      callbacks.onError?.(err instanceof Error ? err : new Error(String(err)));
    }
  },
};
