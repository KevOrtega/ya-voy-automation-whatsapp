# Ya Voy

WhatsApp automation for a delivery dispatcher. Stores send orders to a bot number over
WhatsApp; the bot validates them, asks the store to confirm a summary, publishes confirmed
orders to a couriers' WhatsApp group, and lets couriers claim an order by reacting to its
message. Stores and the admin can cancel orders (the group post is deleted and the assigned
courier is notified). Stores and couriers are auto-registered from WhatsApp. The admin can
close, reopen or extend order intake in natural language (with confirmation), receives
alerts, and gets a daily `.xlsx` report once all of the day's orders are resolved, plus a
labeled correction if an order changes afterwards. There is no delivery tracking: an order
is done once claimed.

> Status: **stage 3 (webhook and queue)**. Docker Compose dev stack, full PostgreSQL schema
> ([docs/05](docs/05-modelo-datos.md)), the Evolution webhook (filtering, dedup into
> `inbound_events`, enqueue), the worker's `process_inbound_event` skeleton with the per-sender rate
> limit, the outbox (`send_outbound` through the Evolution REST client: FIFO per recipient, pacing,
> retries) and the recovery sweeps. No order logic yet (stage 4+).

## Stack

- Python + FastAPI (webhook receiver)
- arq worker (background processing, outbound messages, scheduled tasks, daily report with openpyxl)
- PostgreSQL (source of truth: orders, drafts, conversation history, audit log)
- Redis (job queue and locks; also required by Evolution API)
- Evolution API (WhatsApp gateway, Baileys-based)
- LLM (provider-agnostic) for structured order extraction only
- Docker Compose for local development; Dokku on a VPS for production

## Development setup

Requirements: Docker with the Compose plugin, Python 3.12+ (only to run tests/lint locally), `make`.

```bash
make env          # creates .env from .env.example with random EVOLUTION_API_KEY / WEBHOOK_SECRET
make up           # docker compose up -d --build: postgres, redis, evolution, migrate, web, worker
make ps           # all services should be healthy (migrate exits 0)
curl localhost:8000/health   # {"status":"ok","checks":{"database":"ok","redis":"ok"}}
make logs         # follow web + worker logs (JSON)
make down         # stop (make clean also deletes volumes, including the WhatsApp session)
```

| Service | Host port | Notes |
|---------|-----------|-------|
| `web` | 8000 | FastAPI (`--reload`, code mounted). `GET /health`, `POST /webhooks/evolution/{WEBHOOK_SECRET}`. |
| `worker` | — | `arq ya_voy.worker.WorkerSettings --watch ya_voy`. |
| `migrate` | — | `alembic upgrade head`, runs once before `web`/`worker`. |
| `evolution` | 8080 | Evolution API `v2.3.7`; manager and QR at <http://localhost:8080/manager> (API key = `EVOLUTION_API_KEY`). Use a test number, never the production one. |
| `postgres` | 5432 | User/password `yavoy`/`yavoy`; databases `yavoy` (app) and `evolution`. |
| `redis` | 6379 | DB 0: arq queue; DB 1: Evolution cache. |

Local tooling (outside Docker; tests don't need Postgres or Redis):

```bash
make venv         # .venv with the app and dev dependencies
make test         # pytest
make lint         # ruff check + ruff format --check
make format       # ruff --fix + format
```

Full suite in Docker, including integration tests against the compose Postgres/Redis:
`make test-docker`. Failed inbound events: `make reprocess ids="12 15"` (or `ids=--all`).
Without an LLM key, `LLM_PROVIDER=fake` gives a deterministic, offline extractor for development.
New migrations (stage 2 onwards):
`make revision m="create orders"`, then `make migrate`.

All settings are environment variables (see [`.env.example`](.env.example) and
[architecture §6](docs/06-arquitectura.md#6-variables-de-entorno)). `DATABASE_URL` accepts the
`postgres://` form injected by Dokku and is converted to `postgresql+asyncpg://`.

## Documentation

Design documents (in Spanish) live in [`docs/`](docs/README.md):

1. [Requirements](docs/01-requisitos.md)
2. [Use cases](docs/02-casos-de-uso.md)
3. [Order state machine](docs/03-estados-pedido.md)
4. [Sequence diagrams](docs/04-secuencias.md)
5. [Data model](docs/05-modelo-datos.md)
6. [Architecture and deployment](docs/06-arquitectura.md)
7. [Message catalog](docs/07-catalogo-mensajes.md)

Diagrams are written in [Mermaid](https://mermaid.js.org/) and render natively on GitHub.

## Conventions

- All code, identifiers, database objects, env vars and commit messages are in English.
- User-facing WhatsApp messages are in Spanish and are defined in the message catalog.
