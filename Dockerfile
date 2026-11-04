# syntax=docker/dockerfile:1
#
# Multi-stage build. Each `FROM ... AS name` is a stage; later stages copy only
# what they need from earlier ones, so the final image stays small and does not
# contain build tools.
#
#   builder      installs runtime deps into /app/.venv with uv
#   builder-dev  same, plus dev deps (pytest, ruff) for local dev and tests
#   dev          image used by compose.yaml for local development
#   runtime      production image (the LAST stage, so it's the default target)

# ---------------------------------------------------------------- base
FROM python:3.12.16-slim-trixie AS base

# Don't write .pyc files at runtime; print logs immediately (no buffering).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# ---------------------------------------------------------------- builder
FROM base AS builder

# Copy just the uv binary from its official image (pinned version).
COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /bin/uv

# UV_COMPILE_BYTECODE: pre-compile .pyc at build time for faster startup.
# UV_LINK_MODE=copy:   copy files (not hard-link), required across build stages.
# UV_PYTHON_DOWNLOADS: use the image's Python, never download another one.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

# Copy only the dependency files first. Docker caches each step, so as long as
# these files don't change, rebuilding after a code change skips this slow step.
COPY pyproject.toml uv.lock .python-version ./

# --locked: fail if uv.lock doesn't match pyproject.toml (no silent changes).
# --no-dev: production deps only.
# --no-install-project: our app isn't a package; we copy its code in later.
RUN uv sync --locked --no-dev --no-install-project

# ---------------------------------------------------------------- builder-dev
FROM builder AS builder-dev
RUN uv sync --locked --no-install-project

# ---------------------------------------------------------------- runtime-base
FROM base AS runtime-base

# Run as an unprivileged user, never as root.
RUN groupadd --system app && useradd --system --gid app --no-create-home app

# Put the virtualenv's programs (uvicorn, pytest, ...) first on PATH.
ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8000

# Docker marks the container "unhealthy" if /health stops answering.
# (The slim image has no curl, so use Python's standard library.)
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

# ---------------------------------------------------------------- dev
FROM runtime-base AS dev
COPY --from=builder-dev /app/.venv /app/.venv
COPY pyproject.toml ./
COPY alembic.ini ./
COPY migrations ./migrations
COPY app ./app
COPY tests ./tests
# /app is read-only for the non-root user, so turn off pytest's cache
# (it only speeds up re-running failed tests).
ENV PYTEST_ADDOPTS="-p no:cacheprovider"
USER app

# ---------------------------------------------------------------- runtime
FROM runtime-base AS runtime
COPY --from=builder /app/.venv /app/.venv
# Migrations ship in the production image so they can run from it
# (e.g. as a Kubernetes Job before a new version starts).
COPY alembic.ini ./
COPY migrations ./migrations
COPY app ./app
USER app
