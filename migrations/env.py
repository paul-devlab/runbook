"""Alembic migration environment.

Alembic runs this file for every command (`alembic upgrade head`, etc.). It
connects to the database using our app settings (from environment variables),
not a URL in alembic.ini, so credentials never live in a file.
"""

from logging.config import fileConfig

from alembic import context

from app.config import get_settings
from app.db import get_engine
from app.models import Base

config = context.config

# Set up logging as described in alembic.ini.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Our models' table definitions. `alembic revision --autogenerate` compares
# these with the real database to draft a migration.
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """'Offline' mode: print the SQL instead of running it (`alembic upgrade head --sql`)."""
    context.configure(
        url=get_settings().database_url.render_as_string(hide_password=True),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """'Online' mode: connect to the database and apply migrations."""
    with get_engine().connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
