import { apiClient } from './client';
import type {
  AccessibleKnowledgeBase,
  AdminAuditLog,
  AdminDepartment,
  AdminKbGrant,
  AdminKnowledgeBase,
  AdminPosition,
  AdminRole,
  AdminUserSummary,
  GrantEffect,
  GrantSubjectType,
  KbAction,
  KbVisibility,
} from '../types/authz';

/**
 * 统一复用 apiClient (/api/v1/*) 对接 Python 单一权限治理与策略决策中心：
 * Java 认证网关仅负责 OIDC 登录与 JWT 签发；所有知识库/ACL/映射治理接口统一走 /api/v1/admin/*。
 */
export const accessControlApi = {
  async listAccessibleKnowledgeBases(action: KbAction = 'READ'): Promise<AccessibleKnowledgeBase[]> {
    return apiClient.get('/knowledge-bases', { params: { action } });
  },

  async listDepartments(): Promise<AdminDepartment[]> {
    return apiClient.get('/admin/departments');
  },
  async listUsers(): Promise<AdminUserSummary[]> {
    return apiClient.get('/admin/users');
  },
  async revokeUser(userId: string, reason?: string): Promise<any> {
    return apiClient.post(`/admin/users/${userId}/revoke`, { reason });
  },
  async restoreUser(userId: string): Promise<any> {
    return apiClient.delete(`/admin/users/${userId}/revoke`);
  },

  async listPositions(): Promise<AdminPosition[]> {
    return apiClient.get('/admin/positions');
  },
  async upsertPosition(payload: {
    positionCode: string;
    positionName: string;
    rankOrder: number;
    defaultClearanceLevel: number;
    description?: string;
    reason?: string;
  }): Promise<AdminPosition> {
    return apiClient.put('/admin/positions', payload);
  },
  async listRoles(): Promise<AdminRole[]> {
    return apiClient.get('/admin/roles');
  },

  async listKnowledgeBases(): Promise<AdminKnowledgeBase[]> {
    return apiClient.get('/admin/knowledge-bases');
  },
  async createKnowledgeBase(payload: {
    name: string;
    description?: string;
    ownerDepartmentId?: string;
    ownerDeptPath?: string;
    visibility: KbVisibility;
    minClassificationLevel: number;
    defaultDocLevel: number;
    maxDocLevel: number;
    reason?: string;
  }): Promise<AdminKnowledgeBase> {
    return apiClient.post('/admin/knowledge-bases', payload);
  },
  async deleteKnowledgeBase(kbId: string): Promise<any> {
    return apiClient.delete(`/admin/knowledge-bases/${kbId}`);
  },
  async listKbGrants(kbId: string): Promise<AdminKbGrant[]> {
    return apiClient.get(`/admin/knowledge-bases/${kbId}/grants`);
  },
  async createKbGrant(
    kbId: string,
    payload: {
      subjectType: GrantSubjectType;
      subjectId: string;
      includeSubDepts?: boolean;
      action: KbAction;
      effect: GrantEffect;
      expiresAt?: string | null;
      reason?: string;
    }
  ): Promise<AdminKbGrant> {
    return apiClient.post(`/admin/knowledge-bases/${kbId}/grants`, payload);
  },
  async deleteKbGrant(kbId: string, grantId: string): Promise<any> {
    return apiClient.delete(`/admin/knowledge-bases/${kbId}/grants/${grantId}`);
  },

  async listAuditLogs(params?: { action?: string; status?: string; limit?: number }): Promise<AdminAuditLog[]> {
    return apiClient.get('/admin/audit-logs', { params });
  },
};
