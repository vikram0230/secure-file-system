import hmac
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from starlette.concurrency import run_in_threadpool

from storage_node.config import Settings
from storage_node.storage import (
    ShardCorruptedError,
    ShardExistsError,
    ShardNotFoundError,
    StorageFullError,
    delete_shard,
    read_shard,
    sha256_hex,
    validate_shard_id,
    write_shard,
)

CHECKSUM_HEADER = "X-Checksum-Sha256"
# Distinguishes "this shard is corrupt" from any other server error.
SHARD_STATUS_HEADER = "X-Shard-Status"


def node_settings(request: Request) -> Settings:
    return request.app.state.settings


NodeSettings = Annotated[Settings, Depends(node_settings)]


def require_node_token(
    settings: NodeSettings, x_node_token: Annotated[str | None, Header()] = None
) -> None:
    if x_node_token is None or not hmac.compare_digest(x_node_token, settings.token):
        raise HTTPException(status_code=401, detail="invalid node token")


def valid_shard_id(shard_id: str) -> str:
    try:
        return validate_shard_id(shard_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


router = APIRouter(prefix="/shards", tags=["shards"], dependencies=[Depends(require_node_token)])
ShardId = Annotated[str, Depends(valid_shard_id)]


async def _read_body_capped(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise HTTPException(status_code=413, detail="shard too large")

    chunks: list[bytes] = []
    received = 0
    async for chunk in request.stream():
        received += len(chunk)
        if received > limit:
            raise HTTPException(status_code=413, detail="shard too large")
        chunks.append(chunk)
    return b"".join(chunks)


@router.put("/{shard_id}", status_code=201)
async def put_shard(
    shard_id: ShardId,
    request: Request,
    settings: NodeSettings,
    x_checksum_sha256: Annotated[str, Header()],
) -> dict:
    data = await _read_body_capped(request, settings.max_shard_bytes)
    if not data:
        raise HTTPException(status_code=400, detail="empty shard body")
    if not hmac.compare_digest(sha256_hex(data), x_checksum_sha256.lower()):
        raise HTTPException(status_code=400, detail="checksum mismatch in transit")

    try:
        checksum = await run_in_threadpool(write_shard, Path(settings.shard_dir), shard_id, data)
    except ShardExistsError as exc:
        raise HTTPException(status_code=409, detail="shard already exists") from exc
    except StorageFullError as exc:
        raise HTTPException(status_code=507, detail="insufficient storage") from exc

    return {"shard_id": shard_id, "checksum_sha256": checksum, "size_bytes": len(data)}


@router.get("/{shard_id}")
def get_shard(shard_id: ShardId, settings: NodeSettings) -> Response:
    try:
        data = read_shard(Path(settings.shard_dir), shard_id)
    except ShardNotFoundError as exc:
        raise HTTPException(status_code=404, detail="shard not found") from exc
    except ShardCorruptedError as exc:
        raise HTTPException(
            status_code=500,
            detail="shard checksum mismatch",
            headers={SHARD_STATUS_HEADER: "corrupt"},
        ) from exc

    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={CHECKSUM_HEADER: sha256_hex(data)},
    )


@router.delete("/{shard_id}", status_code=204)
def delete_shard_endpoint(shard_id: ShardId, settings: NodeSettings) -> Response:
    if not delete_shard(Path(settings.shard_dir), shard_id):
        raise HTTPException(status_code=404, detail="shard not found")
    return Response(status_code=204)
