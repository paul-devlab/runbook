"""Tests for the database connection check."""

import pytest

from app.db import database_is_reachable


def test_unreachable_database_reports_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point at a port where nothing listens: the check must return False, not crash."""
    monkeypatch.setenv("POSTGRES_USER", "runbook")
    monkeypatch.setenv("POSTGRES_PASSWORD", "unused")
    monkeypatch.setenv("POSTGRES_DB", "runbook")
    monkeypatch.setenv("POSTGRES_HOST", "127.0.0.1")
    monkeypatch.setenv("POSTGRES_PORT", "1")  # nothing listens on port 1

    assert database_is_reachable() is False


@pytest.mark.integration
def test_real_database_is_reachable() -> None:
    assert database_is_reachable() is True
