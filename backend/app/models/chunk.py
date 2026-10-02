import uuid
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
from sqlalchemy import String, Integer, DateTime, Text, Boolean, ForeignKey, Index
from sqlalchemy.dialects.postgresql import UUID, JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column, relationship
from pgvector.sqlalchemy import Vector
from backend.app.core.database import Base

class DocumentChunk(Base):
    """
    知识库文档切片实体（承载 Phase 2 三路混合索引与 Phase 3 溯源召回）
    - 稠密向量 dense_embedding (1024 维 BGE-M3)
    - 归一化稀疏向量 sparse_vector (JSONB 格式)
    - 全文分词 tsv_content (TSVECTOR 格式)
    - 原文页码与坐标 page_idx / bbox / asset_url
    """
    __tablename__ = "rag_document_chunks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("rag_documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_label: Mapped[str] = mapped_column(String(64), nullable=False) # 如 "技术部 · 第 12 片" (对应溯源标签)

    content: Mapped[str] = mapped_column(Text, nullable=False)
    is_code: Mapped[bool] = mapped_column(Boolean, default=False)
    is_table: Mapped[bool] = mapped_column(Boolean, default=False)
    table_html: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # 三路混合索引核心列
    dense_embedding: Mapped[List[float]] = mapped_column(Vector(1024), nullable=False)
    sparse_vector: Mapped[Dict[str, float]] = mapped_column(JSONB, nullable=False)
    tsv_content: Mapped[Any] = mapped_column(TSVECTOR, nullable=False)

    # 溯源与权限元数据
    page_idx: Mapped[int] = mapped_column(Integer, nullable=False)
    bbox: Mapped[List[int]] = mapped_column(JSONB, default=list)
    breadcrumb: Mapped[List[str]] = mapped_column(JSONB, default=list)
    asset_url: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    department_path: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc)
    )

    document = relationship("Document", back_populates="chunks")

    __table_args__ = (
        Index("idx_chunks_dept_doc", "department_path", "document_id"),
    )
