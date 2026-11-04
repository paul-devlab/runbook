"""Runbook web application entry point.

`app` is the FastAPI application object. Uvicorn (the web server) imports it
from here: `uvicorn app.main:app`.
"""

from fastapi import FastAPI

app = FastAPI(title="Runbook", version="0.1.0")


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness check: answers "is the process running?".

    Deliberately does no other work (no database call), so it stays fast and
    only fails when the app itself is broken. Kubernetes will use it later as
    the liveness probe. A separate /ready endpoint (Phase 0c) will check the
    database.
    """
    return {"status": "ok"}
