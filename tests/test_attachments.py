"""Tests for the attachments table (app/models.py: Attachment)."""

import hashlib
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_engine
from app.models import MAX_ATTACHMENT_BYTES, Attachment, Incident, Runbook, User
from app.users import create_user

pytestmark = pytest.mark.integration  # every test here needs Postgres

PASSWORD = "correct horse battery staple"  # noqa: S105  (fake test value)


@pytest.fixture
def db() -> Iterator[Session]:
    """A session with a fresh user, runbook and incident. Afterwards everything
    is deleted (the runbook and incident first, which removes their files)."""
    with Session(get_engine()) as session:
        user = create_user(
            session,
            email=f"test-{uuid.uuid4().hex[:12]}@example.com",
            display_name="Attachment Tester",
            password=PASSWORD,
        )
        session.flush()
        user_id = user.id
        session.add_all(
            [
                Runbook(title="Restart DNS", created_by_id=user.id, updated_by_id=user.id),
                Incident(
                    title="DNS outage",
                    severity="SEV2",
                    status="open",
                    started_at=datetime.now(UTC),
                    created_by_id=user.id,
                    updated_by_id=user.id,
                ),
            ]
        )
        session.commit()
        session.info["user_id"] = user_id
        yield session
        session.rollback()
        session.execute(delete(Runbook).where(Runbook.created_by_id == user_id))
        session.execute(delete(Incident).where(Incident.created_by_id == user_id))
        session.execute(delete(Attachment).where(Attachment.uploaded_by_id == user_id))
        session.execute(delete(User).where(User.id == user_id))
        session.commit()


def records(db: Session) -> tuple[Runbook, Incident]:
    user_id = db.info["user_id"]
    runbook = db.scalars(select(Runbook).where(Runbook.created_by_id == user_id)).one()
    incident = db.scalars(select(Incident).where(Incident.created_by_id == user_id)).one()
    return runbook, incident


def attachment(db: Session, data: bytes = b"hello\n", **overrides) -> Attachment:
    values = {
        "filename": "notes.txt",
        "content_type": "text/plain",
        "size_bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "data": data,
        "uploaded_by_id": db.info["user_id"],
    }
    return Attachment(**(values | overrides))


def test_a_file_is_stored_and_read_back(db) -> None:
    runbook_id = records(db)[0].id
    db.add(attachment(db, runbook_id=runbook_id))
    db.commit()
    db.expunge_all()  # forget loaded objects, so the next query really reads the database

    stored = db.scalars(select(Attachment).where(Attachment.runbook_id == runbook_id)).one()

    assert "data" in inspect(stored).unloaded  # not fetched until it's needed
    assert stored.data == b"hello\n"
    assert stored.uploaded_by.display_name == "Attachment Tester"


@pytest.mark.parametrize("kind", ["runbook", "incident"])
def test_deleting_a_record_deletes_its_files(db, kind) -> None:
    runbook, incident = records(db)
    owner = runbook if kind == "runbook" else incident
    db.add(attachment(db, **{f"{kind}_id": owner.id}))
    db.commit()

    db.delete(owner)
    db.commit()

    assert (
        db.scalars(select(Attachment).where(Attachment.uploaded_by_id == db.info["user_id"])).all()
        == []
    )


def test_a_file_belongs_to_exactly_one_record(db) -> None:
    runbook, incident = records(db)

    for owners in ({}, {"runbook_id": runbook.id, "incident_id": incident.id}):
        db.add(attachment(db, **owners))
        with pytest.raises(IntegrityError, match="ck_attachments_one_owner"):
            db.commit()
        db.rollback()


def test_the_stored_size_must_be_the_real_size(db) -> None:
    runbook, _ = records(db)
    db.add(attachment(db, runbook_id=runbook.id, size_bytes=1))

    with pytest.raises(IntegrityError, match="ck_attachments_size_valid"):
        db.commit()


def test_files_over_the_limit_are_refused(db) -> None:
    runbook, _ = records(db)
    db.add(attachment(db, data=b"x" * (MAX_ATTACHMENT_BYTES + 1), runbook_id=runbook.id))

    with pytest.raises(IntegrityError, match="ck_attachments_size_valid"):
        db.commit()
