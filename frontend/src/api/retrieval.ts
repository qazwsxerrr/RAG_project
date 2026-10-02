import { apiClient } from './client';
import type { ThinkingProcess, CitationSource } from '../types';

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
      const response = await fetch('/api/v1/retrieval/chat', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          ...payload,
          stream: true,
        }),
        signal: abortSignal,
      });

      if (!response.ok) {
        const errorText = await response.text();
        throw new Error(`问答服务异常 (${response.status}): ${errorText}`);
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
