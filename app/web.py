"""HTML pages, rendered on the server with Jinja2 templates.

Templates live in app/templates/ and static files (CSS, JS) in app/static/.
Every page extends templates/base.html, which holds the shared layout.

Security: Jinja2 *autoescaping* is on for .html templates, so a value like
"<script>" in a record is shown as text, not run as code. Never mark user
content `|safe`; Markdown gets its own sanitiser later.
"""

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.auth import (
    SESSION_COOKIE,
    authenticate,
    clear_failures,
    cookie_settings,
    current_user,
    end_session,
    record_failure,
    require_user,
    start_session,
    too_many_failures,
)
from app.config import get_settings
from app.csrf import csrf_context, verify_csrf
from app.db import get_db
from app.models import User

APP_DIR = Path(__file__).parent
STATIC_DIR = APP_DIR / "static"

# context_processors add values to every template: here {{ csrf_token }}.
templates = Jinja2Templates(directory=APP_DIR / "templates", context_processors=[csrf_context])

router = APIRouter()

# The same message for every failed login, so it never reveals whether an
# account exists for that email.
LOGIN_FAILED = "Email or password is incorrect."
TOO_MANY_ATTEMPTS = "Too many failed login attempts. Wait 15 minutes and try again."


def render(request: Request, template: str, status_code: int = 200, **context) -> HTMLResponse:
    return templates.TemplateResponse(request, template, context, status_code=status_code)


@router.get("/", response_class=HTMLResponse)
def home(request: Request, user: Annotated[User, Depends(require_user)]) -> HTMLResponse:
    return render(request, "home.html", page_title="Home", user=user)


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, user: Annotated[User | None, Depends(current_user)]) -> Response:
    if user is not None:  # already logged in
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    return render(request, "login.html", page_title="Log in")


def client_ip(request: Request) -> str:
    """The browser's IP address. Behind a proxy (Traefik on staging), Uvicorn
    takes it from the X-Forwarded-For header, but only from proxies listed in
    FORWARDED_ALLOW_IPS, so a client can't fake it."""
    return request.client.host if request.client else "unknown"


@router.post("/login", response_class=HTMLResponse, dependencies=[Depends(verify_csrf)])
def login(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    email: Annotated[str, Form(max_length=320)],
    password: Annotated[str, Form(max_length=256)],
) -> Response:
    ip = client_ip(request)
    if too_many_failures(db, email, ip):
        # 429 Too Many Requests. Checked before the password, on purpose.
        return render(
            request,
            "login.html",
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            page_title="Log in",
            error=TOO_MANY_ATTEMPTS,
            email=email,
        )
    user = authenticate(db, email, password)
    if user is None:
        record_failure(db, email, ip)
        # 400 so it's clear in logs that the form was rejected. The email is
        # shown again (escaped by Jinja2) so it needn't be retyped.
        return render(
            request,
            "login.html",
            status_code=status.HTTP_400_BAD_REQUEST,
            page_title="Log in",
            error=LOGIN_FAILED,
            email=email,
        )
    clear_failures(db, email)
    token = start_session(db, user)  # commits, including clear_failures
    # 303 See Other: after a POST, tell the browser to GET the next page, so
    # pressing Refresh doesn't resubmit the login form.
    response = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        value=token,
        max_age=get_settings().session_ttl_hours * 3600,
        **cookie_settings(),
    )
    return response


@router.post("/logout", dependencies=[Depends(verify_csrf)])
def logout(request: Request, db: Annotated[Session, Depends(get_db)]) -> Response:
    """POST only: a GET logout link could be triggered by any web page (e.g. an <img>)."""
    end_session(db, request.cookies.get(SESSION_COOKIE))
    response = RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(**cookie_settings())
    return response
