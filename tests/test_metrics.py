"""Tests for Prometheus metrics (app/metrics.py)."""

import os
import socket
import urllib.request

import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from app.main import app
from app.metrics import PRICES, estimated_cost, start_metrics_server


def value(name: str, **labels) -> float:
    """A metric's current value (0 if it hasn't been recorded yet)."""
    return REGISTRY.get_sample_value(name, labels) or 0.0


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")
    for name in ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"):
        if name not in os.environ:
            monkeypatch.setenv(name, "unused")
    return TestClient(app, follow_redirects=False)


def test_cost_estimate() -> None:
    # A million tokens each way on Opus 5.5: $4 in + $20 out.
    assert estimated_cost("claude-opus-5-5", 1_000_000, 1_000_000) == 24.0
    assert estimated_cost("claude-haiku-5-5", 2_000, 1_000) == pytest.approx(0.0007)


def test_unknown_models_are_costed_at_the_highest_price() -> None:
    highest = max(PRICES.values())
    assert estimated_cost("claude-new-model", 1_000_000, 0) == highest[0]


@pytest.mark.parametrize(
    ("path", "route", "status"),
    [
        ("/runbooks/12345", "/runbooks/{runbook_id}", "303"),  # the template, not the id
        ("/static/app.css", "/static", "200"),
        ("/no-such-page", "unmatched", "404"),
    ],
)
def test_requests_are_counted_by_route_template(client, path, route, status) -> None:
    labels = {"method": "GET", "route": route}
    before = value("runbook_http_requests_total", **labels, status=status)
    timed_before = value("runbook_http_request_duration_seconds_count", **labels)

    assert str(client.get(path).status_code) == status

    assert value("runbook_http_requests_total", **labels, status=status) == before + 1
    assert value("runbook_http_request_duration_seconds_count", **labels) == timed_before + 1


def test_ask_outcomes_start_at_zero() -> None:
    # Every outcome exists from the start, so graphs show 0 rather than nothing.
    assert (
        REGISTRY.get_sample_value("runbook_ask_questions_total", {"outcome": "refused"}) is not None
    )


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_metrics_are_served_on_their_own_port(client) -> None:
    client.get("/health")
    port = free_port()
    server = start_metrics_server(port)
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5) as response:
            text = response.read().decode()
    finally:
        server.shutdown()

    assert 'runbook_http_requests_total{method="GET",route="/health",status="200"}' in text
    assert "runbook_ask_cost_dollars" in text
    assert "process_resident_memory_bytes" in text  # memory use, built in


def test_metrics_can_be_turned_off() -> None:
    assert start_metrics_server(0) is None


def test_metrics_are_not_on_the_website(client) -> None:
    assert client.get("/metrics").status_code == 404
