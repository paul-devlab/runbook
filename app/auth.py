"""Logging in and out, and knowing who's logged in.

How a login works:
1. The user posts their email and password to /login.
2. We check the password (argon2, see app/security.py).
3. We make a long random *token*, store a SHA-256 hash of it in the `sessions`
   table, and give the browser the token in a cookie.
4. On each request, we hash the cookie's token and look it up. If there's an
   unexpired session for an active user, that's who's logged in.

The token is random and 256 bits long, so a fast hash (SHA-256) is enough to
protect it; a slow hash like argon2 is only needed for human-chosen passwords.
"""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import User, UserSession
from app.security import hash_password, needs_rehash, verify_password

SESSION_COOKIE = "runbook_session"

# A real argon2 hash of a random password, used when the email doesn't exist,
# so a login attempt for an unknown email takes as long as one for a real
# account. Otherwise the response time would reveal which emails have accounts.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def authenticate(db: Session, email: str, password: str) -> User | None:
    """Return the active user with this email and password, or None.

    Callers must show the same message for every failure, so it doesn't reveal
    whether the email exists, the password was wrong or the account is disabled.
    """
    user = db.scalars(select(User).where(User.email == email.strip().lower())).one_or_none()
    if user is None:
        verify_password(_DUMMY_HASH, password)  # same work as a real check (see above)
        return None
    if not verify_password(user.password_hash, password) or not user.is_active:
        return None
    if needs_rehash(user.password_hash):
        # Our hashing settings have been strengthened since this hash was made.
        user.password_hash = hash_password(password)
    return user


def start_session(db: Session, user: User) -> str:
    """Create a session for `user` and return the token for the cookie. Commits."""
    now = datetime.now(UTC)
    # Housekeeping: remove expired sessions, so the table doesn't grow forever.
    db.execute(delete(UserSession).where(UserSession.expires_at < now))
    token = secrets.token_urlsafe(32)  # 32 random bytes = 256 bits
    db.add(
        UserSession(
            user_id=user.id,
            token_hash=_hash_token(token),
            expires_at=now + timedelta(hours=get_settings().session_ttl_hours),
        )
    )
    db.commit()
    return token


def end_session(db: Session, token: str | None) -> None:
    """Delete the session for this token, if any (logout). Commits."""
    if token:
        db.execute(delete(UserSession).where(UserSession.token_hash == _hash_token(token)))
        db.commit()


def user_for_token(db: Session, token: str | None) -> User | None:
    """Return the logged-in user for a cookie token, or None if it's missing,
    unknown, expired or belongs to a deactivated user."""
    if not token:
        return None
    return db.scalars(
        select(User)
        .join(UserSession, UserSession.user_id == User.id)
        .where(
            UserSession.token_hash == _hash_token(token),
            UserSession.expires_at > datetime.now(UTC),
            User.is_active,
        )
    ).one_or_none()


# --- FastAPI dependencies -------------------------------------------------------


class LoginRequired(Exception):
    """Raised by `require_user`; app/main.py turns it into a redirect to /login."""


def current_user(request: Request, db: Annotated[Session, Depends(get_db)]) -> User | None:
    """The logged-in user, or None. For pages that work either way."""
    return user_for_token(db, request.cookies.get(SESSION_COOKIE))


def require_user(user: Annotated[User | None, Depends(current_user)]) -> User:
    """The logged-in user. For pages that need a login: others are sent to /login."""
    if user is None:
        raise LoginRequired
    return user


def cookie_settings() -> dict:
    """How the session cookie is set (also used to delete it, with the same settings)."""
    settings = get_settings()
    return {
        "key": SESSION_COOKIE,
        "httponly": True,  # JavaScript can't read it, so injected scripts can't steal it
        "samesite": "lax",  # not sent with form posts from other websites
        "secure": settings.session_cookie_secure,  # HTTPS only (see app/config.py)
        "path": "/",
    }
