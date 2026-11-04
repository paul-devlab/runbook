"""Tests for the /health liveness endpoint."""

from fastapi.testclient import TestClient

from app.main import app

# TestClient calls the app directly in memory: no real server or network needed.
client = TestClient(app)


def test_health_returns_ok() -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
