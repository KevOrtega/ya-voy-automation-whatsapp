---
name: ya-voy-conventions
description: Use at the start of any task in the Ya Voy repository, and whenever you need to know the stack, language rules, where requirements and client decisions live, or how to trace a change back to RF/CU/T/DC ids in docs/
---

# Ya Voy conventions

## What the system is

A WhatsApp order dispatcher. Stores message a bot number; the bot extracts the order with an LLM,
validates it in code, asks the store to confirm a summary, publishes it to a couriers' group, and the
first courier who reacts to the post gets it. Cancellations, admin commands, schedule changes in
natural language and a daily `.xlsx` report complete the scope. No delivery tracking.

## Stack (fixed, do not change without Kevin)

- Python, FastAPI (`web`: webhook + `/health` only), arq worker (`worker`: all business logic,
  outbound sends, scheduled tasks, report with openpyxl).
- SQLAlchemy 2 async + asyncpg, Alembic, PostgreSQL 16 (source of truth).
- Redis 7: arq queue, per-store lock, rate limits, Evolution cache. Never business state (RNF-04).
- Evolution API v2 (Baileys) as the WhatsApp gateway.
- LLM behind project interfaces (`OrderExtractor`, `ScheduleIntentParser`); provider is config (RNF-13).
  The LLM never decides: its output is validated in code (RF-13, D-05).
- Docker Compose for **development only**. Production goes to Dokku later; do not add production
  deployment artifacts unless a task asks for them.

## Language rules (RNF-14)

- Code, identifiers, DB objects, env vars, branch names and commit messages: **English**.
- User-facing WhatsApp text and the report: **Spanish**, taken only from
  `docs/07-catalogo-mensajes.md` by template key. Never hardcode new user-facing text; if a
  message is missing, flag it instead of inventing it.
- Design docs under `docs/` are in Spanish. Keep them in Spanish when you update them.

## Where things live

| Need | Document |
|------|----------|
| Actors, RF, RNF, RN, DC-01..DC-35, open questions (PA) | `docs/01-requisitos.md` |
| Use cases CU-xx with alternative flows | `docs/02-casos-de-uso.md` |
| Order state machine, transitions T01..T19 and their SQL | `docs/03-estados-pedido.md` |
| Sequence diagrams (claim race, claim vs cancel, outbox) | `docs/04-secuencias.md` |
| Tables, constraints, indexes, extraction schemas | `docs/05-modelo-datos.md` |
| Containers, package layout, D-xx design decisions, risks R-xx, env vars | `docs/06-arquitectura.md` |
| Every message template | `docs/07-catalogo-mensajes.md` |
| Glossary (store/courier/admin, business_date, ...) | `docs/README.md` |

Id prefixes: `RF` functional, `RNF` non-functional, `RN` business rule, `CU` use case, `T` transition,
`DC` client decision, `PA` open question, `D` design decision, `R` risk.

## Tracing a change

1. Before coding, find the ids that govern the behaviour (`rg -n "RF-23|T10" docs/`).
2. Implement exactly what they say: names of tables, columns, statuses, env vars and template keys
   come from the docs verbatim.
3. Name the ids in test names or docstrings where useful (e.g. `test_claim_race_single_winner_rf23`)
   and list them in the stage hand-off.
4. If the docs are ambiguous, contradict each other or seem wrong, stop and ask. Client decisions
   (DC-xx) are Kevin's; never "fix" one silently. A doc change that alters behaviour needs his OK.

## Package layout (proposed in docs/06 §2)

`ya_voy/{api,worker,domain,schedule,identity,llm,whatsapp,messaging,reports,db}` plus `config.py`.
`domain` is pure (no FastAPI, arq or Evolution imports) and is where most unit tests go.
`whatsapp` owns Evolution payload parsing; `identity` owns `@lid`/phone resolution.

## Hard rules

- Correctness guarantees live in Postgres (conditional `UPDATE`, unique constraints, row locks),
  not in Redis or in arrival order. See the `concurrency-and-race-testing` skill.
- Times stored as UTC `timestamptz`; schedule and `business_date` computed with `zoneinfo` from `TZ`.
- Secrets only via env vars; never commit `.env`.
- The repo is local-only for now: no remote, no push.
