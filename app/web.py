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
    cookie_settings,
    current_user,
    end_session,
    require_user,
    start_session,
)
from app.config import get_settings
from app.db import get_db
from app.models import User

APP_DIR = Path(__file__).parent
STATIC_DIR = APP_DIR / "static"

templates = Jinja2Templates(directory=APP_DIR / "templates")

router = APIRouter()

# The same message for every failed login, so it never reveals whether an
# account exists for that email.
LOGIN_FAILED = "Email or password is incorrect."


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


@router.post("/login", response_class=HTMLResponse)
def login(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    email: Annotated[str, Form(max_length=320)],
    password: Annotated[str, Form(max_length=256)],
) -> Response:
    user = authenticate(db, email, password)
    if user is None:
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
    token = start_session(db, user)
    # 303 See Other: after a POST, tell the browser to GET the next page, so
    # pressing Refresh doesn't resubmit the login form.
    response = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        value=token,
        max_age=get_settings().session_ttl_hours * 3600,
        **cookie_settings(),
    )
    return response


@router.post("/logout")
def logout(request: Request, db: Annotated[Session, Depends(get_db)]) -> Response:
    """POST only: a GET logout link could be triggered by any web page (e.g. an <img>)."""
    end_session(db, request.cookies.get(SESSION_COOKIE))
    response = RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(**cookie_settings())
    return response
