import secrets
import uuid

import pytest

from api_service.services.signing import InvalidLinkError, sign, verify

KEY = secrets.token_hex(32)
KEYS = {"k1": KEY}
FILE_ID = uuid.uuid4()
NOW = 1_700_000_000
EXP = NOW + 300


def _params(**overrides):
    params = {
        "file_id": str(FILE_ID),
        "exp": str(EXP),
        "v": "1",
        "kid": "k1",
        "sig": sign(KEY, "k1", FILE_ID, EXP, 1),
    }
    params.update(overrides)
    return params


def test_valid_link_verifies():
    claims = verify(KEYS, now=NOW, **_params())
    assert (claims.file_id, claims.expires, claims.link_version, claims.kid) == (
        FILE_ID,
        EXP,
        1,
        "k1",
    )


def test_signature_is_unpadded_base64url_of_sha256():
    assert len(sign(KEY, "k1", FILE_ID, EXP, 1)) == 43


@pytest.mark.parametrize(
    "override",
    [
        {"file_id": str(uuid.uuid4())},
        {"exp": str(EXP + 1)},
        {"v": "2"},
        {"kid": "k2"},
        {"sig": "A" * 43},
        {"sig": sign(KEY, "k1", FILE_ID, EXP, 1) + "="},
        {"sig": sign(secrets.token_hex(32), "k1", FILE_ID, EXP, 1)},
    ],
)
def test_any_change_invalidates(override):
    with pytest.raises(InvalidLinkError):
        verify(KEYS, now=NOW, **_params(**override))


@pytest.mark.parametrize(
    "override",
    [
        {"exp": None},
        {"sig": None},
        {"exp": "-1"},
        {"exp": "1e9"},
        {"exp": "1" * 13},
        {"v": "x"},
        {"file_id": "not-a-uuid"},
        {"file_id": FILE_ID.hex},  # valid UUID, non-canonical form
    ],
)
def test_malformed_input_is_rejected(override):
    with pytest.raises(InvalidLinkError):
        verify(KEYS, now=NOW, **_params(**override))


def test_expiry_is_exclusive():
    verify(KEYS, now=EXP - 1, **_params())
    with pytest.raises(InvalidLinkError):
        verify(KEYS, now=EXP, **_params())


def test_retired_key_still_verifies_by_kid():
    old = secrets.token_hex(32)
    keys = {"k2": secrets.token_hex(32), "k1": old}
    params = _params(sig=sign(old, "k1", FILE_ID, EXP, 1))
    assert verify(keys, now=NOW, **params).kid == "k1"


def test_kid_is_bound_into_the_signature():
    shared = secrets.token_hex(32)
    keys = {"a": shared, "b": shared}
    params = _params(kid="b", sig=sign(shared, "a", FILE_ID, EXP, 1))
    with pytest.raises(InvalidLinkError):
        verify(keys, now=NOW, **params)
