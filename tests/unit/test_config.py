import secrets

import pytest
from pydantic import ValidationError

from api_service.config import Settings

SECRET = secrets.token_hex(32)


def _settings(**overrides) -> Settings:
    values = {
        "database_url": "postgresql+psycopg://u@h/db",
        "secret_key": SECRET,
        "node_token": SECRET,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_minimal_valid_settings():
    settings = _settings()
    assert settings.shard_total_count == 6
    assert settings.signing_keys == {"k1": SECRET}


@pytest.mark.parametrize("missing", ["database_url", "secret_key", "node_token"])
def test_required_values_have_no_defaults(missing, monkeypatch):
    monkeypatch.delenv(f"SFS_{missing.upper()}", raising=False)
    values = {"database_url": "x", "secret_key": SECRET, "node_token": SECRET}
    del values[missing]
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **values)


@pytest.mark.parametrize(
    "overrides",
    [
        {"secret_key": "short"},
        {"node_token": "short"},
        {"shard_data_count": 0},
        {"shard_parity_count": 0},
        {"shard_data_count": 200, "shard_parity_count": 57},
        {"min_ttl_seconds": 100, "max_ttl_seconds": 50},
        {"secret_key_id": "has space"},
        {"previous_secret_keys": "nokeyseparator"},
        {"previous_secret_keys": "k0:short"},
        {"previous_secret_keys": f"k1:{SECRET}"},  # reuses the current kid
        {"previous_secret_keys": f"k0:{SECRET},k0:{SECRET}"},
    ],
)
def test_invalid_settings_rejected(overrides):
    with pytest.raises(ValidationError):
        _settings(**overrides)


def test_previous_keys_join_the_verification_set():
    old = secrets.token_hex(32)
    settings = _settings(secret_key_id="k2", previous_secret_keys=f"k1:{old}")
    assert settings.signing_keys == {"k1": old, "k2": SECRET}
