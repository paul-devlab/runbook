"""Tests for the runbook pages (app/runbooks.py)."""

import os
import re
import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.csrf import CSRF_COOKIE
from app.db import get_engine
from app.main import app
from app.models import Runbook, User
from app.runbooks import TITLE_MAX, RunbookForm
from app.users import create_user

PASSWORD = "correct horse battery staple"  # noqa: S105  (fake test value)
CSRF = "r" * 43


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")
    # Unit tests need settings to exist; in Compose and CI the real ones are kept.
    for name in ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"):
        if name not in os.environ:
            monkeypatch.setenv(name, "unused")
    return TestClient(app, follow_redirects=False, cookies={CSRF_COOKIE: CSRF})


# --- Unit tests (no database) -------------------------------------------------


def test_form_tidies_input() -> None:
    form = RunbookForm.from_input("  Restart DNS  ", " Quick fix ", "1. step\r\n2. step\r\n")

    assert (form.title, form.summary, form.body_md) == (
        "Restart DNS",
        "Quick fix",
        "1. step\n2. step",
    )
    assert form.errors == []


def test_form_needs_a_title() -> None:
    assert RunbookForm.from_input("   ").errors == ["Give the runbook a title."]


@pytest.mark.parametrize("path", ["/runbooks", "/runbooks/new", "/runbooks/1", "/runbooks/1/edit"])
def test_pages_need_login(client, path) -> None:
    response = client.get(path)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_create_needs_a_csrf_token(client) -> None:
    assert client.post("/runbooks", data={"title": "x"}).status_code == 403


# --- Integration tests (real Postgres) ----------------------------------------


@pytest.fixture
def user() -> Iterator[User]:
    """A fresh user. Afterwards their runbooks, then the user, are deleted."""
    email = f"test-{uuid.uuid4().hex[:12]}@example.com"
    with Session(get_engine()) as db:
        new_user = create_user(db, email=email, display_name="Runbook Tester", password=PASSWORD)
        db.commit()
        db.refresh(new_user)
        db.expunge(new_user)
    yield new_user
    with Session(get_engine()) as db:
        db.execute(delete(Runbook).where(Runbook.created_by_id == new_user.id))
        db.execute(delete(User).where(User.id == new_user.id))
        db.commit()


@pytest.fixture
def logged_in(client, user) -> TestClient:
    response = client.post(
        "/login", data={"email": user.email, "password": PASSWORD, "csrf_token": CSRF}
    )
    assert response.status_code == 303
    return client


def create(client: TestClient, title: str = "Restart DNS", **fields) -> int:
    """Create a runbook through the form; return its id."""
    response = client.post("/runbooks", data={"title": title, "csrf_token": CSRF, **fields})
    assert response.status_code == 303
    match = re.fullmatch(r"/runbooks/(\d+)", response.headers["location"])
    assert match, response.headers["location"]
    return int(match.group(1))


def load(runbook_id: int) -> Runbook | None:
    with Session(get_engine()) as db:
        return db.get(Runbook, runbook_id)


@pytest.mark.integration
def test_create_and_view(logged_in, user) -> None:
    runbook_id = create(
        logged_in, "Restart DNS", summary="When lookups fail", body_md="1. ssh dns01\n2. restart"
    )

    page = logged_in.get(f"/runbooks/{runbook_id}")

    assert page.status_code == 200
    assert "Restart DNS" in page.text
    assert "When lookups fail" in page.text
    assert "1. ssh dns01\n2. restart" in page.text
    assert "Runbook Tester" in page.text
    saved = load(runbook_id)
    assert saved.created_by_id == saved.updated_by_id == user.id


@pytest.mark.integration
def test_blank_title_is_rejected_and_input_kept(logged_in) -> None:
    response = logged_in.post(
        "/runbooks", data={"title": " ", "body_md": "keep me", "csrf_token": CSRF}
    )

    assert response.status_code == 400
    assert "Give the runbook a title." in response.text
    assert "keep me" in response.text  # nothing typed is lost


@pytest.mark.integration
def test_overlong_title_is_rejected(logged_in) -> None:
    response = logged_in.post(
        "/runbooks", data={"title": "x" * (TITLE_MAX + 1), "csrf_token": CSRF}
    )

    assert response.status_code == 422


@pytest.mark.integration
def test_list_shows_most_recently_updated_first(logged_in) -> None:
    older = create(logged_in, "Older runbook")
    newer = create(logged_in, "Newer runbook")
    logged_in.post(f"/runbooks/{older}/edit", data={"title": "Older, edited", "csrf_token": CSRF})

    text = logged_in.get("/runbooks").text

    assert text.index(f"/runbooks/{older}") < text.index(f"/runbooks/{newer}")


@pytest.mark.integration
def test_edit_form_is_prefilled_and_saves(logged_in) -> None:
    runbook_id = create(logged_in, "Before", summary="Old summary")
    created = load(runbook_id)

    form = logged_in.get(f"/runbooks/{runbook_id}/edit")
    assert 'value="Before"' in form.text
    assert 'value="Old summary"' in form.text

    response = logged_in.post(
        f"/runbooks/{runbook_id}/edit",
        data={"title": "After", "summary": "", "body_md": "new steps", "csrf_token": CSRF},
    )

    assert response.headers["location"] == f"/runbooks/{runbook_id}"
    saved = load(runbook_id)
    assert (saved.title, saved.summary, saved.body_md) == ("After", "", "new steps")
    assert saved.updated_at > created.updated_at


@pytest.mark.integration
def test_delete_asks_first_then_deletes(logged_in) -> None:
    runbook_id = create(logged_in, "Doomed")

    confirm = logged_in.get(f"/runbooks/{runbook_id}/delete")
    assert confirm.status_code == 200
    assert "can't be undone" in confirm.text
    assert load(runbook_id) is not None  # the GET alone deletes nothing

    response = logged_in.post(f"/runbooks/{runbook_id}/delete", data={"csrf_token": CSRF})

    assert response.headers["location"] == "/runbooks"
    assert load(runbook_id) is None
    assert logged_in.get(f"/runbooks/{runbook_id}").status_code == 404


@pytest.mark.integration
@pytest.mark.parametrize(
    ("method", "suffix"),
    [("get", ""), ("get", "/edit"), ("get", "/delete"), ("post", "/edit"), ("post", "/delete")],
)
def test_unknown_runbook_is_404(logged_in, method, suffix) -> None:
    with Session(get_engine()) as db:
        missing = (db.scalar(select(Runbook.id).order_by(Runbook.id.desc()).limit(1)) or 0) + 1000
    data = {"title": "x", "csrf_token": CSRF}

    if method == "get":
        response = logged_in.get(f"/runbooks/{missing}{suffix}")
    else:
        response = logged_in.post(f"/runbooks/{missing}{suffix}", data=data)

    assert response.status_code == 404
    assert "Not found" in response.text


@pytest.mark.integration
def test_content_is_shown_as_text_not_html(logged_in) -> None:
    """Record content is untrusted: a <script> in it must never run (XSS)."""
    evil = "<script>alert(1)</script>"
    runbook_id = create(logged_in, evil, summary=evil, body_md=evil)

    for page in (logged_in.get("/runbooks"), logged_in.get(f"/runbooks/{runbook_id}")):
        assert evil not in page.text
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page.text
