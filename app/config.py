"""Application settings, read from environment variables.

pydantic-settings maps each field to an env var of the same name
(case-insensitive): `postgres_user` <- POSTGRES_USER, and so on. Values are
validated on load, so a missing or malformed setting fails fast with a clear
error instead of causing a confusing failure later.

In Docker Compose these variables come from `.env` (see `.env.example`).
"""

from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings
from sqlalchemy import URL


class Settings(BaseSettings):
    postgres_user: str
    # SecretStr hides the value when printed or logged: it shows as '**********'.
    postgres_password: SecretStr
    postgres_db: str
    postgres_host: str = "db"  # the Compose service name
    postgres_port: int = 5432

    @property
    def database_url(self) -> URL:
        """SQLAlchemy connection URL for Postgres via the psycopg 3 driver.

        URL.create escapes special characters (like @ : /) in the password,
        which a hand-built "postgresql://user:pass@host/db" string would not.
        """
        return URL.create(
            drivername="postgresql+psycopg",
            username=self.postgres_user,
            password=self.postgres_password.get_secret_value(),
            host=self.postgres_host,
            port=self.postgres_port,
            database=self.postgres_db,
        )


@lru_cache
def get_settings() -> Settings:
    """Load settings once, on first use (not at import time).

    Loading lazily means modules can be imported, and unit tests run,
    without database variables being set.
    """
    return Settings()  # type: ignore[call-arg]  # values come from env vars
