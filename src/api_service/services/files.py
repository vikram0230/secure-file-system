"""File lifecycle over the erasure-coded storage fleet. See docs/DESIGN.md §4 and §7."""

import asyncio
import hashlib
import logging
import random
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from api_service.db.models import File, Shard, StorageNode, User
from api_service.services import audit, erasure
from api_service.services.node_client import NodeClient, NodeError

log = logging.getLogger(__name__)


class InsufficientNodesError(Exception):
    """Fewer healthy nodes than k + m; refuse rather than store with reduced redundancy."""


class UploadFailedError(Exception):
    """A shard write failed; the upload was rolled back (or handed to GC)."""


class FileUnavailableError(Exception):
    """Fewer than k usable shards could be read."""


class FileCorruptedError(Exception):
    """The decoded file failed its whole-file checksum."""


@dataclass(frozen=True)
class Availability:
    state: str  # healthy | degraded | unavailable
    usable_shards: int
    total_shards: int
    required_shards: int


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def healthy_nodes(db: AsyncSession) -> list[StorageNode]:
    return list(await db.scalars(select(StorageNode).where(StorageNode.status == "healthy")))


async def upload_file(
    db: AsyncSession,
    nodes: NodeClient,
    *,
    owner: User,
    filename: str,
    content_type: str,
    data: bytes,
    k: int,
    m: int,
    ip_address: str | None,
) -> File:
    blocks = await run_in_threadpool(erasure.encode, data, k, m)
    file_checksum = await run_in_threadpool(sha256_hex, data)

    candidates = await healthy_nodes(db)
    if len(candidates) < k + m:
        raise InsufficientNodesError(f"{len(candidates)} healthy nodes, need {k + m}")
    placement = random.sample(candidates, k + m)

    file = File(
        id=uuid.uuid4(),
        owner_user_id=owner.id,
        original_name=filename,
        content_type=content_type,
        size_bytes=len(data),
        checksum_sha256=file_checksum,
        data_shards=k,
        parity_shards=m,
        status="uploading",
    )
    shards = [
        Shard(
            id=uuid.uuid4(),
            file_id=file.id,
            shard_index=index,
            kind="data" if index < k else "parity",
            node_id=node.id,
            checksum_sha256=sha256_hex(block),
            size_bytes=len(block),
        )
        for index, (node, block) in enumerate(zip(placement, blocks, strict=True))
    ]
    # Rows before bytes: if this process dies mid-upload, GC can find every shard.
    db.add(file)
    db.add_all(shards)
    await db.commit()

    results = await asyncio.gather(
        *(
            nodes.put_shard(node.address, str(shard.id), block)
            for node, shard, block in zip(placement, shards, blocks, strict=True)
        ),
        return_exceptions=True,
    )
    failures = [r for r in results if isinstance(r, BaseException)]
    if failures:
        log.warning("upload %s failed on %d shard(s): %s", file.id, len(failures), failures[0])
        await _roll_back_upload(db, nodes, file, zip(placement, shards, strict=True))
        raise UploadFailedError(str(failures[0]))

    file.status = "available"
    audit.record(
        db, "file_uploaded", file_id=file.id, actor_user_id=owner.id, ip_address=ip_address
    )
    await db.commit()
    return file


async def _roll_back_upload(
    db: AsyncSession,
    nodes: NodeClient,
    file: File,
    placed: Iterable[tuple[StorageNode, Shard]],
) -> None:
    # Delete every shard, not just acknowledged ones: a timed-out PUT may have landed.
    results = await asyncio.gather(
        *(nodes.delete_shard(node.address, str(shard.id)) for node, shard in placed),
        return_exceptions=True,
    )
    if any(isinstance(r, BaseException) for r in results):
        log.warning("upload %s cleanup incomplete; leaving it for GC", file.id)
        return
    # Shard rows go with it via ON DELETE CASCADE.
    await db.execute(delete(File).where(File.id == file.id))
    await db.commit()


async def shard_candidates(
    db: AsyncSession, file: File, exclude: set[uuid.UUID] = frozenset()
) -> list[tuple[Shard, StorageNode]]:
    """Readable shards, best first: healthy nodes, then data shards (cheapest decode)."""
    rows = await db.execute(
        select(Shard, StorageNode)
        .join(StorageNode, Shard.node_id == StorageNode.id)
        .where(Shard.file_id == file.id, StorageNode.status != "decommissioned")
    )
    candidates = [(s, n) for s, n in rows.tuples() if s.id not in exclude]
    candidates.sort(key=lambda sn: (sn[1].status != "healthy", sn[0].shard_index))
    return candidates


async def fetch_blocks(
    nodes: NodeClient, candidates: Sequence[tuple[Shard, StorageNode]], k: int
) -> dict[int, bytes]:
    """Read k verified blocks, moving on to further candidates as reads fail."""
    collected: dict[int, bytes] = {}
    remaining = list(candidates)
    while len(collected) < k and remaining:
        batch, remaining = remaining[: k - len(collected)], remaining[k - len(collected) :]
        results = await asyncio.gather(
            *(nodes.get_shard(node.address, str(shard.id)) for shard, node in batch),
            return_exceptions=True,
        )
        for (shard, node), result in zip(batch, results, strict=True):
            if isinstance(result, NodeError):
                log.info("shard %s on %s unreadable: %s", shard.id, node.address, result)
            elif isinstance(result, BaseException):
                raise result
            elif sha256_hex(result) != shard.checksum_sha256:
                log.warning("shard %s on %s failed checksum", shard.id, node.address)
            else:
                collected[shard.shard_index] = result
    return collected


async def read_file(db: AsyncSession, nodes: NodeClient, file: File) -> bytes:
    candidates = await shard_candidates(db, file)
    blocks = await fetch_blocks(nodes, candidates, file.data_shards)
    if len(blocks) < file.data_shards:
        raise FileUnavailableError(f"{file.id}: {len(blocks)}/{file.data_shards} shards readable")

    data = await run_in_threadpool(
        erasure.decode, blocks, file.data_shards, file.parity_shards, file.size_bytes
    )
    if await run_in_threadpool(sha256_hex, data) != file.checksum_sha256:
        raise FileCorruptedError(str(file.id))
    return data


async def availability(db: AsyncSession, files: Sequence[File]) -> dict[uuid.UUID, Availability]:
    if not files:
        return {}
    rows = await db.execute(
        select(
            Shard.file_id,
            func.count().filter(StorageNode.status == "healthy"),
        )
        .join(StorageNode, Shard.node_id == StorageNode.id)
        .where(Shard.file_id.in_([f.id for f in files]))
        .group_by(Shard.file_id)
    )
    usable = {file_id: count for file_id, count in rows.tuples()}
    result = {}
    for file in files:
        total = file.data_shards + file.parity_shards
        count = usable.get(file.id, 0)
        if count >= total:
            state = "healthy"
        elif count >= file.data_shards:
            state = "degraded"
        else:
            state = "unavailable"
        result[file.id] = Availability(state, count, total, file.data_shards)
    return result


async def delete_file(
    db: AsyncSession, nodes: NodeClient, file: File, *, actor: User, ip_address: str | None
) -> None:
    file.status = "deleted"
    file.deleted_at = datetime.now(UTC)
    file.link_version += 1  # revokes every outstanding signed link
    audit.record(db, "file_deleted", file_id=file.id, actor_user_id=actor.id, ip_address=ip_address)
    await db.commit()
    await remove_deleted_shards(db, nodes, file)


async def remove_deleted_shards(db: AsyncSession, nodes: NodeClient, file: File) -> int:
    """Best-effort shard removal for a deleted file; the worker retries what's left."""
    rows = (
        await db.execute(
            select(Shard, StorageNode)
            .join(StorageNode, Shard.node_id == StorageNode.id)
            .where(Shard.file_id == file.id)
        )
    ).tuples()
    placed = list(rows)
    results = await asyncio.gather(
        *(nodes.delete_shard(node.address, str(shard.id)) for shard, node in placed),
        return_exceptions=True,
    )
    removed = 0
    for (shard, _node), result in zip(placed, results, strict=True):
        if not isinstance(result, BaseException):
            await db.delete(shard)
            removed += 1
    await db.commit()
    return removed
