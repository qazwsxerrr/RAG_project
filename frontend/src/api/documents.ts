import { apiClient } from './client';
import type { DocumentItem, ChunkItem, PipelineSnapshot } from '../types';
import { useAuthStore } from '../stores/auth';

export interface StreamTicketResult {
  ticket: string;
  doc_id: string;
  purpose: 'preview' | 'events';
  expires_in: number;
}

export interface ManagedPipelineStream {
  close: () => void;
}

export const documentsApi = {
  /** 文档上传接入并启动流水线 */
  async uploadDocument(formData: FormData): Promise<{
    doc_id: string;
    file_name: string;
    kb_id: string;
    kb_name: string;
    classification_level: number;
    preview_url: string;
    status: string;
  }> {
    return apiClient.post('/documents/upload', formData, {
      headers: {
        'Content-Type': 'multipart/form-data',
      },
      timeout: 120000,
    });
  },

  /** 获取所有入库/运行中文档列表 */
  async getDocuments(params?: { kb_id?: string; classification_level?: number }): Promise<DocumentItem[]> {
    return apiClient.get('/documents', { params });
  },

  /** 单篇文档在线独立改密 */
  async updateDocumentClassification(
    docId: string,
    classificationLevel: number,
    reason?: string
  ): Promise<{
    doc_id: string;
    kb_id: string;
    previous_level: number;
    classification_level: number;
    effective_classification_level: number;
    policy_epoch: number;
    message: string;
  }> {
    return apiClient.patch(`/documents/${docId}/classification`, {
      classification_level: classificationLevel,
      reason,
    });
  },

  /** 删除文档及其全部切片 */
  async deleteDocument(docId: string): Promise<{ status: string; message: string }> {
    return apiClient.delete(`/documents/${docId}`);
  },

  /** 获取指定文档已切分入库的全部切片详情 (支持溯源定位与内容核对) */
  async getDocumentChunks(docId: string): Promise<ChunkItem[]> {
    return apiClient.get(`/documents/${docId}/chunks`);
  },

  /** 获取流水线静态快照 (对应 Img 04 重新连接与初始状态) */
  async getPipelineStatus(docId: string): Promise<PipelineSnapshot> {
    return apiClient.get(`/documents/${docId}/status`);
  },

  /** 提交人工审核确认 (对应 Img 07 ReviewDialog 确认并继续) */
  async submitReview(docId: string, items: Array<{ item_id: string; user_description: string; remark?: string; approved?: boolean }>): Promise<any> {
    return apiClient.post(`/documents/${docId}/review`, {
      doc_id: docId,
      items,
    });
  },

  /** 解决防重冲突 (覆盖升级 / 独立新建 / 取消) */
  async resolveDuplicate(docId: string, action: 'overwrite' | 'create_new' | 'cancel', matchedDocId?: string): Promise<any> {
    return apiClient.post(`/documents/${docId}/resolve_duplicate`, {
      action,
      matched_doc_id: matchedDocId,
    });
  },

  /** 流水线断点重试 (支持指定步骤重试，如从 Step 9 切片断点续跑) */
  async retryPipeline(docId: string, fromStep?: number): Promise<{ doc_id: string; status: string; from_step: number; message: string }> {
    return apiClient.post(`/documents/${docId}/retry`, fromStep ? { from_step: fromStep } : {});
  },

  /** 签发绑定单文档与单一用途的短期受限票据 (TTL 60s) */
  async createStreamTicket(docId: string, purpose: 'preview' | 'events'): Promise<StreamTicketResult> {
    return apiClient.post(`/documents/${docId}/stream-ticket`, { purpose });
  },

  /**
   * 创建受控流水线 SSE 长连接：
   * 1. 绝不在 URL 暴露通用业务 JWT，仅携带 60s 单文档受限 ticket；
   * 2. 网络瞬断或短票过期断开时，自动按指数退避重新换取全新短票并携带 last_event_id 无缝续传历史事件；
   * 3. 收到 auth_revoked 或换票失败（如账号停用、越权、文档不存在）时立即停止重连并上报真实异常。
   */
  async createPipelineEventSource(
    docId: string,
    onEvent: (eventName: string, data: any) => void,
    onFatalError?: (err: any) => void
  ): Promise<ManagedPipelineStream> {
    const knownEvents = [
      'initial_state',
      'pipeline_queued',
      'step_started',
      'step_progress',
      'step_completed',
      'step_failed',
      'review_required',
      'review_completed',
      'duplicate_warning',
      'pipeline_resumed',
      'pipeline_completed',
      'pipeline_failed',
      'pipeline_cancelled',
    ];

    const terminalEvents = new Set(['pipeline_completed', 'pipeline_failed', 'pipeline_cancelled']);
    let activeSource: EventSource | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    let manuallyClosed = false;
    let lastEventId = 0;
    let retryAttempt = 0;
    const maxRetries = 5;

    const cleanupCurrent = () => {
      if (reconnectTimer) {
        clearTimeout(reconnectTimer);
        reconnectTimer = null;
      }
      if (activeSource) {
        activeSource.close();
        activeSource = null;
      }
    };

    const closeAll = () => {
      manuallyClosed = true;
      cleanupCurrent();
    };

    const openWithFreshTicket = async (): Promise<void> => {
      if (manuallyClosed) return;
      cleanupCurrent();

      const ticketResp = await this.createStreamTicket(docId, 'events');
      if (manuallyClosed) return;

      const query = new URLSearchParams({ ticket: ticketResp.ticket });
      if (lastEventId > 0) {
        query.set('last_event_id', String(lastEventId));
      }
      const source = new EventSource(
        `/api/v1/documents/${encodeURIComponent(docId)}/events?${query.toString()}`
      );
      activeSource = source;

      source.onopen = () => {
        retryAttempt = 0;
      };

      source.addEventListener('auth_revoked', () => {
        if (manuallyClosed || activeSource !== source) return;
        closeAll();
        const authStore = useAuthStore();
        authStore.logout();
      });

      source.addEventListener('auth_expired', () => {
        if (manuallyClosed || activeSource !== source) return;
        cleanupCurrent();
        // 换票请求经现有鉴权拦截器续期，并携带 last_event_id 恢复事件消费。
        void openWithFreshTicket().catch((ticketErr) => {
          if (manuallyClosed) return;
          closeAll();
          onFatalError?.(ticketErr);
        });
      });

      knownEvents.forEach((evtName) => {
        source.addEventListener(evtName, (e: MessageEvent) => {
          if (e.lastEventId) {
            const parsedId = parseInt(e.lastEventId, 10);
            if (!Number.isNaN(parsedId) && parsedId > lastEventId) {
              lastEventId = parsedId;
            }
          }
          let payload;
          try {
            payload = JSON.parse(e.data);
          } catch (err) {
            closeAll();
            onFatalError?.(err);
            return;
          }
          if (evtName === 'initial_state' && payload && ['completed', 'active', 'failed', 'cancelled'].includes(payload.overall_status)) {
            closeAll();
          } else if (terminalEvents.has(evtName)) {
            closeAll();
          }
          onEvent(evtName, payload);
        });
      });

      source.onerror = (err) => {
        if (manuallyClosed || activeSource !== source) return;
        // 立即关闭旧实例（杜绝浏览器用已过期的旧 ticket 死循环重连）
        source.close();
        activeSource = null;

        if (retryAttempt >= maxRetries) {
          manuallyClosed = true;
          onFatalError?.(err);
          return;
        }

        const delayMs = Math.min(1000 * Math.pow(2, retryAttempt), 10000);
        retryAttempt += 1;
        reconnectTimer = setTimeout(async () => {
          if (manuallyClosed) return;
          try {
            await openWithFreshTicket();
          } catch (ticketErr) {
            manuallyClosed = true;
            onFatalError?.(ticketErr);
          }
        }, delayMs);
      };
    };

    await openWithFreshTicket();
    return { close: closeAll };
  }
};
