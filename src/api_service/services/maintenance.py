"""Background upkeep of the storage fleet. See docs/DESIGN.md §7.5-§7.6."""

import asyncio
import logging
import random
import uuid
from collections import defaultdict
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.concurrency import run_in_threadpool

from api_service.config import Settings
from api_service.db.models import File, Shard, StorageNode
from api_service.services import erasure, files
from api_service.services.node_client import (
    NodeClient,
    NodeError,
    ShardCorruptError,
    ShardMissingError,
)

log = logging.getLogger(__name__)


class Maintenance:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        nodes: NodeClient,
        settings: Settings,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._nodes = nodes
        self._settings = settings
        self._consecutive_failures: dict[uuid.UUID, int] = defaultdict(int)

    async def run_cycle(self) -> None:
        for job in (
            self.check_node_health,
            self.gc_stale_uploads,
            self.gc_deleted_files,
            self.repair_lost_shards,
            self.scrub,
        ):
            try:
                await job()
            except Exception:
                # One failing job must not starve the others.
                log.exception("maintenance job %s failed", job.__name__)

    async def check_node_health(self) -> None:
        async with self._sessionmaker() as db:
            nodes = list(
                await db.scalars(select(StorageNode).where(StorageNode.status != "decommissioned"))
            )
            results = await asyncio.gather(*(self._nodes.is_healthy(n.address) for n in nodes))
            now = datetime.now(UTC)
            for node, healthy in zip(nodes, results, strict=True):
                if healthy:
                    self._consecutive_failures[node.id] = 0
                    node.last_seen_at = now
                    if node.status != "healthy":
                        log.info("node %s is healthy again", node.address)
                        node.status = "healthy"
                    continue
                self._consecutive_failures[node.id] += 1
                failures = self._consecutive_failures[node.id]
                if node.status == "healthy" and failures >= self._settings.node_failure_threshold:
                    log.warning("node %s unreachable after %d probes", node.address, failures)
                    node.status = "unreachable"
            await db.commit()

    async def gc_stale_uploads(self) -> None:
        cutoff = datetime.now(UTC) - timedelta(seconds=self._settings.stale_upload_seconds)
        async with self._sessionmaker() as db:
            stale = list(
                await db.scalars(
                    select(File).where(File.status == "uploading", File.uploaded_at < cutoff)
                )
            )
            for file in stale:
                placed = await self._placed_shards(db, file.id)
                results = await asyncio.gather(
                    *(self._nodes.delete_shard(n.address, str(s.id)) for s, n in placed),
                    return_exceptions=True,
                )
                if any(isinstance(r, BaseException) for r in results):
                    log.info("stale upload %s: some shards not yet removable", file.id)
                    continue
                await db.execute(delete(File).where(File.id == file.id))
                await db.commit()
                log.info("removed stale upload %s", file.id)

    async def gc_deleted_files(self) -> None:
        async with self._sessionmaker() as db:
            pending = list(
                await db.scalars(
                    select(File)
                    .where(File.status == "deleted")
                    .where(select(Shard.id).where(Shard.file_id == File.id).exists())
                )
            )
            for file in pending:
                removed = await files.remove_deleted_shards(db, self._nodes, file)
                log.info("deleted file %s: removed %d shard(s)", file.id, removed)

    async def repair_lost_shards(self) -> int:
        """Rebuild shards on nodes that are decommissioned or down past the grace period."""
        cutoff = datetime.now(UTC) - timedelta(seconds=self._settings.repair_grace_seconds)
        async with self._sessionmaker() as db:
            rows = await db.execute(
                select(Shard.file_id, Shard.id)
                .join(StorageNode, Shard.node_id == StorageNode.id)
                .join(File, Shard.file_id == File.id)
                .where(File.status == "available")
                .where(
                    or_(
                        StorageNode.status == "decommissioned",
                        (StorageNode.status == "unreachable")
                        & (
                            func.coalesce(StorageNode.last_seen_at, StorageNode.created_at) < cutoff
                        ),
                    )
                )
            )
            lost: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
            for file_id, shard_id in rows.tuples():
                lost[file_id].add(shard_id)

        repaired = 0
        for file_id, shard_ids in lost.items():
            repaired += await self.repair_file(file_id, shard_ids)
        return repaired

    async def scrub(self) -> int:
        """Read a random sample of shards to find corruption before a download does."""
        if self._settings.scrub_batch_size == 0:
            return 0
        async with self._sessionmaker() as db:
            sample = (
                await db.execute(
                    select(Shard, StorageNode)
                    .join(StorageNode, Shard.node_id == StorageNode.id)
                    .join(File, Shard.file_id == File.id)
                    .where(File.status == "available", StorageNode.status == "healthy")
                    .order_by(func.random())
                    .limit(self._settings.scrub_batch_size)
                )
            ).tuples()
            sample = list(sample)

        bad: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
        for shard, node in sample:
            try:
                data = await self._nodes.get_shard(node.address, str(shard.id))
            except (ShardMissingError, ShardCorruptError) as exc:
                log.warning("scrub: %s", exc)
                bad[shard.file_id].add(shard.id)
                continue
            except NodeError:
                continue  # node trouble is the health checker's job
            if files.sha256_hex(data) != shard.checksum_sha256:
                log.warning("scrub: shard %s on %s mismatches metadata", shard.id, node.address)
                bad[shard.file_id].add(shard.id)

        repaired = 0
        for file_id, shard_ids in bad.items():
            repaired += await self.repair_file(file_id, shard_ids)
        return repaired

    async def repair_file(self, file_id: uuid.UUID, bad_shard_ids: set[uuid.UUID]) -> int:
        """Regenerate the given shards from k good ones onto nodes not holding this file."""
        async with self._sessionmaker() as db:
            file = await db.get(File, file_id)
            if file is None or file.status != "available":
                return 0
            placed = await self._placed_shards(db, file_id)
            bad = [(s, n) for s, n in placed if s.id in bad_shard_ids]
            good = await files.shard_candidates(db, file, exclude=bad_shard_ids)
            blocks = await files.fetch_blocks(self._nodes, good, file.data_shards)
            if len(blocks) < file.data_shards:
                log.error("repair %s: only %d readable shards", file_id, len(blocks))
                return 0

            occupied = {n.id for _, n in placed}
            targets = [n for n in await files.healthy_nodes(db) if n.id not in occupied]
            random.shuffle(targets)
            if len(targets) < len(bad):
                log.warning(
                    "repair %s: %d spare node(s) for %d shard(s)", file_id, len(targets), len(bad)
                )

            rebuilt = await run_in_threadpool(
                erasure.regenerate,
                blocks,
                file.data_shards,
                file.parity_shards,
                [s.shard_index for s, _ in bad],
            )
            repaired = 0
            for (shard, old_node), block, target in zip(bad, rebuilt, targets, strict=False):
                if files.sha256_hex(block) != shard.checksum_sha256:
                    log.error(
                        "repair %s: regenerated shard %d mismatches", file_id, shard.shard_index
                    )
                    continue
                old_id, new_id = shard.id, uuid.uuid4()
                try:
                    await self._nodes.put_shard(target.address, str(new_id), block)
                except NodeError as exc:
                    log.warning("repair %s: write to %s failed: %s", file_id, target.address, exc)
                    continue
                # Shards are immutable, so the rebuilt copy gets a new id (= object key).
                # Core UPDATE (not ORM-synchronized) so ``shard`` isn't mutated under us.
                await db.execute(
                    update(Shard)
                    .where(Shard.id == old_id)
                    .values(id=new_id, node_id=target.id, created_at=func.now())
                    .execution_options(synchronize_session=False)
                )
                await db.commit()
                repaired += 1
                log.info(
                    "repaired shard %d of %s: %s -> %s",
                    shard.shard_index,
                    file_id,
                    old_node.address,
                    target.address,
                )
                try:
                    await self._nodes.delete_shard(old_node.address, str(old_id))
                except NodeError:
                    pass  # old node is down; its copy is an orphan (DESIGN.md §12)
            return repaired

    @staticmethod
    async def _placed_shards(
        db: AsyncSession, file_id: uuid.UUID
    ) -> list[tuple[Shard, StorageNode]]:
        rows = await db.execute(
            select(Shard, StorageNode)
            .join(StorageNode, Shard.node_id == StorageNode.id)
            .where(Shard.file_id == file_id)
        )
        return list(rows.tuples())
