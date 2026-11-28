export type DataScope = 'CUSTOM_ACL' | 'DEPT_AND_SUB' | 'ALL_ORG';
export type KbAction = 'READ' | 'UPLOAD' | 'REVIEW' | 'DELETE' | 'MANAGE';
export type KbVisibility = 'PUBLIC' | 'DEPARTMENT' | 'RESTRICTED';
export type KbSourceType = 'PUBLIC' | 'DEPARTMENT' | 'SHARED_ACL';
export type GrantSubjectType = 'USER' | 'DEPARTMENT' | 'ROLE';
export type GrantEffect = 'ALLOW' | 'DENY';

export interface AccessibleKnowledgeBase {
  id: string;
  tenant_id: string;
  name: string;
  description?: string | null;
  owner_department_id?: string | null;
  owner_dept_path: string;
  visibility: KbVisibility;
  source_type: KbSourceType;
  min_classification_level: number;
  default_doc_level: number;
  max_doc_level: number;
  status: string;
  can_read: boolean;
  can_upload: boolean;
  can_review: boolean;
  can_delete: boolean;
  can_manage: boolean;
  allowed_upload_levels: number[];
}

export interface AdminDepartment {
  id: string;
  tenantId: string;
  parentId?: string | null;
  name: string;
  path: string;
  sortOrder: number;
  status: 'ACTIVE' | 'DISABLED';
  createdAt?: string;
}

export interface AdminPosition {
  tenantId: string;
  positionCode: string;
  positionName: string;
  rankOrder: number;
  defaultClearanceLevel: number;
  minAllowedClearance: number;
  maxAllowedClearance: number;
  description?: string | null;
  updatedAt?: string;
}

export interface AdminRole {
  tenantId: string;
  roleCode: string;
  roleName: string;
  dataScope: DataScope;
  permissionsJson: string;
  allowedPositions: string[];
  allowedDeptPrefixes: string[];
  primaryDeptOnly: boolean;
}

/** 从企业 IdP/HR 同步到本地影子表的只读员工摘要 */
export interface AdminUserSummary {
  userId: string;
  tenantId: string;
  externalId: string;
  username: string;
  isActive: boolean;
  positionCode: string;
  positionName: string;
  effectiveClearanceLevel: number;
  authzVersion: number;
  primaryDeptPath: string;
  effectiveDeptPaths: string[];
  lastLoginAt?: string | null;
  createdAt?: string;
}

export interface AdminKnowledgeBase {
  id: string;
  tenantId: string;
  name: string;
  description?: string | null;
  ownerDepartmentId?: string | null;
  ownerDeptPath: string;
  visibility: KbVisibility;
  minClassificationLevel: number;
  defaultDocLevel: number;
  maxDocLevel: number;
  status: 'ACTIVE' | 'ARCHIVED';
  createdBy?: string | null;
  createdAt?: string;
  updatedAt?: string;
}

export interface AdminKbGrant {
  id: string;
  tenantId: string;
  kbId: string;
  subjectType: GrantSubjectType;
  subjectId: string;
  subjectDeptPath?: string | null;
  includeSubDepts: boolean;
  action: KbAction;
  effect: GrantEffect;
  expiresAt?: string | null;
  reason?: string | null;
  grantedBy?: string | null;
  createdAt?: string;
}

export interface AdminAuditLog {
  id: string;
  requestId: string;
  tenantId: string;
  userId?: string | null;
  action: string;
  resourceId?: string | null;
  durationMs: number;
  status: string;
  errorMsg?: string | null;
  detailsJson: string;
  createdAt?: string;
}

export const CLASSIFICATION_META: Record<number, { code: string; label: string; short: string }> = {
  1: { code: 'L1', label: 'L1 全员公开', short: '公开' },
  2: { code: 'L2', label: 'L2 部门内部', short: '内部' },
  3: { code: 'L3', label: 'L3 管理层机密', short: '机密' },
  4: { code: 'L4', label: 'L4 核心绝密', short: '绝密' },
};
