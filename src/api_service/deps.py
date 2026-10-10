from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from api_service.config import Settings
from api_service.db.session import get_db
from api_service.services.node_client import NodeClient

RETRY_AFTER_SECONDS = "5"


def get_app_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_nodes(request: Request) -> NodeClient:
    return request.app.state.nodes


def client_ip(request: Request) -> str | None:
    """Peer address; uvicorn rewrites it from X-Forwarded-For only for trusted proxies."""
    return request.client.host if request.client else None


async def transfer_slot(request: Request) -> AsyncIterator[None]:
    """Bound concurrent in-memory transfers per instance; shed load instead of queueing."""
    slots = request.app.state.transfer_slots
    if slots.locked():
        raise HTTPException(
            status_code=503,
            detail="server busy, retry shortly",
            headers={"Retry-After": RETRY_AFTER_SECONDS},
        )
    async with slots:
        yield


def enforce_rate_limit(limiter, key: str) -> None:
    if not limiter.allow(key):
        raise HTTPException(
            status_code=429,
            detail="rate limit exceeded",
            headers={"Retry-After": RETRY_AFTER_SECONDS},
        )


Db = Annotated[AsyncSession, Depends(get_db)]
AppSettings = Annotated[Settings, Depends(get_app_settings)]
Nodes = Annotated[NodeClient, Depends(get_nodes)]
ClientIp = Annotated[str | None, Depends(client_ip)]
TransferSlot = Depends(transfer_slot)
