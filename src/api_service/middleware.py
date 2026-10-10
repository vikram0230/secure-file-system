import json
import logging
import time
from urllib.parse import parse_qsl, urlencode

from fastapi import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

access_log = logging.getLogger("api_service.access")

REDACTED_QUERY_PARAMS = frozenset({"sig"})


class BodyTooLargeError(HTTPException):
    # An HTTPException so FastAPI's body parser re-raises it as-is instead of
    # converting it into a generic 400 "error parsing the body".
    def __init__(self) -> None:
        super().__init__(status_code=413, detail="request body too large")


class BodySizeLimitMiddleware:
    """Cap request bodies as bytes arrive, whatever Content-Length claims.

    Without this the multipart parser would spool an arbitrarily large body to
    disk before the endpoint could reject it.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        default_max_bytes: int,
        overrides: dict[tuple[str, str], int],
    ) -> None:
        self.app = app
        self.default_max_bytes = default_max_bytes
        self.overrides = overrides

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        limit = self.overrides.get((scope["method"], scope["path"]), self.default_max_bytes)
        declared = dict(scope["headers"]).get(b"content-length", b"")
        if declared.isdigit() and int(declared) > limit:
            await _send_413(send)
            return

        received = 0
        response_started = False

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise BodyTooLargeError
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except BodyTooLargeError:
            if not response_started:
                await _send_413(send)


async def _send_413(send: Send) -> None:
    body = json.dumps({"detail": "request body too large"}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class AccessLogMiddleware:
    """One JSON line per request, with signed-URL signatures redacted."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        status = 500

        async def capture_send(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, capture_send)
        finally:
            client = scope.get("client")
            access_log.info(
                json.dumps(
                    {
                        "method": scope["method"],
                        "path": scope["path"],
                        "query": redact_query(scope.get("query_string", b"").decode("latin-1")),
                        "status": status,
                        "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                        "client": client[0] if client else None,
                    }
                )
            )


def redact_query(query: str) -> str:
    pairs = parse_qsl(query, keep_blank_values=True)
    return urlencode([(k, "REDACTED" if k in REDACTED_QUERY_PARAMS else v) for k, v in pairs])
