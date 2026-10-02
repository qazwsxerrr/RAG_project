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

export const systemApi = {
  /** 获取系统核心组件与运行中模型真实健康状态 */
  async getHealth(): Promise<SystemHealth> {
    return apiClient.get('/health');
  },
};
