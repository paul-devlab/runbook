"""Tests for full-text search and the list filters (app/search.py and the list pages).

All need a real Postgres: the searching is done by the database.
"""

import os
import re
import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.csrf import CSRF_COOKIE
from app.db import get_engine
from app.main import app
from app.models import Incident, Revision, Runbook, User
from app.search import QUERY_MAX, search_vector_sql, tidy_query
from app.tags import delete_unused_tags
from app.users import create_user

PASSWORD = "correct horse battery staple"  # noqa: S105  (fake test value)
CSRF = "s" * 43


# --- Unit tests (no database) -------------------------------------------------


def test_search_vector_sql_weights_each_column() -> None:
    assert search_vector_sql(title="A", body_md="C") == (
        "setweight(to_tsvector('english', coalesce(title, '')), 'A') || "
        "setweight(to_tsvector('english', coalesce(body_md, '')), 'C')"
    )


def test_tidy_query() -> None:
    assert tidy_query(None) == ""
    assert tidy_query("  dns  ") == "dns"
    assert len(tidy_query("x" * 1000)) == QUERY_MAX


# --- Integration tests (real Postgres) ----------------------------------------


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")
    for name in ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"):
        if name not in os.environ:
            monkeypatch.setenv(name, "unused")
    return TestClient(app, follow_redirects=False, cookies={CSRF_COOKIE: CSRF})


@pytest.fixture
def logged_in(client) -> Iterator[TestClient]:
    email = f"test-{uuid.uuid4().hex[:12]}@example.com"
    with Session(get_engine()) as db:
        user = create_user(db, email=email, display_name="Search Tester", password=PASSWORD)
        db.commit()
        user_id = user.id
    client.post("/login", data={"email": email, "password": PASSWORD, "csrf_token": CSRF})
    yield client
    with Session(get_engine()) as db:
        db.execute(delete(Incident).where(Incident.created_by_id == user_id))
        db.execute(delete(Runbook).where(Runbook.created_by_id == user_id))
        db.execute(delete(Revision).where(Revision.edited_by_id == user_id))
        db.execute(delete(User).where(User.id == user_id))
        delete_unused_tags(db)
        db.commit()


def word() -> str:
    """A made-up word no other record contains, so results are predictable
    even with other data in the database."""
    return "zq" + uuid.uuid4().hex[:8]


def runbook(client: TestClient, title: str, **fields) -> int:
    response = client.post("/runbooks", data={"title": title, "csrf_token": CSRF, **fields})
    return int(response.headers["location"].rsplit("/", 1)[1])


def incident(client: TestClient, title: str, **fields) -> int:
    data = {"severity": "SEV3", "status": "open", "started_at": "2026-11-01T09:00"}
    response = client.post(
        "/incidents", data={"title": title, "csrf_token": CSRF, **data, **fields}
    )
    assert response.status_code == 303, response.text
    return int(response.headers["location"].rsplit("/", 1)[1])


def listed(client: TestClient, url: str, kind: str) -> list[int]:
    """The ids on a list page, in order."""
    return [int(i) for i in re.findall(rf'<a href="/{kind}/(\d+)">', client.get(url).text)]


@pytest.mark.integration
def test_search_finds_other_forms_of_a_word(logged_in) -> None:
    w = word()
    found = runbook(logged_in, "Network", body_md=f"Restarting the {w} service")

    # "restart" finds "Restarting": both are stemmed to the same word.
    assert listed(logged_in, f"/runbooks?q=restart {w}", "runbooks") == [found]


@pytest.mark.integration
def test_title_matches_rank_above_body_matches(logged_in) -> None:
    w = word()
    in_body = runbook(logged_in, "Something else", body_md=f"mentions {w} once")
    in_title = runbook(logged_in, f"All about {w}")

    assert listed(logged_in, f"/runbooks?q={w}", "runbooks") == [in_title, in_body]


@pytest.mark.integration
def test_search_syntax_phrase_or_and_exclude(logged_in) -> None:
    a, b = word(), word()
    both = runbook(logged_in, f"{a} {b}")
    only_a = runbook(logged_in, f"{a} alone")
    reversed_order = runbook(logged_in, f"{b} {a}")

    assert set(listed(logged_in, f"/runbooks?q={a} -{b}", "runbooks")) == {only_a}
    assert listed(logged_in, f'/runbooks?q="{a} {b}"', "runbooks") == [both]
    assert set(listed(logged_in, f"/runbooks?q={b} or {a}", "runbooks")) == {
        both,
        only_a,
        reversed_order,
    }


@pytest.mark.integration
@pytest.mark.parametrize("odd", ['"', "!!! &&& |||", "-", "(", "x" * 500, "ünïcödé", "' OR 1=1 --"])
def test_odd_search_text_never_errors(logged_in, odd) -> None:
    assert logged_in.get("/runbooks", params={"q": odd}).status_code == 200
    assert logged_in.get("/incidents", params={"q": odd}).status_code == 200


@pytest.mark.integration
def test_search_sees_edits(logged_in) -> None:
    old, new = word(), word()
    runbook_id = runbook(logged_in, f"Before {old}")

    logged_in.post(
        f"/runbooks/{runbook_id}/edit", data={"title": f"After {new}", "csrf_token": CSRF}
    )

    assert listed(logged_in, f"/runbooks?q={old}", "runbooks") == []
    assert listed(logged_in, f"/runbooks?q={new}", "runbooks") == [runbook_id]


@pytest.mark.integration
def test_search_and_tag_combine(logged_in) -> None:
    w, tag = word(), word()
    tagged = runbook(logged_in, w, tags=tag)
    runbook(logged_in, w)

    page = logged_in.get(f"/runbooks?q={w}&tag={tag}").text

    assert listed(logged_in, f"/runbooks?q={w}&tag={tag}", "runbooks") == [tagged]
    assert "1 runbook" in page
    assert f'name="tag" value="{tag}"' in page  # searching again keeps the tag


@pytest.mark.integration
def test_incident_search_covers_root_cause_and_resolution(logged_in) -> None:
    cause, fix = word(), word()
    found = incident(logged_in, "Outage", root_cause_md=f"cert {cause}", resolution_md=fix)

    assert listed(logged_in, f"/incidents?q={cause}", "incidents") == [found]
    assert listed(logged_in, f"/incidents?q={fix}", "incidents") == [found]


@pytest.mark.integration
def test_incident_filters_combine(logged_in) -> None:
    w = word()
    sev1_open = incident(logged_in, w, severity="SEV1")
    incident(logged_in, w, severity="SEV1", status="resolved")
    incident(logged_in, w, severity="SEV2")

    assert listed(logged_in, f"/incidents?q={w}&severity=SEV1&status=open", "incidents") == [
        sev1_open
    ]
    assert len(listed(logged_in, f"/incidents?q={w}&severity=SEV1", "incidents")) == 2
    assert len(listed(logged_in, f"/incidents?q={w}&status=open", "incidents")) == 2


@pytest.mark.integration
def test_unknown_filter_values_mean_any(logged_in) -> None:
    w = word()
    incident(logged_in, w)

    page = logged_in.get(f"/incidents?q={w}&severity=SEV9&status=closed&q2=x")

    assert page.status_code == 200
    assert len(listed(logged_in, f"/incidents?q={w}&severity=SEV9&status=", "incidents")) == 1


@pytest.mark.integration
def test_filter_form_shows_current_choices(logged_in) -> None:
    page = logged_in.get("/incidents?severity=SEV2&status=resolved&q=dns").text

    assert '<option value="SEV2" selected>' in page
    assert '<option value="resolved" selected>' in page
    assert 'name="q" value="dns"' in page
    assert "Clear filters" in page
