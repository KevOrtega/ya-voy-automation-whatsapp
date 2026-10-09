# AGENTS.md

Instructions for AI coding agents working on Ya Voy. Humans start at [README.md](README.md).

## Project

WhatsApp order dispatcher: stores send orders to a bot number, the bot extracts and validates them,
the store confirms a summary, the order is published to a couriers' group and the first courier who
reacts gets it. Full analysis and design (in Spanish) is in [`docs/`](docs/README.md), with client
decisions DC-01..DC-35 in `docs/01-requisitos.md`. The docs are the source of truth; read the parts
that govern your task before writing code.

## Non-negotiable rules

- Stack: Python, FastAPI, SQLAlchemy 2 async + asyncpg, Alembic, PostgreSQL 16, Redis 7 + arq,
  Evolution API v2. LLM only for extraction/interpretation, always validated in code.
- Code, identifiers, DB objects, env vars, branch names and commit messages in **English**.
  User-facing WhatsApp text in Spanish, only from `docs/07-catalogo-mensajes.md`. Docs stay in Spanish.
- Docker Compose builds the **development** environment only. Production (Dokku) comes later.
- Correctness lives in Postgres: conditional `UPDATE`s, unique constraints, row locks. Atomic claim and
  race cases (claim vs claim, claim vs cancel, webhook dedup, outbox idempotency) must have tests
  against real Postgres before a stage is done.
- Local git only: one branch per stage/task, conventional commits, no remote, no push, no merge
  unless Kevin asks.
- Never change a client decision (DC-xx) or documented behaviour on your own; ask.
- Secrets only through env vars; never commit `.env`.

## Commands

Defined in stage 1 (`Makefile`, `docker-compose.yml`). Run from the repo root.

```bash
# One-time setup
make env                  # .env from .env.example with random EVOLUTION_API_KEY / WEBHOOK_SECRET
make venv                 # .venv with the app + dev deps (pip install -e ".[dev]")

# Stack (development only)
make up                   # docker compose up -d --build: postgres, redis, evolution, migrate, web, worker
make ps                   # all healthy; migrate "Exited (0)"
curl localhost:8000/health   # {"status":"ok","checks":{"database":"ok","redis":"ok"}}
make logs                 # follow web + worker (JSON logs)
make down                 # stop, keep volumes (make clean = down -v, also wipes the WhatsApp session)

# Tests and lint
make test                 # pytest locally; unit tests only, integration tests are skipped
make test-docker          # full suite in Docker incl. integration tests against compose Postgres/Redis
make lint                 # ruff check . && ruff format --check .
make format               # ruff check --fix . && ruff format .

# Migrations (Alembic, async; URL from DATABASE_URL)
make revision m="create orders table"   # alembic revision --autogenerate (in Docker)
make migrate                            # docker compose run --rm migrate (alembic upgrade head)
docker compose run --rm migrate alembic downgrade -1
```

Layout: `ya_voy/` package (`api/`, `worker/`, `db/` with `migrations/`, `config.py`, `queue.py`,
`logging.py`), `tests/`. Integration tests are marked `integration` and run only with
`RUN_INTEGRATION=1` (set by `make test-docker`). Ports: web 8000, Evolution 8080 (manager at
`/manager`), Postgres 5432 (`yavoy`/`yavoy`, DBs `yavoy` and `evolution`), Redis 6379.

## Workflow

1. Read the relevant docs and the `ya-voy-conventions` skill.
2. For multi-step work, write a plan (`writing-plans`) and save it to
   `docs/plans/YYYY-MM-DD-<name>.md` (in English; this overrides the upstream default
   `docs/superpowers/plans/`). Execute it with `executing-plans` or `subagent-driven-development`.
3. Test first (`test-driven-development`); for concurrency use `concurrency-and-race-testing`.
4. Before claiming done: `verification-before-completion`, then `stage-delivery-checklist`, then
   hand off to the auditor.

## Skills

Skills live in [`.claude/skills/`](.claude/skills/README.md), one folder per skill with a `SKILL.md`.
Claude Code loads them automatically; with other tools, open the matching `SKILL.md` when its
situation applies.

Project-specific:

| Skill | Use when |
|-------|----------|
| `ya-voy-conventions` | Starting any task; finding docs, ids and language rules |
| `concurrency-and-race-testing` | Touching order transitions, dedup, outbox, courier auto-registration or sweeps |
| `evolution-api-events` | Parsing webhook payloads, `@lid`/phone identity, reactions, sending/deleting messages, fixtures |
| `stage-delivery-checklist` | Before telling the auditor a stage or task is ready |

Vendored from [obra/superpowers](https://github.com/obra/superpowers) (MIT, see
`.claude/skills/README.md`):

| Skill | Use when |
|-------|----------|
| `test-driven-development` | Implementing any feature or bug fix, before writing implementation code |
| `systematic-debugging` | A bug, failing test or unexpected behaviour, before proposing a fix |
| `verification-before-completion` | About to say something works, is fixed or passes |
| `writing-plans` | You have requirements for a multi-step task, before touching code |
| `executing-plans` | Executing a written plan yourself, task by task |
| `subagent-driven-development` | Executing a plan by dispatching one subagent per task with reviews |
| `dispatching-parallel-agents` | Two or more independent tasks with no shared state |
| `requesting-code-review` | Finishing a task or stage, before handing it to review |
| `receiving-code-review` | Acting on auditor or reviewer feedback; verify before agreeing |
| `using-git-worktrees` | Work that must not disturb another branch checked out in the main tree |
| `finishing-a-development-branch` | Work is complete and tests pass; deciding how to integrate (here: keep local, ask Kevin) |
