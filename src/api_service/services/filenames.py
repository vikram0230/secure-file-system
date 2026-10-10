import re
import unicodedata
from urllib.parse import quote

MAX_FILENAME_BYTES = 255
FALLBACK_FILENAME = "file"


def sanitize_filename(raw: str | None) -> str:
    """Reduce a client-supplied name to a safe display name (never used as a path)."""
    name = (raw or "").replace("\\", "/").rsplit("/", 1)[-1]
    # Drop control (Cc) and format (Cf) characters: CR/LF header injection,
    # NULs, and bidi overrides that disguise extensions.
    name = "".join(ch for ch in name if unicodedata.category(ch) not in ("Cc", "Cf")).strip()
    name = name.encode()[:MAX_FILENAME_BYTES].decode(errors="ignore")
    if name in ("", ".", ".."):
        return FALLBACK_FILENAME
    return name


def content_disposition(filename: str) -> str:
    """RFC 6266 attachment header with an ASCII fallback and an RFC 5987 UTF-8 name."""
    ascii_name = unicodedata.normalize("NFKD", filename).encode("ascii", "ignore").decode()
    ascii_name = "".join(
        "_" if ch in '"\\' or ord(ch) < 0x20 or ord(ch) == 0x7F else ch for ch in ascii_name
    )
    ascii_name = ascii_name.strip() or FALLBACK_FILENAME
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename, safe='')}"


_MIME_TOKEN = re.compile(r"[A-Za-z0-9!#$&^_.+-]{1,63}/[A-Za-z0-9!#$&^_.+-]{1,63}")
DEFAULT_CONTENT_TYPE = "application/octet-stream"


def sanitize_content_type(raw: str | None) -> str:
    """Keep a well-formed ``type/subtype`` as metadata only; it is never served back."""
    candidate = (raw or "").split(";", 1)[0].strip().lower()
    return candidate if _MIME_TOKEN.fullmatch(candidate) else DEFAULT_CONTENT_TYPE
