import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, StrictInt


class AvailabilityOut(BaseModel):
    state: str
    usable_shards: int
    total_shards: int
    required_shards: int


class FileOut(BaseModel):
    id: uuid.UUID
    filename: str
    content_type: str
    size_bytes: int
    checksum_sha256: str
    uploaded_at: datetime
    status: str
    availability: AvailabilityOut


class FileList(BaseModel):
    items: list[FileOut]
    limit: int
    offset: int


class LinkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Strict: rejects "60", 60.5 and true rather than coercing them.
    ttl_seconds: StrictInt


class LinkOut(BaseModel):
    url: str
    expires_at: datetime
