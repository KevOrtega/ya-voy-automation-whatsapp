# Ya Voy

WhatsApp automation for a delivery dispatcher. Stores send orders to a bot number over
WhatsApp; the bot validates them, publishes complete orders to a couriers' WhatsApp group,
and lets couriers claim an order by reacting to its message.

> Status: **design phase**. This repository currently contains only the system analysis
> and design documents. No application code yet.

## Stack (planned)

- Python + FastAPI (webhook receiver and admin API)
- arq worker (background processing, outbound messages, timeouts)
- PostgreSQL (source of truth: orders, drafts, conversation history, audit log)
- Redis (job queue and locks; also required by Evolution API)
- Evolution API (WhatsApp gateway, Baileys-based)
- LLM (provider-agnostic) for structured order extraction only
- Docker Compose for local development; Dokku on a VPS for production

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
