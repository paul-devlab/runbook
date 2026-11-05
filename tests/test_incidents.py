"""Tests for the incident pages (app/incidents.py)."""

import os
import re
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.csrf import CSRF_COOKIE
from app.db import get_engine
from app.incidents import IncidentForm, format_duration, parse_time
from app.main import app
from app.models import Incident, Runbook, Tag, User
from app.tags import delete_unused_tags
from app.users import create_user

PASSWORD = "correct horse battery staple"  # noqa: S105  (fake test value)
CSRF = "i" * 43


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")
    for name in ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"):
        if name not in os.environ:
            monkeypatch.setenv(name, "unused")
    return TestClient(app, follow_redirects=False, cookies={CSRF_COOKIE: CSRF})


def form_input(**overrides) -> dict:
    """Valid form values, with some changed."""
    values = {
        "title": "DNS outage",
        "severity": "SEV2",
        "status": "open",
        "started_at": "2026-11-01T09:00",
    }
    return values | overrides


# --- Unit tests (no database) -------------------------------------------------


def test_parse_time_reads_the_picker_format_as_utc() -> None:
    assert parse_time("2026-11-01T09:30") == datetime(2026, 11, 1, 9, 30, tzinfo=UTC)
    assert parse_time("") is None
    assert parse_time("yesterday") is None


@pytest.mark.parametrize(
    ("delta", "text"),
    [
        (timedelta(0), "0m"),
        (timedelta(minutes=45), "45m"),
        (timedelta(hours=2, minutes=15), "2h 15m"),
        (timedelta(hours=3), "3h"),
        (timedelta(days=1, hours=1, minutes=5), "1d 1h 5m"),
        (None, ""),
    ],
)
def test_format_duration(delta, text) -> None:
    assert format_duration(delta) == text


def test_valid_form_has_no_errors() -> None:
    form = IncidentForm.from_input(**form_input(tags="DNS, On Call"))

    assert form.errors == []
    assert form.started_at == datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
    assert form.resolved_at is None
    assert form.tag_names == ["dns", "on-call"]


def test_resolved_without_a_time_means_now() -> None:
    before = datetime.now(UTC) - timedelta(minutes=1)

    form = IncidentForm.from_input(**form_input(status="resolved"))

    assert form.errors == []
    assert form.resolved_at >= before


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"title": "  "}, "Give the incident a title."),
        ({"severity": "SEV9"}, "Choose a severity."),
        ({"status": "closed"}, "Choose a status."),
        ({"started_at": ""}, "Enter when the incident started."),
        ({"started_at": "not a time"}, "Enter when the incident started."),
        ({"status": "resolved", "resolved_at": "soon"}, "isn't a valid date and time"),
        ({"resolved_at": "2026-11-01T10:00"}, "An open incident can't have a resolved time"),
        (
            {"status": "resolved", "resolved_at": "2026-11-01T08:00"},
            "can't be before the start time",
        ),
        ({"tags": "bad!"}, "can only use letters"),
    ],
)
def test_form_rules(overrides, error) -> None:
    errors = IncidentForm.from_input(**form_input(**overrides)).errors

    assert any(error in e for e in errors), errors


def test_blank_form_defaults_to_sev3_open_started_now() -> None:
    form = IncidentForm.blank()

    assert (form.severity, form.status) == ("SEV3", "open")
    assert parse_time(form.started_at_text) <= datetime.now(UTC)


@pytest.mark.parametrize("path", ["/incidents", "/incidents/new", "/incidents/1"])
def test_pages_need_login(client, path) -> None:
    assert client.get(path).headers["location"] == "/login"


def test_create_needs_a_csrf_token(client) -> None:
    assert client.post("/incidents", data=form_input()).status_code == 403


# --- Integration tests (real Postgres) ----------------------------------------


@pytest.fixture
def user() -> Iterator[User]:
    """A fresh user; afterwards their incidents, runbooks and the user are deleted."""
    email = f"test-{uuid.uuid4().hex[:12]}@example.com"
    with Session(get_engine()) as db:
        new_user = create_user(db, email=email, display_name="Incident Tester", password=PASSWORD)
        db.commit()
        db.refresh(new_user)
        db.expunge(new_user)
    yield new_user
    with Session(get_engine()) as db:
        db.execute(delete(Incident).where(Incident.created_by_id == new_user.id))
        db.execute(delete(Runbook).where(Runbook.created_by_id == new_user.id))
        db.execute(delete(User).where(User.id == new_user.id))
        delete_unused_tags(db)
        db.commit()


@pytest.fixture
def logged_in(client, user) -> TestClient:
    response = client.post(
        "/login", data={"email": user.email, "password": PASSWORD, "csrf_token": CSRF}
    )
    assert response.status_code == 303
    return client


def create(client: TestClient, **overrides) -> int:
    """Create an incident through the form; return its id."""
    response = client.post("/incidents", data=form_input(csrf_token=CSRF, **overrides))
    assert response.status_code == 303, response.text
    match = re.fullmatch(r"/incidents/(\d+)", response.headers["location"])
    assert match, response.headers["location"]
    return int(match.group(1))


def load(incident_id: int) -> Incident | None:
    with Session(get_engine()) as db:
        return db.get(Incident, incident_id)


def unique_tag() -> str:
    return f"t-{uuid.uuid4().hex[:10]}"


@pytest.mark.integration
def test_create_and_view(logged_in, user) -> None:
    incident_id = create(
        logged_in,
        status="resolved",
        resolved_at="2026-11-01T11:15",
        summary="Lookups failed",
        root_cause_md="**Expired** zone file",
        resolution_md="1. Reloaded named",
    )

    page = logged_in.get(f"/incidents/{incident_id}").text

    assert "DNS outage" in page
    assert "SEV2" in page and "Resolved" in page
    assert "01 Nov 2026 09:00 UTC" in page
    assert "(2h 15m)" in page
    assert "<strong>Expired</strong>" in page  # Markdown rendered
    assert "<li>Reloaded named</li>" in page
    assert "Incident Tester" in page
    saved = load(incident_id)
    assert saved.resolved_at == datetime(2026, 11, 1, 11, 15, tzinfo=UTC)
    assert saved.created_by_id == saved.updated_by_id == user.id


@pytest.mark.integration
def test_invalid_input_is_rejected_and_kept(logged_in) -> None:
    response = logged_in.post(
        "/incidents",
        data=form_input(
            csrf_token=CSRF, status="resolved", resolved_at="2026-10-01T00:00", summary="keep me"
        ),
    )

    assert response.status_code == 400
    assert "be before the start time" in response.text  # (Jinja escapes the ')
    assert 'value="keep me"' in response.text
    assert 'value="2026-10-01T00:00"' in response.text


@pytest.mark.integration
def test_list_shows_most_recent_first_with_badges(logged_in) -> None:
    older = create(logged_in, title="Older", started_at="2026-01-01T00:00")
    newer = create(logged_in, title="Newer", started_at="2026-06-01T00:00", severity="SEV1")

    text = logged_in.get("/incidents").text

    assert text.index(f"/incidents/{newer}") < text.index(f"/incidents/{older}")
    assert 'class="badge sev1"' in text


@pytest.mark.integration
def test_edit_prefills_and_can_reopen(logged_in) -> None:
    incident_id = create(logged_in, status="resolved", resolved_at="2026-11-01T10:00")

    form = logged_in.get(f"/incidents/{incident_id}/edit").text
    assert 'value="2026-11-01T09:00"' in form
    assert 'value="2026-11-01T10:00"' in form
    assert '<option value="resolved" selected>' in form

    response = logged_in.post(
        f"/incidents/{incident_id}/edit",
        data=form_input(csrf_token=CSRF, title="Reopened", status="open", resolved_at=""),
    )

    assert response.headers["location"] == f"/incidents/{incident_id}"
    saved = load(incident_id)
    assert (saved.title, saved.status, saved.resolved_at) == ("Reopened", "open", None)


@pytest.mark.integration
def test_delete_asks_first_then_deletes(logged_in) -> None:
    incident_id = create(logged_in)

    assert "can't be undone" in logged_in.get(f"/incidents/{incident_id}/delete").text
    assert load(incident_id) is not None

    response = logged_in.post(f"/incidents/{incident_id}/delete", data={"csrf_token": CSRF})

    assert response.headers["location"] == "/incidents"
    assert load(incident_id) is None
    assert logged_in.get(f"/incidents/{incident_id}").status_code == 404


@pytest.mark.integration
@pytest.mark.parametrize("suffix", ["", "/edit", "/delete"])
def test_unknown_incident_is_404(logged_in, suffix) -> None:
    with Session(get_engine()) as db:
        missing = (db.scalar(select(Incident.id).order_by(Incident.id.desc()).limit(1)) or 0) + 1000

    response = logged_in.get(f"/incidents/{missing}{suffix}")

    assert response.status_code == 404
    assert "Not found" in response.text


@pytest.mark.integration
def test_tags_filter_and_link_to_incidents(logged_in) -> None:
    wanted = unique_tag()
    tagged = create(logged_in, tags=wanted)
    other = create(logged_in)

    page = logged_in.get(f"/incidents?tag={wanted}").text

    assert f"/incidents/{tagged}" in page
    assert f"/incidents/{other}" not in page
    assert f'href="/incidents?tag={wanted}"' in page


@pytest.mark.integration
def test_runbooks_and_incidents_share_tags(logged_in) -> None:
    shared = unique_tag()
    create(logged_in, tags=shared)
    logged_in.post("/runbooks", data={"title": "Fix DNS", "tags": shared, "csrf_token": CSRF})

    with Session(get_engine()) as db:
        assert len(db.scalars(select(Tag).where(Tag.name == shared)).all()) == 1
        # Still used by both, so cleanup must keep it.
        delete_unused_tags(db)
        db.commit()
        assert db.scalars(select(Tag).where(Tag.name == shared)).one()


@pytest.mark.integration
def test_text_fields_are_sanitised(logged_in) -> None:
    evil = "<script>alert(1)</script>"
    incident_id = create(
        logged_in, title=evil, summary=evil, root_cause_md=evil, resolution_md=evil
    )

    for page in (logged_in.get("/incidents"), logged_in.get(f"/incidents/{incident_id}")):
        assert evil not in page.text


@pytest.mark.integration
@pytest.mark.parametrize(
    "bad_values",
    [
        {"severity": "SEV9"},
        {"status": "closed"},
        {"status": "resolved"},  # resolved, but no resolved time
        {"resolved_at": datetime(2026, 11, 1, tzinfo=UTC)},  # open, but has a resolved time
        {"status": "resolved", "resolved_at": datetime(2026, 1, 1, tzinfo=UTC)},  # before start
    ],
)
def test_database_rejects_bad_data_even_without_the_form(logged_in, bad_values) -> None:
    """The check constraints are a second line of defence behind the form."""
    incident_id = create(logged_in)

    with Session(get_engine()) as db, pytest.raises(IntegrityError):
        db.execute(update(Incident).where(Incident.id == incident_id).values(**bad_values))
        db.commit()


# --- Links to runbooks --------------------------------------------------------


def test_linked_runbook_ids_are_deduped_and_limited() -> None:
    assert IncidentForm.from_input(**form_input(runbook_ids=[3, 1, 3])).runbook_ids == [3, 1]
    errors = IncidentForm.from_input(**form_input(runbook_ids=list(range(1, 22)))).errors
    assert "Link at most 20 runbooks." in errors


def create_runbook(client: TestClient, title: str) -> int:
    response = client.post("/runbooks", data={"title": title, "csrf_token": CSRF})
    return int(response.headers["location"].rsplit("/", 1)[1])


@pytest.mark.integration
def test_incident_links_to_runbooks_both_ways(logged_in) -> None:
    dns = create_runbook(logged_in, "Restart DNS")
    vpn = create_runbook(logged_in, "Reset VPN")
    create_runbook(logged_in, "Unrelated")

    form = logged_in.get("/incidents/new").text
    assert f'name="runbook_ids" value="{dns}"' in form  # every runbook is offered

    incident_id = create(logged_in, title="Outage", runbook_ids=[dns, vpn])

    incident_page = logged_in.get(f"/incidents/{incident_id}").text
    assert f'href="/runbooks/{dns}"' in incident_page
    assert f'href="/runbooks/{vpn}"' in incident_page
    runbook_page = logged_in.get(f"/runbooks/{dns}").text
    assert "Used in incidents" in runbook_page
    assert f'href="/incidents/{incident_id}"' in runbook_page


@pytest.mark.integration
def test_editing_changes_the_links(logged_in) -> None:
    dns = create_runbook(logged_in, "Restart DNS")
    vpn = create_runbook(logged_in, "Reset VPN")
    incident_id = create(logged_in, runbook_ids=[dns])

    form = logged_in.get(f"/incidents/{incident_id}/edit").text
    assert re.search(rf'value="{dns}"\s+checked', form)  # ticked
    assert not re.search(rf'value="{vpn}"\s+checked', form)  # not ticked

    logged_in.post(
        f"/incidents/{incident_id}/edit", data=form_input(csrf_token=CSRF, runbook_ids=[vpn])
    )

    with Session(get_engine()) as db:
        assert [r.id for r in db.get(Incident, incident_id).runbooks] == [vpn]


@pytest.mark.integration
def test_ticking_a_deleted_runbook_is_an_error(logged_in) -> None:
    gone = create_runbook(logged_in, "Soon deleted")
    logged_in.post(f"/runbooks/{gone}/delete", data={"csrf_token": CSRF})

    response = logged_in.post("/incidents", data=form_input(csrf_token=CSRF, runbook_ids=[gone]))

    assert response.status_code == 400
    assert "has been deleted" in response.text


@pytest.mark.integration
def test_deleting_either_side_keeps_the_other(logged_in) -> None:
    runbook_id = create_runbook(logged_in, "Keep me")
    incident_id = create(logged_in, runbook_ids=[runbook_id])
    other_runbook = create_runbook(logged_in, "Linked, then deleted")
    other_incident = create(logged_in, runbook_ids=[other_runbook])

    logged_in.post(f"/incidents/{incident_id}/delete", data={"csrf_token": CSRF})
    logged_in.post(f"/runbooks/{other_runbook}/delete", data={"csrf_token": CSRF})

    assert logged_in.get(f"/runbooks/{runbook_id}").status_code == 200
    assert "Used in incidents" not in logged_in.get(f"/runbooks/{runbook_id}").text
    assert load(other_incident) is not None
    assert "None linked." in logged_in.get(f"/incidents/{other_incident}").text
