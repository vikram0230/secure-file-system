import uuid

import httpx
from sqlalchemy import select, text

from api_service.db.models import AuditEvent, Shard, StorageNode


async def upload(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    data: bytes = b"hello distributed world" * 100,
    filename: str = "report.pdf",
    content_type: str = "application/pdf",
) -> httpx.Response:
    return await client.post(
        "/files", headers=headers, files={"file": (filename, data, content_type)}
    )


async def uploaded_id(client, headers, data: bytes = b"payload bytes" * 50) -> str:
    resp = await upload(client, headers, data)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def make_link(client, headers, file_id: str, ttl: int = 300) -> str:
    resp = await client.post(f"/files/{file_id}/links", headers=headers, json={"ttl_seconds": ttl})
    assert resp.status_code == 201, resp.text
    return resp.json()["url"]


def shard_locations(db_session, file_id: str) -> list[tuple[uuid.UUID, str, int]]:
    """(shard_id, node address, shard_index) for a file, ordered by index."""
    db_session.expire_all()
    rows = db_session.execute(
        select(Shard.id, StorageNode.address, Shard.shard_index)
        .join(StorageNode, Shard.node_id == StorageNode.id)
        .where(Shard.file_id == uuid.UUID(file_id))
        .order_by(Shard.shard_index)
    )
    result = list(rows.tuples())
    db_session.commit()
    return result


def audit_events(db_session, event_type: str) -> list[AuditEvent]:
    db_session.expire_all()
    events = list(db_session.scalars(select(AuditEvent).where(AuditEvent.event_type == event_type)))
    db_session.commit()
    return events


def set_node_status(db_session, addresses, status: str, last_seen_sql: str = "now()") -> None:
    db_session.execute(
        text(
            f"UPDATE storage_nodes SET status = :s, last_seen_at = {last_seen_sql} "
            "WHERE address = ANY(:a)"
        ),
        {"s": status, "a": list(addresses)},
    )
    db_session.commit()


def corrupt(path) -> None:
    raw = bytearray(path.read_bytes())
    raw[-1] ^= 0xFF
    path.write_bytes(bytes(raw))
