import uuid
from pathlib import Path

import pytest

from storage_node.storage import (
    TEMP_PREFIX,
    ShardCorruptedError,
    ShardExistsError,
    ShardNotFoundError,
    delete_shard,
    read_shard,
    sha256_hex,
    validate_shard_id,
    write_shard,
)


def test_validate_shard_id_accepts_canonical_uuid():
    shard_id = str(uuid.uuid4())
    assert validate_shard_id(shard_id) == shard_id


def test_validate_shard_id_normalizes_uppercase():
    shard_id = str(uuid.uuid4())
    assert validate_shard_id(shard_id.upper()) == shard_id


@pytest.mark.parametrize(
    "bad_id",
    ["../../etc/passwd", "not-a-uuid", "", "..", "a/b", "{" + str(uuid.UUID(int=1)) + "}"],
)
def test_validate_shard_id_rejects_non_canonical(bad_id):
    with pytest.raises(ValueError):
        validate_shard_id(bad_id)


def test_write_then_read_round_trip(tmp_path: Path):
    shard_id = str(uuid.uuid4())
    data = b"some shard bytes"

    assert write_shard(tmp_path, shard_id, data) == sha256_hex(data)
    assert read_shard(tmp_path, shard_id) == data


def test_write_leaves_no_temp_files(tmp_path: Path):
    write_shard(tmp_path, str(uuid.uuid4()), b"data")
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(TEMP_PREFIX)]


def test_write_refuses_to_overwrite(tmp_path: Path):
    shard_id = str(uuid.uuid4())
    write_shard(tmp_path, shard_id, b"first")

    with pytest.raises(ShardExistsError):
        write_shard(tmp_path, shard_id, b"second")
    assert read_shard(tmp_path, shard_id) == b"first"


def test_read_missing_shard_raises_not_found(tmp_path: Path):
    with pytest.raises(ShardNotFoundError):
        read_shard(tmp_path, str(uuid.uuid4()))


def test_read_detects_tampered_payload(tmp_path: Path):
    shard_id = str(uuid.uuid4())
    write_shard(tmp_path, shard_id, b"original bytes")

    path = tmp_path / shard_id
    raw = bytearray(path.read_bytes())
    raw[-1] ^= 0xFF
    path.write_bytes(bytes(raw))

    with pytest.raises(ShardCorruptedError):
        read_shard(tmp_path, shard_id)


def test_read_detects_truncated_file(tmp_path: Path):
    shard_id = str(uuid.uuid4())
    (tmp_path / shard_id).write_bytes(b"short")

    with pytest.raises(ShardCorruptedError):
        read_shard(tmp_path, shard_id)


def test_delete_shard(tmp_path: Path):
    shard_id = str(uuid.uuid4())
    write_shard(tmp_path, shard_id, b"data")

    assert delete_shard(tmp_path, shard_id) is True
    assert delete_shard(tmp_path, shard_id) is False
    with pytest.raises(ShardNotFoundError):
        read_shard(tmp_path, shard_id)
