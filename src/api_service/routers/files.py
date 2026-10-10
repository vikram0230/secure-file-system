import time
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Query, Request, Response
from sqlalchemy import select
from starlette.datastructures import UploadFile

from api_service.db import models
from api_service.deps import AppSettings, ClientIp, Db, Nodes, TransferSlot, enforce_rate_limit
from api_service.schemas.files import AvailabilityOut, FileList, FileOut, LinkOut, LinkRequest
from api_service.services import audit, files, signing
from api_service.services.auth import CurrentUser
from api_service.services.filenames import sanitize_content_type, sanitize_filename

router = APIRouter(prefix="/files", tags=["files"])


def _file_out(file: models.File, availability: files.Availability) -> FileOut:
    return FileOut(
        id=file.id,
        filename=file.original_name,
        content_type=file.content_type,
        size_bytes=file.size_bytes,
        checksum_sha256=file.checksum_sha256,
        uploaded_at=file.uploaded_at,
        status=file.status,
        availability=AvailabilityOut(**availability.__dict__),
    )


async def _owned_file(db: Db, user: models.User, file_id: uuid.UUID) -> models.File:
    """Another user's file is indistinguishable from a missing one (404, never 403)."""
    file = await db.scalar(
        select(models.File).where(
            models.File.id == file_id,
            models.File.owner_user_id == user.id,
            models.File.status == "available",
        )
    )
    if file is None:
        raise HTTPException(status_code=404, detail="file not found")
    return file


UPLOAD_FIELD = "file"
UPLOAD_OPENAPI = {
    "requestBody": {
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "required": [UPLOAD_FIELD],
                    "properties": {UPLOAD_FIELD: {"type": "string", "format": "binary"}},
                }
            }
        },
    }
}


# The multipart body is parsed inside the handler, not declared as an UploadFile
# parameter: FastAPI reads declared bodies *before* running dependencies, which
# would let unauthenticated clients spool large uploads to disk.
@router.post(
    "",
    status_code=201,
    response_model=FileOut,
    dependencies=[TransferSlot],
    openapi_extra=UPLOAD_OPENAPI,
)
async def upload_file(
    request: Request,
    user: CurrentUser,
    db: Db,
    nodes: Nodes,
    settings: AppSettings,
    ip: ClientIp,
) -> FileOut:
    enforce_rate_limit(request.app.state.api_limiter, f"user:{user.id}")

    async with request.form(max_files=1, max_fields=1) as form:
        upload = form.get(UPLOAD_FIELD)
        if not isinstance(upload, UploadFile):
            raise HTTPException(
                status_code=422, detail=f"multipart field '{UPLOAD_FIELD}' required"
            )
        data = await upload.read(settings.max_upload_bytes + 1)
        filename, content_type = upload.filename, upload.content_type

    if len(data) > settings.max_upload_bytes:
        raise HTTPException(status_code=413, detail="file too large")
    if not data:
        raise HTTPException(status_code=400, detail="file is empty")

    try:
        file = await files.upload_file(
            db,
            nodes,
            owner=user,
            filename=sanitize_filename(filename),
            content_type=sanitize_content_type(content_type),
            data=data,
            k=settings.shard_data_count,
            m=settings.shard_parity_count,
            ip_address=ip,
        )
    except files.InsufficientNodesError as exc:
        raise HTTPException(status_code=503, detail="not enough healthy storage nodes") from exc
    except files.UploadFailedError as exc:
        raise HTTPException(status_code=502, detail="storage write failed, retry") from exc

    await db.refresh(file)
    availability = await files.availability(db, [file])
    return _file_out(file, availability[file.id])


@router.get("", response_model=FileList)
async def list_files(
    user: CurrentUser,
    db: Db,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> FileList:
    rows = await db.scalars(
        select(models.File)
        .where(models.File.owner_user_id == user.id, models.File.status == "available")
        .order_by(models.File.uploaded_at.desc(), models.File.id)
        .limit(limit)
        .offset(offset)
    )
    page = list(rows)
    availability = await files.availability(db, page)
    return FileList(
        items=[_file_out(f, availability[f.id]) for f in page], limit=limit, offset=offset
    )


@router.get("/{file_id}", response_model=FileOut)
async def get_file(file_id: uuid.UUID, user: CurrentUser, db: Db) -> FileOut:
    file = await _owned_file(db, user, file_id)
    availability = await files.availability(db, [file])
    return _file_out(file, availability[file.id])


@router.post("/{file_id}/links", status_code=201, response_model=LinkOut)
async def create_link(
    file_id: uuid.UUID,
    body: LinkRequest,
    request: Request,
    user: CurrentUser,
    db: Db,
    settings: AppSettings,
    ip: ClientIp,
) -> LinkOut:
    enforce_rate_limit(request.app.state.api_limiter, f"user:{user.id}")
    if not settings.min_ttl_seconds <= body.ttl_seconds <= settings.max_ttl_seconds:
        raise HTTPException(
            status_code=422,
            detail=(
                f"ttl_seconds must be between {settings.min_ttl_seconds} "
                f"and {settings.max_ttl_seconds}"
            ),
        )

    file = await _owned_file(db, user, file_id)
    expires = int(time.time()) + body.ttl_seconds
    sig = signing.sign(
        settings.secret_key, settings.secret_key_id, file.id, expires, file.link_version
    )
    url = request.url_for("download_file", file_id=str(file.id)).include_query_params(
        exp=expires, v=file.link_version, kid=settings.secret_key_id, sig=sig
    )

    audit.record(
        db,
        "link_generated",
        file_id=file.id,
        actor_user_id=user.id,
        ttl_seconds=body.ttl_seconds,
        ip_address=ip,
    )
    await db.commit()
    return LinkOut(url=str(url), expires_at=datetime.fromtimestamp(expires, UTC))


@router.delete("/{file_id}", status_code=204)
async def delete_file(
    file_id: uuid.UUID, user: CurrentUser, db: Db, nodes: Nodes, ip: ClientIp
) -> Response:
    file = await _owned_file(db, user, file_id)
    await files.delete_file(db, nodes, file, actor=user, ip_address=ip)
    return Response(status_code=204)
