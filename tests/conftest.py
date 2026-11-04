"""Shared pytest setup (pytest loads conftest.py automatically).

Tests marked `@pytest.mark.integration` need a real Postgres. They run when
POSTGRES_HOST is set (as it is inside Docker Compose and in CI) and are
skipped otherwise, with the reason shown in the test summary.
"""

import os

import pytest

from app.config import get_settings
from app.db import get_engine


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get("POSTGRES_HOST"):
        return
    skip = pytest.mark.skip(reason="needs Postgres: set POSTGRES_HOST (run inside Docker Compose)")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _clear_cached_settings():
    """get_settings() and get_engine() cache their result; reset around each
    test so a test that changes env vars can't leak into the next one."""
    get_settings.cache_clear()
    get_engine.cache_clear()
    yield
    get_settings.cache_clear()
    get_engine.cache_clear()
