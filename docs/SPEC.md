# Runbook — Specification

Status: draft · Last updated: 2026-11-04

## 1. Problem and goals

Knowledge about how we run and fix systems gets lost in chat threads, tickets and
people's heads. When something breaks again, we waste time rediscovering the fix.

**Runbook** is an IT runbook and incident knowledge base. It lets a team:

- record **runbooks** (step-by-step procedures) and **incidents** (what broke, why, and how it was fixed)
- ask plain-language questions like *"how did we fix the DNS outage last time?"* and get
  a short answer that **cites the records it came from**

Secondary goal: Runbook is a learning project for modern AI-assisted development,
CI/CD, GitOps (k3s + Argo CD) and enterprise security practices.

## 2. Users

- A small, trusted team.
- Every logged-in user can read and edit everything (no roles in the MVP).
- There is no self-signup. An admin creates accounts with a CLI command.

## 3. MVP features

### Authentication
- Log in and out with email and password.
- Passwords are hashed with argon2.
- Server-side sessions: the browser holds only a random session ID in a cookie (see §7).

### Runbooks
- Create, view, edit and delete.
- Fields: title, summary, body (Markdown), tags.

### Incidents
- Create, view, edit and delete.
- Fields: title, summary, severity (SEV1–SEV4), status (open / resolved),
  started_at, resolved_at, root cause (Markdown), resolution (Markdown), tags.
- Can link to one or more runbooks ("we used runbook X to fix this").

### Browse and filter
- List runbooks and incidents.
- Filter by tag, severity, status and text.

### Revision history
- On every edit, save a snapshot of the previous version.
- View the list of past versions and read any one of them.
- Out of scope for the MVP: comparing versions (diff) and restoring an old version.

### Ask (AI-assisted search)
1. The user types a question.
2. Postgres full-text search finds the top N most relevant runbooks and incidents.
3. Those records and the question are sent to Claude.
4. Claude writes a short answer that cites the records it used, linked by ID.
5. If nothing relevant is found, the app says so instead of guessing.

### User interface
- Server-rendered HTML pages (Jinja2 templates + HTMX) served by FastAPI:
  one language (Python) and one container.
- A JSON API is also available, with interactive docs at `/docs`.

## 4. Non-goals (MVP)

These are deliberately **out of scope** for the MVP. Some appear later in the roadmap.

- Roles, permissions (RBAC) and single sign-on (SSO/OIDC)
- Integrations (Slack, PagerDuty, email) and notifications/alerting
- Single-page app (SPA) front end
- Semantic/vector search (pgvector embeddings)
- File attachments
- Bulk import of existing documents
- Comparing (diff) and restoring revisions
- Automated secret detection or redaction
- Multi-tenancy (multiple separate organisations)

## 5. Data model sketch

```
users ──< sessions

users ──< runbooks  >──< tags      (via runbook_tags)
users ──< incidents >──< tags      (via incident_tags)
incidents >──< runbooks            (via incident_runbooks)

runbooks / incidents ──< revisions (entity_type + entity_id)
```

`──<` = one-to-many, `>──<` = many-to-many.

| Table | Key columns |
|---|---|
| `users` | id, email (unique), display_name, password_hash, is_active, created_at |
| `sessions` | id, user_id, token_hash, created_at, expires_at |
| `runbooks` | id, title, summary, body_md, created_by, updated_by, created_at, updated_at, search_vector |
| `incidents` | id, title, summary, severity, status, started_at, resolved_at, root_cause_md, resolution_md, created_by, updated_by, created_at, updated_at, search_vector |
| `tags` | id, name (unique, lowercase) |
| `runbook_tags` | runbook_id, tag_id |
| `incident_tags` | incident_id, tag_id |
| `incident_runbooks` | incident_id, runbook_id |
| `revisions` | id, entity_type (`runbook` / `incident`), entity_id, snapshot (JSONB), edited_by, edited_at |

Notes:
- `search_vector` is a generated `tsvector` column with a GIN index. It powers full-text search.
- Sessions store a **hash** of the token, so a leaked database dump can't be used to log in.
- Later (Phase 5): an `attachments` table with entity_type, entity_id, filename, content_type,
  size_bytes, storage_key, uploaded_by, uploaded_at.
- Later (Phase 6): a pgvector `embedding` column (or a separate table) for semantic search.

## 6. AI behaviour

- Uses the Claude API via the official `anthropic` Python SDK.
- The model name comes from an environment variable (e.g. `ANTHROPIC_MODEL`), so it can change without a code change.
- The API key comes from the environment only (`ANTHROPIC_API_KEY`). It is never stored in the database or in code.
- Each request has a `max_tokens` cap.
- Claude answers **only** from the supplied records and cites them. It gets no tools and takes no actions.
- Questions and answers are not stored in the MVP.

## 7. Security baseline (MVP)

| Area | Rule |
|---|---|
| Secrets | Only in environment variables / `.env` (git-ignored). `.env.example` lists variable names with no real values. |
| Content policy | Never put passwords, keys or tokens in runbooks or incidents. Hostnames and IPs are fine. The UI shows a reminder on edit forms. |
| Prompt injection | Record content is **untrusted data**. Records go to Claude inside clearly delimited blocks, and the system prompt says they are reference material, never instructions. Claude has no tools, so a malicious record can't trigger actions. |
| XSS | All Markdown (user content **and** Claude's answers) is rendered to HTML and then sanitised against an allowlist (e.g. markdown-it-py + nh3). Jinja autoescaping stays on. |
| Rate limiting | Login is rate-limited (against password guessing). Ask is rate-limited per user/IP (against cost and abuse). |
| Session cookie | `HttpOnly`, `Secure` (relaxed only for local http dev), `SameSite=Lax`. |
| CSRF | Forms that change data include a CSRF token. |
| Passwords | argon2 hashing; no plaintext anywhere, including logs. |
| AI spend | A monthly spend limit set in the Anthropic Console, plus the per-request `max_tokens` cap. |

## 8. Roadmap

Each phase is split into small PRs. CI and a real deployment come **first**, so every
feature ships through the actual pipeline from day one.

| Phase | Goal | Done when |
|---|---|---|
| **0 — Foundations** | Repo layout, Docker Compose (app + Postgres with pgvector, current pinned major), FastAPI skeleton with `/health`, Alembic, pytest, ruff, pinned versions + `uv.lock` | `docker compose up` works on the dev machine; `/health` returns OK; tests pass |
| **1 — CI** | GitHub Actions: lint, test, build **multi-arch images (linux/amd64 + linux/arm64) with Docker Buildx**, push to GHCR. **Dependabot version updates** for Python (uv), Docker and GitHub Actions. Enable GitHub **secret scanning** and **push protection**. | Every PR runs lint + tests; merges to `main` publish a multi-arch image; Dependabot opens update PRs; a pushed test secret is blocked |
| **2 — Walking skeleton deploy** | Single-node k3s **staging** cluster on an arm64 Raspberry Pi. Argo CD syncs manifests from `deploy/`. No database yet. | `/health` reachable on staging, deployed by Argo CD from Git |
| **3a — Database + auth** | Postgres in the cluster, users/sessions, login/logout, CLI to create users, base page layout | Can log in on the deployed app |
| **3b — Core content** | Runbooks, incidents, tags, incident↔runbook links, browse/filter | Can create, edit and find records |
| **3c — Revision history** | Snapshot on edit; list and view past versions | Edit history is visible per record |
| **3d — Ask** | Full-text search + Claude answer with citations, rate limits, spend controls | "How did we fix X?" returns a cited answer |
| **4 — Observability** | Structured (JSON) logs, Prometheus metrics (requests, latency, errors, Ask usage/cost), Grafana dashboards, basic alerts (app down, error rate, Ask spend) | Dashboards show live staging traffic; a test alert fires |
| **5 — Attachments** | Upload/download files on records; size limit + type allowlist; never sent to Claude | Files are stored and downloadable |
| **6 — Semantic search** | pgvector embeddings (decide then: local model vs. Voyage AI), hybrid with full-text | Ask finds relevant records without exact keywords |
| **7 — Security hardening** | SSO/OIDC, RBAC, audit log, secret detection/redaction, TLS everywhere, image and dependency scanning, network policies | Security checklist passes |
| **Later** | SPA front end, Slack/PagerDuty integrations, notifications | — |

The k3s cluster is a **staging** environment. A production environment and additional
amd64 nodes are future decisions (see open questions).

## 9. Open questions

- **Postgres in k3s:** a plain StatefulSet (simpler to learn) or an operator such as CloudNativePG (handles backups and failover)? Decide at the start of Phase 3a.
- **Backups:** what, where and how often; how to test a restore.
- **Pi capacity:** will k3s + Argo CD + Postgres + the app fit in the Pi's RAM? Should Postgres data live on a USB SSD instead of the SD card? SD cards wear out under database writes.
- **Attachment storage (Phase 5):** a PersistentVolumeClaim, or S3-compatible object storage (e.g. MinIO)?
- **More nodes:** when to add amd64 nodes, and whether workloads should prefer them.
- **Production:** where and when a production environment exists, and how changes are promoted from staging.
- **Observability footprint:** can Prometheus + Grafana run on the staging node, or should they live elsewhere?
