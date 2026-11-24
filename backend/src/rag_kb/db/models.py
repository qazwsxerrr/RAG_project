"""src/rag_kb/db/models.py —— SQLAlchemy ORM 表结构定义。

与 V1__rbac.sql / V2__enterprise_multidim_authz.sql 严格对齐：
- 组织与多维权限治理表：Department, TenantPolicyEpoch, Position, Role, SysUser,
  UserDepartment, KnowledgeBase, KbGrant
- 知识文档与混合索引表：Document, DocumentReviewItem, DocumentChunk
- 会话历史与审计表：ChatSession, ChatMessage (含完整 source_doc_ids), AuditLog
"""

import uuid
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any
from sqlalchemy import (
    String,
    Integer,
    SmallInteger,
    BigInteger,
    DateTime,
    Text,
    Boolean,
    ForeignKey,
    ForeignKeyConstraint,
    UniqueConstraint,
    CheckConstraint,
    Index,
)
from sqlalchemy.dialects.postgresql import UUID, JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column, relationship
from pgvector.sqlalchemy import Vector
from rag_kb.db.base import Base


class TenantPolicyEpoch(Base):
    """租户级策略版本表 (policy_epoch 持久化单一事实源)"""
    __tablename__ = "sys_tenant_policy_epochs"

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    policy_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class Department(Base):
    """多租户企业组织架构部门树 (sys_departments)"""
    __tablename__ = "sys_departments"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_sys_departments_tenant_id"),
        UniqueConstraint("tenant_id", "path", name="uq_sys_departments_tenant_path"),
        ForeignKeyConstraint(
            ["tenant_id", "parent_id"],
            ["sys_departments.tenant_id", "sys_departments.id"],
            name="fk_sys_departments_tenant_parent",
        ),
        CheckConstraint("status IN ('ACTIVE', 'DISABLED')", name="chk_sys_departments_status"),
        Index("idx_sys_departments_tenant_path", "tenant_id", "path"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, default="default_tenant", index=True)
    parent_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    path: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )

    documents = relationship("Document", back_populates="department_rel")


class Position(Base):
    """职位等级字典表 (sys_positions)"""
    __tablename__ = "sys_positions"
    __table_args__ = (
        CheckConstraint("default_clearance_level BETWEEN 1 AND 4", name="chk_sys_positions_level"),
    )

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True, default="default_tenant")
    position_code: Mapped[str] = mapped_column(String(50), primary_key=True)
    position_name: Mapped[str] = mapped_column(String(100), nullable=False)
    rank_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    default_clearance_level: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class Role(Base):
    """多租户角色策略字典 (sys_roles)"""
    __tablename__ = "sys_roles"
    __table_args__ = (
        CheckConstraint(
            "data_scope IN ('CUSTOM_ACL', 'DEPT_AND_SUB', 'ALL_ORG')",
            name="chk_sys_roles_data_scope_v2",
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True, default="default_tenant")
    role_code: Mapped[str] = mapped_column(String(50), primary_key=True)
    role_name: Mapped[str] = mapped_column(String(100), nullable=False)
    data_scope: Mapped[str] = mapped_column(String(20), nullable=False)
    permissions: Mapped[List[str]] = mapped_column(JSONB, nullable=False, default=list)


class SysUser(Base):
    """员工主档表 (sys_users)"""
    __tablename__ = "sys_users"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_sys_users_tenant_id"),
        UniqueConstraint("tenant_id", "id_source", "external_id", name="uq_sys_users_tenant_source_ext"),
        ForeignKeyConstraint(
            ["tenant_id", "position_code"],
            ["sys_positions.tenant_id", "sys_positions.position_code"],
            name="fk_sys_users_tenant_position",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, default="default_tenant", index=True)
    external_id: Mapped[str] = mapped_column(String(128), nullable=False)
    id_source: Mapped[str] = mapped_column(String(32), nullable=False)
    real_name: Mapped[str] = mapped_column(String(50), nullable=False)
    dept_path: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    position_code: Mapped[str] = mapped_column(String(50), nullable=False, default="STAFF")
    authz_version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )


class UserDepartment(Base):
    """员工多部门归属表 (sys_user_departments，双来源 IDP / MANUAL 共存)"""
    __tablename__ = "sys_user_departments"
    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id", "department_id", "source", name="uq_user_dept_source"),
        CheckConstraint("source IN ('IDP', 'MANUAL')", name="chk_user_dept_source"),
        ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            ["sys_users.tenant_id", "sys_users.id"],
            ondelete="CASCADE",
            name="fk_user_dept_user",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "department_id"],
            ["sys_departments.tenant_id", "sys_departments.id"],
            ondelete="CASCADE",
            name="fk_user_dept_dept",
        ),
        Index("idx_sys_user_departments_tenant_user", "tenant_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, default="default_tenant")
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    department_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    dept_path: Mapped[str] = mapped_column(String(255), nullable=False)
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    granted_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )


class KnowledgeBase(Base):
    """知识库独立实体表 (knowledge_bases，承载 L1-L4 密级区间与可见性策略)"""
    __tablename__ = "knowledge_bases"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_knowledge_bases_tenant_id"),
        UniqueConstraint("tenant_id", "name", name="uq_knowledge_bases_tenant_name"),
        ForeignKeyConstraint(
            ["tenant_id", "owner_department_id"],
            ["sys_departments.tenant_id", "sys_departments.id"],
            name="fk_kb_owner_dept",
        ),
        CheckConstraint("visibility IN ('PUBLIC', 'DEPARTMENT', 'RESTRICTED')", name="chk_kb_visibility"),
        CheckConstraint("status IN ('ACTIVE', 'ARCHIVED')", name="chk_kb_status"),
        CheckConstraint(
            "min_classification_level BETWEEN 1 AND 4 "
            "AND default_doc_level BETWEEN 1 AND 4 "
            "AND max_doc_level BETWEEN 1 AND 4 "
            "AND min_classification_level <= default_doc_level "
            "AND default_doc_level <= max_doc_level",
            name="chk_kb_classification_range",
        ),
        CheckConstraint(
            "visibility <> 'PUBLIC' OR (min_classification_level = 1 AND default_doc_level = 1 AND max_doc_level = 1)",
            name="chk_kb_public_only_l1",
        ),
        Index("idx_knowledge_bases_tenant_vis_dept", "tenant_id", "status", "visibility", "owner_dept_path"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, default="default_tenant", index=True)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    owner_department_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    owner_dept_path: Mapped[str] = mapped_column(String(255), nullable=False)
    visibility: Mapped[str] = mapped_column(String(20), nullable=False, default="DEPARTMENT")
    min_classification_level: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=2)
    default_doc_level: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=2)
    max_doc_level: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=4)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ACTIVE")
    created_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    grants = relationship("KbGrant", back_populates="knowledge_base", cascade="all, delete-orphan")


class KbGrant(Base):
    """知识库精细化 ACL 授权表 (kb_grants: USER / DEPARTMENT[含子部门] / ROLE, ALLOW / DENY)"""
    __tablename__ = "kb_grants"
    __table_args__ = (
        CheckConstraint("subject_type IN ('USER', 'DEPARTMENT', 'ROLE')", name="chk_kb_grants_subject_type"),
        CheckConstraint("action IN ('READ', 'UPLOAD', 'REVIEW', 'DELETE', 'MANAGE')", name="chk_kb_grants_action"),
        CheckConstraint("effect IN ('ALLOW', 'DENY')", name="chk_kb_grants_effect"),
        ForeignKeyConstraint(
            ["tenant_id", "kb_id"],
            ["knowledge_bases.tenant_id", "knowledge_bases.id"],
            ondelete="CASCADE",
            name="fk_kb_grants_tenant_kb",
        ),
        Index("idx_kb_grants_lookup", "tenant_id", "kb_id", "action", "effect"),
        Index("idx_kb_grants_subject", "tenant_id", "subject_type", "subject_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, default="default_tenant")
    kb_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    subject_type: Mapped[str] = mapped_column(String(20), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(128), nullable=False)
    subject_dept_path: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    include_sub_depts: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    action: Mapped[str] = mapped_column(String(20), nullable=False)
    effect: Mapped[str] = mapped_column(String(10), nullable=False)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    granted_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )

    knowledge_base = relationship("KnowledgeBase", back_populates="grants")


class Document(Base):
    """知识库文档主表 (rag_documents)"""
    __tablename__ = "rag_documents"
    __table_args__ = (
        CheckConstraint("classification_level BETWEEN 1 AND 4", name="chk_rag_documents_classification_level"),
        ForeignKeyConstraint(
            ["tenant_id", "kb_id"],
            ["knowledge_bases.tenant_id", "knowledge_bases.id"],
            name="fk_rag_documents_tenant_kb",
        ),
        Index("idx_rag_documents_tenant_kb_level", "tenant_id", "kb_id", "classification_level"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, default="default_tenant", index=True)
    kb_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    classification_level: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    created_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    title: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    file_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    raw_oss_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    enhanced_md_oss_url: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)

    # 组织归属与分类
    department_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sys_departments.id"),
        nullable=False,
        index=True,
    )
    department_path: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    category: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    tags: Mapped[List[str]] = mapped_column(JSONB, default=list)

    # 标题防重专用特征向量 (1024 维 BGE-M3)
    title_embedding: Mapped[Optional[List[float]]] = mapped_column(Vector(1024), nullable=True)

    # 流水线执行状态与 11 节点持久化快照
    overall_status: Mapped[str] = mapped_column(String(32), default="running", index=True)
    current_step_index: Mapped[int] = mapped_column(Integer, default=1)
    pipeline_steps: Mapped[List[dict]] = mapped_column(JSONB, default=list)

    version: Mapped[int] = mapped_column(Integer, default=1)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    department_rel = relationship("Department", back_populates="documents")
    chunks = relationship("DocumentChunk", back_populates="document", cascade="all, delete-orphan")
    review_items = relationship("DocumentReviewItem", back_populates="document", cascade="all, delete-orphan")


class DocumentReviewItem(Base):
    """待审核图元与人工校核修正记录表"""
    __tablename__ = "rag_document_reviews"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("rag_documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    item_key: Mapped[str] = mapped_column(String(64), nullable=False)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    page_idx: Mapped[int] = mapped_column(Integer, nullable=False)
    display_page: Mapped[int] = mapped_column(Integer, nullable=False)
    line_number: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    asset_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    raw_content: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    vlm_description: Mapped[str] = mapped_column(Text, nullable=False)
    user_description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    remark: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="pending")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    document = relationship("Document", back_populates="review_items")


class DocumentChunk(Base):
    """知识库文档切片实体（承载三路混合索引与物理坐标溯源）"""
    __tablename__ = "rag_document_chunks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("rag_documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_label: Mapped[str] = mapped_column(String(64), nullable=False)

    content: Mapped[str] = mapped_column(Text, nullable=False)
    is_code: Mapped[bool] = mapped_column(Boolean, default=False)
    is_table: Mapped[bool] = mapped_column(Boolean, default=False)
    table_html: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # 三路混合索引核心列
    dense_embedding: Mapped[List[float]] = mapped_column(Vector(1024), nullable=False)
    sparse_vector: Mapped[Dict[str, float]] = mapped_column(JSONB, nullable=False)
    tsv_content: Mapped[Any] = mapped_column(TSVECTOR, nullable=False)

    # 溯源与部门冗余元数据（授权一律关联真实 Document & KnowledgeBase 判定）
    page_idx: Mapped[int] = mapped_column(Integer, nullable=False)
    bbox: Mapped[List[int]] = mapped_column(JSONB, default=list)
    breadcrumb: Mapped[List[str]] = mapped_column(JSONB, default=list)
    asset_url: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    department_path: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )

    document = relationship("Document", back_populates="chunks")

    __table_args__ = (
        Index("idx_chunks_dept_doc", "department_path", "document_id"),
    )


class ChatSession(Base):
    """对话会话表 (绑定 tenant_id + user_id 实现员工级私有会话隔离)"""
    __tablename__ = "rag_chat_sessions"
    __table_args__ = (
        Index("idx_chat_sessions_tenant_user", "tenant_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(String(255), nullable=False, default="新会话")
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, default="default_tenant", index=True)
    user_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    messages = relationship(
        "ChatMessage",
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="ChatMessage.created_at",
    )


class ChatMessage(Base):
    """会话消息历史表（source_doc_ids 保存回答实际使用的完整来源文档 ID，用于回看与多轮复核）"""
    __tablename__ = "rag_chat_messages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("rag_chat_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    citations: Mapped[List[dict]] = mapped_column(JSONB, default=list)
    source_doc_ids: Mapped[Optional[List[str]]] = mapped_column(JSONB, nullable=True)
    reasoning_content: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    thinking_process: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )

    session = relationship("ChatSession", back_populates="messages")


class AuditLog(Base):
    """企业级操作与安全审计日志表 (sys_audit_logs)"""
    __tablename__ = "sys_audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, default="default_tenant", index=True)
    user_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    resource_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default="SUCCESS", index=True)
    error_msg: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    details: Mapped[Dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )
