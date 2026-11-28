-- V5__abac_attribute_constraints.sql
-- 企业 ABAC 用户属性治理、职级允许密级区间、角色三维准入策略、单真源部门归属与去 cv=1 前置清洗

-- 1. sys_positions 增加职级允许密级区间 [min_allowed_clearance, max_allowed_clearance] 并严格校验
ALTER TABLE sys_positions
    ADD COLUMN IF NOT EXISTS min_allowed_clearance SMALLINT;

ALTER TABLE sys_positions
    ADD COLUMN IF NOT EXISTS max_allowed_clearance SMALLINT;

UPDATE sys_positions
SET
    min_allowed_clearance = CASE position_code
        WHEN 'INTERN_EXT' THEN 1
        WHEN 'STAFF' THEN 1
        WHEN 'SUPERVISOR' THEN 2
        WHEN 'MANAGER' THEN 3
        WHEN 'DIRECTOR' THEN 3
        WHEN 'EXECUTIVE' THEN 4
        ELSE COALESCE(min_allowed_clearance, 1)
    END,
    max_allowed_clearance = CASE position_code
        WHEN 'INTERN_EXT' THEN 1
        WHEN 'STAFF' THEN 2
        WHEN 'SUPERVISOR' THEN 3
        WHEN 'MANAGER' THEN 3
        WHEN 'DIRECTOR' THEN 3
        WHEN 'EXECUTIVE' THEN 4
        ELSE COALESCE(max_allowed_clearance, 4)
    END
WHERE position_code IN ('INTERN_EXT', 'STAFF', 'SUPERVISOR', 'MANAGER', 'DIRECTOR', 'EXECUTIVE')
   OR min_allowed_clearance IS NULL
   OR max_allowed_clearance IS NULL;

UPDATE sys_positions
SET default_clearance_level = LEAST(GREATEST(default_clearance_level, min_allowed_clearance), max_allowed_clearance)
WHERE default_clearance_level < min_allowed_clearance
   OR default_clearance_level > max_allowed_clearance;

ALTER TABLE sys_positions
    ALTER COLUMN min_allowed_clearance SET NOT NULL;

ALTER TABLE sys_positions
    ALTER COLUMN max_allowed_clearance SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'sys_positions'::regclass
          AND conname = 'chk_sys_positions_clearance_bounds'
    ) THEN
        ALTER TABLE sys_positions
            ADD CONSTRAINT chk_sys_positions_clearance_bounds
            CHECK (
                min_allowed_clearance BETWEEN 1 AND 4
                AND max_allowed_clearance BETWEEN 1 AND 4
                AND min_allowed_clearance <= default_clearance_level
                AND default_clearance_level <= max_allowed_clearance
            );
    END IF;
END $$;

-- 2. sys_roles 增加角色-职级约束、角色-部门前缀约束与必须主部门标记
ALTER TABLE sys_roles
    ADD COLUMN IF NOT EXISTS allowed_positions JSONB NOT NULL DEFAULT '["*"]'::jsonb;

ALTER TABLE sys_roles
    ADD COLUMN IF NOT EXISTS allowed_dept_prefixes JSONB NOT NULL DEFAULT '["*"]'::jsonb;

ALTER TABLE sys_roles
    ADD COLUMN IF NOT EXISTS primary_dept_only BOOLEAN NOT NULL DEFAULT FALSE;

UPDATE sys_roles
SET
    allowed_positions = CASE role_code
        WHEN 'c_user' THEN '["*"]'::jsonb
        WHEN 'developer' THEN '["INTERN_EXT","STAFF","SUPERVISOR","MANAGER","DIRECTOR"]'::jsonb
        WHEN 'dept_reviewer' THEN '["SUPERVISOR","MANAGER","DIRECTOR"]'::jsonb
        WHEN 'dept_manager' THEN '["MANAGER","DIRECTOR"]'::jsonb
        WHEN 'accountant' THEN '["STAFF","SUPERVISOR","MANAGER","DIRECTOR"]'::jsonb
        WHEN 'admin' THEN '["DIRECTOR","EXECUTIVE"]'::jsonb
        WHEN 'security_admin' THEN '["DIRECTOR","EXECUTIVE"]'::jsonb
        ELSE allowed_positions
    END,
    allowed_dept_prefixes = CASE role_code
        WHEN 'c_user' THEN '["*"]'::jsonb
        WHEN 'developer' THEN '["/总公司/研发中心"]'::jsonb
        WHEN 'dept_reviewer' THEN '["*"]'::jsonb
        WHEN 'dept_manager' THEN '["*"]'::jsonb
        WHEN 'accountant' THEN '["/总公司/职能中心/财务部"]'::jsonb
        WHEN 'admin' THEN '["*"]'::jsonb
        WHEN 'security_admin' THEN '["*"]'::jsonb
        ELSE allowed_dept_prefixes
    END,
    primary_dept_only = CASE role_code
        WHEN 'dept_reviewer' THEN TRUE
        WHEN 'dept_manager' THEN TRUE
        ELSE FALSE
    END
WHERE tenant_id = 'default_tenant'
  AND role_code IN ('c_user', 'developer', 'dept_reviewer', 'dept_manager', 'accountant', 'admin', 'security_admin');

-- 清理冗余角色 kb_admin 及其关联 kb_grants（不向 dept_manager 迁移历史 kb_grants）
DELETE FROM kb_grants
WHERE subject_type = 'ROLE'
  AND subject_id = 'kb_admin';

DELETE FROM sys_roles
WHERE role_code = 'kb_admin';

-- 清理已下线权限常量 user:authz:manage
UPDATE sys_roles
SET permissions = permissions - 'user:authz:manage'
WHERE permissions ? 'user:authz:manage';

-- 3. sys_users 删除默认职级回退与冗余展示字段 dept_path
ALTER TABLE sys_users
    ALTER COLUMN position_code DROP DEFAULT;

ALTER TABLE sys_users
    DROP COLUMN IF EXISTS dept_path;

-- 4. sys_user_departments 清理非 IdP 脏数据并彻底去除双来源与过期字段
DO $$
DECLARE
    v_conname TEXT;
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'sys_user_departments'
          AND column_name = 'source'
    ) THEN
        EXECUTE 'DELETE FROM sys_user_departments WHERE source <> ''IDP''';
    END IF;

    FOR v_conname IN
        SELECT c.conname
        FROM pg_constraint c
        WHERE c.conrelid = 'sys_user_departments'::regclass
          AND c.contype = 'c'
          AND pg_get_constraintdef(c.oid) ILIKE '%source%'
    LOOP
        EXECUTE format('ALTER TABLE sys_user_departments DROP CONSTRAINT IF EXISTS %I', v_conname);
    END LOOP;
END $$;

ALTER TABLE sys_user_departments
    DROP CONSTRAINT IF EXISTS uq_user_dept_source;

ALTER TABLE sys_user_departments
    DROP COLUMN IF EXISTS source,
    DROP COLUMN IF EXISTS expires_at,
    DROP COLUMN IF EXISTS granted_by;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'sys_user_departments'::regclass
          AND conname = 'uq_user_dept'
    ) THEN
        ALTER TABLE sys_user_departments
            ADD CONSTRAINT uq_user_dept UNIQUE (tenant_id, user_id, department_id);
    END IF;
END $$;
