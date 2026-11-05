"""Tests for Markdown rendering and sanitising (app/markdown.py).

Record content is untrusted, so most of these are XSS attempts: each one must
come out harmless. None need a database.
"""

from html.parser import HTMLParser

import pytest
from markupsafe import Markup

from app.markdown import render_markdown


def parsed_tags(html: str) -> list[tuple[str, list[tuple[str, str | None]]]]:
    """The real tags and attributes in `html`, as a browser would see them.
    (Escaped text like "&lt;img&gt;" is just text, so it isn't a tag.)"""
    found = []

    class Collector(HTMLParser):
        def handle_starttag(self, tag, attrs):
            found.append((tag, attrs))

    Collector().feed(html)
    return found


def test_renders_common_markdown() -> None:
    html = render_markdown(
        "# Restart DNS\n\n1. Run `systemctl restart named`\n2. **Check** it\n\n"
        "```\ndig example.com\n```\n\n| Host | Role |\n|---|---|\n| dns01 | primary |\n"
    )

    assert "<h1>Restart DNS</h1>" in html
    assert "<ol>" in html and "<li>Run <code>systemctl restart named</code></li>" in html
    assert "<strong>Check</strong>" in html
    assert "<pre><code>dig example.com\n</code></pre>" in html
    assert "<td>dns01</td>" in html


def test_returns_markup_so_jinja_does_not_escape_it_again() -> None:
    assert isinstance(render_markdown("hi"), Markup)


@pytest.mark.parametrize("empty", [None, ""])
def test_empty_input_gives_empty_output(empty) -> None:
    assert render_markdown(empty) == ""


def test_links_are_kept_with_a_safe_rel() -> None:
    html = render_markdown("[docs](https://example.com/dns)")

    assert 'href="https://example.com/dns"' in html
    assert 'rel="noopener noreferrer nofollow"' in html


@pytest.mark.parametrize(
    "attack",
    [
        "<script>alert(1)</script>",
        "<img src=x onerror=alert(1)>",
        '<a href="#" onclick="alert(1)">click</a>',
        "<iframe src=https://evil.example></iframe>",
        "[click](javascript:alert(1))",
        "[click](JaVaScRiPt:alert(1))",
        "[click](data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==)",
        "[click](vbscript:msgbox(1))",
        '<div style="background:url(javascript:alert(1))">x</div>',
        "![tracker](https://evil.example/pixel.png)",
    ],
)
def test_attacks_come_out_harmless(attack) -> None:
    tags = parsed_tags(render_markdown(attack))

    names = {name for name, _ in tags}
    assert names <= {"p", "a"}, names  # no script, img, iframe, div...
    for _, attrs in tags:
        for attr, value in attrs:
            assert attr in {"href", "rel"}, attr  # no onclick, onerror, style...
            if attr == "href":
                assert value.split(":")[0].lower() in {"https", "http", "mailto", "#"} or (
                    ":" not in value
                ), value


def test_raw_html_is_shown_as_text() -> None:
    """Someone documenting HTML should see it, not have it disappear or run."""
    assert "&lt;b&gt;bold&lt;/b&gt;" in render_markdown("<b>bold</b>")
