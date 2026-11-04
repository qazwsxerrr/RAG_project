-- V1__rbac.sql: Phase-6 RBAC 员工级知识库数据隔离基础表结构与策略字典

CREATE TABLE IF NOT EXISTS sys_users (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     VARCHAR(64)  NOT NULL DEFAULT 'default_tenant',
    external_id   VARCHAR(128) NOT NULL,
    id_source     VARCHAR(32)  NOT NULL,
    real_name     VARCHAR(50)  NOT NULL,
    dept_path     VARCHAR(255) NOT NULL,           -- 缺部门则不允许建档
    is_active     BOOLEAN      NOT NULL DEFAULT TRUE,
    last_login_at TIMESTAMPTZ,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UNIQUE (id_source, external_id)
);

CREATE TABLE IF NOT EXISTS sys_roles (
    -- 策略字典，不是角色分配表：谁拥有该角色由企业 IdP 决定，本方不存关联。
    -- role_code 必须与企业角色码一致（接入时唯一的对齐项，见 §2.7）。
    role_code   VARCHAR(50) PRIMARY KEY,
    role_name   VARCHAR(100) NOT NULL,
    data_scope  VARCHAR(20) NOT NULL CHECK (data_scope IN ('ALL','DEPT_TREE')),
    permissions JSONB NOT NULL DEFAULT '[]'::jsonb
);

INSERT INTO sys_roles (role_code, role_name, data_scope, permissions) VALUES
  ('admin',      '超级管理员', 'ALL',       '["*"]'),
  ('developer',  '研发工程师', 'DEPT_TREE', '["kb:retrieve","doc:upload","doc:delete","doc:review"]'),
  ('accountant', '财务主管',   'DEPT_TREE', '["kb:retrieve"]'),
  ('employee',   '普通员工',   'DEPT_TREE', '["kb:retrieve"]')
ON CONFLICT (role_code) DO NOTHING;
