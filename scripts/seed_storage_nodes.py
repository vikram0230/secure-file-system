"""Register storage nodes in the storage_nodes table.

The table is the source of truth for node addresses; this script only
bootstraps it. Safe to re-run: existing addresses are left untouched.

    SFS_STORAGE_NODES=host1:8000,host2:8000,... python scripts/seed_storage_nodes.py
"""

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.dialects.postgresql import insert

from api_service.config import get_database_settings
from api_service.db.models import StorageNode
from api_service.db.session import make_sessionmaker


class SeedSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SFS_", env_file=".env", extra="ignore")

    storage_nodes: str

    @field_validator("storage_nodes")
    @classmethod
    def _require_host_port(cls, value: str) -> str:
        for entry in _split(value):
            host, sep, port = entry.rpartition(":")
            if not sep or not host or not port.isdigit():
                raise ValueError(f"expected host:port, got {entry!r}")
        return value


def _split(value: str) -> list[str]:
    return [entry.strip() for entry in value.split(",") if entry.strip()]


def main() -> None:
    addresses = _split(SeedSettings().storage_nodes)
    with make_sessionmaker(get_database_settings().database_url)() as db:
        stmt = insert(StorageNode).values([{"address": address} for address in addresses])
        stmt = stmt.on_conflict_do_nothing(index_elements=["address"]).returning(StorageNode.id)
        inserted = len(db.execute(stmt).all())
        db.commit()
    print(f"registered {inserted} new storage node(s); {len(addresses)} configured")


if __name__ == "__main__":
    main()
