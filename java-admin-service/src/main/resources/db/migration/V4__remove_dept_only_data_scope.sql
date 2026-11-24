-- V4__remove_dept_only_data_scope.sql: 下线冗余的 DEPT_ONLY 数据范围，存量角色迁移为 DEPT_AND_SUB 并收紧约束

UPDATE sys_roles
SET data_scope = 'DEPT_AND_SUB'
WHERE data_scope = 'DEPT_ONLY';

ALTER TABLE sys_roles DROP CONSTRAINT IF EXISTS chk_sys_roles_data_scope_v2;
ALTER TABLE sys_roles
    ADD CONSTRAINT chk_sys_roles_data_scope_v2
    CHECK (data_scope IN ('CUSTOM_ACL', 'DEPT_AND_SUB', 'ALL_ORG'));
