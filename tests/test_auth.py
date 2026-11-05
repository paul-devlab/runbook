"""Tests for logging in and out, sessions and login-required pages."""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.auth import SESSION_COOKIE, cookie_settings
from app.db import get_engine
from app.main import app
from app.models import User, UserSession
from app.users import create_user
from app.web import LOGIN_FAILED

PASSWORD = "correct horse battery staple"  # noqa: S105  (fake test value)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    # The test client talks plain http://, and browsers (and httpx) never send
    # Secure cookies over http, so turn Secure off here like local dev does.
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")
    # Don't follow redirects, so tests can check where they point.
    return TestClient(app, follow_redirects=False)


# --- Unit tests (no database) -------------------------------------------------


def test_cookie_is_httponly_lax_and_secure_by_default(monkeypatch) -> None:
    monkeypatch.setenv("POSTGRES_USER", "x")
    monkeypatch.setenv("POSTGRES_PASSWORD", "x")
    monkeypatch.setenv("POSTGRES_DB", "x")
    monkeypatch.delenv("SESSION_COOKIE_SECURE", raising=False)

    settings = cookie_settings()

    assert settings["httponly"] is True
    assert settings["samesite"] == "lax"
    assert settings["secure"] is True


def test_login_page_renders(client, monkeypatch) -> None:
    # No session cookie, so the database is never queried; settings must exist.
    for name in ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"):
        monkeypatch.setenv(name, "unused")
    response = client.get("/login")

    assert response.status_code == 200
    assert 'action="/login"' in response.text
    assert 'type="password"' in response.text


# --- Integration tests (real Postgres) ----------------------------------------


@pytest.fixture
def user() -> Iterator[User]:
    """A fresh active user; deleted afterwards (which also deletes their sessions)."""
    email = f"test-{uuid.uuid4().hex[:12]}@example.com"
    with Session(get_engine()) as db:
        new_user = create_user(db, email=email, display_name="Test User", password=PASSWORD)
        db.commit()
        db.refresh(new_user)
        db.expunge(new_user)
    yield new_user
    with Session(get_engine()) as db:
        db.execute(delete(User).where(User.id == new_user.id))
        db.commit()


def log_in(client: TestClient, email: str, password: str = PASSWORD):
    return client.post("/login", data={"email": email, "password": password})


def session_rows(user_id: int) -> list[UserSession]:
    with Session(get_engine()) as db:
        return list(db.scalars(select(UserSession).where(UserSession.user_id == user_id)))


@pytest.mark.integration
def test_home_needs_login(client) -> None:
    response = client.get("/")

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


@pytest.mark.integration
def test_login_sets_a_safe_cookie_and_stores_only_a_hash(client, user) -> None:
    response = log_in(client, user.email.upper())  # email match ignores case

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    set_cookie = response.headers["set-cookie"].lower()
    assert set_cookie.startswith(f"{SESSION_COOKIE}=")
    assert "httponly" in set_cookie
    assert "samesite=lax" in set_cookie

    token = client.cookies[SESSION_COOKIE]
    [row] = session_rows(user.id)
    assert row.token_hash != token  # the database never holds the token itself
    assert len(row.token_hash) == 64


@pytest.mark.integration
def test_logged_in_user_sees_home_page(client, user) -> None:
    log_in(client, user.email)

    response = client.get("/")

    assert response.status_code == 200
    assert "Welcome, Test User" in response.text
    assert 'action="/logout"' in response.text


@pytest.mark.integration
def test_login_page_redirects_when_already_logged_in(client, user) -> None:
    log_in(client, user.email)

    assert client.get("/login").headers["location"] == "/"


@pytest.mark.integration
@pytest.mark.parametrize("kind", ["wrong password", "unknown email"])
def test_failed_login_gives_the_same_message(client, user, kind) -> None:
    if kind == "wrong password":
        response = log_in(client, user.email, PASSWORD + "!")
    else:
        response = log_in(client, f"nobody-{uuid.uuid4().hex[:8]}@example.com")

    assert response.status_code == 400
    assert LOGIN_FAILED in response.text
    assert SESSION_COOKIE not in response.headers.get("set-cookie", "")
    assert session_rows(user.id) == []


@pytest.mark.integration
def test_deactivated_user_cannot_log_in_or_keep_their_session(client, user) -> None:
    log_in(client, user.email)
    with Session(get_engine()) as db:
        db.execute(update(User).where(User.id == user.id).values(is_active=False))
        db.commit()

    assert client.get("/").status_code == 303  # existing session no longer works
    assert log_in(client, user.email).status_code == 400  # and can't log in again


@pytest.mark.integration
def test_expired_session_is_rejected(client, user) -> None:
    log_in(client, user.email)
    with Session(get_engine()) as db:
        db.execute(
            update(UserSession)
            .where(UserSession.user_id == user.id)
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        db.commit()

    assert client.get("/").status_code == 303


@pytest.mark.integration
def test_logout_deletes_the_session_everywhere(client, user) -> None:
    log_in(client, user.email)
    stolen_token = client.cookies[SESSION_COOKIE]

    response = client.post("/logout")

    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert session_rows(user.id) == []
    # Even someone who copied the cookie before logout can't use it now.
    other = TestClient(app, follow_redirects=False, cookies={SESSION_COOKIE: stolen_token})
    assert other.get("/").status_code == 303


@pytest.mark.integration
def test_made_up_token_is_rejected(client) -> None:
    client.cookies.set(SESSION_COOKIE, "made-up-token")

    assert client.get("/").status_code == 303
