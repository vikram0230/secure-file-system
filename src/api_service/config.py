import re
from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# zfec's Galois-field arithmetic caps the total number of shares at 256.
MAX_TOTAL_SHARDS = 256
MIN_SECRET_LENGTH = 32
KEY_ID_PATTERN = r"^[A-Za-z0-9_-]{1,32}$"


class DatabaseSettings(BaseSettings):
    """Only what's needed to reach Postgres, so migrations don't require app secrets."""

    model_config = SettingsConfigDict(env_prefix="SFS_", env_file=".env", extra="ignore")

    database_url: str


class Settings(DatabaseSettings):
    """Runtime configuration for the API service and maintenance worker.

    Secrets and connection strings have no defaults: every environment must
    supply them explicitly. Storage node addresses live in the storage_nodes
    table, not here, so shard placement records have one source of truth.
    """

    secret_key: str = Field(min_length=MIN_SECRET_LENGTH)
    secret_key_id: str = Field(default="k1", pattern=KEY_ID_PATTERN)
    # Retired signing keys, still accepted for verification: "kid:key,kid:key".
    previous_secret_keys: str = ""
    node_token: str = Field(min_length=MIN_SECRET_LENGTH)

    shard_data_count: int = Field(default=4, ge=1)
    shard_parity_count: int = Field(default=2, ge=1)

    max_upload_bytes: int = Field(default=100 * 1024 * 1024, gt=0)
    max_concurrent_transfers: int = Field(default=4, ge=1)
    node_request_timeout_seconds: float = Field(default=10.0, gt=0)

    min_ttl_seconds: int = Field(default=60, ge=1)
    max_ttl_seconds: int = Field(default=7 * 24 * 60 * 60, ge=1)

    download_rate_per_minute: int = Field(default=60, ge=1)
    api_rate_per_minute: int = Field(default=120, ge=1)

    worker_interval_seconds: float = Field(default=15.0, gt=0)
    node_failure_threshold: int = Field(default=3, ge=1)
    stale_upload_seconds: int = Field(default=15 * 60, ge=60)
    repair_grace_seconds: int = Field(default=30 * 60, ge=0)
    scrub_batch_size: int = Field(default=50, ge=0)

    @property
    def shard_total_count(self) -> int:
        return self.shard_data_count + self.shard_parity_count

    @property
    def signing_keys(self) -> dict[str, str]:
        keys = _parse_key_list(self.previous_secret_keys)
        keys[self.secret_key_id] = self.secret_key
        return keys

    @model_validator(mode="after")
    def _check_ranges(self) -> "Settings":
        if self.shard_total_count > MAX_TOTAL_SHARDS:
            raise ValueError(f"data + parity shards must be <= {MAX_TOTAL_SHARDS}")
        if self.min_ttl_seconds > self.max_ttl_seconds:
            raise ValueError("min_ttl_seconds must be <= max_ttl_seconds")
        if self.secret_key_id in _parse_key_list(self.previous_secret_keys):
            raise ValueError("secret_key_id must not reappear in previous_secret_keys")
        return self


def _parse_key_list(raw: str) -> dict[str, str]:
    keys: dict[str, str] = {}
    for entry in filter(None, (part.strip() for part in raw.split(","))):
        kid, sep, key = entry.partition(":")
        if not sep or not re.fullmatch(KEY_ID_PATTERN, kid) or len(key) < MIN_SECRET_LENGTH:
            raise ValueError("previous_secret_keys entries must be 'kid:key' with a 32+ char key")
        if kid in keys:
            raise ValueError(f"duplicate key id {kid!r} in previous_secret_keys")
        keys[kid] = key
    return keys


@lru_cache
def get_settings() -> Settings:
    return Settings()


@lru_cache
def get_database_settings() -> DatabaseSettings:
    return DatabaseSettings()
