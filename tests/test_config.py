"""Tests for settings loading and the database URL."""

import pytest
from pydantic import ValidationError

from app.config import Settings

# Deliberately awkward password: @ : / would break a hand-built URL string.
AWKWARD_PASSWORD = "p@ss:w/rd"  # noqa: S105  (fake test value, not a real secret)


@pytest.fixture
def db_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the env vars Settings reads (monkeypatch undoes this after the test)."""
    monkeypatch.setenv("POSTGRES_USER", "runbook")
    monkeypatch.setenv("POSTGRES_PASSWORD", AWKWARD_PASSWORD)
    monkeypatch.setenv("POSTGRES_DB", "runbook")
    monkeypatch.setenv("POSTGRES_HOST", "db.example")


def test_database_url_is_built_from_env(db_env) -> None:
    url = Settings().database_url

    assert url.drivername == "postgresql+psycopg"
    assert url.host == "db.example"
    assert url.port == 5432
    assert url.database == "runbook"
    # The password survives intact, special characters and all.
    assert url.password == AWKWARD_PASSWORD


def test_password_is_hidden_when_printed(db_env) -> None:
    settings = Settings()

    assert AWKWARD_PASSWORD not in repr(settings)
    assert AWKWARD_PASSWORD not in str(settings.database_url)  # SQLAlchemy masks it as ***


def test_missing_password_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POSTGRES_USER", "runbook")
    monkeypatch.setenv("POSTGRES_DB", "runbook")
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)

    with pytest.raises(ValidationError, match="postgres_password"):
        Settings()
