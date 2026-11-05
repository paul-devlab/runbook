"""Tests for the HTML pages, the shared layout and static files."""

from fastapi.testclient import TestClient

from app.main import app
from app.web import templates

client = TestClient(app)


def test_pages_use_the_shared_layout(monkeypatch) -> None:
    # /login is public; with no session cookie it never queries the database,
    # but it does need the settings to exist.
    for name in ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"):
        monkeypatch.setenv(name, "unused")
    response = client.get("/login")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    html = response.text
    assert "<title>Log in · Runbook</title>" in html
    assert 'href="/static/app.css"' in html  # a path, not a full URL
    assert "/static/vendor/htmx-2.0.11.min.js" in html
    assert '<main id="main">' in html


def test_static_files_are_served() -> None:
    css = client.get("/static/app.css")
    js = client.get("/static/vendor/htmx-2.0.11.min.js")

    assert css.status_code == 200
    assert css.headers["content-type"].startswith("text/css")
    assert js.status_code == 200
    assert 'version:"2.0.11"' in js.text


def test_missing_static_file_is_404() -> None:
    assert client.get("/static/nope.css").status_code == 404


def test_templates_escape_html() -> None:
    """Autoescaping must stay on: it's what stops record content running as script (XSS)."""
    template = templates.env.get_template("base.html")

    class FakeURL:
        path = "/"

    rendered = template.render(
        page_title="<script>alert(1)</script>", url_for=lambda *a, **k: FakeURL()
    )

    assert "<script>alert(1)</script>" not in rendered
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in rendered
