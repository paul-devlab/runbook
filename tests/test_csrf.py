"""Tests for CSRF protection (app/csrf.py). None of these need a database."""

import re

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.csrf import CSRF_COOKIE, REJECTED, verify_csrf
from app.main import app

TOKEN = "t" * 43  # a well-formed token (43 URL-safe characters)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    # Settings must exist, but these tests never reach the database.
    for name in ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"):
        monkeypatch.setenv(name, "unused")
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")
    return TestClient(app, follow_redirects=False)


def _uses(dependant, func) -> bool:
    return any(d.call is func or _uses(d, func) for d in dependant.dependencies)


def _all_api_routes(routes) -> list[APIRoute]:
    """Every APIRoute in the app, including those inside included routers
    (recent FastAPI keeps an included router wrapped, with `original_router`)."""
    found = []
    for route in routes:
        if isinstance(route, APIRoute):
            found.append(route)
        elif hasattr(route, "original_router"):
            found += _all_api_routes(route.original_router.routes)
        elif hasattr(route, "routes"):
            found += _all_api_routes(route.routes)
    return found


def test_every_post_route_checks_the_csrf_token() -> None:
    """Guards future routes: a new form handler without verify_csrf fails here."""
    unsafe = [
        r for r in _all_api_routes(app.routes) if r.methods & {"POST", "PUT", "PATCH", "DELETE"}
    ]
    assert {r.path for r in unsafe} >= {"/login", "/logout"}
    missing = [r.path for r in unsafe if not _uses(r.dependant, verify_csrf)]
    assert missing == []


def test_login_page_puts_the_cookie_token_in_the_form(client) -> None:
    response = client.get("/login")

    cookie_token = client.cookies[CSRF_COOKIE]
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", cookie_token)
    assert f'name="csrf_token" value="{cookie_token}"' in response.text
    assert "httponly" in response.headers["set-cookie"].lower()


def test_existing_token_is_reused_not_replaced(client) -> None:
    client.cookies.set(CSRF_COOKIE, TOKEN)

    response = client.get("/login")

    assert f'value="{TOKEN}"' in response.text
    assert "set-cookie" not in response.headers


def test_malformed_cookie_token_is_replaced(client) -> None:
    client.cookies.set(CSRF_COOKIE, 'x"><script>')

    response = client.get("/login")

    assert "<script>" not in response.text
    assert CSRF_COOKIE in response.headers["set-cookie"]


def test_health_does_not_set_a_cookie(client) -> None:
    assert "set-cookie" not in client.get("/health").headers


@pytest.mark.parametrize(
    "sent",
    [None, "wrong-" + "x" * 37, "ünïcode"],
    ids=["missing", "wrong", "non-ascii"],
)
def test_post_without_a_matching_token_is_refused(client, sent) -> None:
    client.cookies.set(CSRF_COOKIE, TOKEN)
    data = {"email": "a@example.com", "password": "x"}
    if sent is not None:
        data["csrf_token"] = sent

    response = client.post("/login", data=data)

    assert response.status_code == 403
    assert response.text == REJECTED


def test_post_without_a_cookie_is_refused(client) -> None:
    response = client.post("/logout", data={"csrf_token": TOKEN})

    assert response.status_code == 403


@pytest.mark.parametrize(
    "headers",
    [
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "same-site"},  # a sibling subdomain is still another site
        {"Origin": "https://evil.example"},
    ],
)
def test_cross_site_posts_are_refused_even_with_the_token(client, headers) -> None:
    client.cookies.set(CSRF_COOKIE, TOKEN)

    response = client.post("/logout", data={"csrf_token": TOKEN}, headers=headers)

    assert response.status_code == 403


@pytest.mark.parametrize(
    "headers",
    [{"Sec-Fetch-Site": "same-origin"}, {"Origin": "http://testserver"}, {}],
)
def test_same_site_post_with_the_token_is_accepted(client, headers) -> None:
    client.cookies.set(CSRF_COOKIE, TOKEN)

    # Logout with no session cookie needs no database, so it can run here.
    response = client.post("/logout", data={"csrf_token": TOKEN}, headers=headers)

    assert response.status_code == 303


def test_htmx_header_is_accepted_instead_of_the_form_field(client) -> None:
    client.cookies.set(CSRF_COOKIE, TOKEN)

    response = client.post("/logout", headers={"X-CSRF-Token": TOKEN})

    assert response.status_code == 303


def test_pages_tell_htmx_to_send_the_token(client) -> None:
    client.cookies.set(CSRF_COOKIE, TOKEN)

    assert f'hx-headers=\'{{"X-CSRF-Token": "{TOKEN}"}}\'' in client.get("/login").text
