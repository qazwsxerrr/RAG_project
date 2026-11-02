import { apiClient } from './client';

export interface SystemHealth {
  status: string;
  app_name: string;
  environment: string;
  debug: boolean;
  model_name: string;
  embedding_model?: string;
  db_status: string;
  oss_status: string;
}

export interface RecentBarItem {
  status: 'SUCCESS' | 'FAILED' | 'SWITCHED';
  duration_ms: number;
  ttft_ms?: number | null;
  error_category?: string | null;
  timestamp: number;
  time_str: string;
}

export interface ModelChannelItem {
  name: string;
  model: string;
  provider: string;
  base_url: string;
  masked_key: string;
  backend: string;
  role: 'PRIMARY' | 'FALLBACK' | 'INFRA';
  weight: number;
  state: 'CLOSED' | 'OPEN' | 'HALF_OPEN';
  is_available: boolean;
  sla_percent: number;
  avg_latency_ms: number;
  p95_latency_ms: number;
  ttft_ms: number;
  total_calls: number;
  recent_bars: RecentBarItem[];
}

export interface VendorGroup {
  vendor_id: string;
  vendor_name: string;
  channels: ModelChannelItem[];
}

export interface ChannelsStatusResponse {
  status: string;
  updated_at: string;
  overall_sla: number;
  overall_avg_ttft: number;
  total_channels: number;
  groups: VendorGroup[];
}

export interface AuditLogItem {
  id: string;
  request_id: string;
  action: string;
  resource_id: string;
  duration_ms: number;
  status: string;
  error_msg?: string | null;
  details: Record<string, any>;
  created_at: string;
}

export interface ChannelAuditsResponse {
  node_name: string;
  count: number;
  items: AuditLogItem[];
  warning?: string;
}

export const systemApi = {
  /** 获取系统核心组件健康状态 */
  async getHealth(): Promise<SystemHealth> {
    return apiClient.get('/health');
  },

  /** 获取所有大模型渠道状态大盘数据 */
  async getChannelsStatus(): Promise<ChannelsStatusResponse> {
    return apiClient.get('/system/llm/channels');
  },

  /** 查询指定渠道的近 30 条真实审计流水 */
  async getChannelAudits(nodeName: string): Promise<ChannelAuditsResponse> {
    return apiClient.get(`/system/llm/channels/${nodeName}/audits`);
  },

  /** 管理员手动重置某节点的熔断状态至 CLOSED */
  async resetChannel(nodeName: string): Promise<{ node_name: string; state: string; message: string }> {
    return apiClient.post(`/system/llm/channels/${nodeName}/reset`);
  }
};
