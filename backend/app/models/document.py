import uuid
from datetime import datetime, timezone
from typing import List, Optional, Any
from sqlalchemy import String, Integer, DateTime, Text, Boolean, ForeignKey
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from pgvector.sqlalchemy import Vector
from backend.app.core.database import Base

class Department(Base):
    """企业组织架构部门表（支持树状层级与动态新增，对应 Img 01）"""
    __tablename__ = "sys_departments"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True, index=True)
    path: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc)
    )

    documents = relationship("Document", back_populates="department_rel")

class Document(Base):
    """知识库文档主表"""
    __tablename__ = "rag_documents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    file_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    raw_oss_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    enhanced_md_oss_url: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)

    # 组织权限隔离与分类
    department_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sys_departments.id"),
        nullable=False,
        index=True
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
        default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc)
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
        index=True
    )
    item_key: Mapped[str] = mapped_column(String(64), nullable=False)  # tbl_rev_0, img_rev_1
    type: Mapped[str] = mapped_column(String(32), nullable=False)       # table / image
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    page_idx: Mapped[int] = mapped_column(Integer, nullable=False)
    display_page: Mapped[int] = mapped_column(Integer, nullable=False)
    line_number: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    asset_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    raw_content: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    vlm_description: Mapped[str] = mapped_column(Text, nullable=False)
    user_description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    remark: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="pending")  # pending / approved / modified

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc)
    )

    document = relationship("Document", back_populates="review_items")
