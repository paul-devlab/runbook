"""Tests for JSON logging (app/logs.py)."""

import json
import logging
import os
import re
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.db import database_is_reachable
from app.logs import JsonFormatter, request_id, request_log_middleware
from app.main import app


def make_record(msg: str = "hello %s", args=("world",), **extra) -> logging.LogRecord:
    record = logging.makeLogRecord({"name": "app.test", "levelname": "INFO", "msg": msg})
    record.args = args
    for key, value in extra.items():
        setattr(record, key, value)
    return record


# --- The formatter ------------------------------------------------------------------


def test_each_line_is_json_with_the_basics() -> None:
    line = json.loads(JsonFormatter().format(make_record()))

    assert line["msg"] == "hello world"
    assert line["level"] == "INFO"
    assert line["logger"] == "app.test"
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}\+00:00", line["time"])


def test_extra_fields_are_included() -> None:
    line = json.loads(JsonFormatter().format(make_record(status=404, path="/x")))

    assert (line["status"], line["path"]) == (404, "/x")
    assert "args" not in line  # standard record attributes aren't repeated


def test_uvicorns_colour_codes_are_dropped() -> None:
    line = json.loads(JsonFormatter().format(make_record(color_message="\x1b[36mhi")))

    assert "color_message" not in line


def test_exceptions_are_kept_in_one_line() -> None:
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        record = make_record(exc_info=sys.exc_info())

    text = JsonFormatter().format(record)

    assert "\n" not in text  # one event = one line, even with a traceback
    assert "RuntimeError: boom" in json.loads(text)["error"]


# --- One line per request ----------------------------------------------------------


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")
    for name in ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"):
        if name not in os.environ:
            monkeypatch.setenv(name, "unused")
    return TestClient(app, follow_redirects=False)


def request_lines(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "app.requests"]


def test_request_is_logged_with_its_id_and_without_the_query(client, caplog) -> None:
    caplog.set_level(logging.INFO)

    response = client.get("/login?q=something-private")

    [record] = request_lines(caplog)
    assert (record.method, record.path, record.status) == ("GET", "/login", 200)
    assert record.duration_ms >= 0
    assert record.request_id == response.headers["X-Request-ID"]
    assert re.fullmatch(r"[0-9a-f]{16}", record.request_id)
    assert "something-private" not in JsonFormatter().format(record)


def test_every_request_gets_a_new_id(client) -> None:
    first, second = (client.get("/login").headers["X-Request-ID"] for _ in range(2))

    assert first != second
    assert request_id.get() == ""  # nothing left over after a request


def test_healthy_probes_are_not_logged_but_failures_are(client, caplog) -> None:
    caplog.set_level(logging.INFO)
    client.get("/health")
    app.dependency_overrides[database_is_reachable] = lambda: False
    try:
        client.get("/ready")
    finally:
        app.dependency_overrides.pop(database_is_reachable)

    [record] = request_lines(caplog)
    assert (record.path, record.status, record.levelname) == ("/ready", 503, "ERROR")


def test_a_crash_is_logged_as_a_500(caplog) -> None:
    crashing = FastAPI()
    crashing.middleware("http")(request_log_middleware)

    @crashing.get("/boom")
    def boom() -> None:
        raise RuntimeError("boom")

    caplog.set_level(logging.INFO)
    TestClient(crashing, raise_server_exceptions=False).get("/boom")

    [record] = request_lines(caplog)
    assert (record.status, record.levelname) == (500, "ERROR")
