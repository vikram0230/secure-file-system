import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import INET, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from api_service.db.base import Base

FILE_STATUSES = ("uploading", "available", "deleted")
NODE_STATUSES = ("healthy", "unreachable", "decommissioned")
SHARD_KINDS = ("data", "parity")
AUDIT_EVENT_TYPES = (
    "file_uploaded",
    "link_generated",
    "download_success",
    "download_denied",
    "file_deleted",
)


def _in_check(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String, unique=True)
    # SHA-256 of a high-entropy random key; a slow KDF adds nothing for 256-bit secrets.
    api_key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    files: Mapped[list["File"]] = relationship(back_populates="owner")


class File(Base):
    __tablename__ = "files"
    __table_args__ = (
        CheckConstraint(_in_check("status", FILE_STATUSES), name="ck_files_status"),
        CheckConstraint("size_bytes > 0", name="ck_files_size_positive"),
        CheckConstraint("data_shards >= 1 AND parity_shards >= 1", name="ck_files_shard_counts"),
        Index("idx_files_owner", "owner_user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    original_name: Mapped[str] = mapped_column(String)
    content_type: Mapped[str] = mapped_column(String)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    checksum_sha256: Mapped[str] = mapped_column(String(64))
    # Erasure-coding parameters are frozen per file so changing the global
    # config never makes previously written files undecodable.
    data_shards: Mapped[int] = mapped_column(SmallInteger)
    parity_shards: Mapped[int] = mapped_column(SmallInteger)
    # Part of the signed-URL payload; bumping it revokes every outstanding link.
    link_version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    status: Mapped[str] = mapped_column(String, default="uploading", server_default="uploading")
    uploaded_at: Mapped[datetime] = mapped_column(server_default=func.now())
    deleted_at: Mapped[datetime | None]

    owner: Mapped["User"] = relationship(back_populates="files")
    shards: Mapped[list["Shard"]] = relationship(
        back_populates="file", cascade="all, delete-orphan"
    )


class StorageNode(Base):
    __tablename__ = "storage_nodes"
    __table_args__ = (
        CheckConstraint(_in_check("status", NODE_STATUSES), name="ck_storage_nodes_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # "host:port" reachable from the API service (private VPC address in production).
    address: Mapped[str] = mapped_column(String, unique=True)
    status: Mapped[str] = mapped_column(String, default="healthy", server_default="healthy")
    last_seen_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    shards: Mapped[list["Shard"]] = relationship(back_populates="node")


class Shard(Base):
    __tablename__ = "shards"
    __table_args__ = (
        UniqueConstraint("file_id", "shard_index", name="uq_shards_file_index"),
        CheckConstraint(_in_check("kind", SHARD_KINDS), name="ck_shards_kind"),
        CheckConstraint("shard_index >= 0", name="ck_shards_index_nonnegative"),
        Index("idx_shards_node", "node_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    file_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("files.id", ondelete="CASCADE"))
    shard_index: Mapped[int] = mapped_column(SmallInteger)
    kind: Mapped[str] = mapped_column(String)
    node_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("storage_nodes.id"))
    checksum_sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    file: Mapped["File"] = relationship(back_populates="shards")
    node: Mapped["StorageNode"] = relationship(back_populates="shards")


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        CheckConstraint(_in_check("event_type", AUDIT_EVENT_TYPES), name="ck_audit_event_type"),
        Index("idx_audit_file", "file_id"),
        Index("idx_audit_created_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Nullable so a denied download for a nonexistent file can still be recorded.
    file_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("files.id", ondelete="SET NULL"))
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    event_type: Mapped[str] = mapped_column(String)
    ttl_seconds: Mapped[int | None] = mapped_column(Integer)
    ip_address: Mapped[str | None] = mapped_column(INET)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
