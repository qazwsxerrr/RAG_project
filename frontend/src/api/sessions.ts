import { apiClient } from './client';
import type { CitationSource, ThinkingProcess } from '../types';

export interface BackendSessionItem {
  id: string;
  title: string;
  tenant_id: string;
  message_count?: number;
  created_at: string;
  updated_at: string;
}

export interface BackendMessageItem {
  id: string;
  session_id: string;
  role: 'user' | 'assistant';
  content: string;
  citations: CitationSource[];
  reasoning_content?: string;
  thinking_process?: ThinkingProcess;
  created_at: string;
}

export const sessionsApi = {
  /** 获取当前登录员工租户下的历史会话列表 */
  async listSessions(limit: number = 50, offset: number = 0): Promise<BackendSessionItem[]> {
    return apiClient.get('/sessions', {
      params: { limit, offset },
    });
  },

  /** 创建云端新会话 (租户由后端从 Token 派生) */
  async createSession(title: string = '新会话'): Promise<BackendSessionItem> {
    return apiClient.post('/sessions', {
      title,
    });
  },

  /** 物理删除云端会话及其全部级联消息 */
  async deleteSession(sessionId: string): Promise<{ status: string; message: string }> {
    return apiClient.delete(`/sessions/${sessionId}`);
  },

  /** 更新云端会话元数据（如显式重命名标题） */
  async updateSession(sessionId: string, title: string): Promise<BackendSessionItem> {
    return apiClient.patch(`/sessions/${sessionId}`, {
      title,
    });
  },

  /** 获取指定会话的历史多轮对话消息 */
  async getSessionMessages(sessionId: string, limit: number = 100): Promise<BackendMessageItem[]> {
    return apiClient.get(`/sessions/${sessionId}/messages`, {
      params: { limit },
    });
  },
};
