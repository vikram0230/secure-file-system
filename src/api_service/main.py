import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import InterfaceError, OperationalError

from api_service.config import Settings, get_settings
from api_service.db.session import make_async_engine, make_async_sessionmaker
from api_service.middleware import AccessLogMiddleware, BodySizeLimitMiddleware
from api_service.routers import download, files, health
from api_service.services.node_client import NodeClient
from api_service.services.rate_limit import RateLimiter

MULTIPART_OVERHEAD_BYTES = 64 * 1024
DEFAULT_BODY_LIMIT_BYTES = 64 * 1024


def configure_logging() -> None:
    logger = logging.getLogger("api_service")
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)


def create_app(
    settings: Settings | None = None,
    node_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """App factory; run with ``uvicorn --factory api_service.main:create_app``."""
    settings = settings or get_settings()
    configure_logging()
    engine = make_async_engine(settings.database_url)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.nodes = NodeClient(
            settings.node_token, settings.node_request_timeout_seconds, transport=node_transport
        )
        try:
            yield
        finally:
            await app.state.nodes.aclose()
            await engine.dispose()

    app = FastAPI(title="Secure File System API", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.sessionmaker = make_async_sessionmaker(engine)
    app.state.transfer_slots = asyncio.Semaphore(settings.max_concurrent_transfers)
    app.state.download_limiter = RateLimiter(settings.download_rate_per_minute)
    app.state.api_limiter = RateLimiter(settings.api_rate_per_minute)

    app.add_middleware(
        BodySizeLimitMiddleware,
        default_max_bytes=DEFAULT_BODY_LIMIT_BYTES,
        overrides={("POST", "/files"): settings.max_upload_bytes + MULTIPART_OVERHEAD_BYTES},
    )
    app.add_middleware(AccessLogMiddleware)

    @app.middleware("http")
    async def nosniff(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        return response

    @app.exception_handler(OperationalError)
    @app.exception_handler(InterfaceError)
    async def database_unavailable(request: Request, exc: Exception) -> JSONResponse:
        # Fail closed: without the database there is no auth, ownership or audit.
        logging.getLogger("api_service").error("database unavailable: %s", exc)
        return JSONResponse(status_code=503, content={"detail": "service temporarily unavailable"})

    app.include_router(health.router)
    app.include_router(files.router)
    app.include_router(download.router)
    return app
