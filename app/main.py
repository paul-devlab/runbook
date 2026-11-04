"""Runbook web application entry point.

`app` is the FastAPI application object. Uvicorn (the web server) imports it
from here: `uvicorn app.main:app`.
"""

from typing import Annotated

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse

from app.db import database_is_reachable

app = FastAPI(title="Runbook", version="0.1.0")


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
