"""Protection against cross-site request forgery (CSRF).

The attack: you're logged in to Runbook, then visit an evil web page. That page
contains a hidden form that posts to Runbook (say, "delete this runbook") and
submits itself. Your browser sends your Runbook cookie along, so without
protection the request would look like it came from you.

Two layers stop it:

1. **Origin check** (`csrf_middleware`). Browsers label every request with where
   it came from: `Sec-Fetch-Site` ("same-origin", "cross-site", ...) and, for
   POSTs, `Origin` ("http://staging.local"). Any POST from another site is
   refused before it reaches our code.

2. **Token check** (`verify_csrf`). Each browser gets a random token in a
   cookie, and every form we render repeats it in a hidden field. A POST is
   accepted only when the two match. The evil page can make the browser send
   the cookie, but it can't *read* it, so it can't put the right value in the
   form. This is the "double-submit cookie" pattern.

Every POST route must use `Depends(verify_csrf)`; tests/test_csrf.py checks that
none is missed.
"""

import re
import secrets
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Form, Request, Response
from fastapi.responses import PlainTextResponse

from app.config import get_settings

CSRF_COOKIE = "runbook_csrf"
CSRF_FIELD = "csrf_token"  # the hidden form field's name
CSRF_HEADER = "x-csrf-token"  # for HTMX requests that aren't plain forms

# Methods that change things. GET/HEAD/OPTIONS must never change anything.
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

# What secrets.token_urlsafe(32) produces: 43 URL-safe characters.
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}")

REJECTED = (
    "This form has expired or came from another site. Go back, reload the page and try again."
)


class CsrfFailed(Exception):
    """Raised by `verify_csrf`; app/main.py turns it into a 403 response."""


def _is_cross_origin(request: Request) -> bool:
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site is not None:
        # "none" = typed in the address bar or a bookmark; never cross-site.
        return fetch_site not in ("same-origin", "none")
    # Older browsers: compare the Origin header with the Host we were sent to.
    origin = request.headers.get("origin")
    if origin is None:
        return False  # not a browser (curl, tests): the token check still applies
    return urlsplit(origin).netloc != request.headers.get("host")


async def csrf_middleware(request: Request, call_next) -> Response:
    """Runs on every request: refuses cross-site POSTs, and gives the browser a
    CSRF cookie if a page rendered a form for a browser that didn't have one."""
    if request.method in UNSAFE_METHODS and _is_cross_origin(request):
        return PlainTextResponse(REJECTED, status_code=403)

    response = await call_next(request)
    new_token = getattr(request.state, "new_csrf_token", None)
    if new_token:
        response.set_cookie(
            CSRF_COOKIE,
            new_token,
            httponly=True,  # only the server reads it; forms get it from the page
            samesite="lax",
            secure=get_settings().session_cookie_secure,
            path="/",
        )
    return response


def verify_csrf(
    request: Request,
    csrf_token: Annotated[str | None, Form(max_length=100)] = None,
) -> None:
    """FastAPI dependency for every POST route: the form field (or the
    X-CSRF-Token header) must match the cookie."""
    sent = csrf_token or request.headers.get(CSRF_HEADER)
    expected = request.cookies.get(CSRF_COOKIE)
    # compare_digest takes the same time however many characters match, so the
    # response time doesn't leak the token a character at a time.
    if not sent or not expected or not secrets.compare_digest(sent.encode(), expected.encode()):
        raise CsrfFailed


def csrf_context(request: Request) -> dict[str, str]:
    """Jinja2 context processor: makes {{ csrf_token }} available in every template.

    Reuses the browser's token if it has one; otherwise makes a new one, which
    the middleware then sends as a cookie. So only pages that render get a
    cookie, not /health or static files."""
    token = request.cookies.get(CSRF_COOKIE)
    # Only reuse a well-formed token, so an odd cookie value never ends up in the page.
    if not token or not TOKEN_PATTERN.fullmatch(token):
        token = getattr(request.state, "new_csrf_token", None) or secrets.token_urlsafe(32)
        request.state.new_csrf_token = token
    return {"csrf_token": token}
