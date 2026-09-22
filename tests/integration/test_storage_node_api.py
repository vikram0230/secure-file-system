import secrets
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from storage_node.config import Settings
from storage_node.main import create_app
from storage_node.storage import sha256_hex

TOKEN = secrets.token_hex(32)
MAX_SHARD_BYTES = 1024


@pytest.fixture
def app(tmp_path: Path):
    return create_app(
        Settings(
            _env_file=None,
            shard_dir=str(tmp_path),
            token=TOKEN,
            max_shard_bytes=MAX_SHARD_BYTES,
        )
    )


@pytest.fixture
def client(app) -> Iterator[TestClient]:
    yield TestClient(app, headers={"X-Node-Token": TOKEN})


def _put(client: TestClient, shard_id: str, body: bytes, checksum: str | None = None):
    return client.put(
        f"/shards/{shard_id}",
        content=body,
        headers={"X-Checksum-Sha256": checksum or sha256_hex(body)},
    )


def test_put_get_delete_round_trip(client: TestClient):
    shard_id = str(uuid.uuid4())
    body = b"hello shard"

    put_resp = _put(client, shard_id, body)
    assert put_resp.status_code == 201
    assert put_resp.json() == {
        "shard_id": shard_id,
        "checksum_sha256": sha256_hex(body),
        "size_bytes": len(body),
    }

    get_resp = client.get(f"/shards/{shard_id}")
    assert get_resp.status_code == 200
    assert get_resp.content == body
    assert get_resp.headers["X-Checksum-Sha256"] == sha256_hex(body)

    assert client.delete(f"/shards/{shard_id}").status_code == 204
    assert client.get(f"/shards/{shard_id}").status_code == 404


@pytest.mark.parametrize("token", [None, "wrong-token"])
def test_rejects_missing_or_wrong_token(app, token: str | None):
    headers = {"X-Node-Token": token} if token else {}
    fresh = TestClient(app, headers=headers)
    assert fresh.get(f"/shards/{uuid.uuid4()}").status_code == 401


def test_healthz_needs_no_token(app):
    assert TestClient(app).get("/healthz").status_code == 200


def test_get_nonexistent_shard_returns_404(client: TestClient):
    assert client.get(f"/shards/{uuid.uuid4()}").status_code == 404


def test_put_rejects_non_uuid_shard_id(client: TestClient):
    assert _put(client, "not-a-uuid", b"data").status_code == 400


def test_put_rejects_empty_body(client: TestClient):
    assert _put(client, str(uuid.uuid4()), b"").status_code == 400


def test_put_rejects_checksum_mismatch(client: TestClient):
    resp = _put(client, str(uuid.uuid4()), b"data", checksum=sha256_hex(b"other"))
    assert resp.status_code == 400


def test_put_requires_checksum_header(client: TestClient):
    assert client.put(f"/shards/{uuid.uuid4()}", content=b"data").status_code == 422


def test_put_rejects_oversized_shard(client: TestClient):
    assert _put(client, str(uuid.uuid4()), b"x" * (MAX_SHARD_BYTES + 1)).status_code == 413


def test_put_existing_shard_returns_409(client: TestClient):
    shard_id = str(uuid.uuid4())
    assert _put(client, shard_id, b"first").status_code == 201
    assert _put(client, shard_id, b"second").status_code == 409
    assert client.get(f"/shards/{shard_id}").content == b"first"


def test_get_detects_corruption(client: TestClient, tmp_path: Path):
    shard_id = str(uuid.uuid4())
    _put(client, shard_id, b"original")

    path = tmp_path / shard_id
    raw = bytearray(path.read_bytes())
    raw[-1] ^= 0xFF
    path.write_bytes(bytes(raw))

    resp = client.get(f"/shards/{shard_id}")
    assert resp.status_code == 500
    assert resp.headers["X-Shard-Status"] == "corrupt"
