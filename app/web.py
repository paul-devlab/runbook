"""HTML pages, rendered on the server with Jinja2 templates.

Templates live in app/templates/ and static files (CSS, JS) in app/static/.
Every page extends templates/base.html, which holds the shared layout.

Security: Jinja2 *autoescaping* is on for .html templates, so a value like
"<script>" in a record is shown as text, not run as code. Never mark user
content `|safe`; Markdown gets its own sanitiser later.
"""

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

APP_DIR = Path(__file__).parent
STATIC_DIR = APP_DIR / "static"

templates = Jinja2Templates(directory=APP_DIR / "templates")

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def home(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "home.html", {"page_title": "Home"})
