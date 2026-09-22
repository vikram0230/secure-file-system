from fastapi import FastAPI

from api_service.routers import health

app = FastAPI(title="Secure File System API", version="0.1.0")

app.include_router(health.router)
