"""Revision history: a snapshot of the old version on every edit, and pages to
read past versions.

    GET /runbooks/{id}/history             list of versions
    GET /runbooks/{id}/history/{rev_id}    one past version, read-only
    (the same under /incidents/...)

Comparing versions (diff) and restoring old ones are out of scope for the MVP
(docs/SPEC.md §3).
"""

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.auth import require_user
from app.db import get_db
from app.models import Incident, Revision, Runbook, User
from app.web import NotFound, render

router = APIRouter()

CurrentUser = Annotated[User, Depends(require_user)]
Db = Annotated[Session, Depends(get_db)]

# Snapshot keys that describe the version rather than its content. They're
# left out when checking "did anything actually change?".
_ABOUT_THE_VERSION = {"updated_at", "updated_by_id", "updated_by_name"}


def _common(record: Runbook | Incident) -> dict[str, Any]:
    return {
        "title": record.title,
        "summary": record.summary,
        "tags": [tag.name for tag in record.tags],
        # Who wrote this version, and when. The name is copied in, so the
        # history still reads correctly if the user's name changes later.
        "updated_at": record.updated_at.isoformat(),
        "updated_by_id": record.updated_by_id,
        "updated_by_name": record.updated_by.display_name,
    }


def snapshot_runbook(runbook: Runbook) -> dict[str, Any]:
    """The runbook as plain JSON-friendly data (dates become ISO text)."""
    return _common(runbook) | {"body_md": runbook.body_md}


def snapshot_incident(incident: Incident) -> dict[str, Any]:
    return _common(incident) | {
        "severity": incident.severity,
        "status": incident.status,
        "started_at": incident.started_at.isoformat(),
        "resolved_at": incident.resolved_at.isoformat() if incident.resolved_at else None,
        "root_cause_md": incident.root_cause_md,
        "resolution_md": incident.resolution_md,
        # Titles are copied too, in case a linked runbook is deleted later.
        "runbooks": [{"id": r.id, "title": r.title} for r in incident.runbooks],
    }


def content_changed(before: dict[str, Any], after: dict[str, Any]) -> bool:
    def content(snapshot: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in snapshot.items() if k not in _ABOUT_THE_VERSION}

    return content(before) != content(after)


def save_revision(
    db: Session, entity_type: str, entity_id: int, before: dict, after: dict, user: User
) -> None:
    """Keep `before` as a past version, unless the edit changed nothing.
    Doesn't commit: it's saved in the same transaction as the edit itself, so
    either both are saved or neither is."""
    if content_changed(before, after):
        db.add(
            Revision(
                entity_type=entity_type, entity_id=entity_id, snapshot=before, edited_by_id=user.id
            )
        )


def delete_revisions(db: Session, entity_type: str, entity_id: int) -> None:
    """Delete a record's history (when the record itself is deleted). Doesn't commit."""
    db.execute(
        delete(Revision).where(Revision.entity_type == entity_type, Revision.entity_id == entity_id)
    )


def readable(snapshot: dict[str, Any]) -> dict[str, Any]:
    """A snapshot ready for a template: ISO date text turned back into datetimes."""
    return {
        key: datetime.fromisoformat(value) if key.endswith("_at") and value else value
        for key, value in snapshot.items()
    }


# --- Pages -----------------------------------------------------------------------

# URL part -> (entity_type, model). Both record types share these routes.
_KINDS = {"runbooks": ("runbook", Runbook), "incidents": ("incident", Incident)}


def _record(db: Session, kind: str, record_id: int):
    if kind not in _KINDS:
        raise NotFound
    entity_type, model = _KINDS[kind]
    record = db.get(model, record_id)
    if record is None:
        raise NotFound
    return entity_type, record


@router.get("/{kind}/{record_id}/history", response_class=HTMLResponse)
def history(request: Request, kind: str, record_id: int, user: CurrentUser, db: Db):
    entity_type, record = _record(db, kind, record_id)
    revisions = db.scalars(
        select(Revision)
        .where(Revision.entity_type == entity_type, Revision.entity_id == record_id)
        .order_by(Revision.edited_at.desc(), Revision.id.desc())
    ).all()
    return render(
        request,
        "history.html",
        page_title=f"History of {record.title}",
        user=user,
        kind=kind,
        record=record,
        versions=[(rev, readable(rev.snapshot)) for rev in revisions],
    )


@router.get("/{kind}/{record_id}/history/{revision_id}", response_class=HTMLResponse)
def old_version(
    request: Request, kind: str, record_id: int, revision_id: int, user: CurrentUser, db: Db
):
    entity_type, record = _record(db, kind, record_id)
    revision = db.get(Revision, revision_id)
    # The revision must belong to *this* record, or the URL is wrong.
    if revision is None or (revision.entity_type, revision.entity_id) != (entity_type, record_id):
        raise NotFound
    return render(
        request,
        f"{kind}/version.html",
        page_title=f"Old version of {record.title}",
        user=user,
        record=record,
        revision=revision,
        old=readable(revision.snapshot),
    )
