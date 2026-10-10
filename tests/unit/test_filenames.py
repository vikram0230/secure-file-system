import pytest

from api_service.services.filenames import (
    content_disposition,
    sanitize_content_type,
    sanitize_filename,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("report.pdf", "report.pdf"),
        ("../../etc/passwd", "passwd"),
        ("C:\\Users\\me\\secret.txt", "secret.txt"),
        ("evil\r\nSet-Cookie: x.txt", "evilSet-Cookie: x.txt"),
        ("invoice\u202efdp.exe", "invoicefdp.exe"),
        ("nul\x00byte", "nulbyte"),
        ("  spaced.txt  ", "spaced.txt"),
        ("", "file"),
        (None, "file"),
        ("..", "file"),
        ("dir/", "file"),
        ("résumé.pdf", "résumé.pdf"),
    ],
)
def test_sanitize_filename(raw, expected):
    assert sanitize_filename(raw) == expected


def test_sanitize_filename_caps_bytes_without_splitting_characters():
    name = sanitize_filename("é" * 200)  # 400 bytes of 2-byte characters
    assert len(name.encode()) <= 255
    assert name == "é" * 127


def test_content_disposition_has_ascii_fallback_and_utf8_name():
    header = content_disposition('naïve "quote".txt')
    assert header == (
        "attachment; filename=\"naive _quote_.txt\"; filename*=UTF-8''na%C3%AFve%20%22quote%22.txt"
    )


def test_content_disposition_never_contains_raw_line_breaks():
    header = content_disposition("a\r\nb")
    assert "\r" not in header and "\n" not in header


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("application/pdf", "application/pdf"),
        ("TEXT/HTML; charset=utf-8", "text/html"),
        ("image/svg+xml", "image/svg+xml"),
        ("not a mime", "application/octet-stream"),
        ("text/html\r\nX: y", "application/octet-stream"),
        ("", "application/octet-stream"),
        (None, "application/octet-stream"),
    ],
)
def test_sanitize_content_type(raw, expected):
    assert sanitize_content_type(raw) == expected
