"""Working with user accounts (kept separate from the CLI so it can be reused and tested)."""

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import User
from app.security import hash_password


class UserError(ValueError):
    """A problem with the user details given (shown to whoever entered them)."""


def normalise_email(email: str) -> str:
    """Trim and lowercase an email address, and check it looks like one."""
    email = email.strip().lower()
    local, _, domain = email.partition("@")
    # Deliberately simple: the real test of an address is whether mail arrives.
    if not local or "." not in domain or " " in email or email.count("@") != 1:
        raise UserError(f"{email!r} doesn't look like an email address.")
    return email


def create_user(session: Session, *, email: str, display_name: str, password: str) -> User:
    """Add a new active user and return it. The caller commits the session.

    Raises UserError if the details are invalid or the email is already taken.
    """
    email = normalise_email(email)
    display_name = display_name.strip()
    if not display_name:
        raise UserError("Display name can't be empty.")
    if session.scalar(select(User.id).where(User.email == email)) is not None:
        raise UserError(f"A user with email {email!r} already exists.")

    user = User(email=email, display_name=display_name, password_hash=hash_password(password))
    session.add(user)
    try:
        session.flush()  # send the INSERT now so the database assigns user.id
    except IntegrityError as error:
        # Someone else created the same email between our check and the INSERT;
        # the unique index caught it.
        session.rollback()
        raise UserError(f"A user with email {email!r} already exists.") from error
    return user
