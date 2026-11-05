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

- Development happens on **amd64**.
- The k3s **staging** cluster runs on **arm64**. amd64 nodes may join later.
- So **every image must build for both `linux/amd64` and `linux/arm64`** (Docker Buildx).

Machine-specific details (hostnames, hardware, capacity) live in `CLAUDE.local.md`,
which is git-ignored and never committed.

## Repository layout (planned)

```
app/          FastAPI application (routes, models, templates, services)
tests/        pytest tests
migrations/   Alembic database migrations
deploy/       Kubernetes manifests synced by Argo CD
docs/         Specification and other docs (staging cluster build: docs/staging.md)
compose.yaml  Local development stack
.env.example  Environment variable names (no real values)
```

## How to run and test

Update this section as things change.

Available now (needs [uv](https://docs.astral.sh/uv/) installed):

```bash
uv sync                           # create .venv and install pinned deps from uv.lock
uv run pytest                     # run tests
uv run ruff check .               # lint
uv run ruff format .              # format
uv run uvicorn app.main:app --reload   # run the app at http://127.0.0.1:8000 (try /health, /docs)
```

With Docker Compose (app + Postgres):

```bash
cp .env.example .env              # once; then set your own POSTGRES_PASSWORD (never commit .env)
docker compose up --build         # start app + Postgres; app at http://127.0.0.1:8000
docker compose run --rm app pytest   # run ALL tests, incl. ones that need the database
docker compose run --rm app alembic upgrade head   # apply database migrations
docker compose down               # stop (add -v to also delete the database volume)
```

Logging in: every page except `/login`, `/health`, `/ready` and `/static/` needs a login.
Sessions are server-side (`sessions` table, token hashed) with an `HttpOnly`, `SameSite=Lax`
cookie that is `Secure` unless `SESSION_COOKIE_SECURE=false` (plain-HTTP setups only).
Every POST needs a CSRF token (`app/csrf.py`): forms include
`<input type="hidden" name="csrf_token" value="{{ csrf_token }}">`, HTMX sends it as a
header automatically, and each POST route needs `dependencies=[Depends(verify_csrf)]`
(a test fails if one is missing). Cross-site POSTs are refused. Logins are rate-limited:
5 failures per email or 20 per IP in 15 minutes gives a 429 (`login_failures` table).

Create a login (there's no self-signup; the password is prompted for, twice):

```bash
docker compose run --rm app python -m app.cli create-user --email you@example.com --name "Your Name"
```

Health endpoints: `/health` = process is alive; `/ready` = database is reachable (503 if not).

Tests marked `@pytest.mark.integration` need a real Postgres. They run inside Compose and
are skipped (with a reason) under plain `uv run pytest`.

CI (`.github/workflows/ci.yml`) runs on every PR and every push to `main`: `ruff check`,
`ruff format --check`, `alembic upgrade head` and `pytest`, with a throwaway Postgres so
the integration tests run too. Run the same checks locally before opening a PR.

The image workflow (`.github/workflows/image.yml`) builds the `runtime` stage for
`linux/amd64` and `linux/arm64`. On PRs it only builds; on `main` it pushes to
`ghcr.io/paul-devlab/runbook` tagged `sha-<full commit SHA>` (deploy this one) and `main`.
There is no `latest` tag.

Dependabot (`.github/dependabot.yml`) opens update PRs every Monday for Python packages
(uv), Dockerfile and `compose.yaml` images, and GitHub Actions. It waits until a release
is 7 days old. Review and merge them like any other PR. It does **not** update these
pins, so bump them by hand, keeping duplicates in step:

- `pgvector/pgvector` image in `ci.yml` and `deploy/staging/06-postgres.yaml` (match `compose.yaml`)
- `ghcr.io/astral-sh/uv` image in the `Dockerfile`'s `COPY --from=` line
- uv `version:` in `ci.yml` (match the `uv` image in the `Dockerfile`)
- `tonistiigi/binfmt` and `moby/buildkit` in `image.yml`
- HTMX, vendored as `app/static/vendor/htmx-<version>.min.js` (check the npm `integrity` hash when updating, and update the `<script>` tag in `app/templates/base.html`)

Kubernetes manifests: `deploy/staging/` (the app) and `deploy/argocd/` (Argo CD, a
Kustomize overlay of the pinned upstream install). Cluster setup and access are in
[docs/staging.md](docs/staging.md). Argo CD's version is pinned by commit SHA in
`deploy/argocd/kustomization.yaml`; Dependabot doesn't update it.
Secrets for the cluster go into Git **only as SealedSecrets** (encrypted with `kubeseal`,
see docs/staging.md §8), never as plain `Secret` manifests. The Sealed Secrets controller
(`deploy/sealed-secrets/controller.yaml`, vendored v0.40.0) is also a manual pin.

New migration (no database needed; ruff runs on the new file automatically):

```bash
uv run alembic revision -m "describe the change"
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
- In templates, render Markdown only with `{{ text | markdown }}` (`app/markdown.py`: markdown-it-py, then nh3 allowlist). Never use `|safe` on record content.
- Keep session cookies `HttpOnly`, `Secure`, `SameSite=Lax`; protect forms with CSRF tokens.
- See the security baseline in [docs/SPEC.md §7](docs/SPEC.md#7-security-baseline-mvp).
