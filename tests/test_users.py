"""Tests for user accounts and the `create-user` command."""

import uuid
from collections.abc import Iterator

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app import cli
from app.db import get_engine
from app.models import Base, User
from app.security import verify_password
from app.users import UserError, create_user, normalise_email

PASSWORD = "correct horse battery staple"  # noqa: S105  (fake test value)


# --- Unit tests (no database) -------------------------------------------------


def test_normalise_email_trims_and_lowercases() -> None:
    assert normalise_email("  Paul@Example.COM ") == "paul@example.com"


@pytest.mark.parametrize("bad", ["", "paul", "paul@", "@example.com", "a@b@c.com", "a b@c.com"])
def test_normalise_email_rejects_non_addresses(bad: str) -> None:
    with pytest.raises(UserError):
        normalise_email(bad)


def fake_prompts(monkeypatch: pytest.MonkeyPatch, *answers: str) -> None:
    """Make getpass return these answers in order, instead of reading the keyboard."""
    replies = iter(answers)
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": next(replies))


def test_cli_rejects_mismatched_passwords(monkeypatch, capsys) -> None:
    fake_prompts(monkeypatch, PASSWORD, PASSWORD + "typo")

    assert cli.main(["create-user", "--email", "a@example.com", "--name", "A"]) == 1
    assert "don't match" in capsys.readouterr().err


def test_cli_rejects_short_password(monkeypatch, capsys) -> None:
    fake_prompts(monkeypatch, "short")

    assert cli.main(["create-user", "--email", "a@example.com", "--name", "A"]) == 1
    assert "at least" in capsys.readouterr().err


def test_cli_requires_email_and_name() -> None:
    with pytest.raises(SystemExit):  # argparse exits with a usage message
        cli.main(["create-user", "--email", "a@example.com"])


# --- Integration tests (real Postgres, migrated with `alembic upgrade head`) ---


@pytest.fixture
def email() -> Iterator[str]:
    """A unique email for one test; any user with it is deleted afterwards."""
    address = f"test-{uuid.uuid4().hex[:12]}@example.com"
    yield address
    with Session(get_engine()) as session:
        session.execute(delete(User).where(User.email == address))
        session.commit()


@pytest.mark.integration
def test_migrations_match_the_models() -> None:
    """The database (after `alembic upgrade head`) has exactly what models.py describes.

    Catches a model change that someone forgot to write a migration for.
    """
    with get_engine().connect() as connection:
        differences = compare_metadata(MigrationContext.configure(connection), Base.metadata)
    assert differences == []


@pytest.mark.integration
def test_cli_creates_user_with_hashed_password(monkeypatch, capsys, email) -> None:
    fake_prompts(monkeypatch, PASSWORD, PASSWORD)

    assert cli.main(["create-user", "--email", email.upper(), "--name", " Test User "]) == 0
    assert "Created user" in capsys.readouterr().out

    with Session(get_engine()) as session:
        user = session.scalars(select(User).where(User.email == email)).one()
    assert user.display_name == "Test User"
    assert user.is_active is True
    assert user.created_at is not None
    assert user.password_hash != PASSWORD
    assert verify_password(user.password_hash, PASSWORD)


@pytest.mark.integration
def test_duplicate_email_is_refused_in_any_case(email) -> None:
    with Session(get_engine()) as session:
        create_user(session, email=email, display_name="First", password=PASSWORD)
        session.commit()

    with Session(get_engine()) as session, pytest.raises(UserError, match="already exists"):
        create_user(session, email=email.upper(), display_name="Second", password=PASSWORD)
