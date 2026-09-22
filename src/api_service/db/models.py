import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, ForeignKey, Integer, SmallInteger, String
from sqlalchemy.dialects.postgresql import INET, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from api_service.db.base import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)

    files: Mapped[list["File"]] = relationship(back_populates="owner")


class File(Base):
    __tablename__ = "files"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    original_name: Mapped[str] = mapped_column(String, nullable=False)
    content_type: Mapped[str] = mapped_column(String, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    checksum_sha256: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="uploading")
    uploaded_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)

    owner: Mapped["User"] = relationship(back_populates="files")
    shards: Mapped[list["Shard"]] = relationship(back_populates="file", cascade="all, delete-orphan")
    audit_events: Mapped[list["AuditEvent"]] = relationship(
        back_populates="file", cascade="all, delete-orphan"
    )


class StorageNode(Base):
    __tablename__ = "storage_nodes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hostname: Mapped[str] = mapped_column(String, nullable=False)
    private_ip: Mapped[str] = mapped_column(INET, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="healthy")

    shards: Mapped[list["Shard"]] = relationship(back_populates="node")


class Shard(Base):
    __tablename__ = "shards"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    file_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("files.id"), nullable=False)
    shard_index: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)  # data | parity
    node_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("storage_nodes.id"), nullable=False
    )
    checksum_sha256: Mapped[str] = mapped_column(String, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)

    file: Mapped["File"] = relationship(back_populates="shards")
    node: Mapped["StorageNode"] = relationship(back_populates="shards")


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    file_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("files.id"), nullable=False)
    actor_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    ttl_seconds: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    ip_address: Mapped[Optional[str]] = mapped_column(INET, nullable=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)

    file: Mapped["File"] = relationship(back_populates="audit_events")
