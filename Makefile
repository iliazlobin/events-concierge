# Events Concierge -- foundation dev tasks.
# Uses uv (https://docs.astral.sh/uv/) to manage a Python 3.12 venv regardless of host Python.
.DEFAULT_GOAL := help
UV ?= uv
COMPOSE ?= docker compose

.PHONY: help install up api stack build ps down reset logs app-logs migrate lint typecheck test test-unit test-integration quality quality-load slice notifier request-starter change-delivery handoff-expiry lifecycle-invariants workflow-worker catalog-refresh catalog-cadence fmt

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

install: ## Create the 3.12 venv and install deps (app + dev)
	$(UV) sync --locked --extra dev

up: ## Start dependency services only (postgres, redis, temporal, Temporal UI)
	$(COMPOSE) up -d --wait postgres redis temporal temporal-ui

api: ## Build and start the containerized API plus its dependencies (app profile)
	$(COMPOSE) --profile app up -d --build --wait api

stack: ## Build and start the complete containerized API + worker stack (app profile)
	$(COMPOSE) --profile app up -d --build --wait

build: ## Build the locked, non-root application image
	$(COMPOSE) --profile app build

ps: ## Show Compose service state
	$(COMPOSE) --profile app ps

down: ## Stop all services and networks; preserve database and claim-check volumes
	$(COMPOSE) --profile app down --remove-orphans

reset: ## DANGER: remove all services AND local data volumes; requires CONFIRM=reset
	@test "$(CONFIRM)" = "reset" || (echo "Refusing to delete local data. Re-run: make reset CONFIRM=reset"; exit 2)
	$(COMPOSE) --profile app down --volumes --remove-orphans

logs: ## Tail service logs
	$(COMPOSE) --profile app logs -f

app-logs: ## Tail API and durable worker logs
	$(COMPOSE) --profile app logs -f api workflow-worker request-starter notifier change-delivery handoff-expiry lifecycle-invariants

migrate: ## Apply database migrations against the running postgres
	EC_MIGRATION_URL=postgresql+psycopg://ec:ec@localhost:5433/ec $(UV) run alembic upgrade head

lint: ## Ruff lint
	$(UV) run ruff check src tests

fmt: ## Ruff format + autofix
	$(UV) run ruff format src tests && $(UV) run ruff check --fix src tests

typecheck: ## mypy strict
	$(UV) run mypy

test-unit: ## Fast unit tests (no external services)
	$(UV) run pytest -m "not integration"

test-integration: up migrate ## Integration tests against compose services
	EC_DATABASE_URL=postgresql+psycopg://ec_app:ec_app@localhost:5433/ec \
	EC_MIGRATION_URL=postgresql+psycopg://ec:ec@localhost:5433/ec \
	EC_TEMPORAL_TARGET=localhost:7234 \
	EC_REDIS_URL=redis://localhost:6380/0 \
	$(UV) run pytest -m "integration and not quality_load"

quality: up migrate ## Run the hermetic synthetic G1-style workflow quality matrix
	EC_DATABASE_URL=postgresql+psycopg://ec_app:ec_app@localhost:5433/ec \
	EC_MIGRATION_URL=postgresql+psycopg://ec:ec@localhost:5433/ec \
	EC_REDIS_URL=redis://localhost:6380/0 \
	$(UV) run pytest tests/integration/test_g1_quality_harness.py::test_g1_quality_harness_exercises_the_synthetic_workflow_matrix -m integration

quality-load: up migrate ## Run bounded offline P5b repetition coverage; not a benchmark
	EC_DATABASE_URL=postgresql+psycopg://ec_app:ec_app@localhost:5433/ec \
	EC_MIGRATION_URL=postgresql+psycopg://ec:ec@localhost:5433/ec \
	EC_REDIS_URL=redis://localhost:6380/0 \
	EC_QUALITY_LOAD_REPEATS=5 \
	$(UV) run pytest tests/integration/test_g1_quality_harness.py -m "integration and quality_load"

test: test-unit ## Alias for the fast unit suite

slice: up migrate ## Run the end-to-end vertical slice against mocks + compose
	EC_DATABASE_URL=postgresql+psycopg://ec_app:ec_app@localhost:5433/ec \
	EC_REDIS_URL=redis://localhost:6380/0 \
	$(UV) run python -m events_concierge.slice_demo

notifier: up migrate ## Run the durable outbox notifier worker against the configured NotificationPort
	EC_DATABASE_URL=postgresql+psycopg://ec_app:ec_app@localhost:5433/ec \
	EC_REDIS_URL=redis://localhost:6380/0 \
	$(UV) run python -m events_concierge.workers.notifier

request-starter: up migrate ## Run the durable EventRequest start-outbox worker against Temporal
	EC_DATABASE_URL=postgresql+psycopg://ec_app:ec_app@localhost:5433/ec \
	EC_TEMPORAL_TARGET=localhost:7234 \
	$(UV) run python -m events_concierge.workers.request_starter

change-delivery: up migrate ## Project lifecycle watches and fan out recorded organizer changes
	EC_DATABASE_URL=postgresql+psycopg://ec_app:ec_app@localhost:5433/ec \
	EC_TEMPORAL_TARGET=localhost:7234 \
	$(UV) run python -m events_concierge.workers.change_detection

handoff-expiry: up migrate ## Repair only task TTLs whose Temporal child is closed or missing
	EC_DATABASE_URL=postgresql+psycopg://ec_app:ec_app@localhost:5433/ec \
	EC_TEMPORAL_TARGET=localhost:7234 \
	$(UV) run python -m events_concierge.workers.handoff_expiry

lifecycle-invariants: up migrate ## Read-only nightly lifecycle/watch/handoff divergence scanner
	EC_DATABASE_URL=postgresql+psycopg://ec_app:ec_app@localhost:5433/ec \
	EC_TEMPORAL_TARGET=localhost:7234 \
	$(UV) run python -m events_concierge.workers.lifecycle_invariants

workflow-worker: up migrate ## Serve Temporal workflows, including P15a single-GET catalog refreshes
	EC_DATABASE_URL=postgresql+psycopg://ec_app:ec_app@localhost:5433/ec \
	EC_REDIS_URL=redis://localhost:6380/0 \
	EC_TEMPORAL_TARGET=localhost:7234 \
	EC_PACER_BACKEND=redis \
	$(UV) run python -m events_concierge.workflows.worker

catalog-refresh: up migrate ## Queue/refresh one approved registry source; pass SOURCE_KEY=...
	@test -n "$(SOURCE_KEY)" || (echo "SOURCE_KEY is required"; exit 2)
	EC_DATABASE_URL=postgresql+psycopg://ec_app:ec_app@localhost:5433/ec \
	EC_REDIS_URL=redis://localhost:6380/0 \
	EC_TEMPORAL_TARGET=localhost:7234 \
	EC_PACER_BACKEND=redis \
	$(UV) run python -m events_concierge.workers.catalog_refresh "$(SOURCE_KEY)" --run-key "$(RUN_KEY)"

catalog-cadence: up migrate ## Dispatch one bounded pass of due reviewed catalog sources
	EC_DATABASE_URL=postgresql+psycopg://ec_app:ec_app@localhost:5433/ec \
	EC_REDIS_URL=redis://localhost:6380/0 \
	EC_TEMPORAL_TARGET=localhost:7234 \
	EC_PACER_BACKEND=redis \
	$(UV) run python -m events_concierge.workers.catalog_refresh_dispatcher
