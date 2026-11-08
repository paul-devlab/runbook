"""Runbook pages: list, view, create, edit and delete.

URLs follow a common pattern for server-rendered apps:

    GET  /runbooks              list
    GET  /runbooks/new          empty form       POST /runbooks            create
    GET  /runbooks/{id}         view
    GET  /runbooks/{id}/edit    filled-in form   POST /runbooks/{id}/edit  save
    GET  /runbooks/{id}/delete  "are you sure?"  POST /runbooks/{id}/delete

Every POST answers with a redirect (303) to a page to GET, so pressing Refresh
never submits a form twice ("Post/Redirect/Get"). Forms work without
JavaScript; HTMX can make them smoother later.

Any logged-in user can do everything (roles come in Phase 7).
"""

from dataclasses import dataclass, field
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import require_user
from app.csrf import verify_csrf
from app.db import get_db
from app.models import Runbook, Tag, User
from app.revisions import delete_revisions, save_revision, snapshot_runbook
from app.search import matches, tidy_query
from app.tags import get_or_create_tags, normalise_tag, parse_tags
from app.web import NotFound, render

router = APIRouter(prefix="/runbooks")

TITLE_MAX = 200
SUMMARY_MAX = 500
BODY_MAX = 100_000  # ~100 KB of Markdown is a very long runbook

# FastAPI rejects anything longer with a 422 before our code runs. The forms
# also set maxlength, so browsers stop people typing more in the first place.
TitleField = Annotated[str, Form(max_length=TITLE_MAX)]
SummaryField = Annotated[str, Form(max_length=SUMMARY_MAX)]
BodyField = Annotated[str, Form(max_length=BODY_MAX)]
TagsField = Annotated[str, Form(max_length=1000)]  # the comma-separated text

CurrentUser = Annotated[User, Depends(require_user)]
Db = Annotated[Session, Depends(get_db)]


@dataclass
class RunbookForm:
    """What was typed into the form, tidied up, plus any problems with it."""

    title: str
    summary: str
    body_md: str
    tags_text: str = ""  # as typed, so it can be shown again after an error
    tag_names: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @classmethod
    def from_input(
        cls, title: str, summary: str = "", body_md: str = "", tags: str = ""
    ) -> "RunbookForm":
        tag_names, tag_errors = parse_tags(tags)
        form = cls(
            title=title.strip(),
            summary=summary.strip(),
            # Browsers send line breaks as \r\n; store plain \n.
            body_md=body_md.replace("\r\n", "\n").strip(),
            tags_text=tags,
            tag_names=tag_names,
        )
        if not form.title:
            form.errors.append("Give the runbook a title.")
        form.errors += tag_errors
        return form

    @classmethod
    def from_runbook(cls, runbook: Runbook) -> "RunbookForm":
        return cls(
            title=runbook.title,
            summary=runbook.summary,
            body_md=runbook.body_md,
            tags_text=", ".join(tag.name for tag in runbook.tags),
        )


def get_runbook(db: Session, runbook_id: int) -> Runbook:
    runbook = db.get(Runbook, runbook_id)
    if runbook is None:
        raise NotFound
    return runbook


def form_page(
    request: Request,
    user: User,
    form: RunbookForm,
    *,
    runbook: Runbook | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    """The create/edit form. `runbook` is None when creating."""
    return render(
        request,
        "runbooks/form.html",
        status_code=status_code,
        page_title=f"Edit {runbook.title}" if runbook else "New runbook",
        user=user,
        form=form,
        runbook=runbook,
    )


def see_other(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=status.HTTP_303_SEE_OTHER)


@router.get("", response_class=HTMLResponse)
def list_runbooks(
    request: Request,
    user: CurrentUser,
    db: Db,
    tag: str | None = None,
    q: str | None = None,
) -> HTMLResponse:
    """All runbooks, filtered by ?tag=name and/or searched with ?q=words.

    Filters are plain URL parameters, so a filtered list can be bookmarked.
    """
    query = select(Runbook)
    tag = normalise_tag(tag)[:100] if tag else None
    if tag:
        # .any() = "has at least one tag with this name" (an EXISTS subquery).
        query = query.where(Runbook.tags.any(Tag.name == tag))
    q = tidy_query(q)
    if q:
        condition, rank = matches(Runbook.search_vector, q)
        query = query.where(condition).order_by(rank.desc())  # best match first
    query = query.order_by(Runbook.updated_at.desc())
    return render(
        request,
        "runbooks/list.html",
        page_title="Runbooks",
        user=user,
        runbooks=db.scalars(query).all(),
        tag=tag,
        q=q,
        filtered=bool(tag or q),
    )


@router.get("/new", response_class=HTMLResponse)
def new_runbook(request: Request, user: CurrentUser) -> HTMLResponse:
    return form_page(request, user, RunbookForm(title="", summary="", body_md=""))


@router.post("", dependencies=[Depends(verify_csrf)])
def create_runbook(
    request: Request,
    user: CurrentUser,
    db: Db,
    title: TitleField,
    summary: SummaryField = "",
    body_md: BodyField = "",
    tags: TagsField = "",
) -> Response:
    form = RunbookForm.from_input(title, summary, body_md, tags)
    if form.errors:
        return form_page(request, user, form, status_code=status.HTTP_400_BAD_REQUEST)
    runbook = Runbook(
        title=form.title,
        summary=form.summary,
        body_md=form.body_md,
        created_by_id=user.id,
        updated_by_id=user.id,
        tags=get_or_create_tags(db, form.tag_names),
    )
    db.add(runbook)
    db.commit()
    return see_other(f"/runbooks/{runbook.id}")


def view_page(
    request: Request,
    user: User,
    runbook: Runbook,
    *,
    problem: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    """The runbook's page. `problem` is why an attachment was refused, if one was."""
    return render(
        request,
        "runbooks/view.html",
        status_code=status_code,
        page_title=runbook.title,
        user=user,
        runbook=runbook,
        attachment_problem=problem,
    )


@router.get("/{runbook_id}", response_class=HTMLResponse)
def view_runbook(request: Request, runbook_id: int, user: CurrentUser, db: Db) -> HTMLResponse:
    return view_page(request, user, get_runbook(db, runbook_id))


@router.get("/{runbook_id}/edit", response_class=HTMLResponse)
def edit_runbook(request: Request, runbook_id: int, user: CurrentUser, db: Db) -> HTMLResponse:
    runbook = get_runbook(db, runbook_id)
    return form_page(request, user, RunbookForm.from_runbook(runbook), runbook=runbook)


@router.post("/{runbook_id}/edit", dependencies=[Depends(verify_csrf)])
def update_runbook(
    request: Request,
    runbook_id: int,
    user: CurrentUser,
    db: Db,
    title: TitleField,
    summary: SummaryField = "",
    body_md: BodyField = "",
    tags: TagsField = "",
) -> Response:
    runbook = get_runbook(db, runbook_id)
    before = snapshot_runbook(runbook)  # for the history (app/revisions.py)
    form = RunbookForm.from_input(title, summary, body_md, tags)
    if form.errors:
        return form_page(
            request, user, form, runbook=runbook, status_code=status.HTTP_400_BAD_REQUEST
        )
    runbook.title = form.title
    runbook.summary = form.summary
    runbook.body_md = form.body_md
    runbook.tags = get_or_create_tags(db, form.tag_names)
    runbook.updated_by_id = user.id
    save_revision(db, "runbook", runbook.id, before, snapshot_runbook(runbook), user)
    db.commit()
    return see_other(f"/runbooks/{runbook.id}")


@router.get("/{runbook_id}/delete", response_class=HTMLResponse)
def confirm_delete(request: Request, runbook_id: int, user: CurrentUser, db: Db) -> HTMLResponse:
    runbook = get_runbook(db, runbook_id)
    return render(
        request,
        "runbooks/delete.html",
        page_title=f"Delete {runbook.title}",
        user=user,
        runbook=runbook,
    )


@router.post("/{runbook_id}/delete", dependencies=[Depends(verify_csrf)])
def delete_runbook(runbook_id: int, user: CurrentUser, db: Db) -> Response:
    db.delete(get_runbook(db, runbook_id))
    delete_revisions(db, "runbook", runbook_id)
    db.commit()
    return see_other("/runbooks")
