"""Streaming binary bodies — the attachment-download path."""

import io

import pytest

import tdapi
from tests.conftest import API_ROOT

BODY = b"%PDF-1.4\n" + b"x" * 200_000


def test_the_body_is_written_to_the_caller_s_file_handle(conn, requests_mock):
    requests_mock.get(API_ROOT + "attachments/a1/content", content=BODY)
    buf = io.BytesIO()
    result = conn.content_request("attachments/a1/content", buf)
    assert buf.getvalue() == BODY
    assert result["bytes"] == len(BODY)


def test_headers_the_caller_needs_are_reported(conn, requests_mock):
    requests_mock.get(
        API_ROOT + "attachments/a1/content",
        content=BODY,
        headers={
            "Content-Type": "application/pdf",
            "Content-Disposition": 'attachment; filename="minutes.pdf"',
        },
    )
    result = conn.content_request("attachments/a1/content", io.BytesIO())
    assert result["content_type"] == "application/pdf"
    assert result["filename"] == "minutes.pdf"


def test_the_declared_content_length_is_reported(conn, requests_mock):
    requests_mock.get(
        API_ROOT + "attachments/a1/content",
        content=BODY,
        headers={"Content-Length": str(len(BODY))},
    )
    result = conn.content_request("attachments/a1/content", io.BytesIO())
    assert result["content_length"] == len(BODY)
    # Declared and actual agree here; the caller compares them itself.
    assert result["bytes"] == result["content_length"]


def test_an_error_response_never_reaches_the_file(conn, requests_mock):
    # The failure this prevents: an "attachment" on disk whose contents
    # are an HTML error page, indistinguishable from a real short file.
    requests_mock.get(
        API_ROOT + "attachments/gone/content", status_code=404, text="<html>not found</html>"
    )
    buf = io.BytesIO()
    with pytest.raises(tdapi.TDException):
        conn.content_request("attachments/gone/content", buf)
    assert buf.getvalue() == b""


def test_a_401_relogins_and_retries_against_the_same_handle(conn, requests_mock):
    requests_mock.get(
        API_ROOT + "attachments/a1/content",
        [
            {"status_code": 401, "text": "expired"},
            {"status_code": 200, "content": b"real-bytes"},
        ],
    )
    buf = io.BytesIO()
    conn.content_request("attachments/a1/content", buf)
    assert buf.getvalue() == b"real-bytes"


def test_a_5xx_is_retried_like_any_other_request(conn, requests_mock):
    requests_mock.get(
        API_ROOT + "attachments/a1/content",
        [
            {"status_code": 503, "headers": {"Retry-After": "0"}},
            {"status_code": 200, "content": b"real-bytes"},
        ],
    )
    buf = io.BytesIO()
    conn.content_request("attachments/a1/content", buf)
    assert buf.getvalue() == b"real-bytes"


class TestFilenameFromContentDisposition:
    def test_quoted_filename(self):
        assert (
            tdapi.filename_from_content_disposition('attachment; filename="report.pdf"')
            == "report.pdf"
        )

    def test_unquoted_filename(self):
        assert (
            tdapi.filename_from_content_disposition("attachment; filename=report.pdf")
            == "report.pdf"
        )

    def test_rfc_5987_encoded_filename_wins(self):
        header = (
            'attachment; filename="fallback.docx"; ' "filename*=UTF-8''sp%C3%A9cial%20name.docx"
        )
        assert tdapi.filename_from_content_disposition(header) == "spécial name.docx"

    @pytest.mark.parametrize("value", [None, "", "attachment"])
    def test_no_filename_is_none(self, value):
        assert tdapi.filename_from_content_disposition(value) is None
