"""Tests for the /ready readiness endpoint."""

import pytest
from fastapi.testclient import TestClient

from app.db import database_is_reachable
from app.main import app

client = TestClient(app)


@pytest.fixture
def fake_database():
    """Replace the real database check with a fake we control.

    Returns a function: fake_database(True) or fake_database(False).
    """

    def set_reachable(reachable: bool) -> None:
        app.dependency_overrides[database_is_reachable] = lambda: reachable

    yield set_reachable
    app.dependency_overrides.clear()


def test_ready_returns_200_when_database_is_up(fake_database) -> None:
    fake_database(True)

    response = client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_ready_returns_503_when_database_is_down(fake_database) -> None:
    fake_database(False)

    response = client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}


@pytest.mark.integration
def test_ready_with_real_database() -> None:
    """End to end: no fake, the real check against the Compose Postgres."""
    response = client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
