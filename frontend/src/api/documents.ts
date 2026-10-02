import { apiClient } from './client';
import type { DepartmentItem, DocumentItem, ChunkItem, PipelineSnapshot } from '../types';

export const documentsApi = {
  /** 获取可用部门列表（支持下拉与动态匹配） */
  async getDepartments(): Promise<DepartmentItem[]> {
    return apiClient.get('/documents/departments');
  },

  /** 文档上传接入并启动流水线 */
  async uploadDocument(formData: FormData): Promise<{ doc_id: string; file_name: string; oss_url: string; status: string }> {
    return apiClient.post('/documents/upload', formData, {
      headers: {
        'Content-Type': 'multipart/form-data',
      },
      timeout: 120000,
    });
  },

  /** 获取所有入库/运行中文档列表 */
  async getDocuments(): Promise<DocumentItem[]> {
    return apiClient.get('/documents');
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

  /** 创建流水线 SSE 长连接 (监听 11 节点流转事件) */
  createPipelineEventSource(
    docId: string,
    onEvent: (eventName: string, data: any) => void,
    onError?: (err: any) => void
  ): EventSource {
    const eventSource = new EventSource(`/api/v1/documents/${docId}/events`);

    const knownEvents = [
      'initial_state',
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
      'pipeline_cancelled'
    ];

    knownEvents.forEach((evtName) => {
      eventSource.addEventListener(evtName, (e: MessageEvent) => {
        try {
          const parsed = JSON.parse(e.data);
          // 实时事件的 SSE data 是 { event, data } 包装；initial_state 直接是快照
          const payload =
            parsed && typeof parsed === 'object' && parsed.event && parsed.data && typeof parsed.data === 'object'
              ? parsed.data
              : parsed;
          onEvent(evtName, payload);
        } catch {
          onEvent(evtName, e.data);
        }
      });
    });

    eventSource.onerror = (err) => {
      console.warn(`[SSE 断线/重连中] doc_id: ${docId}`, err);
      if (onError) onError(err);
    };

    return eventSource;
  }
};
