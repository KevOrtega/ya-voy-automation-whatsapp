# Development shortcuts. Docker targets use `docker compose`; local targets use .venv.
.DEFAULT_GOAL := help
COMPOSE ?= docker compose
VENV ?= .venv
PY := $(VENV)/bin/python

.PHONY: help env venv up down logs ps test test-docker lint format migrate revision check-migrations replay reprocess shell-web psql redis-cli clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-12s %s\n", $$1, $$2}'

env: ## Create .env from .env.example with random dev secrets (won't overwrite)
	@test -f .env && echo ".env already exists" || ( \
		sed -e "s/^EVOLUTION_API_KEY=$$/EVOLUTION_API_KEY=$$(python3 -c 'import secrets; print(secrets.token_hex(16))')/" \
		    -e "s/^WEBHOOK_SECRET=$$/WEBHOOK_SECRET=$$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')/" \
		    .env.example > .env && echo "Created .env" )

venv: ## Create a local virtualenv with dev dependencies
	python3 -m venv $(VENV)
	$(PY) -m pip install -U pip
	$(PY) -m pip install -e ".[dev]"

up: ## Build and start the whole stack in the background
	$(COMPOSE) up -d --build

down: ## Stop the stack (keeps volumes)
	$(COMPOSE) down

logs: ## Follow logs of web and worker
	$(COMPOSE) logs -f web worker

ps: ## Show service status
	$(COMPOSE) ps -a

test: ## Run the test suite locally
	$(PY) -m pytest

test-docker: ## Run the full suite in Docker, including integration tests against compose Postgres/Redis
	$(COMPOSE) up -d --wait postgres redis
	$(COMPOSE) run --rm --no-deps -e RUN_INTEGRATION=1 web pytest

lint: ## Run ruff (lint + format check)
	$(VENV)/bin/ruff check .
	$(VENV)/bin/ruff format --check .

format: ## Auto-fix lint issues and format code
	$(VENV)/bin/ruff check --fix .
	$(VENV)/bin/ruff format .

migrate: ## Apply migrations inside Docker
	$(COMPOSE) run --rm migrate

revision: ## Autogenerate a migration: make revision m="create orders"
	$(COMPOSE) run --rm migrate alembic revision --autogenerate -m "$(m)"

check-migrations: ## Fail if the models differ from the migrations (alembic check, in Docker)
	$(COMPOSE) run --rm migrate alembic check

replay: ## POST Evolution fixtures to the running web twice: make replay f="tests/fixtures/evolution/*.json"
	$(COMPOSE) exec web python -m scripts.replay_fixture --url http://localhost:8000 --times 2 $(f)

reprocess: ## Re-enqueue failed inbound events: make reprocess ids="12 15" (or ids=--all)
	$(COMPOSE) exec worker python -m scripts.reprocess_inbound $(ids)

shell-web: ## Open a shell in the web container
	$(COMPOSE) exec web bash

psql: ## Open psql on the app database
	$(COMPOSE) exec postgres psql -U yavoy -d yavoy

redis-cli: ## Open redis-cli
	$(COMPOSE) exec redis redis-cli

clean: ## Stop the stack and DELETE all volumes (database, Redis, WhatsApp session)
	$(COMPOSE) down -v
