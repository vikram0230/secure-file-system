import logging
import time
import uuid

from fastapi import APIRouter, HTTPException, Request, Response

from api_service.db.models import File
from api_service.deps import AppSettings, ClientIp, Db, Nodes, TransferSlot, enforce_rate_limit
from api_service.services import audit, files, signing
from api_service.services.filenames import content_disposition

log = logging.getLogger(__name__)
router = APIRouter(tags=["download"])

DOWNLOAD_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; sandbox",
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
}


async def _deny(
    db: Db, raw_file_id: str, ip: str | None, status: int, detail: str
) -> HTTPException:
    """Audit a refused download; link the row to the file only if it really exists."""
    file_id = None
    try:
        candidate = uuid.UUID(raw_file_id)
    except ValueError:
        candidate = None
    if candidate is not None and await db.get(File, candidate) is not None:
        file_id = candidate
    audit.record(db, "download_denied", file_id=file_id, ip_address=ip)
    await db.commit()
    return HTTPException(
        status_code=status, detail=detail, headers={"Referrer-Policy": "no-referrer"}
    )


@router.get("/download/{file_id}", name="download_file", dependencies=[TransferSlot])
async def download_file(
    file_id: str,
    request: Request,
    db: Db,
    nodes: Nodes,
    settings: AppSettings,
    ip: ClientIp,
    exp: str | None = None,
    v: str | None = None,
    kid: str | None = None,
    sig: str | None = None,
) -> Response:
    # Rate limit first, so a flood of bad links never reaches the database.
    enforce_rate_limit(request.app.state.download_limiter, f"ip:{ip}")

    try:
        claims = signing.verify(
            settings.signing_keys,
            file_id=file_id,
            exp=exp,
            v=v,
            kid=kid,
            sig=sig,
            now=int(time.time()),
        )
    except signing.InvalidLinkError as exc:
        log.info("download denied for %s: %s", file_id, exc)
        raise await _deny(db, file_id, ip, 403, "invalid or expired link") from exc

    file = await db.get(File, claims.file_id)
    if file is None or file.status != "available":
        raise await _deny(db, file_id, ip, 404, "file not found")
    if file.link_version != claims.link_version:
        raise await _deny(db, file_id, ip, 403, "link has been revoked")

    try:
        data = await files.read_file(db, nodes, file)
    except files.FileUnavailableError as exc:
        log.error("download of %s failed: %s", file.id, exc)
        raise HTTPException(status_code=503, detail="file temporarily unavailable") from exc
    except files.FileCorruptedError as exc:
        log.error("file %s failed its whole-file checksum", file.id)
        raise HTTPException(status_code=500, detail="file integrity check failed") from exc

    # Commit the audit row before sending bytes: no unaudited downloads.
    audit.record(db, "download_success", file_id=file.id, ip_address=ip)
    await db.commit()

    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={
            **DOWNLOAD_HEADERS,
            "Content-Disposition": content_disposition(file.original_name),
        },
    )
