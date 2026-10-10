"""On-disk shard store.

Each shard is one file named by its UUID. The first 32 bytes are the raw
SHA-256 digest of the payload that follows, so data and checksum can never be
written or replaced independently of each other.
"""

import errno
import hashlib
import os
import tempfile
import uuid
from pathlib import Path

DIGEST_SIZE = hashlib.sha256().digest_size
TEMP_PREFIX = ".tmp-"


class ShardNotFoundError(Exception):
    pass


class ShardExistsError(Exception):
    pass


class ShardCorruptedError(Exception):
    pass


class StorageFullError(Exception):
    pass


def validate_shard_id(shard_id: str) -> str:
    """Accept only a canonical UUID, closing off path traversal."""
    try:
        canonical = str(uuid.UUID(shard_id))
    except ValueError as exc:
        raise ValueError(f"invalid shard_id: {shard_id!r}") from exc
    if canonical != shard_id.lower():
        raise ValueError(f"shard_id must be a canonical UUID: {shard_id!r}")
    return canonical


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_shard(shard_dir: Path, shard_id: str, data: bytes) -> str:
    """Durably write a new shard; shards are immutable, so an existing id is an error."""
    shard_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(data).digest()
    final_path = shard_dir / shard_id

    fd, tmp_name = tempfile.mkstemp(prefix=TEMP_PREFIX, dir=shard_dir)
    try:
        try:
            with os.fdopen(fd, "wb") as tmp:
                tmp.write(digest)
                tmp.write(data)
                tmp.flush()
                os.fsync(tmp.fileno())
            # link() fails if the target exists, giving atomic create-if-absent.
            os.link(tmp_name, final_path)
        except FileExistsError as exc:
            raise ShardExistsError(shard_id) from exc
        except OSError as exc:
            if exc.errno in (errno.ENOSPC, errno.EDQUOT):
                raise StorageFullError(shard_id) from exc
            raise
    finally:
        Path(tmp_name).unlink(missing_ok=True)

    _fsync_dir(shard_dir)
    return digest.hex()


def read_shard(shard_dir: Path, shard_id: str) -> bytes:
    try:
        raw = (shard_dir / shard_id).read_bytes()
    except FileNotFoundError as exc:
        raise ShardNotFoundError(shard_id) from exc

    stored_digest, data = raw[:DIGEST_SIZE], raw[DIGEST_SIZE:]
    if len(stored_digest) != DIGEST_SIZE or hashlib.sha256(data).digest() != stored_digest:
        raise ShardCorruptedError(shard_id)
    return data


def delete_shard(shard_dir: Path, shard_id: str) -> bool:
    try:
        (shard_dir / shard_id).unlink()
    except FileNotFoundError:
        return False
    _fsync_dir(shard_dir)
    return True


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
