"""Database connection.

The *engine* is SQLAlchemy's connection manager: it keeps a pool of open
connections to Postgres and hands them out as needed. We create one per process.
"""

import logging
from functools import lru_cache

from sqlalchemy import Engine, create_engine, text

from app.config import get_settings

logger = logging.getLogger(__name__)


@lru_cache
def get_engine() -> Engine:
    return create_engine(
        get_settings().database_url,
        # Test each pooled connection before use, so a restarted database
        # doesn't leave us holding dead connections.
        pool_pre_ping=True,
        # Give up quickly if Postgres is unreachable instead of hanging.
        connect_args={"connect_timeout": 3},
    )


def database_is_reachable() -> bool:
    """Return True if a trivial query (SELECT 1) succeeds.

    Any failure is logged for operators and reported as False. The caller
    never sees the error details (they can include hostnames or usernames).
    """
    try:
        with get_engine().connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception:
        logger.exception("Readiness check: database is not reachable")
        return False
    return True
