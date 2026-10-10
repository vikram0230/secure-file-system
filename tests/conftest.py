"""Shared fixtures.

Database-backed tests need a real Postgres (the schema uses INET, UUID and
ON CONFLICT). Point SFS_TEST_DATABASE_URL at a database whose name ends in
``_test``; it is created if missing, migrated with Alembic, and truncated
between tests. Without the variable those tests are skipped.
"""

import os
import secrets
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url

from api_service.config import Settings, get_database_settings
from api_service.db.models import StorageNode, User
from api_service.db.session import make_sessionmaker
from api_service.main import create_app
from api_service.services.auth import generate_api_key, hash_api_key
from storage_node.config import Settings as NodeSettings
from storage_node.main import create_app as create_node_app

ROOT = Path(__file__).resolve().parent.parent
TEST_DB_ENV = "SFS_TEST_DATABASE_URL"
NODE_TOKEN = secrets.token_hex(32)
NODE_COUNT = 7  # k + m = 6, plus one spare so repair has somewhere to go


def _ensure_database(url: URL) -> None:
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            exists = conn.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": url.database}
            )
            if not exists:
                conn.execute(text(f'CREATE DATABASE "{url.database}"'))
    finally:
        admin.dispose()


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    raw = os.environ.get(TEST_DB_ENV)
    if not raw:
        if os.environ.get("CI"):
            pytest.fail(f"{TEST_DB_ENV} must be set in CI; database tests may not be skipped")
        pytest.skip(f"set {TEST_DB_ENV} to run database tests")
    url = make_url(raw)
    if not (url.database or "").endswith("_test"):
        pytest.fail(f"{TEST_DB_ENV} must name a *_test database; refusing to truncate it")

    _ensure_database(url)
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("SFS_DATABASE_URL", raw)
        get_database_settings.cache_clear()
        command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
    get_database_settings.cache_clear()
    yield raw


@pytest.fixture
def db_session(database_url):
    """Synchronous session for arranging and asserting database state."""
    sessionmaker = make_sessionmaker(database_url)
    with sessionmaker() as session:
        tables = "audit_events, shards, files, storage_nodes, users"
        session.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
        session.commit()
        yield session
    sessionmaker.kw["bind"].dispose()


class Fleet(httpx.AsyncBaseTransport):
    """Routes node traffic to in-process storage nodes, with fault injection."""

    def __init__(self, root: Path) -> None:
        self.addresses = [f"node-{i}:8000" for i in range(NODE_COUNT)]
        self.dirs = {a: root / a.split(":")[0] for a in self.addresses}
        self._transports = {
            address: httpx.ASGITransport(
                create_node_app(NodeSettings(_env_file=None, shard_dir=str(path), token=NODE_TOKEN))
            )
            for address, path in self.dirs.items()
        }
        self.down: set[str] = set()
        self.fail_writes: set[str] = set()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        address = f"{request.url.host}:{request.url.port}"
        if address in self.down:
            raise httpx.ConnectError(f"{address} is down", request=request)
        if address in self.fail_writes and request.method == "PUT":
            return httpx.Response(507, request=request)
        return await self._transports[address].handle_async_request(request)

    def shard_path(self, address: str, shard_id: uuid.UUID) -> Path:
        return self.dirs[address] / str(shard_id)


@pytest.fixture
def fleet(tmp_path: Path, db_session) -> Fleet:
    fleet = Fleet(tmp_path)
    db_session.add_all(StorageNode(address=a) for a in fleet.addresses)
    db_session.commit()
    return fleet


@pytest.fixture
def settings(database_url) -> Settings:
    return Settings(
        _env_file=None,
        database_url=database_url,
        secret_key=secrets.token_hex(32),
        node_token=NODE_TOKEN,
        max_upload_bytes=1_000_000,
        download_rate_per_minute=10_000,
        api_rate_per_minute=10_000,
        repair_grace_seconds=0,
    )


@pytest.fixture
async def app(settings, fleet):
    app = create_app(settings, node_transport=fleet)
    async with app.router.lifespan_context(app):
        yield app


@pytest.fixture
async def client(app) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://testserver"
    ) as client:
        yield client


def make_user(db_session, email: str) -> tuple[uuid.UUID, dict[str, str]]:
    api_key = generate_api_key()
    user = User(email=email, api_key_hash=hash_api_key(api_key))
    db_session.add(user)
    db_session.commit()
    return user.id, {"Authorization": f"Bearer {api_key}"}


@pytest.fixture
def alice(db_session) -> dict[str, str]:
    return make_user(db_session, "alice@example.com")[1]


@pytest.fixture
def bob(db_session) -> dict[str, str]:
    return make_user(db_session, "bob@example.com")[1]
