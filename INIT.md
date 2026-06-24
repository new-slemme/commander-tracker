# Project Init Document

## Project Identity

**Name:** commander-tracker
**Type:** web-app
**Status:** active-development

## Primary Programming Languages

List every language used in this project. Agents for unlisted languages will be pruned.

- [x] TypeScript / JavaScript
- [x] Python
- [ ] Go
- [ ] Rust
- [ ] Kotlin / Java (Android)
- [ ] Swift (iOS / macOS)
- [ ] Dart (Flutter)
- [x] Other: HTML, CSS, Jinja2 templates, Docker Compose

## Tech Stack

**Frontend:** Jinja2 templates, vanilla JavaScript, Bootstrap 5, css-design-derived CSS
**Backend:** Flask, Flask-SQLAlchemy, Flask-Limiter
**Database:** SQLite at `/data/commander.db`
**Runtime:** Python 3.11 in Docker
**CI/CD:** Docker Compose deployment; Python unittest/pytest-compatible tests

## Communication & Collaboration Tools

List tools in active use. Agents for unlisted tools will be pruned.

- [ ] Gmail / email
- [ ] Slack
- [ ] GitHub Issues / PRs
- [ ] Linear
- [ ] Notion
- [x] None - this is a pure engineering project

## Scope Boundaries

Answer each question to help the orchestrator decide which agents to keep.

**Will this project have E2E tests?** later
**Will this project use a PostgreSQL database?** no
**Will this project handle authentication or user data?** yes
**Is there a multi-channel communication workflow?** no
**Are there autonomous agent loops running unattended?** no
**Will there be regular documentation / codemaps?** yes
**Does this project make LLM API calls?** no
**Does this project use Terraform / Docker / Kubernetes?** yes - Docker Compose
**Does this project have a production environment?** yes
**Are there performance targets or SLAs?** no

## Compliance Scope

Which regulations apply? The `compliance-reviewer` agent only activates for declared scopes.

- [x] GDPR / RGPD (any project with EU users)
- [ ] COPPA (directed at or collecting data from under-13)
- [ ] PCI-DSS (handles payment card data)
- [ ] SOC2 (B2B SaaS storing customer data)
- [ ] HIPAA (US healthcare, PHI handling)
- [ ] None

## Team & Workflow

**Number of developers:** 1
**Branching model:** feature-branches
**Review process:** self-review

## Quality Standards

**Minimum test coverage:** none
**Linting enforced:** no
**Type checking enforced:** no

## Special Constraints

Any domain-specific guardrails the orchestrator should enforce across all agents:

- Preserve the existing Flask/SQLite/Jinja2 architecture; do not introduce a frontend build system or split `app.py` without strong justification.
- Schema evolution must use the custom `run_schema_migrations()` path and `schema_migrations` table; `db.create_all()` is only for brand-new installs.
- Runtime data under `data/`, cached art, APK artifacts, and secrets such as `FLASK_SECRET_KEY` are not source changes.
- Tests should keep the existing Python `unittest` style; pytest may run them, but do not add pytest fixtures unless explicitly requested.
- Keep CSS aligned with `~/mine/css-design`; use CSS custom properties and avoid hardcoded token values.
- Keep Android API docs in sync with the Android repo when changing standalone/mobile API behavior.

## CLI Environment

Which AI coding CLI(s) will be active in this project? The orchestrator uses this to verify
that the correct platform configs are in place and to flag incompatibilities at init time.

- [x] Claude Code (native - uses `.claude/`)
- [ ] Codex CLI (uses `.codex-plugin/`)
- [ ] Cursor (uses `.cursor-plugin/`)
- [ ] OpenCode (uses `.opencode/`)
- [ ] Multiple CLIs simultaneously (see multi-CLI notes below)

## Daily Workflow Mode

How will the orchestrator be invoked?

- [ ] Daily standup mode (morning / tick / report cycle)
- [x] On-demand dispatch (ad hoc per feature/bug)
- [ ] CI/CD triggered (automated pipeline)

## Existing TASKS.md

Does this project already have a `TASKS.md` backlog the orchestrator should consume?
no - orchestrator will create one

---

> Once this file is complete, run `/orchestrate init` to initialize the team.
