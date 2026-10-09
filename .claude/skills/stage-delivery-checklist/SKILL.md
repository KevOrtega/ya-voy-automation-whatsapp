---
name: stage-delivery-checklist
description: Use before announcing that a Ya Voy stage (or any task branch) is ready for the auditor - lists the checks, evidence and hand-off note every delivery must include
---

# Stage delivery checklist

The project is built in stages (dev environment, data model, webhook and queue, order intake,
publish and claim, cancellations, schedule and admin commands, daily report and corrections). An
auditor reviews each stage against `docs/`. Hand off only when every applicable item holds, and show
the evidence (command output), per `verification-before-completion`.

## Branch and commits

- One local branch per stage/task (e.g. `stage-3-webhook-queue`). No remote, no push, no merge
  unless Kevin asks.
- Conventional commits in English (`feat(db): ...`, `fix(worker): ...`, `test: ...`), small and
  focused. No secrets, no `.env`, no generated files.

## Checks (run them fresh, paste the result)

- [ ] Full test suite green against the compose Postgres/Redis, not only the new tests.
- [ ] Lint clean (`ruff check`) and formatting clean with the repo's configured formatter.
- [ ] Migrations: `alembic upgrade head` on an empty database works; downgrade one step and upgrade
      again works; models and migrations agree (no pending autogenerate diff).
- [ ] `docker compose up` still brings the stack up and `/health` responds.
- [ ] Exact commands live in `AGENTS.md` (stage 1 defines them); use those.

## Correctness

- [ ] Names (tables, columns, statuses, constraints, env vars, template keys) match `docs/` verbatim.
- [ ] Constraints and indexes from `docs/05-modelo-datos.md` exist in the migration, not only in models.
- [ ] Any change to order transitions, dedup, outbox, auto-registration or sweeps has the race tests
      from `concurrency-and-race-testing` (both interleavings + replay). This is a hard gate.
- [ ] User-facing text comes from `docs/07-catalogo-mensajes.md`; nothing new invented.
- [ ] Nothing depends on an unverified Evolution behaviour outside `ya_voy/whatsapp`/`identity`
      (see `evolution-api-events`).
- [ ] Development environment only: no production deployment config unless the task asks.

## Hand-off note (post in the project chat)

1. Branch name and commit list (`git log --oneline main..HEAD`).
2. What was built, mapped to ids: `RF-xx / CU-xx / Txx / DC-xx -> module / test`.
3. Evidence: test count and result, lint result, migration round-trip result.
4. Deviations from the docs or open questions, stated plainly (none hidden in code comments).
5. What is deliberately left for later stages.
