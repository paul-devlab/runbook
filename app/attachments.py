"""Attaching files to runbooks and incidents (Phase 5).

    POST /runbooks/{id}/attachments     upload a file to a runbook
    POST /incidents/{id}/attachments    upload a file to an incident
    GET  /attachments/{id}              download a file
    GET  /attachments/{id}/delete       "are you sure?"
    POST /attachments/{id}/delete       delete a file

An uploaded file is untrusted, so before storing one we check:
- its size (up to MAX_ATTACHMENT_BYTES, and not empty);
- its name, which is cleaned up: no folders, no invisible characters;
- its type, from the *content*, which must agree with the extension. What the
  browser claims (Content-Type) is ignored because anyone can fake it. SVG and
  HTML are never allowed: they can contain scripts.

Downloads are always served as downloads, never shown as part of the site, so
a file that slipped through these checks still can't run with the site's access.

Files are stored in Postgres (see the Attachment model) and never sent to Claude.
"""

import hashlib
import logging
import re
import unicodedata
from pathlib import PurePosixPath
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Request, UploadFile, status
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from sqlalchemy.orm import Session
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.auth import require_user
from app.csrf import verify_csrf
from app.db import get_db
from app.incidents import get_incident
from app.incidents import view_page as incident_page
from app.models import MAX_ATTACHMENT_BYTES, Attachment, Incident, Runbook, User
from app.runbooks import get_runbook
from app.runbooks import view_page as runbook_page
from app.web import NotFound, render

logger = logging.getLogger(__name__)

router = APIRouter()

CurrentUser = Annotated[User, Depends(require_user)]
Db = Annotated[Session, Depends(get_db)]
UploadedFile = Annotated[UploadFile, File()]

MAX_PER_RECORD = 20  # the Pi's SD card is small
FILENAME_MAX = 255

# Binary types: extension -> (type we store, the bytes every such file starts with).
SIGNATURES = {
    ".png": ("image/png", b"\x89PNG\r\n\x1a\n"),
    ".jpg": ("image/jpeg", b"\xff\xd8\xff"),
    ".jpeg": ("image/jpeg", b"\xff\xd8\xff"),
    ".pdf": ("application/pdf", b"%PDF-"),
}
TEXT_EXTENSIONS = {".txt", ".log", ".md", ".yaml", ".yml", ".json", ".conf"}
ALLOWED = "PNG, JPEG, PDF or text (.txt, .log, .md, .yaml, .yml, .json, .conf)"

NO_FILE = "Choose a file to attach."
EMPTY = "That file is empty."
BAD_NAME = "That file name can't be used. Rename the file and try again."
BAD_TYPE = f"That type of file can't be attached. Allowed: {ALLOWED}."
TOO_BIG = f"That file is too big. The limit is {MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB."
TOO_MANY = f"This record already has {MAX_PER_RECORD} attachments, the most it can have."

# Largest request accepted: one maximum-size file plus room for the form around it.
MAX_BODY_BYTES = MAX_ATTACHMENT_BYTES + 1024 * 1024
BODY_TOO_BIG = "That upload is too big.\n"


class BodyTooBig(Exception):
    """Raised while reading a request body that has gone over the limit."""


class LimitBodySize:
    """Refuses request bodies over MAX_BODY_BYTES, with a 413.

    The web framework reads a form, saving files to temporary storage, *before*
    any route code runs, even the login check. Without this limit, anyone could
    fill the pod's temporary space with one huge request.

    Most clients state the size up front (Content-Length), so we can refuse
    without reading anything. One that doesn't is counted as the data arrives.
    Written as plain ASGI (the interface under FastAPI), because it has to
    watch the body while it's being read.
    """

    def __init__(self, app: ASGIApp, max_bytes: int = MAX_BODY_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared = dict(scope["headers"]).get(b"content-length", b"")
        if declared.isdigit() and int(declared) > self.max_bytes:
            await PlainTextResponse(BODY_TOO_BIG, status_code=413)(scope, receive, send)
            return

        received = 0
        too_big = False
        started = False
        replaced = False

        async def counting_receive() -> Message:
            nonlocal received, too_big
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    too_big = True
                    raise BodyTooBig  # stops the form being read any further
            return message

        async def replying_send(message: Message) -> None:
            """The framework answers a failed read with a generic 400; swap it for our 413."""
            nonlocal started, replaced
            if replaced:
                return  # the framework's own reply, which we've swapped
            if too_big and not started:
                replaced = True
                await PlainTextResponse(BODY_TOO_BIG, status_code=413)(scope, receive, send)
                return
            started = True
            await send(message)

        await self.app(scope, counting_receive, replying_send)


def clean_filename(raw: str) -> str:
    """The name to store: just the last part of any path, without control or
    invisible characters (a right-to-left mark can disguise "exe.txt" as
    "txt.exe"), or leading dots. May return "" if nothing usable is left."""
    name = re.split(r"[/\\]", raw)[-1]
    name = unicodedata.normalize("NFC", name)
    name = "".join(ch for ch in name if unicodedata.category(ch) not in ("Cc", "Cf", "Cs"))
    return name.strip().lstrip(". ")


def detect_type(filename: str, data: bytes) -> str | None:
    """The content type, if the extension is allowed and the content really is
    that kind of file. Otherwise None."""
    extension = PurePosixPath(filename).suffix.lower()
    if extension in SIGNATURES:
        content_type, signature = SIGNATURES[extension]
        return content_type if data.startswith(signature) else None
    if extension in TEXT_EXTENSIONS and b"\0" not in data:
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            return None
        return "text/plain"
    return None


def add_attachment(
    db: Session, user: User, owner: Runbook | Incident, file: UploadFile
) -> tuple[str, int] | None:
    """Validate the upload and attach it to `owner`. Returns (message, HTTP
    status) if the file was refused, None if it was stored."""
    bad_request = status.HTTP_400_BAD_REQUEST
    if not file.filename:
        return NO_FILE, bad_request
    filename = clean_filename(file.filename)
    if not filename or len(filename) > FILENAME_MAX:
        return BAD_NAME, bad_request
    if len(owner.attachments) >= MAX_PER_RECORD:
        return TOO_MANY, bad_request

    data = file.file.read(MAX_ATTACHMENT_BYTES + 1)  # one extra byte reveals "too big"
    if len(data) > MAX_ATTACHMENT_BYTES:
        return TOO_BIG, status.HTTP_413_CONTENT_TOO_LARGE
    if not data:
        return EMPTY, bad_request
    content_type = detect_type(filename, data)
    if content_type is None:
        return BAD_TYPE, bad_request

    attachment = Attachment(
        filename=filename,
        content_type=content_type,
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        data=data,
        uploaded_by_id=user.id,
    )
    owner.attachments.append(attachment)
    db.commit()
    # Never the file name: it's user content, like the records themselves.
    logger.info(
        "Attachment uploaded",
        extra={"attachment_id": attachment.id, "size_bytes": len(data), "type": content_type},
    )
    return None


def see_owner(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=status.HTTP_303_SEE_OTHER)


@router.post("/runbooks/{runbook_id}/attachments", dependencies=[Depends(verify_csrf)])
def upload_to_runbook(
    request: Request, runbook_id: int, user: CurrentUser, db: Db, file: UploadedFile
) -> Response:
    runbook = get_runbook(db, runbook_id)
    refused = add_attachment(db, user, runbook, file)
    if refused:
        return runbook_page(request, user, runbook, problem=refused[0], status_code=refused[1])
    return see_owner(f"/runbooks/{runbook_id}")


@router.post("/incidents/{incident_id}/attachments", dependencies=[Depends(verify_csrf)])
def upload_to_incident(
    request: Request, incident_id: int, user: CurrentUser, db: Db, file: UploadedFile
) -> Response:
    incident = get_incident(db, incident_id)
    refused = add_attachment(db, user, incident, file)
    if refused:
        return incident_page(request, user, incident, problem=refused[0], status_code=refused[1])
    return see_owner(f"/incidents/{incident_id}")


def get_attachment(db: Session, attachment_id: int) -> Attachment:
    attachment = db.get(Attachment, attachment_id)
    if attachment is None:
        raise NotFound
    return attachment


def owner_url(attachment: Attachment) -> str:
    """The page of the runbook or incident the file belongs to."""
    if attachment.runbook_id is not None:
        return f"/runbooks/{attachment.runbook_id}"
    return f"/incidents/{attachment.incident_id}"


def content_disposition(filename: str) -> str:
    """Tells the browser to save the file, under its own name. `filename` is an
    ASCII-only fallback for old browsers; `filename*` is the exact name,
    percent-encoded so no character in it can alter the header."""
    fallback = re.sub(r"[^A-Za-z0-9._-]", "_", filename)
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename, safe='')}"


@router.get("/attachments/{attachment_id}")
def download_attachment(attachment_id: int, user: CurrentUser, db: Db) -> Response:
    attachment = get_attachment(db, attachment_id)
    return Response(
        attachment.data,
        media_type=attachment.content_type,
        headers={
            "Content-Disposition": content_disposition(attachment.filename),
            # Believe our Content-Type: no guessing that a text file is really HTML.
            "X-Content-Type-Options": "nosniff",
            # If a browser displays it anyway, nothing in it may run.
            "Content-Security-Policy": "default-src 'none'; sandbox",
            # Needs a login, so shared caches and the browser mustn't keep a copy.
            "Cache-Control": "private, no-store",
        },
    )


@router.get("/attachments/{attachment_id}/delete", response_class=HTMLResponse)
def confirm_delete(request: Request, attachment_id: int, user: CurrentUser, db: Db) -> HTMLResponse:
    attachment = get_attachment(db, attachment_id)
    return render(
        request,
        "attachment_delete.html",
        page_title=f"Delete {attachment.filename}",
        user=user,
        attachment=attachment,
        back_url=owner_url(attachment),
    )


@router.post("/attachments/{attachment_id}/delete", dependencies=[Depends(verify_csrf)])
def delete_attachment(attachment_id: int, user: CurrentUser, db: Db) -> Response:
    attachment = get_attachment(db, attachment_id)
    back_url = owner_url(attachment)
    db.delete(attachment)
    db.commit()
    logger.info("Attachment deleted", extra={"attachment_id": attachment_id})
    return see_owner(back_url)
