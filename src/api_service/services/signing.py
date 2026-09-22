"""Stateless signed download links.

A link is valid on any API instance, across restarts, as long as the signing
key identified by ``kid`` is configured. See docs/DESIGN.md §6.
"""

import base64
import hashlib
import hmac
import re
import uuid
from dataclasses import dataclass

PAYLOAD_PREFIX = "sfs-download-v1"
_DIGITS = re.compile(r"[0-9]{1,12}")


class InvalidLinkError(Exception):
    """Malformed, tampered with, signed by an unknown key, or expired."""


@dataclass(frozen=True)
class LinkClaims:
    file_id: uuid.UUID
    expires: int
    link_version: int
    kid: str


def _payload(kid: str, file_id: uuid.UUID, expires: int, link_version: int) -> bytes:
    return f"{PAYLOAD_PREFIX}\n{kid}\n{file_id}\n{expires}\n{link_version}".encode()


def sign(key: str, kid: str, file_id: uuid.UUID, expires: int, link_version: int) -> str:
    """Unpadded base64url HMAC-SHA256; exactly one valid encoding per payload."""
    digest = hmac.new(key.encode(), _payload(kid, file_id, expires, link_version), hashlib.sha256)
    return base64.urlsafe_b64encode(digest.digest()).rstrip(b"=").decode()


def verify(
    keys: dict[str, str],
    *,
    file_id: str,
    exp: str | None,
    v: str | None,
    kid: str | None,
    sig: str | None,
    now: int,
) -> LinkClaims:
    """Validate every field strictly; any failure is the same opaque error."""
    if exp is None or v is None or kid is None or sig is None:
        raise InvalidLinkError("missing parameter")
    if not _DIGITS.fullmatch(exp) or not _DIGITS.fullmatch(v):
        raise InvalidLinkError("malformed parameter")
    key = keys.get(kid)
    if key is None:
        raise InvalidLinkError("unknown key id")
    try:
        parsed_id = uuid.UUID(file_id)
    except ValueError as exc:
        raise InvalidLinkError("malformed file id") from exc
    if str(parsed_id) != file_id.lower():
        raise InvalidLinkError("non-canonical file id")

    expires, link_version = int(exp), int(v)
    expected = sign(key, kid, parsed_id, expires, link_version)
    if not hmac.compare_digest(sig.encode(), expected.encode()):
        raise InvalidLinkError("bad signature")
    if expires <= now:
        raise InvalidLinkError("expired")
    return LinkClaims(file_id=parsed_id, expires=expires, link_version=link_version, kid=kid)
