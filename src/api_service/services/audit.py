import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from api_service.db.models import AuditEvent


def record(
    db: AsyncSession,
    event_type: str,
    *,
    file_id: uuid.UUID | None = None,
    actor_user_id: uuid.UUID | None = None,
    ttl_seconds: int | None = None,
    ip_address: str | None = None,
) -> None:
    """Stage an audit row in the caller's transaction, so it commits (or not) with the action."""
    db.add(
        AuditEvent(
            event_type=event_type,
            file_id=file_id,
            actor_user_id=actor_user_id,
            ttl_seconds=ttl_seconds,
            ip_address=ip_address,
        )
    )
