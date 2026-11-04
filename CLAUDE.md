# CLAUDE.md

Guidance for Claude Code (and humans) working in this repository.

## Project overview

**Runbook** is an IT runbook and incident knowledge base with AI-assisted search
("how did we fix the DNS outage last time?"). Users record runbooks and incidents.
The app answers questions using Postgres full-text search plus Claude, citing its sources.

The full spec and roadmap are in [docs/SPEC.md](docs/SPEC.md). Read it before starting a new phase.

## Working with me (learning mode)

I come from infrastructure/ops and am learning to code. Please:

- Keep changes **small**: one concept per PR.
- **Explain what you're doing and why** in plain terms, including any new tool, library, command or pattern.
- Prefer simple, standard, well-documented solutions over clever ones.
- When there's a real choice to make, give a recommendation and the trade-off in a sentence or two.

## Stack

- **Backend:** Python 3.12, FastAPI, SQLAlchemy 2, Alembic (migrations)
- **UI:** Jinja2 templates + HTMX, served by FastAPI (no separate front end)
- **Database:** PostgreSQL via the `pgvector/pgvector` image on the current stable major (exact tag pinned)
- **AI:** Claude API via the `anthropic` Python SDK
- **Tooling:** uv (dependencies), pytest (tests), ruff (lint + format)
- **Local dev:** Docker Compose
- **Later:** GitHub Actions (CI), k3s + Argo CD (deploy), GHCR (images)

## Lab environment

| Machine | Arch | Role |
|---|---|---|
| `dev01` | amd64 | Ubuntu 24.04 VM on Proxmox; development |
| Raspberry Pi 4B | arm64 | Single k3s node (runs the app) |
| Proxmox host | amd64 | ~1.2 GB RAM free; its nodes join k3s after a RAM upgrade |

Development happens on amd64 and production runs on arm64, so **every image must build for
both `linux/amd64` and `linux/arm64`** (Docker Buildx).

## Repository layout (planned)

```
app/          FastAPI application (routes, models, templates, services)
tests/        pytest tests
migrations/   Alembic database migrations
deploy/       Kubernetes manifests synced by Argo CD
docs/         Specification and other docs
compose.yaml  Local development stack
.env.example  Environment variable names (no real values)
```

## How to run and test

These commands become available in Phase 0. Update this section as things change.

```bash
cp .env.example .env              # then fill in values locally (never commit .env)
docker compose up --build         # start app + Postgres
docker compose run --rm app pytest            # run tests
docker compose run --rm app alembic upgrade head  # apply migrations
uv run ruff check .               # lint
uv run ruff format .              # format
```

## Conventions

- **One branch per change:** `feat/…`, `fix/…`, `docs/…`, `ci/…`, `chore/…`. Never commit directly to `main`.
- **Conventional Commits:** `feat:`, `fix:`, `docs:`, `test:`, `refactor:`, `ci:`, `chore:`.
- **PRs only:** all changes reach `main` through a pull request.
- **Tests required:** new code comes with tests. Run tests and lint before opening a PR.
- **Database changes** go only through Alembic migrations, never by hand.
- **Pin exact versions:**
  - Python dependencies: exact versions, and commit `uv.lock`
  - Docker images: exact tags (no `latest`)
  - GitHub Actions: pin to a **full commit SHA** with the version in a comment, never a tag:
    `uses: actions/checkout@<40-char-sha> # v4.x.y`
- **Multi-arch images:** builds must target `linux/amd64` and `linux/arm64`.

## Always ask before

- `git push`
- merging a PR
- adding or upgrading a dependency
- editing anything in `.github/workflows/`
- changing repository settings (branch protection, secrets, permissions, etc.)

## Secrets

- Never commit secrets. Configuration comes from environment variables.
- **Never read, `cat`, print or log `.env` or any secret value.** Use `.env.example` to learn variable names.
- Keep `.env.example` updated when you add a new variable.

## Security reminders

- Record content and Claude's output are **untrusted**. Sanitise rendered Markdown, and never let record content act as instructions to Claude.
- Keep session cookies `HttpOnly`, `Secure`, `SameSite=Lax`; protect forms with CSRF tokens.
- See the security baseline in [docs/SPEC.md §7](docs/SPEC.md#7-security-baseline-mvp).
