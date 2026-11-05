"""Incident pages: list, view, create, edit and delete.

Same URL pattern and Post/Redirect/Get flow as app/runbooks.py.

Times: the form uses the browser's date-time picker (<input type="datetime-local">),
which sends "2026-11-05T14:30" with no time zone. For now everything is UTC:
entered as UTC, stored as UTC, shown as UTC. Showing local times can come later.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import require_user
from app.csrf import verify_csrf
from app.db import get_db
from app.models import SEVERITIES, STATUSES, Incident, Runbook, Tag, User
from app.runbooks import see_other
from app.search import matches, tidy_query
from app.tags import get_or_create_tags, normalise_tag, parse_tags
from app.web import NotFound, render, templates

router = APIRouter(prefix="/incidents")

TITLE_MAX = 200
SUMMARY_MAX = 500
TEXT_MAX = 100_000
PICKER_FORMAT = "%Y-%m-%dT%H:%M"  # what <input type="datetime-local"> uses

TitleField = Annotated[str, Form(max_length=TITLE_MAX)]
SummaryField = Annotated[str, Form(max_length=SUMMARY_MAX)]
ChoiceField = Annotated[str, Form(max_length=20)]
TimeField = Annotated[str, Form(max_length=40)]
TextField = Annotated[str, Form(max_length=TEXT_MAX)]
TagsField = Annotated[str, Form(max_length=1000)]
# Ticked checkboxes arrive as repeated fields: runbook_ids=3&runbook_ids=7.
RunbookIdsField = Annotated[list[int], Form()]
MAX_LINKED_RUNBOOKS = 20

CurrentUser = Annotated[User, Depends(require_user)]
Db = Annotated[Session, Depends(get_db)]


def now_utc() -> datetime:
    """The current time in UTC, to the minute (the picker has no seconds)."""
    return datetime.now(UTC).replace(second=0, microsecond=0)


def parse_time(text: str) -> datetime | None:
    """'2026-11-05T14:30' -> that time in UTC. None if blank or not a valid time."""
    try:
        return datetime.fromisoformat(text.strip()).replace(tzinfo=UTC) if text.strip() else None
    except ValueError:
        return None


def format_duration(delta: timedelta | None) -> str:
    """timedelta(hours=2, minutes=15) -> '2h 15m'. Shown as "time to resolve"."""
    if delta is None:
        return ""
    minutes = int(delta.total_seconds() // 60)
    days, minutes = divmod(minutes, 24 * 60)
    hours, minutes = divmod(minutes, 60)
    parts = [f"{days}d"] if days else []
    if hours:
        parts.append(f"{hours}h")
    if minutes or not parts:
        parts.append(f"{minutes}m")
    return " ".join(parts)


templates.env.filters["duration"] = format_duration


@dataclass
class IncidentForm:
    """What was typed into the form (as text, so it can be shown again after an
    error), the tidied values, and any problems."""

    title: str = ""
    summary: str = ""
    severity: str = "SEV3"
    status: str = "open"
    started_at_text: str = ""
    resolved_at_text: str = ""
    root_cause_md: str = ""
    resolution_md: str = ""
    tags_text: str = ""
    started_at: datetime | None = None
    resolved_at: datetime | None = None
    tag_names: list[str] = field(default_factory=list)
    runbook_ids: list[int] = field(default_factory=list)  # linked runbooks
    errors: list[str] = field(default_factory=list)

    @classmethod
    def blank(cls) -> "IncidentForm":
        """A new incident: SEV3, open, started now."""
        return cls(started_at_text=now_utc().strftime(PICKER_FORMAT))

    @classmethod
    def from_incident(cls, incident: Incident) -> "IncidentForm":
        return cls(
            title=incident.title,
            summary=incident.summary,
            severity=incident.severity,
            status=incident.status,
            started_at_text=incident.started_at.astimezone(UTC).strftime(PICKER_FORMAT),
            resolved_at_text=(
                incident.resolved_at.astimezone(UTC).strftime(PICKER_FORMAT)
                if incident.resolved_at
                else ""
            ),
            root_cause_md=incident.root_cause_md,
            resolution_md=incident.resolution_md,
            tags_text=", ".join(tag.name for tag in incident.tags),
            runbook_ids=[runbook.id for runbook in incident.runbooks],
        )

    @classmethod
    def from_input(
        cls,
        *,
        title: str,
        severity: str,
        status: str,
        started_at: str,
        summary: str = "",
        resolved_at: str = "",
        root_cause_md: str = "",
        resolution_md: str = "",
        tags: str = "",
        runbook_ids: list[int] | None = None,
    ) -> "IncidentForm":
        tag_names, tag_errors = parse_tags(tags)
        runbook_ids = list(dict.fromkeys(runbook_ids or []))  # drop repeats, keep order
        form = cls(
            title=title.strip(),
            summary=summary.strip(),
            severity=severity,
            status=status,
            started_at_text=started_at,
            resolved_at_text=resolved_at,
            root_cause_md=root_cause_md.replace("\r\n", "\n").strip(),
            resolution_md=resolution_md.replace("\r\n", "\n").strip(),
            tags_text=tags,
            tag_names=tag_names,
            started_at=parse_time(started_at),
            resolved_at=parse_time(resolved_at),
            runbook_ids=runbook_ids,
        )
        errors = form.errors
        if not form.title:
            errors.append("Give the incident a title.")
        if severity not in SEVERITIES:
            errors.append("Choose a severity.")
        if status not in STATUSES:
            errors.append("Choose a status.")
        if form.started_at is None:
            errors.append("Enter when the incident started.")
        if resolved_at.strip() and form.resolved_at is None:
            errors.append("The resolved time isn't a valid date and time.")
        elif status == "open" and form.resolved_at is not None:
            errors.append(
                "An open incident can't have a resolved time: "
                "set the status to resolved, or clear the time."
            )
        elif status == "resolved" and form.resolved_at is None:
            # Marking it resolved without a time means "resolved just now".
            form.resolved_at = now_utc()
            form.resolved_at_text = form.resolved_at.strftime(PICKER_FORMAT)
        if form.started_at and form.resolved_at and form.resolved_at < form.started_at:
            errors.append("The resolved time can't be before the start time.")
        errors += tag_errors
        if len(runbook_ids) > MAX_LINKED_RUNBOOKS:
            errors.append(f"Link at most {MAX_LINKED_RUNBOOKS} runbooks.")
        return form


def get_incident(db: Session, incident_id: int) -> Incident:
    incident = db.get(Incident, incident_id)
    if incident is None:
        raise NotFound
    return incident


def linked_runbooks(db: Session, form: IncidentForm) -> list[Runbook]:
    """The runbooks ticked on the form. Adds an error if any no longer exist
    (someone deleted one while this form was open)."""
    if not form.runbook_ids:
        return []
    runbooks = db.scalars(select(Runbook).where(Runbook.id.in_(form.runbook_ids))).all()
    if len(runbooks) != len(form.runbook_ids):
        form.errors.append("A runbook you ticked has been deleted. Check the list and save again.")
        found = {r.id for r in runbooks}
        form.runbook_ids = [i for i in form.runbook_ids if i in found]
    return list(runbooks)


def form_page(
    request: Request,
    db: Session,
    user: User,
    form: IncidentForm,
    *,
    incident: Incident | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    """The create/edit form. `incident` is None when creating."""
    # Every runbook, for the "Runbooks used" checkboxes (id and title only).
    all_runbooks = db.execute(select(Runbook.id, Runbook.title).order_by(Runbook.title)).all()
    return render(
        request,
        "incidents/form.html",
        status_code=status_code,
        page_title=f"Edit {incident.title}" if incident else "New incident",
        user=user,
        form=form,
        incident=incident,
        severities=SEVERITIES,
        statuses=STATUSES,
        all_runbooks=all_runbooks,
    )


def apply_form(
    db: Session, incident: Incident, form: IncidentForm, runbooks: list[Runbook], user: User
) -> None:
    """Copy the form's values onto an incident (new or existing)."""
    incident.title = form.title
    incident.summary = form.summary
    incident.severity = form.severity
    incident.status = form.status
    incident.started_at = form.started_at
    incident.resolved_at = form.resolved_at
    incident.root_cause_md = form.root_cause_md
    incident.resolution_md = form.resolution_md
    incident.tags = get_or_create_tags(db, form.tag_names)
    incident.runbooks = runbooks
    incident.updated_by_id = user.id


@router.get("", response_class=HTMLResponse)
def list_incidents(
    request: Request,
    user: CurrentUser,
    db: Db,
    tag: str | None = None,
    q: str | None = None,
    severity: str | None = None,
    status: str | None = None,
) -> HTMLResponse:
    """All incidents, most recent first. Filters combine (AND): ?severity=SEV1,
    ?status=open, ?tag=name, ?q=words. Unknown severity/status values are
    ignored, like "Any"."""
    query = select(Incident)
    tag = normalise_tag(tag)[:100] if tag else None
    if tag:
        query = query.where(Incident.tags.any(Tag.name == tag))
    severity = severity if severity in SEVERITIES else None
    if severity:
        query = query.where(Incident.severity == severity)
    status = status if status in STATUSES else None
    if status:
        query = query.where(Incident.status == status)
    q = tidy_query(q)
    if q:
        condition, rank = matches(Incident.search_vector, q)
        query = query.where(condition).order_by(rank.desc())  # best match first
    query = query.order_by(Incident.started_at.desc(), Incident.id.desc())
    return render(
        request,
        "incidents/list.html",
        page_title="Incidents",
        user=user,
        incidents=db.scalars(query).all(),
        tag=tag,
        q=q,
        severity=severity,
        status=status,
        severities=SEVERITIES,
        statuses=STATUSES,
        filtered=bool(tag or q or severity or status),
    )


@router.get("/new", response_class=HTMLResponse)
def new_incident(request: Request, user: CurrentUser, db: Db) -> HTMLResponse:
    return form_page(request, db, user, IncidentForm.blank())


@router.post("", dependencies=[Depends(verify_csrf)])
def create_incident(
    request: Request,
    user: CurrentUser,
    db: Db,
    title: TitleField,
    severity: ChoiceField,
    status: ChoiceField,
    started_at: TimeField,
    summary: SummaryField = "",
    resolved_at: TimeField = "",
    root_cause_md: TextField = "",
    resolution_md: TextField = "",
    tags: TagsField = "",
    runbook_ids: RunbookIdsField = [],  # noqa: B006  (FastAPI copies defaults per request)
) -> Response:
    form = IncidentForm.from_input(
        title=title,
        summary=summary,
        severity=severity,
        status=status,
        started_at=started_at,
        resolved_at=resolved_at,
        root_cause_md=root_cause_md,
        resolution_md=resolution_md,
        tags=tags,
        runbook_ids=runbook_ids,
    )
    runbooks = linked_runbooks(db, form)
    if form.errors:  # 400 Bad Request (a plain number: `status` is a form field here)
        return form_page(request, db, user, form, status_code=400)
    incident = Incident(created_by_id=user.id)
    apply_form(db, incident, form, runbooks, user)
    db.add(incident)
    db.commit()
    return see_other(f"/incidents/{incident.id}")


@router.get("/{incident_id}", response_class=HTMLResponse)
def view_incident(request: Request, incident_id: int, user: CurrentUser, db: Db) -> HTMLResponse:
    incident = get_incident(db, incident_id)
    return render(
        request, "incidents/view.html", page_title=incident.title, user=user, incident=incident
    )


@router.get("/{incident_id}/edit", response_class=HTMLResponse)
def edit_incident(request: Request, incident_id: int, user: CurrentUser, db: Db) -> HTMLResponse:
    incident = get_incident(db, incident_id)
    return form_page(request, db, user, IncidentForm.from_incident(incident), incident=incident)


@router.post("/{incident_id}/edit", dependencies=[Depends(verify_csrf)])
def update_incident(
    request: Request,
    incident_id: int,
    user: CurrentUser,
    db: Db,
    title: TitleField,
    severity: ChoiceField,
    status: ChoiceField,
    started_at: TimeField,
    summary: SummaryField = "",
    resolved_at: TimeField = "",
    root_cause_md: TextField = "",
    resolution_md: TextField = "",
    tags: TagsField = "",
    runbook_ids: RunbookIdsField = [],  # noqa: B006  (FastAPI copies defaults per request)
) -> Response:
    incident = get_incident(db, incident_id)
    form = IncidentForm.from_input(
        title=title,
        summary=summary,
        severity=severity,
        status=status,
        started_at=started_at,
        resolved_at=resolved_at,
        root_cause_md=root_cause_md,
        resolution_md=resolution_md,
        tags=tags,
        runbook_ids=runbook_ids,
    )
    runbooks = linked_runbooks(db, form)
    if form.errors:
        return form_page(request, db, user, form, incident=incident, status_code=400)
    apply_form(db, incident, form, runbooks, user)
    db.commit()
    return see_other(f"/incidents/{incident.id}")


@router.get("/{incident_id}/delete", response_class=HTMLResponse)
def confirm_delete(request: Request, incident_id: int, user: CurrentUser, db: Db) -> HTMLResponse:
    incident = get_incident(db, incident_id)
    return render(
        request,
        "incidents/delete.html",
        page_title=f"Delete {incident.title}",
        user=user,
        incident=incident,
    )


@router.post("/{incident_id}/delete", dependencies=[Depends(verify_csrf)])
def delete_incident(incident_id: int, user: CurrentUser, db: Db) -> Response:
    db.delete(get_incident(db, incident_id))
    db.commit()
    return see_other("/incidents")
