-- V3__dept_manager_kb_grant.sql: 为默认租户部门经理 (dept_manager) 补充 kb:grant 授权管理权，并递增策略纪元

UPDATE sys_roles
SET permissions = permissions || '["kb:grant"]'::jsonb
WHERE tenant_id = 'default_tenant'
  AND role_code = 'dept_manager'
  AND NOT (permissions @> '["kb:grant"]'::jsonb)
  AND NOT (permissions @> '["*"]'::jsonb);

INSERT INTO sys_tenant_policy_epochs (tenant_id, policy_epoch, updated_at)
VALUES ('default_tenant', 2, NOW())
ON CONFLICT (tenant_id) DO UPDATE
SET policy_epoch = sys_tenant_policy_epochs.policy_epoch + 1,
    updated_at   = NOW();
