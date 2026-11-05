"""Database table definitions (SQLAlchemy ORM models).

Each class is one table; each `Mapped[...]` attribute is one column. Alembic
reads `Base.metadata` to compare these models with the real database when
generating migrations (`alembic revision --autogenerate`).
"""

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, MetaData, String, Text, func, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# Give constraints and indexes predictable names (e.g. "pk_users",
# "fk_sessions_user_id_users"). Without this, Postgres invents names, which
# makes later migrations that change or drop them fragile.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class User(Base):
    """Someone who can log in. Accounts are created by an admin (`app.cli create-user`)."""

    __tablename__ = "users"
    __table_args__ = (
        # Emails are stored lowercase, so "Paul@x" and "paul@x" can't become two
        # accounts. The app lowercases them; this makes the database enforce it.
        CheckConstraint("email = lower(email)", name="email_lowercase"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True)  # 320 = max email length
    display_name: Mapped[str] = mapped_column(String(200))
    # argon2 hash (see app/security.py), never the password itself.
    password_hash: Mapped[str] = mapped_column(String(255))
    # Deactivate instead of deleting, so records they wrote keep their author.
    is_active: Mapped[bool] = mapped_column(server_default=text("true"))
    # Set by the database when the row is inserted (timezone-aware, stored in UTC).
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class UserSession(Base):
    """A login: the browser holds a random token in a cookie; we store only its hash.

    Storing the hash (not the token) means a leaked database dump can't be used
    to log in as anyone. Named UserSession to avoid confusion with SQLAlchemy's
    database Session.
    """

    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Deleting a user deletes their sessions too (ON DELETE CASCADE).
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    # SHA-256 of the cookie token, as 64 hex characters.
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class LoginFailure(Base):
    """One failed login attempt, used to slow down password guessing.

    app/auth.py counts recent rows per email and per IP address and refuses
    further attempts once there are too many. Old rows are deleted as we go.
    """

    __tablename__ = "login_failures"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Whatever email was typed (lowercased), whether or not an account exists.
    email: Mapped[str] = mapped_column(String(320), index=True)
    # IPv4 or IPv6 address as text (an IPv6 address is at most 45 characters).
    ip_address: Mapped[str] = mapped_column(String(45), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class Runbook(Base):
    """A how-to: the steps for a routine task or for fixing a known problem."""

    __tablename__ = "runbooks"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    # One or two sentences, shown in lists.
    summary: Mapped[str] = mapped_column(String(500), server_default="")
    # The steps, written in Markdown. Text = no length limit in the database;
    # the form limits it instead (see app/runbooks.py).
    body_md: Mapped[str] = mapped_column(Text, server_default="")
    # Users are deactivated, never deleted, so these always point somewhere.
    # (ondelete defaults to RESTRICT-like "NO ACTION": deleting a user who
    # wrote runbooks fails instead of silently losing the author.)
    created_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    updated_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # onupdate: SQLAlchemy sets this to now() whenever the row is updated.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), index=True
    )

    # Lets templates write runbook.created_by.display_name. lazy="joined"
    # loads the user in the same query as the runbook.
    created_by: Mapped[User] = relationship(foreign_keys=[created_by_id], lazy="joined")
    updated_by: Mapped[User] = relationship(foreign_keys=[updated_by_id], lazy="joined")
