# Task Routing — commander-tracker

Generated: 2026-06-24

Routing table for the Lead Orchestrator. Maps task types to the specialist agent best
suited for this project's stack: Flask / Python 3.11 / SQLite, Jinja2 + vanilla JS +
Bootstrap 5 (no build system), Docker Compose, GDPR scope.

## Routing Table

| Task type / trigger | Route to | Notes |
|---------------------|----------|-------|
| Python / Flask / SQLAlchemy backend change (`app.py`, `deck_import.py`) | python-reviewer | Primary backend reviewer |
| Frontend change (templates, `static/js`, `static/css`, PWA manifest, service worker) | typescript-reviewer | Covers vanilla JS, accessibility, PWA, client-side security |
| SQL, schema, query, or `run_schema_migrations()` change | database-reviewer | SQLite schema/query review |
| Database schema evolution / data backfill | data-migration (skill) | Must use custom migration path, not `db.create_all()` |
| Auth, sessions, tokens, user input, API endpoints, secrets | security-reviewer | Project handles auth + user data |
| Data collection / privacy / user data / third-party integration PR | compliance-reviewer | GDPR / RGPD scope from INIT.md |
| Dockerfile, docker-compose.yaml, deployment, CI/CD | infra-reviewer | Docker Compose deployment |
| Any code change (general quality gate) | code-reviewer | Runs after writing/modifying code |
| Bug, test failure, unexpected behavior | debugger | Root cause before any fix |
| Build / import / startup failure | build-error-resolver | Minimal diffs only |
| New feature (full flow) | brainstorming -> planner -> subagent-driven-development | |
| Complex feature / refactor planning | planner | Structured plan with phases |
| Architectural decision | architect | Produces ADRs |
| API endpoint design / contract (`/api/...`, docs/API.md) | api-contract-first (skill) | Contract-first for REST API |
| Dead code / duplication cleanup | refactor-cleaner | Not during active feature work |
| Documentation / codemaps (CLAUDE.md, AGENTS.md, docs/) | doc-updater | Docs in scope |
| Production incident | incident-response (skill) | Project has production env |
| Writing or fixing tests | tdd-guide | Keep existing unittest style |
| Pipeline / rule-compliance audit | harness-optimizer | Verifies skills actually ran |

## Project-Specific Constraints (enforce on every dispatch)

- Preserve Flask / SQLite / Jinja2 architecture. Do not introduce a frontend build system.
  Do not split `app.py` without strong justification.
- Schema changes MUST go through `run_schema_migrations()` and the `schema_migrations`
  table. `db.create_all()` is for brand-new installs only.
- Keep CSS aligned with `~/mine/css-design`; use CSS custom properties, no hardcoded tokens.
- Keep tests in Python `unittest` style; do not add pytest fixtures unless requested.
- Runtime data under `data/`, cached art, APK artifacts, and `FLASK_SECRET_KEY` are not
  source changes.
- GDPR: any change touching user data, registration, or PII routes through
  compliance-reviewer before merge.
- Keep Android API docs in sync when changing standalone/mobile API behavior.

## File Claims
| File | Agent | Task | Status |
|------|-------|------|--------|
| (empty — all waves complete 2026-07-02; backlog 25/25) | | | |
