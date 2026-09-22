from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for a storage node, loaded from environment variables."""

    model_config = SettingsConfigDict(env_prefix="SFS_NODE_", env_file=".env", extra="ignore")

    shard_dir: str
    # Shared with the API service; storage nodes reject any caller without it.
    token: str = Field(min_length=32)
    max_shard_bytes: int = Field(default=64 * 1024 * 1024, gt=0)


@lru_cache
def get_settings() -> Settings:
    return Settings()
