# Scaffold Engine — Developer Makefile
# Run targets from project root: ~/scaffold-engine/

CONTAINER := scaffold-orchestrator
COMPOSE   := docker compose
API_KEY   ?= $(SCAFFOLD_API_KEY)
API_URL   ?= http://localhost:8000

.PHONY: test test-cli test-sdk agent eval bench build logs logs-follow restart dev-up migrate clean clean-pyc status health ci help bootstrap doctor init sync-valves reindex openapi-snapshot openapi-check sync-schemas

## ──────────────────────────────────────────────
## Testing
## ──────────────────────────────────────────────

test: ## Run all tests in Docker (~745 passing, 5 skipped)
	docker exec $(CONTAINER) pytest tests/ --timeout=30 -v

test-cli: ## Run scaffold CLI tests (cli/tests/) inside the dev container
	docker exec $(CONTAINER) sh -c "cd /code/cli && python -m pytest tests/ --timeout=10 -v"

test-sdk: ## Run scaffold SDK tests (sdk/tests/) inside the dev container
	docker exec $(CONTAINER) sh -c "cd /code/sdk && python -m pytest tests/ --timeout=10 -v"

agent: ## Run execution agent tests only
	docker exec $(CONTAINER) pytest tests/test_execution_agent.py -m smoke --timeout=30 -v

eval: ## Run retrieval eval against ground truth
	docker exec -e SCAFFOLD_API_KEY=$(API_KEY) $(CONTAINER) python3 tests/eval_retrieval.py

bench: ## Run performance benchmark suite
	docker exec $(CONTAINER) python3 tests/benchmarks/bench_pipeline.py

ci: ## Run CI-safe tests (no live Ollama/Milvus needed)
	docker exec $(CONTAINER) pytest tests/ --timeout=30 -v \
		--ignore=tests/eval_retrieval.py \
		-m "not validate"

## ──────────────────────────────────────────────
## Setup
## ──────────────────────────────────────────────

bootstrap: ## First-time setup: generate .env, create network/volumes, build + start stack
	@bash scripts/bootstrap.sh $(BOOTSTRAP_ARGS)

doctor: ## Health audit: probe every dep + verify key sync (read-only)
	@bash scripts/doctor.sh

init: ## Provider/model wizard: pick per-role provider + collect API keys, update .env
	@bash scripts/init.sh

sync-valves: ## Wipe baked-in api_key from pipelines/*/valves.json (.env becomes single source)
	@bash scripts/sync_valves.sh

reindex: ## Re-embed the toon_v2 corpus (after switching MODEL_EMBEDDER_PIPELINE)
	docker exec -it $(CONTAINER) python scripts/reindex.py $(REINDEX_ARGS)

openapi-snapshot: ## Regenerate docs/openapi.json from the live FastAPI app
	@docker exec $(CONTAINER) python scripts/openapi_snapshot.py > docs/openapi.json.tmp && \
		mv docs/openapi.json.tmp docs/openapi.json && \
		echo "Wrote docs/openapi.json ($$(wc -c < docs/openapi.json) bytes)."

openapi-check: ## Verify docs/openapi.json matches the live spec (CI gate)
	docker exec $(CONTAINER) python scripts/openapi_snapshot.py --check

sync-schemas: ## Refresh sdk/scaffold_client/schemas.py from app/schemas.py (byte-equal vendor)
	cp app/schemas.py sdk/scaffold_client/schemas.py
	@echo "Vendored sdk/scaffold_client/schemas.py from app/schemas.py."

## ──────────────────────────────────────────────
## Build & Ops
## ──────────────────────────────────────────────

build: ## Rebuild and restart the orchestrator container
	$(COMPOSE) up -d --build $(CONTAINER)

logs: ## Tail orchestrator logs (last 50 lines)
	docker logs $(CONTAINER) --tail=50

logs-follow: ## Follow orchestrator logs in real time
	docker logs $(CONTAINER) -f

restart: ## Restart the orchestrator (no rebuild)
	$(COMPOSE) restart $(CONTAINER)

dev-up: ## Bring up orchestrator with the dev image (mounts pipelines/, Dockerfile, .github/)
	$(COMPOSE) -f docker-compose.yml -f docker-compose.dev.yml up -d $(CONTAINER)

migrate: ## Apply pending DB migrations inside the orchestrator container
	docker exec $(CONTAINER) python -m app.migrations

clean-pyc: ## Drop stale .pyc / __pycache__ in repo and inside the dev container
	find . -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
	find . -name '*.pyc' -delete 2>/dev/null || true
	-docker exec $(CONTAINER) find /code -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null
	@echo "Removed .pyc / __pycache__ from host and container."

## ──────────────────────────────────────────────
## API Operations
## ──────────────────────────────────────────────

clean: ## Cleanup stale jobs via API
	@if [ -z "$(API_KEY)" ]; then \
		echo "ERROR: Set SCAFFOLD_API_KEY env var first"; \
		exit 1; \
	fi
	curl -s -X POST $(API_URL)/jobs/cleanup \
		-H "X-API-Key: $(API_KEY)" | python3 -m json.tool

status: ## Query /status endpoint
	@if [ -z "$(API_KEY)" ]; then \
		echo "ERROR: Set SCAFFOLD_API_KEY env var first"; \
		exit 1; \
	fi
	curl -s $(API_URL)/status \
		-H "X-API-Key: $(API_KEY)" | python3 -m json.tool

health: ## Query /health endpoint (no auth required)
	curl -s $(API_URL)/health | python3 -m json.tool

## ──────────────────────────────────────────────
## Help
## ──────────────────────────────────────────────

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

.DEFAULT_GOAL := help
