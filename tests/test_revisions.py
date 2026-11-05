"""Tests for revision history (app/revisions.py)."""

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
from app.models import Incident, Revision, Runbook, User
from app.revisions import content_changed, readable
from app.tags import delete_unused_tags
from app.users import create_user

PASSWORD = "correct horse battery staple"  # noqa: S105  (fake test value)
CSRF = "h" * 43


# --- Unit tests (no database) -------------------------------------------------


def test_only_content_changes_count() -> None:
    before = {"title": "A", "updated_at": "2026-11-01T09:00:00+00:00", "updated_by_id": 1}
    same_content = before | {"updated_at": "2026-11-02T09:00:00+00:00", "updated_by_id": 2}

    assert not content_changed(before, same_content)
    assert content_changed(before, before | {"title": "B"})


def test_readable_turns_iso_text_back_into_datetimes() -> None:
    old = readable({"title": "x", "updated_at": "2026-11-01T09:00:00+00:00", "resolved_at": None})

    assert old["updated_at"].hour == 9
    assert old["resolved_at"] is None
    assert old["title"] == "x"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")
    for name in ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"):
        if name not in os.environ:
            monkeypatch.setenv(name, "unused")
    return TestClient(app, follow_redirects=False, cookies={CSRF_COOKIE: CSRF})


@pytest.mark.parametrize("path", ["/runbooks/1/history", "/incidents/1/history/1"])
def test_history_needs_login(client, path) -> None:
    assert client.get(path).headers["location"] == "/login"


# --- Integration tests (real Postgres) ----------------------------------------


@pytest.fixture
def logged_in(client) -> Iterator[TestClient]:
    email = f"test-{uuid.uuid4().hex[:12]}@example.com"
    with Session(get_engine()) as db:
        user = create_user(db, email=email, display_name="History Tester", password=PASSWORD)
        db.commit()
        user_id = user.id
    client.post("/login", data={"email": email, "password": PASSWORD, "csrf_token": CSRF})
    yield client
    with Session(get_engine()) as db:
        db.execute(delete(Revision).where(Revision.edited_by_id == user_id))
        db.execute(delete(Incident).where(Incident.created_by_id == user_id))
        db.execute(delete(Runbook).where(Runbook.created_by_id == user_id))
        db.execute(delete(User).where(User.id == user_id))
        delete_unused_tags(db)
        db.commit()


def new_id(response) -> int:
    assert response.status_code == 303, response.text
    return int(response.headers["location"].rsplit("/", 1)[1])


def create_runbook(client: TestClient, **fields) -> int:
    data = {"title": "Restart DNS", "body_md": "1. old step", "csrf_token": CSRF}
    return new_id(client.post("/runbooks", data=data | fields))


def edit_runbook(client: TestClient, runbook_id: int, **fields) -> None:
    data = {"title": "Restart DNS", "body_md": "1. old step", "csrf_token": CSRF}
    response = client.post(f"/runbooks/{runbook_id}/edit", data=data | fields)
    assert response.status_code == 303


INCIDENT = {
    "title": "Outage",
    "severity": "SEV2",
    "status": "open",
    "started_at": "2026-11-01T09:00",
}


def revisions_of(entity_type: str, entity_id: int) -> list[Revision]:
    with Session(get_engine()) as db:
        return list(
            db.scalars(
                select(Revision)
                .where(Revision.entity_type == entity_type, Revision.entity_id == entity_id)
                .order_by(Revision.id)
            )
        )


@pytest.mark.integration
def test_new_record_has_no_history(logged_in) -> None:
    runbook_id = create_runbook(logged_in)

    page = logged_in.get(f"/runbooks/{runbook_id}/history").text

    assert revisions_of("runbook", runbook_id) == []
    assert "Current version" in page
    assert "No earlier versions" in page


@pytest.mark.integration
def test_edit_keeps_the_previous_version(logged_in) -> None:
    runbook_id = create_runbook(logged_in, tags="dns")

    edit_runbook(logged_in, runbook_id, title="Restart DNS safely", body_md="1. new step")

    [revision] = revisions_of("runbook", runbook_id)
    assert revision.snapshot["title"] == "Restart DNS"
    assert revision.snapshot["body_md"] == "1. old step"
    assert revision.snapshot["tags"] == ["dns"]
    assert revision.snapshot["updated_by_name"] == "History Tester"


@pytest.mark.integration
def test_saving_without_changes_adds_no_version(logged_in) -> None:
    runbook_id = create_runbook(logged_in)

    edit_runbook(logged_in, runbook_id)  # same values

    assert revisions_of("runbook", runbook_id) == []


@pytest.mark.integration
def test_history_lists_versions_newest_first_and_shows_one(logged_in) -> None:
    runbook_id = create_runbook(logged_in, title="Version one")
    edit_runbook(logged_in, runbook_id, title="Version two")
    edit_runbook(logged_in, runbook_id, title="Version three", body_md="**bold** step")

    history = logged_in.get(f"/runbooks/{runbook_id}/history").text
    assert history.index("Version two") < history.index("Version one")
    first, second = revisions_of("runbook", runbook_id)

    page = logged_in.get(f"/runbooks/{runbook_id}/history/{first.id}").text

    assert "You're reading an old version" in page.replace("&#39;", "'")
    assert "<h1>Version one</h1>" in page
    assert "<li>old step</li>" in page  # Markdown rendered
    assert f'href="/runbooks/{runbook_id}"' in page  # link back to current


@pytest.mark.integration
def test_old_versions_are_sanitised_too(logged_in) -> None:
    evil = "<script>alert(1)</script>"
    runbook_id = create_runbook(logged_in, title=evil, body_md=evil)
    edit_runbook(logged_in, runbook_id, title="Cleaned up")
    [revision] = revisions_of("runbook", runbook_id)

    page = logged_in.get(f"/runbooks/{runbook_id}/history/{revision.id}").text

    assert evil not in page


@pytest.mark.integration
def test_incident_history_includes_tags_and_linked_runbooks(logged_in) -> None:
    runbook_id = create_runbook(logged_in, title="Restart DNS")
    incident_id = new_id(
        logged_in.post(
            "/incidents",
            data=INCIDENT | {"tags": "dns", "runbook_ids": [runbook_id], "csrf_token": CSRF},
        )
    )
    logged_in.post(
        f"/incidents/{incident_id}/edit",
        data=INCIDENT | {"status": "resolved", "csrf_token": CSRF},
    )

    [revision] = revisions_of("incident", incident_id)
    assert revision.snapshot["status"] == "open"
    assert revision.snapshot["resolved_at"] is None
    assert revision.snapshot["runbooks"] == [{"id": runbook_id, "title": "Restart DNS"}]

    page = logged_in.get(f"/incidents/{incident_id}/history/{revision.id}").text
    assert "Restart DNS" in page
    assert "Open" in page and "SEV2" in page
    assert "Tags: dns" in page


@pytest.mark.integration
def test_deleting_a_record_deletes_its_history(logged_in) -> None:
    runbook_id = create_runbook(logged_in)
    edit_runbook(logged_in, runbook_id, title="Changed")

    logged_in.post(f"/runbooks/{runbook_id}/delete", data={"csrf_token": CSRF})

    assert revisions_of("runbook", runbook_id) == []


@pytest.mark.integration
def test_wrong_ids_are_404(logged_in) -> None:
    one = create_runbook(logged_in)
    two = create_runbook(logged_in)
    edit_runbook(logged_in, one, title="Changed")
    [revision] = revisions_of("runbook", one)

    # A real revision, but of a different record: refused.
    assert logged_in.get(f"/runbooks/{two}/history/{revision.id}").status_code == 404
    # The runbook's revision asked for under /incidents: refused either way
    # (no such incident, or the revision isn't an incident's).
    assert logged_in.get(f"/incidents/{one}/history/{revision.id}").status_code == 404
    assert logged_in.get(f"/runbooks/{one}/history/{revision.id + 100000}").status_code == 404
    assert logged_in.get(f"/nonsense/{one}/history").status_code == 404
    assert re.search(r"Not found", logged_in.get(f"/runbooks/{two + 100000}/history").text)
