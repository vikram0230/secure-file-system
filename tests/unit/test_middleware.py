import logging

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from api_service.middleware import AccessLogMiddleware, BodySizeLimitMiddleware, redact_query


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(
        BodySizeLimitMiddleware, default_max_bytes=10, overrides={("POST", "/big"): 100}
    )

    @app.post("/small")
    @app.post("/big")
    async def echo(request: Request) -> dict:
        return {"size": len(await request.body())}

    return app


def test_body_limits_are_per_route():
    client = TestClient(_app())
    assert client.post("/small", content=b"x" * 10).json() == {"size": 10}
    assert client.post("/small", content=b"x" * 11).status_code == 413
    assert client.post("/big", content=b"x" * 100).json() == {"size": 100}
    assert client.post("/big", content=b"x" * 101).status_code == 413


def test_body_limit_enforced_without_content_length():
    def chunks():
        for _ in range(5):
            yield b"x" * 5

    resp = TestClient(_app()).post("/small", content=chunks())
    assert resp.status_code == 413


def test_redact_query_hides_signatures_only():
    assert redact_query("exp=1&v=1&kid=k1&sig=secret") == "exp=1&v=1&kid=k1&sig=REDACTED"
    assert redact_query("") == ""


def test_access_log_never_contains_the_signature(caplog):
    app = FastAPI()
    app.add_middleware(AccessLogMiddleware)

    @app.get("/download/x")
    def download() -> dict:
        return {}

    with caplog.at_level(logging.INFO, logger="api_service.access"):
        TestClient(app).get("/download/x?exp=1&sig=topsecret")

    assert caplog.records
    assert "topsecret" not in caplog.text
    assert "REDACTED" in caplog.text
