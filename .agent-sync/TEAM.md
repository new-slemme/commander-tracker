# Active Team — commander-tracker

Generated: 2026-06-24

Project: Flask / Python 3.11 / SQLite web app (Magic: The Gathering Commander tracker).
Frontend is Jinja2 + vanilla JavaScript + Bootstrap 5 (no build system). Deployed via
Docker Compose. Single developer, feature-branch workflow, GDPR compliance scope.

## Active Agents

- orchestrator: Core. Lead Orchestrator for init and task dispatch.
- architect: Kept by default. System design / ADRs for larger changes to `app.py`.
- code-reviewer: Kept by default. Reviews all code changes for quality and maintainability.
- python-reviewer: Python is the primary backend language (Flask, SQLAlchemy in `app.py`).
- typescript-reviewer: JavaScript is in the language list; covers the vanilla-JS frontend,
  PWA / service-worker correctness, accessibility, and client-side security.
- security-reviewer: Kept by default. Project handles authentication and user data
  (sessions, password hashes, registration approval, join tokens).
- compliance-reviewer: GDPR / RGPD is declared in INIT.md complianceScope.
- database-reviewer: A database is in the tech stack (SQLite, custom schema migrations,
  query-heavy stats/leaderboard routes). Schema and query review still apply.
- debugger: Kept by default. Root-cause investigation for bugs and test failures.
- build-error-resolver: Kept by default. Resolves build / import errors with minimal diffs.
- infra-reviewer: Docker / Docker Compose / CI-CD are in the tech stack (Dockerfile,
  docker-compose.yaml, container deployment on ports 5000/5001).
- doc-updater: Documentation is in scope (CLAUDE.md, AGENTS.md, docs/API.md, docs/qa/).
- planner: Kept by default. Structured implementation plans for complex features.
- tdd-guide: Kept by default. Test-first guidance for the unittest suite.
- refactor-cleaner: Kept by default. Dead-code cleanup and consolidation.
- harness-optimizer: Always kept. Audits that required skills actually ran.

## Pruned Agents

- ai-reviewer: Project makes no LLM API calls and is not AI-native.
- chief-of-staff: No email / Slack / multi-channel communication tools in use.
- e2e-runner: E2E tests are "later", not currently in scope. Re-add when E2E lands.
- flutter-reviewer: No Dart / Flutter in the language list.
- go-reviewer: No Go in the language list.
- kotlin-reviewer: No Kotlin / Java / Android in the language list.
- swift-reviewer: No Swift / iOS / macOS in the language list.
- rust-reviewer: No Rust in the language list.
- loop-operator: No autonomous agent loops run unattended.
- performance-profiler: No performance targets or SLAs declared; profiling not in scope.

## Active Skills

- smart-init: Init infrastructure (ROADMAP extraction / interview).
- using-a-team: Core team-coordination skill.
- brainstorming: active-development / greenfield feature ideation.
- writing-plans: active-development planning.
- executing-plans: active-development plan execution.
- subagent-driven-development: Complex features expected (large single-file app).
- systematic-debugging: Active development; structured debugging.
- five-whys: Root-cause analysis support for debugging.
- test-driven-development: Kept by default; matches the unittest suite.
- verification-before-completion: Kept by default; core completion gate.
- dispatching-parallel-agents: Multiple independent problems expected.
- using-git-worktrees: Parallel-dispatch isolation infrastructure.
- finishing-a-development-branch: Git workflow is feature-branches.
- api-contract-first: Project exposes API endpoints (`/api/...`, REST API in docs/API.md).
- data-migration: Project uses a database (custom `run_schema_migrations()` path).
- incident-response: Project has a production environment.
- skill-duplication-audit: Maintenance utility; kept by default.
- writing-skills: Maintenance utility; kept by default.

## Pruned Skills

- performance-audit: No performance targets declared and no performance-critical
  features in scope.
