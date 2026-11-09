"""Tests for uploading attachments (app/attachments.py)."""

import asyncio
import os
import re
import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.attachments import (
    BAD_NAME,
    BAD_TYPE,
    EMPTY,
    MAX_BODY_BYTES,
    MAX_PER_RECORD,
    TOO_BIG,
    TOO_MANY,
    BodyTooBig,
    LimitBodySize,
    clean_filename,
    content_disposition,
    detect_type,
)
from app.csrf import CSRF_COOKIE
from app.db import get_engine
from app.main import app
from app.models import MAX_ATTACHMENT_BYTES, Attachment, Incident, Revision, Runbook, User
from app.users import create_user
from app.web import format_size

PASSWORD = "correct horse battery staple"  # noqa: S105  (fake test value)
CSRF = "r" * 43
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 20


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")
    for name in ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"):
        if name not in os.environ:
            monkeypatch.setenv(name, "unused")
    return TestClient(app, follow_redirects=False, cookies={CSRF_COOKIE: CSRF})


# --- Unit tests (no database) -------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("notes.txt", "notes.txt"),
        ("../../etc/passwd.txt", "passwd.txt"),
        ("C:\\Users\\me\\log.txt", "log.txt"),
        ("  .hidden.txt ", "hidden.txt"),
        ("exe\u202etxt.png", "exetxt.png"),  # right-to-left override
        ("a\r\nb\0.txt", "ab.txt"),
        ("...", ""),
    ],
)
def test_clean_filename(raw, expected) -> None:
    assert clean_filename(raw) == expected


@pytest.mark.parametrize(
    ("name", "data", "expected"),
    [
        ("a.png", PNG, "image/png"),
        ("A.PNG", PNG, "image/png"),
        ("a.jpg", b"\xff\xd8\xff\xe0rest", "image/jpeg"),
        ("a.jpeg", b"\xff\xd8\xff\xe0rest", "image/jpeg"),
        ("a.pdf", b"%PDF-1.7 rest", "application/pdf"),
        ("a.log", "caf\u00e9 error\n".encode(), "text/plain"),
        ("a.yaml", b"key: value\n", "text/plain"),
        # The content must really be that type, whatever the name says.
        ("a.png", b"<script>alert(1)</script>", None),
        ("a.pdf", PNG, None),
        ("a.txt", PNG, None),  # has NUL bytes
        ("a.txt", b"\xff\xfe not utf-8", None),
        # Never allowed, whatever the content.
        ("a.svg", b"<svg onload=alert(1)>", None),
        ("a.html", b"%PDF-1.7", None),
        ("a.exe", b"MZ", None),
        ("noextension", b"text", None),
    ],
)
def test_detect_type(name, data, expected) -> None:
    assert detect_type(name, data) == expected


def test_format_size() -> None:
    assert [format_size(n) for n in (5, 2048, 3 * 1024 * 1024)] == ["5 bytes", "2.0 KB", "3.0 MB"]


def test_upload_needs_a_csrf_token(client) -> None:
    response = client.post("/runbooks/1/attachments", files={"file": ("a.txt", b"x")})

    assert response.status_code == 403


def test_upload_needs_login(client) -> None:
    response = client.post(
        "/runbooks/1/attachments", data={"csrf_token": CSRF}, files={"file": ("a.txt", b"x")}
    )

    assert (response.status_code, response.headers["location"]) == (303, "/login")


def test_huge_upload_with_a_stated_size_is_refused_unread(client) -> None:
    response = client.post(
        "/login", content=b"x", headers={"content-length": str(MAX_BODY_BYTES + 1)}
    )

    assert response.status_code == 413


def test_huge_upload_without_a_stated_size_gets_a_413(client) -> None:
    """A streamed ("chunked") body doesn't say how long it is, so it's counted."""

    def body() -> Iterator[bytes]:
        yield b'--x\r\nContent-Disposition: form-data; name="file"; filename="a.txt"\r\n\r\n'
        for _ in range(MAX_BODY_BYTES // (1024 * 1024) + 2):
            yield b"x" * (1024 * 1024)

    response = client.post(
        "/runbooks/1/attachments",
        content=body(),
        headers={"content-type": "multipart/form-data; boundary=x"},
    )

    assert response.status_code == 413


def test_reading_stops_at_the_limit() -> None:
    """Straight at the middleware: the test client buffers bodies, hiding this."""
    chunks_read = 0

    async def receive() -> dict:
        nonlocal chunks_read
        chunks_read += 1
        return {"type": "http.request", "body": b"x" * 10, "more_body": True}

    async def reads_forever(scope, receive, send) -> None:
        while True:
            await receive()

    async def ignore(message) -> None:
        pass

    scope = {"type": "http", "headers": []}
    with pytest.raises(BodyTooBig):
        asyncio.run(LimitBodySize(reads_forever, max_bytes=25)(scope, receive, ignore))

    assert chunks_read == 3


# --- Integration tests (real Postgres) ----------------------------------------


@pytest.fixture
def user() -> Iterator[User]:
    email = f"test-{uuid.uuid4().hex[:12]}@example.com"
    with Session(get_engine()) as db:
        new_user = create_user(db, email=email, display_name="Upload Tester", password=PASSWORD)
        db.commit()
        db.refresh(new_user)
        db.expunge(new_user)
    yield new_user
    with Session(get_engine()) as db:
        db.execute(delete(Runbook).where(Runbook.created_by_id == new_user.id))
        db.execute(delete(Incident).where(Incident.created_by_id == new_user.id))
        db.execute(delete(Revision).where(Revision.edited_by_id == new_user.id))
        db.execute(delete(User).where(User.id == new_user.id))
        db.commit()


@pytest.fixture
def logged_in(client, user) -> TestClient:
    response = client.post(
        "/login", data={"email": user.email, "password": PASSWORD, "csrf_token": CSRF}
    )
    assert response.status_code == 303
    return client


def new_runbook(client: TestClient) -> int:
    response = client.post("/runbooks", data={"title": "Restart DNS", "csrf_token": CSRF})
    return int(re.fullmatch(r"/runbooks/(\d+)", response.headers["location"]).group(1))


def upload(client: TestClient, url: str, name: str, data: bytes, claimed: str = "text/plain"):
    return client.post(url, data={"csrf_token": CSRF}, files={"file": (name, data, claimed)})


def stored(runbook_id: int) -> list[Attachment]:
    with Session(get_engine()) as db:
        query = select(Attachment).where(Attachment.runbook_id == runbook_id)
        return list(db.scalars(query))


@pytest.mark.integration
def test_upload_is_stored_and_listed(logged_in) -> None:
    runbook_id = new_runbook(logged_in)

    response = upload(logged_in, f"/runbooks/{runbook_id}/attachments", "shot.png", PNG)

    assert (response.status_code, response.headers["location"]) == (
        303,
        f"/runbooks/{runbook_id}",
    )
    [attachment] = stored(runbook_id)
    # The type comes from the content, not the "text/plain" the upload claimed.
    assert (attachment.filename, attachment.content_type) == ("shot.png", "image/png")
    assert attachment.size_bytes == len(PNG)
    page = logged_in.get(f"/runbooks/{runbook_id}").text
    assert "shot.png" in page
    assert "Upload Tester" in page


@pytest.mark.integration
def test_upload_to_an_incident(logged_in) -> None:
    created = logged_in.post(
        "/incidents",
        data={
            "title": "DNS outage",
            "severity": "SEV2",
            "status": "open",
            "started_at": "2026-11-01T10:00",
            "csrf_token": CSRF,
        },
    )
    assert created.status_code == 303, created.text
    url = created.headers["location"]

    assert upload(logged_in, f"{url}/attachments", "dig.log", b"timeout\n").status_code == 303
    assert "dig.log" in logged_in.get(url).text


@pytest.mark.integration
@pytest.mark.parametrize(
    ("name", "data", "message", "code"),
    [
        ("evil.png", b"<script>", BAD_TYPE, 400),
        ("evil.svg", b"<svg>", BAD_TYPE, 400),
        ("empty.txt", b"", EMPTY, 400),
        ("...", b"text", BAD_NAME, 400),
        (("a" * 252) + ".txt", b"text", BAD_NAME, 400),
        ("big.txt", b"x" * (MAX_ATTACHMENT_BYTES + 1), TOO_BIG, 413),
    ],
)
def test_bad_uploads_are_refused_with_a_reason(logged_in, name, data, message, code) -> None:
    runbook_id = new_runbook(logged_in)

    response = upload(logged_in, f"/runbooks/{runbook_id}/attachments", name, data)

    assert response.status_code == code
    assert message in response.text.replace("&#39;", "'")
    assert stored(runbook_id) == []


@pytest.mark.integration
def test_a_file_of_exactly_the_limit_is_accepted(logged_in) -> None:
    runbook_id = new_runbook(logged_in)

    response = upload(
        logged_in, f"/runbooks/{runbook_id}/attachments", "big.txt", b"x" * MAX_ATTACHMENT_BYTES
    )

    assert response.status_code == 303


@pytest.mark.integration
def test_a_record_can_only_have_so_many_files(logged_in) -> None:
    runbook_id = new_runbook(logged_in)
    url = f"/runbooks/{runbook_id}/attachments"
    for number in range(MAX_PER_RECORD):
        assert upload(logged_in, url, f"{number}.txt", b"x").status_code == 303

    response = upload(logged_in, url, "one-more.txt", b"x")

    assert response.status_code == 400
    assert TOO_MANY in response.text
    with Session(get_engine()) as db:
        count = db.scalar(
            select(func.count()).select_from(Attachment).where(Attachment.runbook_id == runbook_id)
        )
    assert count == MAX_PER_RECORD


@pytest.mark.integration
def test_upload_to_a_missing_record_is_a_404(logged_in) -> None:
    assert upload(logged_in, "/runbooks/999999999/attachments", "a.txt", b"x").status_code == 404


@pytest.mark.integration
def test_deleting_through_the_page_removes_the_files(logged_in) -> None:
    runbook_id = new_runbook(logged_in)
    upload(logged_in, f"/runbooks/{runbook_id}/attachments", "a.txt", b"x")
    logged_in.get(f"/runbooks/{runbook_id}")

    response = logged_in.post(f"/runbooks/{runbook_id}/delete", data={"csrf_token": CSRF})

    assert response.status_code == 303
    assert stored(runbook_id) == []


@pytest.mark.integration
def test_filename_is_escaped_on_the_page(logged_in) -> None:
    runbook_id = new_runbook(logged_in)

    upload(logged_in, f"/runbooks/{runbook_id}/attachments", "<b>x<b>.txt", b"x")

    page = logged_in.get(f"/runbooks/{runbook_id}").text
    assert "<b>x<b>" not in page
    assert "&lt;b&gt;x&lt;b&gt;.txt" in page


# --- Download and delete ------------------------------------------------------


def test_content_disposition_is_safe_for_any_name() -> None:
    header = content_disposition('a"b;c caf\u00e9.txt')

    assert header == (
        "attachment; filename=\"a_b_c_caf_.txt\"; filename*=UTF-8''a%22b%3Bc%20caf%C3%A9.txt"
    )


@pytest.mark.parametrize("path", ["/attachments/1", "/attachments/1/delete"])
def test_download_and_delete_pages_need_login(client, path) -> None:
    response = client.get(path)

    assert (response.status_code, response.headers["location"]) == (303, "/login")


def test_delete_needs_a_csrf_token(client) -> None:
    assert client.post("/attachments/1/delete").status_code == 403


def uploaded(client: TestClient, name: str = "a.txt", data: bytes = b"hello\n") -> tuple[int, int]:
    """Upload a file to a new runbook; return (runbook id, attachment id)."""
    runbook_id = new_runbook(client)
    assert upload(client, f"/runbooks/{runbook_id}/attachments", name, data).status_code == 303
    return runbook_id, stored(runbook_id)[0].id


@pytest.mark.integration
@pytest.mark.parametrize(
    ("name", "data", "content_type"),
    [
        ("shot.png", PNG, "image/png"),
        ("notes.txt", b"<script>alert(1)</script>", "text/plain; charset=utf-8"),
    ],
)
def test_download_returns_the_file_safely(logged_in, name, data, content_type) -> None:
    _, attachment_id = uploaded(logged_in, name, data)

    response = logged_in.get(f"/attachments/{attachment_id}")

    assert response.status_code == 200
    assert response.content == data
    assert response.headers["content-type"] == content_type
    assert response.headers["content-disposition"].startswith(f'attachment; filename="{name}"')
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in response.headers["content-security-policy"]
    assert response.headers["cache-control"] == "private, no-store"


@pytest.mark.integration
def test_the_page_links_to_download_and_delete(logged_in) -> None:
    runbook_id, attachment_id = uploaded(logged_in)

    page = logged_in.get(f"/runbooks/{runbook_id}").text

    assert f'href="/attachments/{attachment_id}"' in page
    assert f'href="/attachments/{attachment_id}/delete"' in page


@pytest.mark.integration
def test_confirm_page_deletes_nothing_and_post_does(logged_in) -> None:
    runbook_id, attachment_id = uploaded(logged_in)

    assert logged_in.get(f"/attachments/{attachment_id}/delete").status_code == 200
    assert len(stored(runbook_id)) == 1

    response = logged_in.post(f"/attachments/{attachment_id}/delete", data={"csrf_token": CSRF})

    assert (response.status_code, response.headers["location"]) == (
        303,
        f"/runbooks/{runbook_id}",
    )
    assert stored(runbook_id) == []
    assert logged_in.get(f"/attachments/{attachment_id}").status_code == 404


@pytest.mark.integration
def test_missing_attachment_is_a_404(logged_in) -> None:
    assert logged_in.get("/attachments/999999999").status_code == 404
    response = logged_in.post("/attachments/999999999/delete", data={"csrf_token": CSRF})
    assert response.status_code == 404
