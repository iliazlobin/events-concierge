# Events Concierge -- foundation dev tasks.
# Uses uv (https://docs.astral.sh/uv/) to manage a Python 3.12 venv regardless of host Python.
.DEFAULT_GOAL := help
UV ?= uv
COMPOSE ?= docker compose
# Local images identify the exact checked-out commit and clearly retain a `-dirty` suffix when
# uncommitted source changes are present. Release automation supplies its own immutable revision.
LOCAL_RELEASE_REVISION ?= $(shell git describe --always --dirty 2>/dev/null || echo development)

.PHONY: help install web-install web-dev web-build web-typecheck browser-install up api stack build ps down reset logs app-logs migrate lint typecheck test test-unit test-browser test-integration test-operations quality quality-load catalog-coverage slice validate-production-example validate-production staging-canary smoke-local rollback-verify restore-drill-local g1-request-mix notifier request-starter account-erasure change-delivery handoff-expiry lifecycle-invariants ingestion-commands ingestion-cadence workflow-worker catalog-refresh catalog-cadence fmt agent-repl agent-ask

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

install: ## Create the 3.12 venv and install deps (app + dev)
	$(UV) sync --locked --extra dev

web-install: ## Install the locked Next.js frontend dependencies
	cd web && npm ci

web-dev: ## Run the Next.js frontend on port 3001 (API must be on port 8000)
	cd web && npm run dev

web-build: ## Create the production Next.js frontend bundle
	cd web && npm run build

web-typecheck: ## Type-check the Next.js frontend
	cd web && npm run typecheck

browser-install: ## Install the Chromium binary used by the hermetic browser gate
	$(UV) run playwright install chromium

up: ## Start dependency services only (postgres, redis, temporal, Temporal UI)
	$(COMPOSE) up -d --wait postgres redis temporal temporal-ui

api: ## Build and start the containerized API plus its dependencies (app profile)
	EC_RELEASE_REVISION="$(LOCAL_RELEASE_REVISION)" $(COMPOSE) --profile app up -d --build --wait api

stack: ## Build and start the complete API, Next.js frontend, and worker stack
	EC_RELEASE_REVISION="$(LOCAL_RELEASE_REVISION)" $(COMPOSE) --profile app up -d --build --wait

build: ## Build the locked, non-root application image
	EC_RELEASE_REVISION="$(LOCAL_RELEASE_REVISION)" $(COMPOSE) --profile app build

ps: ## Show Compose service state
	$(COMPOSE) --profile app ps

down: ## Stop all services and networks; preserve database and claim-check volumes
	$(COMPOSE) --profile app down --remove-orphans

reset: ## DANGER: remove all services AND local data volumes; requires CONFIRM=reset
	@test "$(CONFIRM)" = "reset" || (echo "Refusing to delete local data. Re-run: make reset CONFIRM=reset"; exit 2)
	$(COMPOSE) --profile app down --volumes --remove-orphans

logs: ## Tail service logs
	$(COMPOSE) --profile app logs -f

app-logs: ## Tail frontend, API, and durable worker logs
	$(COMPOSE) --profile app logs -f frontend api workflow-worker request-starter account-erasure notifier change-delivery handoff-expiry lifecycle-invariants ingestion-commands ingestion-cadence

migrate: ## Apply database migrations against the running postgres
	EC_APP_ROLE_PASSWORD="$${EC_LOCAL_APP_ROLE_PASSWORD:-ec_app}" \
		EC_MIGRATION_URL=postgresql+psycopg://ec:ec@localhost:5433/ec $(UV) run alembic upgrade head

lint: ## Ruff lint
	$(UV) run ruff check src tests

fmt: ## Ruff format + autofix
	$(UV) run ruff format src tests && $(UV) run ruff check --fix src tests

typecheck: ## mypy strict
	$(UV) run mypy

agent-repl: ## Talk to the concierge agent locally against the running catalog
	$(UV) run --env-file .env python -m events_concierge.agent._devkit.repl

agent-ask: ## One-shot agent prompt: make agent-ask Q="what's on this weekend?"
	@test -n "$(Q)" || (echo 'usage: make agent-ask Q="your question"'; exit 2)
	$(UV) run --env-file .env python -m events_concierge.agent._devkit.repl --once "$(Q)"


test-unit: ## Fast unit tests (no external services)
	$(UV) run pytest -m "not integration and not browser_e2e"

test-browser: ## Run the hermetic consumer UI gate (requires make browser-install once)
	$(UV) run pytest tests/e2e -m browser_e2e

test-operations: ## Run deployment validation, canary, metrics, and restore-drill unit contracts
	$(UV) run pytest \
		tests/unit/test_operations_network_safety.py \
		tests/unit/test_production_config_validation.py \
		tests/unit/test_release_canary.py \
		tests/unit/test_metrics.py \
		tests/unit/test_api_operations.py \
		tests/unit/test_local_restore_drill.py \
		tests/unit/test_operations_cli.py

test-integration: up ## Integration tests in a disposable database against compose services
	EC_DATABASE_URL=postgresql+psycopg://ec_app:ec_app@localhost:5433/ec \
	EC_MIGRATION_URL=postgresql+psycopg://ec:ec@localhost:5433/ec \
	EC_TEMPORAL_TARGET=localhost:7234 \
	EC_REDIS_URL=redis://localhost:6380/0 \
	$(UV) run python -m tests.support.run_isolated_integration -- \
		-m "integration and not quality_load"

catalog-coverage: up migrate ## Report per-source admitted-catalog shape; fails on a dark or capped source
	EC_DATABASE_URL=postgresql+psycopg://ec_app:ec_app@localhost:5433/ec \
	$(UV) run python -m events_concierge.quality.catalog_coverage $(COVERAGE_ARGS)

quality: up ## Run the hermetic synthetic G1-style workflow quality matrix in a disposable database
	EC_DATABASE_URL=postgresql+psycopg://ec_app:ec_app@localhost:5433/ec \
	EC_MIGRATION_URL=postgresql+psycopg://ec:ec@localhost:5433/ec \
	EC_TEMPORAL_TARGET=localhost:7234 \
	EC_REDIS_URL=redis://localhost:6380/0 \
	$(UV) run python -m tests.support.run_isolated_integration -- \
		tests/integration/test_g1_quality_harness.py::test_g1_quality_harness_exercises_the_synthetic_workflow_matrix \
		-m integration

quality-load: up ## Run bounded offline P5b repetition coverage in a disposable database
	EC_DATABASE_URL=postgresql+psycopg://ec_app:ec_app@localhost:5433/ec \
	EC_MIGRATION_URL=postgresql+psycopg://ec:ec@localhost:5433/ec \
	EC_TEMPORAL_TARGET=localhost:7234 \
	EC_REDIS_URL=redis://localhost:6380/0 \
	EC_QUALITY_LOAD_REPEATS=5 \
	$(UV) run python -m tests.support.run_isolated_integration -- \
		tests/integration/test_g1_quality_harness.py -m "integration and quality_load"

test: test-unit ## Alias for the fast unit suite

slice: up ## Run the end-to-end vertical slice in a disposable database
	EC_DATABASE_URL=postgresql+psycopg://ec_app:ec_app@localhost:5433/ec \
	EC_MIGRATION_URL=postgresql+psycopg://ec:ec@localhost:5433/ec \
	EC_REDIS_URL=redis://localhost:6380/0 \
	$(UV) run python -m tests.support.run_isolated_integration -- \
		tests/integration/test_slice.py::test_documented_slice_demo_replays_against_a_persistent_catalog \
		-m integration

validate-production-example: ## Validate the versioned, credential-free production config contract
	$(UV) run python -m events_concierge.operations validate-config \
		--env-file deployment/production.env.example --structural-only

validate-production: ## Validate the real EC_ environment and load its runtime provider
	$(UV) run python -m events_concierge.operations validate-config

staging-canary: ## Verify a production-shaped deployment; requires BASE_URL and expected release identity
	@test -n "$(BASE_URL)" || (echo "BASE_URL is required"; exit 2)
	@test -n "$(EXPECTED_RELEASE_REVISION)" || (echo "EXPECTED_RELEASE_REVISION is required"; exit 2)
	@test -n "$(EXPECTED_IMAGE_DIGEST)" || (echo "EXPECTED_IMAGE_DIGEST is required"; exit 2)
	$(UV) run python -m events_concierge.operations canary \
		--base-url "$(BASE_URL)" \
		--expected-release-revision "$(EXPECTED_RELEASE_REVISION)" \
		--expected-image-digest "$(EXPECTED_IMAGE_DIGEST)"

smoke-local: stack ## Exercise the complete local image/worker stack without creating product state
	$(UV) run python -m events_concierge.operations canary \
		--base-url "http://127.0.0.1:$(or $(EC_API_PORT),8000)" \
		--allow-http --allow-local-mode

rollback-verify: staging-canary ## Prove the serving target matches the immutable rollback digest/revision

restore-drill-local: up migrate ## Dump/restore into an isolated DB, prove continuity/RLS, then destroy it
	$(UV) run python -m events_concierge.operations local-restore-drill --project-dir .

g1-request-mix: ## Measure deployed read-only lane mix; requires G1_CORPUS/LABEL/OUTPUT/BASE_URL and env auth
	@test -n "$(G1_CORPUS)" || (echo "G1_CORPUS is required"; exit 2)
	@test -n "$(G1_DEPLOYMENT_LABEL)" || (echo "G1_DEPLOYMENT_LABEL is required"; exit 2)
	@test -n "$(G1_OUTPUT)" || (echo "G1_OUTPUT is required"; exit 2)
	@test -n "$(G1_BASE_URL)" || (echo "G1_BASE_URL is required"; exit 2)
	@test -n "$$G1_TRUSTED_ORIGIN" || (echo "G1_TRUSTED_ORIGIN is required in the environment"; exit 2)
	@test -n "$$G1_AUTHORIZATION" || test -n "$$G1_COOKIE" || \
		(echo "G1_AUTHORIZATION or G1_COOKIE must be set in the environment"; exit 2)
	G1_BASE_URL="$(G1_BASE_URL)" $(UV) run python -m events_concierge.quality.request_mix \
		--corpus "$(G1_CORPUS)" \
		--deployment-label "$(G1_DEPLOYMENT_LABEL)" \
		--output "$(G1_OUTPUT)"

notifier: up migrate ## Run the durable outbox notifier worker against the configured NotificationPort
	EC_DATABASE_URL=postgresql+psycopg://ec_app:ec_app@localhost:5433/ec \
	EC_REDIS_URL=redis://localhost:6380/0 \
	$(UV) run python -m events_concierge.workers.notifier

request-starter: up migrate ## Run the durable EventRequest start-outbox worker against Temporal
	EC_DATABASE_URL=postgresql+psycopg://ec_app:ec_app@localhost:5433/ec \
	EC_TEMPORAL_TARGET=localhost:7234 \
	$(UV) run python -m events_concierge.workers.request_starter

account-erasure: up migrate ## Resume fenced account erasures independently of browser requests
	EC_DATABASE_URL=postgresql+psycopg://ec_app:ec_app@localhost:5433/ec \
	EC_TEMPORAL_TARGET=localhost:7234 \
	$(UV) run python -m events_concierge.workers.account_erasure

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

ingestion-commands: up migrate ## Process durable local ingestion-admin commands
	EC_DATABASE_URL=postgresql+psycopg://ec_app:ec_app@localhost:5433/ec \
	EC_REDIS_URL=redis://localhost:6380/0 \
	EC_TEMPORAL_TARGET=localhost:7234 \
	EC_PACER_BACKEND=redis \
	EC_ADMIN_INGESTION_ENABLED=true \
	$(UV) run python -m events_concierge.workers.ingestion_commands

ingestion-cadence: up migrate ## Schedule due local sources into the durable ingestion queue
	EC_DATABASE_URL=postgresql+psycopg://ec_app:ec_app@localhost:5433/ec \
	EC_ADMIN_INGESTION_ENABLED=true \
	EC_CATALOG_INGESTION_SCHEDULER_ENABLED=true \
	$(UV) run python -m events_concierge.workers.ingestion_cadence

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
