from fastapi import FastAPI

from storage_node.config import Settings, get_settings
from storage_node.router import router


def create_app(settings: Settings | None = None) -> FastAPI:
    app = FastAPI(title="Secure File System Storage Node", version="0.1.0")
    app.state.settings = settings or get_settings()
    app.include_router(router)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
