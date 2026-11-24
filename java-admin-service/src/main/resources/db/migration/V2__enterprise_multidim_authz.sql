-- V2__enterprise_multidim_authz.sql
-- 企业内部知识库多维权限改造数据库结构定义（纯 DDL 与标准字典播种）
-- 覆盖：多租户组织部门树、员工双来源多部门归属、四档 data_scope 角色体系、
--       职位等级映射、知识库 L1-L4 分级与精细化 ACL、租户策略版本、文档密级与会话来源追踪。

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ============================================================================
-- 1. 升级 sys_departments 为完整多租户组织部门树
-- ============================================================================
CREATE TABLE IF NOT EXISTS sys_departments (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id  VARCHAR(64)  NOT NULL DEFAULT 'default_tenant',
    parent_id  UUID         NULL,
    name       VARCHAR(100) NOT NULL,
    path       VARCHAR(255) NOT NULL,
    sort_order INT          NOT NULL DEFAULT 0,
    status     VARCHAR(20)  NOT NULL DEFAULT 'ACTIVE',
    created_at TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

ALTER TABLE sys_departments ADD COLUMN IF NOT EXISTS tenant_id  VARCHAR(64) NOT NULL DEFAULT 'default_tenant';
ALTER TABLE sys_departments ADD COLUMN IF NOT EXISTS parent_id  UUID        NULL;
ALTER TABLE sys_departments ADD COLUMN IF NOT EXISTS sort_order INT         NOT NULL DEFAULT 0;
ALTER TABLE sys_departments ADD COLUMN IF NOT EXISTS status     VARCHAR(20) NOT NULL DEFAULT 'ACTIVE';
ALTER TABLE sys_departments ALTER COLUMN created_at SET DEFAULT NOW();

-- 移除旧的全局 UNIQUE(name) 约束和独立唯一索引，允许同租户不同父节点下同名子部门
DO $$
DECLARE
    rec RECORD;
BEGIN
    FOR rec IN
        SELECT c.conname
        FROM pg_constraint c
        JOIN pg_class t ON c.conrelid = t.oid
        JOIN pg_namespace n ON t.relnamespace = n.oid
        WHERE n.nspname = current_schema()
          AND t.relname = 'sys_departments'
          AND c.contype = 'u'
          AND pg_get_constraintdef(c.oid) ~* '^UNIQUE\s*\(\s*name\s*\)$'
    LOOP
        EXECUTE format('ALTER TABLE sys_departments DROP CONSTRAINT IF EXISTS %I', rec.conname);
    END LOOP;

    FOR rec IN
        SELECT i.relname AS idx_name
        FROM pg_index ix
        JOIN pg_class i ON i.oid = ix.indexrelid
        JOIN pg_class t ON t.oid = ix.indrelid
        JOIN pg_namespace n ON t.relnamespace = n.oid
        WHERE n.nspname = current_schema()
          AND t.relname = 'sys_departments'
          AND ix.indisunique
          AND NOT ix.indisprimary
          AND pg_get_indexdef(i.oid) ~* '\(\s*name\s*\)$'
    LOOP
        EXECUTE format('DROP INDEX IF EXISTS %I', rec.idx_name);
    END LOOP;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'sys_departments'::regclass AND conname = 'uq_sys_departments_tenant_id'
    ) THEN
        ALTER TABLE sys_departments ADD CONSTRAINT uq_sys_departments_tenant_id UNIQUE (tenant_id, id);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'sys_departments'::regclass AND conname = 'uq_sys_departments_tenant_path'
    ) THEN
        ALTER TABLE sys_departments ADD CONSTRAINT uq_sys_departments_tenant_path UNIQUE (tenant_id, path);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'sys_departments'::regclass AND conname = 'chk_sys_departments_status'
    ) THEN
        ALTER TABLE sys_departments ADD CONSTRAINT chk_sys_departments_status CHECK (status IN ('ACTIVE', 'DISABLED'));
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'sys_departments'::regclass AND conname = 'fk_sys_departments_tenant_parent'
    ) THEN
        ALTER TABLE sys_departments
            ADD CONSTRAINT fk_sys_departments_tenant_parent
            FOREIGN KEY (tenant_id, parent_id) REFERENCES sys_departments (tenant_id, id);
    END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS uq_sys_departments_tenant_parent_name
    ON sys_departments (tenant_id, COALESCE(parent_id, '00000000-0000-0000-0000-000000000000'::uuid), name);

CREATE INDEX IF NOT EXISTS idx_sys_departments_tenant_path
    ON sys_departments (tenant_id, path);

-- 补齐默认租户核心组织架构树节点
INSERT INTO sys_departments (id, tenant_id, name, path, sort_order, status)
VALUES
    (gen_random_uuid(), 'default_tenant', '公共知识库', '/public', 0, 'ACTIVE'),
    (gen_random_uuid(), 'default_tenant', '总公司', '/总公司', 10, 'ACTIVE'),
    (gen_random_uuid(), 'default_tenant', '研发中心', '/总公司/研发中心', 20, 'ACTIVE'),
    (gen_random_uuid(), 'default_tenant', '技术部', '/总公司/研发中心/技术部', 21, 'ACTIVE'),
    (gen_random_uuid(), 'default_tenant', '产品部', '/总公司/研发中心/产品部', 22, 'ACTIVE'),
    (gen_random_uuid(), 'default_tenant', '运营中心', '/总公司/运营中心', 30, 'ACTIVE'),
    (gen_random_uuid(), 'default_tenant', '运营部', '/总公司/运营中心/运营部', 31, 'ACTIVE'),
    (gen_random_uuid(), 'default_tenant', '职能中心', '/总公司/职能中心', 40, 'ACTIVE'),
    (gen_random_uuid(), 'default_tenant', '人事行政部', '/总公司/职能中心/人事行政部', 41, 'ACTIVE'),
    (gen_random_uuid(), 'default_tenant', '财务部', '/总公司/职能中心/财务部', 42, 'ACTIVE'),
    (gen_random_uuid(), 'default_tenant', '总裁办', '/总公司/总裁办', 50, 'ACTIVE')
ON CONFLICT (tenant_id, path) DO NOTHING;

-- 根据 path 自动回填可匹配的 parent_id
UPDATE sys_departments child
SET parent_id = parent.id
FROM sys_departments parent
WHERE child.parent_id IS NULL
  AND child.tenant_id = parent.tenant_id
  AND child.path LIKE '/%/%'
  AND regexp_replace(child.path, '/[^/]+$', '') = parent.path;


-- ============================================================================
-- 2. 租户级策略版本表 (policy_epoch 持久化单一事实源)
-- ============================================================================
CREATE TABLE IF NOT EXISTS sys_tenant_policy_epochs (
    tenant_id    VARCHAR(64) PRIMARY KEY,
    policy_epoch BIGINT      NOT NULL DEFAULT 1,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO sys_tenant_policy_epochs (tenant_id, policy_epoch)
VALUES ('default_tenant', 1)
ON CONFLICT (tenant_id) DO NOTHING;


-- ============================================================================
-- 3. 职位等级字典表 (sys_positions)
-- ============================================================================
CREATE TABLE IF NOT EXISTS sys_positions (
    tenant_id               VARCHAR(64)  NOT NULL DEFAULT 'default_tenant',
    position_code           VARCHAR(50)  NOT NULL,
    position_name           VARCHAR(100) NOT NULL,
    rank_order              INT          NOT NULL DEFAULT 0,
    default_clearance_level SMALLINT     NOT NULL CHECK (default_clearance_level BETWEEN 1 AND 4),
    description             TEXT         NULL,
    updated_at              TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, position_code)
);

INSERT INTO sys_positions (tenant_id, position_code, position_name, rank_order, default_clearance_level, description)
VALUES
    ('default_tenant', 'INTERN_EXT', '实习 / 外部协作', 10, 1, '可看 L1 全员公开制度（员工手册、公司制度）'),
    ('default_tenant', 'STAFF',      '普通员工',       20, 2, '可看 L1 + 所属多部门及获授知识库内 L1-L2 文档'),
    ('default_tenant', 'SUPERVISOR', '部门主管',       30, 2, '默认 L2，可在后台按职级动态调整为 L2 或 L3'),
    ('default_tenant', 'MANAGER',    '部门经理',       40, 3, '可看有权部门/知识库内 L1-L3 文档（含预算、人事、合同）'),
    ('default_tenant', 'DIRECTOR',   '总监',           50, 3, '配合上级中心部门路径，覆盖中心及下属多部门 L1-L3 文档'),
    ('default_tenant', 'EXECUTIVE',  '公司高管',       60, 4, '满足 L1-L4 密级门槛，仍遵守知识库范围与受限库 ACL')
ON CONFLICT (tenant_id, position_code) DO NOTHING;


-- ============================================================================
-- 4. 升级 sys_roles 为多租户 + 四档 data_scope，并初始化预置角色
-- ============================================================================
ALTER TABLE sys_roles ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(64) NOT NULL DEFAULT 'default_tenant';

-- 移除旧 data_scope CHECK 约束与旧单列主键
DO $$
DECLARE
    rec RECORD;
BEGIN
    FOR rec IN
        SELECT c.conname
        FROM pg_constraint c
        JOIN pg_class t ON c.conrelid = t.oid
        JOIN pg_namespace n ON t.relnamespace = n.oid
        WHERE n.nspname = current_schema()
          AND t.relname = 'sys_roles'
          AND c.contype = 'c'
          AND pg_get_constraintdef(c.oid) ILIKE '%data_scope%'
    LOOP
        EXECUTE format('ALTER TABLE sys_roles DROP CONSTRAINT IF EXISTS %I', rec.conname);
    END LOOP;

    FOR rec IN
        SELECT c.conname
        FROM pg_constraint c
        JOIN pg_class t ON c.conrelid = t.oid
        JOIN pg_namespace n ON t.relnamespace = n.oid
        WHERE n.nspname = current_schema()
          AND t.relname = 'sys_roles'
          AND c.contype = 'p'
          AND pg_get_constraintdef(c.oid) ~* '^PRIMARY KEY\s*\(\s*role_code\s*\)$'
    LOOP
        EXECUTE format('ALTER TABLE sys_roles DROP CONSTRAINT IF EXISTS %I', rec.conname);
    END LOOP;
END $$;

-- 迁移存量 data_scope 枚举，原有角色操作权限保持不变
UPDATE sys_roles SET data_scope = 'DEPT_AND_SUB' WHERE data_scope = 'DEPT_TREE';
UPDATE sys_roles SET data_scope = 'ALL_ORG'      WHERE data_scope = 'ALL';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'sys_roles'::regclass AND contype = 'p'
    ) THEN
        ALTER TABLE sys_roles ADD CONSTRAINT pk_sys_roles_tenant_code PRIMARY KEY (tenant_id, role_code);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'sys_roles'::regclass AND conname = 'chk_sys_roles_data_scope_v2'
    ) THEN
        ALTER TABLE sys_roles
            ADD CONSTRAINT chk_sys_roles_data_scope_v2
            CHECK (data_scope IN ('CUSTOM_ACL', 'DEPT_ONLY', 'DEPT_AND_SUB', 'ALL_ORG'));
    END IF;
END $$;

INSERT INTO sys_roles (tenant_id, role_code, role_name, data_scope, permissions)
VALUES
    ('default_tenant', 'kb_admin',       '知识库管理员', 'DEPT_AND_SUB', '["kb:retrieve","doc:upload","doc:review","doc:delete","kb:manage","kb:grant"]'::jsonb),
    ('default_tenant', 'security_admin', '安全合规管理员', 'ALL_ORG',     '["kb:retrieve","kb:manage","kb:grant","user:authz:manage","security:classify","security:declassify"]'::jsonb),
    ('default_tenant', 'dept_manager',   '部门经理',     'DEPT_AND_SUB', '["kb:retrieve","doc:upload","doc:review","doc:delete","kb:manage"]'::jsonb),
    ('default_tenant', 'executive',      '公司高管',     'ALL_ORG',      '["kb:retrieve"]'::jsonb)
ON CONFLICT (tenant_id, role_code) DO NOTHING;


-- ============================================================================
-- 5. 扩展 sys_users (职级、个人授权版本号、多租户唯一键)
-- ============================================================================
ALTER TABLE sys_users ADD COLUMN IF NOT EXISTS position_code      VARCHAR(50) NOT NULL DEFAULT 'STAFF';
ALTER TABLE sys_users ADD COLUMN IF NOT EXISTS authz_version      BIGINT      NOT NULL DEFAULT 1;
ALTER TABLE sys_users ALTER COLUMN dept_path SET DEFAULT '';

DO $$
DECLARE
    rec RECORD;
BEGIN
    -- 移除旧跨租户唯一键 UNIQUE(id_source, external_id)
    FOR rec IN
        SELECT c.conname
        FROM pg_constraint c
        JOIN pg_class t ON c.conrelid = t.oid
        JOIN pg_namespace n ON t.relnamespace = n.oid
        WHERE n.nspname = current_schema()
          AND t.relname = 'sys_users'
          AND c.contype = 'u'
          AND pg_get_constraintdef(c.oid) ~* '^UNIQUE\s*\(\s*id_source\s*,\s*external_id\s*\)$'
    LOOP
        EXECUTE format('ALTER TABLE sys_users DROP CONSTRAINT IF EXISTS %I', rec.conname);
    END LOOP;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'sys_users'::regclass AND conname = 'uq_sys_users_tenant_source_ext'
    ) THEN
        ALTER TABLE sys_users ADD CONSTRAINT uq_sys_users_tenant_source_ext UNIQUE (tenant_id, id_source, external_id);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'sys_users'::regclass AND conname = 'uq_sys_users_tenant_id'
    ) THEN
        ALTER TABLE sys_users ADD CONSTRAINT uq_sys_users_tenant_id UNIQUE (tenant_id, id);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'sys_users'::regclass AND conname = 'fk_sys_users_tenant_position'
    ) THEN
        ALTER TABLE sys_users
            ADD CONSTRAINT fk_sys_users_tenant_position
            FOREIGN KEY (tenant_id, position_code) REFERENCES sys_positions (tenant_id, position_code);
    END IF;
END $$;


-- ============================================================================
-- 6. 新增员工多部门归属表 (sys_user_departments)
-- ============================================================================
CREATE TABLE IF NOT EXISTS sys_user_departments (
    id            UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     VARCHAR(64)  NOT NULL DEFAULT 'default_tenant',
    user_id       UUID         NOT NULL,
    department_id UUID         NOT NULL,
    dept_path     VARCHAR(255) NOT NULL,
    is_primary    BOOLEAN      NOT NULL DEFAULT FALSE,
    source        VARCHAR(16)  NOT NULL CHECK (source IN ('IDP', 'MANUAL')),
    expires_at    TIMESTAMPTZ  NULL,
    granted_by    VARCHAR(64)  NULL,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_user_dept_source UNIQUE (tenant_id, user_id, department_id, source),
    CONSTRAINT fk_user_dept_user FOREIGN KEY (tenant_id, user_id) REFERENCES sys_users (tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_user_dept_dept FOREIGN KEY (tenant_id, department_id) REFERENCES sys_departments (tenant_id, id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_user_departments_single_primary
    ON sys_user_departments (tenant_id, user_id)
    WHERE is_primary = TRUE;

CREATE INDEX IF NOT EXISTS idx_sys_user_departments_tenant_user
    ON sys_user_departments (tenant_id, user_id);


-- ============================================================================
-- 7. 知识库实体表 (knowledge_bases) 与精细化 ACL 表 (kb_grants)
-- ============================================================================
CREATE TABLE IF NOT EXISTS knowledge_bases (
    id                       UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id                VARCHAR(64)  NOT NULL DEFAULT 'default_tenant',
    name                     VARCHAR(150) NOT NULL,
    description              TEXT         NULL,
    owner_department_id      UUID         NULL,
    owner_dept_path          VARCHAR(255) NOT NULL,
    visibility               VARCHAR(20)  NOT NULL CHECK (visibility IN ('PUBLIC', 'DEPARTMENT', 'RESTRICTED')),
    min_classification_level SMALLINT     NOT NULL DEFAULT 1,
    default_doc_level        SMALLINT     NOT NULL DEFAULT 2,
    max_doc_level            SMALLINT     NOT NULL DEFAULT 4,
    status                   VARCHAR(20)  NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'ARCHIVED')),
    created_by               VARCHAR(64)  NULL,
    created_at               TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at               TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_knowledge_bases_tenant_id UNIQUE (tenant_id, id),
    CONSTRAINT uq_knowledge_bases_tenant_name UNIQUE (tenant_id, name),
    CONSTRAINT fk_kb_owner_dept FOREIGN KEY (tenant_id, owner_department_id) REFERENCES sys_departments (tenant_id, id),
    CONSTRAINT chk_kb_classification_range CHECK (
        min_classification_level BETWEEN 1 AND 4
        AND default_doc_level BETWEEN 1 AND 4
        AND max_doc_level BETWEEN 1 AND 4
        AND min_classification_level <= default_doc_level
        AND default_doc_level <= max_doc_level
    ),
    CONSTRAINT chk_kb_public_only_l1 CHECK (
        visibility <> 'PUBLIC'
        OR (min_classification_level = 1 AND default_doc_level = 1 AND max_doc_level = 1)
    )
);

CREATE INDEX IF NOT EXISTS idx_knowledge_bases_tenant_vis_dept
    ON knowledge_bases (tenant_id, status, visibility, owner_dept_path);

CREATE TABLE IF NOT EXISTS kb_grants (
    id                UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         VARCHAR(64)  NOT NULL DEFAULT 'default_tenant',
    kb_id             UUID         NOT NULL,
    subject_type      VARCHAR(20)  NOT NULL CHECK (subject_type IN ('USER', 'DEPARTMENT', 'ROLE')),
    subject_id        VARCHAR(128) NOT NULL,
    subject_dept_path VARCHAR(255) NULL,
    include_sub_depts BOOLEAN      NOT NULL DEFAULT FALSE,
    action            VARCHAR(20)  NOT NULL CHECK (action IN ('READ', 'UPLOAD', 'REVIEW', 'DELETE', 'MANAGE')),
    effect            VARCHAR(10)  NOT NULL CHECK (effect IN ('ALLOW', 'DENY')),
    expires_at        TIMESTAMPTZ  NULL,
    reason            TEXT         NULL,
    granted_by        VARCHAR(64)  NULL,
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CONSTRAINT fk_kb_grants_tenant_kb FOREIGN KEY (tenant_id, kb_id) REFERENCES knowledge_bases (tenant_id, id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_kb_grants_lookup
    ON kb_grants (tenant_id, kb_id, action, effect);

CREATE INDEX IF NOT EXISTS idx_kb_grants_subject
    ON kb_grants (tenant_id, subject_type, subject_id);

-- 为 default_tenant 初始化全员公开库与各存量部门的默认部门知识库
INSERT INTO knowledge_bases (
    id, tenant_id, name, description, owner_department_id, owner_dept_path,
    visibility, min_classification_level, default_doc_level, max_doc_level, status, created_by
)
SELECT
    gen_random_uuid(),
    d.tenant_id,
    '公司全员公共知识库',
    '全员公开 L1 知识库（员工手册、公开制度与公告）',
    d.id,
    '/public',
    'PUBLIC',
    1, 1, 1, 'ACTIVE', 'migration_v2'
FROM sys_departments d
WHERE d.tenant_id = 'default_tenant' AND d.path = '/public'
ON CONFLICT (tenant_id, name) DO NOTHING;

INSERT INTO knowledge_bases (
    id, tenant_id, name, description, owner_department_id, owner_dept_path,
    visibility, min_classification_level, default_doc_level, max_doc_level, status, created_by
)
SELECT
    gen_random_uuid(),
    d.tenant_id,
    d.name || '知识库',
    '部门默认知识库 (' || d.path || ')',
    d.id,
    d.path,
    'DEPARTMENT',
    2, 2, 4, 'ACTIVE', 'migration_v2'
FROM sys_departments d
WHERE d.tenant_id = 'default_tenant'
  AND d.path <> '/public'
  AND d.path <> '/'
  AND NOT EXISTS (
      SELECT 1 FROM knowledge_bases kb
      WHERE kb.tenant_id = d.tenant_id
        AND kb.owner_dept_path = d.path
        AND kb.visibility = 'DEPARTMENT'
  )
ON CONFLICT (tenant_id, name) DO NOTHING;


-- ============================================================================
-- 8. 扩展 rag_documents (多租户、知识库外键、L1-L4 文档密级与创建人)
-- ============================================================================
DO $$
DECLARE
    has_rag_docs BOOLEAN;
BEGIN
    SELECT EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = current_schema() AND table_name = 'rag_documents'
    ) INTO has_rag_docs;

    IF has_rag_docs THEN
        ALTER TABLE rag_documents ADD COLUMN IF NOT EXISTS tenant_id            VARCHAR(64) NOT NULL DEFAULT 'default_tenant';
        ALTER TABLE rag_documents ADD COLUMN IF NOT EXISTS kb_id                UUID        NULL;
        ALTER TABLE rag_documents ADD COLUMN IF NOT EXISTS classification_level SMALLINT    NULL;
        ALTER TABLE rag_documents ADD COLUMN IF NOT EXISTS created_by           VARCHAR(64) NULL;

        ALTER TABLE rag_documents ALTER COLUMN kb_id SET NOT NULL;
        ALTER TABLE rag_documents ALTER COLUMN classification_level SET NOT NULL;

        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conrelid = 'rag_documents'::regclass AND conname = 'chk_rag_documents_classification_level'
        ) THEN
            ALTER TABLE rag_documents
                ADD CONSTRAINT chk_rag_documents_classification_level
                CHECK (classification_level BETWEEN 1 AND 4);
        END IF;

        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conrelid = 'rag_documents'::regclass AND conname = 'fk_rag_documents_tenant_kb'
        ) THEN
            ALTER TABLE rag_documents
                ADD CONSTRAINT fk_rag_documents_tenant_kb
                FOREIGN KEY (tenant_id, kb_id) REFERENCES knowledge_bases (tenant_id, id);
        END IF;

        CREATE INDEX IF NOT EXISTS idx_rag_documents_tenant_kb_level
            ON rag_documents (tenant_id, kb_id, classification_level);
    END IF;
END $$;


-- ============================================================================
-- 9. 扩展 rag_chat_messages 保存完整来源文档 ID (source_doc_ids)
-- ============================================================================
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = current_schema() AND table_name = 'rag_chat_messages'
    ) THEN
        ALTER TABLE rag_chat_messages ADD COLUMN IF NOT EXISTS source_doc_ids JSONB NULL;
    END IF;
END $$;


-- ============================================================================
-- 10. 幂等保障 sys_audit_logs 存在 (供 Java 与 Python 共用统一安全审计表)
-- ============================================================================
CREATE TABLE IF NOT EXISTS sys_audit_logs (
    id          UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    request_id  VARCHAR(64)  NOT NULL,
    tenant_id   VARCHAR(64)  NOT NULL DEFAULT 'default_tenant',
    user_id     VARCHAR(64)  NULL,
    action      VARCHAR(64)  NOT NULL,
    resource_id VARCHAR(128) NULL,
    duration_ms INT          NOT NULL DEFAULT 0,
    status      VARCHAR(32)  NOT NULL DEFAULT 'SUCCESS',
    error_msg   TEXT         NULL,
    details     JSONB        NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sys_audit_logs_tenant_created
    ON sys_audit_logs (tenant_id, created_at DESC);
