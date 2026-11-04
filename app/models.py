"""Database table definitions (SQLAlchemy ORM models).

No tables yet: they arrive in Phase 3. Alembic reads `Base.metadata` to
compare these models with the real database when generating migrations.
"""

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

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
