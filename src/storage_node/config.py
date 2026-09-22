from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for a storage node, loaded from environment variables."""

    model_config = SettingsConfigDict(env_prefix="SFS_NODE_", env_file=".env", extra="ignore")

    shard_dir: str


@lru_cache
def get_settings() -> Settings:
    return Settings()
