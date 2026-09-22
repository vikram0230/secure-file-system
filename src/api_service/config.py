from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for the API service, loaded from environment variables."""

    model_config = SettingsConfigDict(env_prefix="SFS_", env_file=".env", extra="ignore")

    # No defaults for connection strings, secrets, or node addresses: every
    # environment (local, CI, DigitalOcean) must supply its own via .env or
    # real environment variables rather than inheriting a hardcoded value.
    database_url: str
    secret_key: str

    # Comma-separated "host:port" pairs, one per storage node.
    storage_nodes: str

    shard_data_count: int = 4
    shard_parity_count: int = 2

    max_upload_bytes: int = 100 * 1024 * 1024  # 100 MB
    min_ttl_seconds: int = 60
    max_ttl_seconds: int = 7 * 24 * 60 * 60  # 7 days

    @property
    def storage_node_urls(self) -> list[str]:
        return [f"http://{node.strip()}" for node in self.storage_nodes.split(",") if node.strip()]

    @property
    def shard_total_count(self) -> int:
        return self.shard_data_count + self.shard_parity_count


@lru_cache
def get_settings() -> Settings:
    return Settings()
