"""Runbook web application entry point.

`app` is the FastAPI application object. Uvicorn (the web server) imports it
from here: `uvicorn app.main:app`.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Request, status
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.ask import router as ask_router
from app.attachments import LimitBodySize
from app.attachments import router as attachments_router
from app.auth import LoginRequired
from app.config import get_settings
from app.csrf import REJECTED, CsrfFailed, csrf_middleware
from app.db import database_is_reachable
from app.incidents import router as incidents_router
from app.logs import request_log_middleware, setup_logging
from app.metrics import metrics_middleware, start_metrics_server
from app.revisions import router as revisions_router
from app.runbooks import router as runbooks_router
from app.web import STATIC_DIR, NotFound, render
from app.web import router as web_router

# Log as JSON, one object per line (app/logs.py).
setup_logging()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Runs once at startup (before `yield`) and once at shutdown (after it)."""
    metrics_server = start_metrics_server(get_settings().metrics_port)
    yield
    if metrics_server:
        metrics_server.shutdown()


app = FastAPI(title="Runbook", version="0.1.0", lifespan=lifespan)

# CSS, JavaScript and images, served from app/static/ at /static/...
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# Refuse cross-site form posts and hand out CSRF tokens (app/csrf.py).
app.middleware("http")(csrf_middleware)

# Refuse oversized request bodies before they're read (app/attachments.py).
app.add_middleware(LimitBodySize)

# Count and time every request for Prometheus (app/metrics.py).
app.middleware("http")(metrics_middleware)

# Give each request an id and log one line per request (app/logs.py). Added
# last, so it runs first and also sees requests the CSRF check refuses.
app.middleware("http")(request_log_middleware)

# HTML pages (app/web.py).
app.include_router(web_router)
app.include_router(runbooks_router)
app.include_router(incidents_router)
app.include_router(revisions_router)
app.include_router(attachments_router)
app.include_router(ask_router)


@app.exception_handler(LoginRequired)
def redirect_to_login(request: Request, exc: LoginRequired) -> RedirectResponse:
    """A page needed a login and there wasn't one: send the browser to /login."""
    return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)


@app.exception_handler(CsrfFailed)
def csrf_failed(request: Request, exc: CsrfFailed) -> PlainTextResponse:
    """A form post without a matching CSRF token (see app/csrf.py)."""
    return PlainTextResponse(REJECTED, status_code=status.HTTP_403_FORBIDDEN)


@app.exception_handler(NotFound)
def not_found(request: Request, exc: Exception):
    """An id that doesn't exist (or was deleted): a friendly 404 page."""
    return render(request, "not_found.html", status_code=404, page_title="Not found")


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness check: answers "is the process running?".

    Deliberately does no other work (no database call), so it stays fast and
    only fails when the app itself is broken. Kubernetes uses this as the
    liveness probe: if it fails, the container is restarted.
    """
    return {"status": "ok"}


@app.get("/ready")
def ready(db_ok: Annotated[bool, Depends(database_is_reachable)]) -> JSONResponse:
    """Readiness check: answers "can this instance serve requests right now?".

    Returns 503 when the database is unreachable. Kubernetes uses this as the
    readiness probe: if it fails, traffic stops being sent here, but the
    container is NOT restarted (restarting wouldn't fix a database outage).

    `Depends(...)` is FastAPI's dependency injection: FastAPI calls
    database_is_reachable() and passes the result in. Tests can swap it for a
    fake, so they don't need a real database.
    """
    if db_ok:
        return JSONResponse({"status": "ready"})
    return JSONResponse({"status": "unavailable"}, status_code=503)
