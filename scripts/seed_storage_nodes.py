"""Populate the storage_nodes table from the SFS_STORAGE_NODES setting.

Run after migrations, before starting the API service:
    python scripts/seed_storage_nodes.py
"""

from api_service.config import get_settings
from api_service.db.models import StorageNode
from api_service.db.session import SessionLocal


def main() -> None:
    settings = get_settings()
    db = SessionLocal()
    try:
        existing = {node.hostname for node in db.query(StorageNode).all()}
        for entry in settings.storage_nodes.split(","):
            host_port = entry.strip()
            if not host_port or host_port in existing:
                continue
            host = host_port.split(":")[0]
            db.add(StorageNode(hostname=host_port, private_ip=_resolve_ip(host)))
        db.commit()
    finally:
        db.close()


def _resolve_ip(host: str) -> str:
    import socket

    try:
        return socket.gethostbyname(host)
    except OSError:
        return "127.0.0.1"


if __name__ == "__main__":
    main()
