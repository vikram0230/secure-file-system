"""Maintenance worker entrypoint: ``python -m api_service.worker``.

Exactly one worker is active: it holds a Postgres session-level advisory lock.
Extra replicas wait as hot standbys and take over if the holder's connection
drops.
"""

import asyncio
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from api_service.config import get_settings
from api_service.db.session import make_async_engine, make_async_sessionmaker
from api_service.main import configure_logging
from api_service.services.maintenance import Maintenance
from api_service.services.node_client import NodeClient

log = logging.getLogger("api_service.worker")

ADVISORY_LOCK_KEY = 0x5F5_0001


async def _try_lock(conn: AsyncConnection) -> bool:
    return bool(
        await conn.scalar(text("SELECT pg_try_advisory_lock(:k)"), {"k": ADVISORY_LOCK_KEY})
    )


async def run() -> None:
    settings = get_settings()
    configure_logging()
    engine = make_async_engine(settings.database_url)
    nodes = NodeClient(settings.node_token, settings.node_request_timeout_seconds)
    try:
        async with engine.connect() as conn:
            lock_conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
            while not await _try_lock(lock_conn):
                log.info("another worker holds the lock; standing by")
                await asyncio.sleep(settings.worker_interval_seconds)
            log.info("maintenance worker active")

            maintenance = Maintenance(make_async_sessionmaker(engine), nodes, settings)
            while True:
                # If the lock connection died, the lock is gone: exit and let the
                # supervisor restart us rather than run alongside a new holder.
                await lock_conn.scalar(text("SELECT 1"))
                await maintenance.run_cycle()
                await asyncio.sleep(settings.worker_interval_seconds)
    finally:
        await nodes.aclose()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(run())
