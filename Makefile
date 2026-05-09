# Scaffold Engine — Developer Makefile
# Run targets from project root: ~/scaffold-engine/

CONTAINER := scaffold-orchestrator
COMPOSE   := docker compose
API_KEY   ?= $(SCAFFOLD_API_KEY)
API_URL   ?= http://localhost:8000

.PHONY: test test-cli test-sdk agent eval bench build build-dev logs logs-follow logs-errors logs-jobs logs-research logs-since restart dev-up migrate clean clean-pyc status status-raw health ci help bootstrap doctor doctor-explain init sync-valves sync-api-key costs reindex openapi-snapshot openapi-check sync-schemas idea resume explain whatnow confirm retry skip node-logs config

## ──────────────────────────────────────────────
## Testing
## ──────────────────────────────────────────────

test: ## Run all tests in Docker (~1226 passing, 4 skipped)
	docker exec $(CONTAINER) pytest tests/ --timeout=30 -v

test-cli: ## Run scaffold CLI tests (cli/tests/) inside the dev container
	docker exec $(CONTAINER) sh -c "cd /code/cli && python -m pytest tests/ --timeout=10 -v"

test-sdk: ## Run scaffold SDK tests (sdk/tests/) inside the dev container
	docker exec $(CONTAINER) sh -c "cd /code/sdk && python -m pytest tests/ --timeout=10 -v"

agent: ## Run execution agent tests only
	docker exec $(CONTAINER) pytest tests/test_execution_agent.py -m smoke --timeout=30 -v

eval: ## Run retrieval eval against ground truth
	docker exec -e SCAFFOLD_API_KEY=$(API_KEY) $(CONTAINER) python3 tests/eval_retrieval.py

bench: ## Run full e2e performance benchmark (~43 min)
	docker exec $(CONTAINER) python3 tests/benchmarks/bench_pipeline.py

bench-rag: ## Run RAG retrieval micro-bench (no LLM, ~30s)
	docker exec $(CONTAINER) python3 tests/benchmarks/bench_rag.py

bench-embed: ## Run embedder + cache micro-bench (~30s)
	docker exec $(CONTAINER) python3 tests/benchmarks/bench_embed.py

bench-check-rag: ## Gate: fail if bench_rag warm_mean_ms regressed >1.5x median of last 3
	docker exec $(CONTAINER) python3 tests/benchmarks/bench_check.py \
		--file tests/benchmarks/bench_rag_results.jsonl \
		--metric summary.warm_mean_ms --threshold 1.5 --direction up

bench-check-embed: ## Gate: fail if bench_embed cold_mean_ms regressed >1.5x median of last 3
	docker exec $(CONTAINER) python3 tests/benchmarks/bench_check.py \
		--file tests/benchmarks/bench_embed_results.jsonl \
		--metric summary.cold_mean_ms --threshold 1.5 --direction up

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

doctor-explain: ## Same as doctor, but with a one-liner per check explaining what it verifies
	@bash scripts/doctor.sh --explain

init: ## Provider/model wizard: pick per-role provider + collect API keys, update .env
	@bash scripts/init.sh

sync-valves: ## Wipe baked-in api_key from pipelines/*/valves.json (.env becomes single source)
	@bash scripts/sync_valves.sh

sync-api-key: ## Strict-sync SCAFFOLD_API_KEY across .env + valves.json + ~/.bashrc (use: make sync-api-key [KEY=sk-...])
	@bash scripts/sync_api_key.sh $(KEY)

costs: ## Top-N most-expensive jobs from llm_call_logs (J.3) — defaults to 10 (use: make costs [N=20])
	@bash scripts/costs_rollup.sh

reindex: ## Re-embed the toon_v2 corpus (after switching MODEL_EMBEDDER_PIPELINE)
	docker exec -it $(CONTAINER) python scripts/reindex.py $(REINDEX_ARGS)

## ──────────────────────────────────────────────
## Project convenience (Sprint U.4)
## ──────────────────────────────────────────────

idea: ## Submit an idea via scaffold project new (use: make idea TEXT="...")
	@if [ -z "$(TEXT)" ]; then \
		echo "Usage: make idea TEXT=\"your idea here\""; \
		exit 2; \
	fi
	docker exec $(CONTAINER) sh -c "cd /code/cli && python -m scaffold_cli.main project new \"$(TEXT)\""

resume: ## Resume a project via scaffold project resume (use: make resume ID=<nickname-or-uuid>)
	@if [ -z "$(ID)" ]; then \
		echo "Usage: make resume ID=<nickname-or-uuid>"; \
		exit 2; \
	fi
	docker exec $(CONTAINER) sh -c "cd /code/cli && python -m scaffold_cli.main project resume $(ID)"

explain: ## Explain a job status (use: make explain STATUS=<name>; omit for the list)
	@if [ -z "$(STATUS)" ]; then \
		docker exec $(CONTAINER) sh -c "cd /code/cli && python -m scaffold_cli.main explain"; \
	else \
		docker exec $(CONTAINER) sh -c "cd /code/cli && python -m scaffold_cli.main explain $(STATUS)"; \
	fi

whatnow: ## Show every job that needs attention + its recommended next step
	docker exec $(CONTAINER) sh -c "cd /code/cli && python -m scaffold_cli.main whatnow"

confirm: ## Confirm a job (use: make confirm ID=<nickname-or-uuid> [CHAIN=1])
	@if [ -z "$(ID)" ]; then \
		echo "Usage: make confirm ID=<nickname-or-uuid> [CHAIN=1]"; \
		exit 2; \
	fi
	docker exec $(CONTAINER) sh -c "cd /code/cli && python -m scaffold_cli.main confirm $(ID) $(if $(CHAIN),--chain,)"

retry: ## Retry a failed/blocked node (use: make retry ID=<id> NODE=<key>)
	@if [ -z "$(ID)" ] || [ -z "$(NODE)" ]; then \
		echo "Usage: make retry ID=<job_id> NODE=<node_key>"; \
		exit 2; \
	fi
	docker exec $(CONTAINER) sh -c "cd /code/cli && python -m scaffold_cli.main exec retry $(ID) $(NODE)"

skip: ## Mark a node as skipped (use: make skip ID=<id> NODE=<key>)
	@if [ -z "$(ID)" ] || [ -z "$(NODE)" ]; then \
		echo "Usage: make skip ID=<job_id> NODE=<node_key>"; \
		exit 2; \
	fi
	docker exec $(CONTAINER) sh -c "cd /code/cli && python -m scaffold_cli.main skip $(ID) $(NODE)"

node-logs: ## Show per-node DAG state for a job (vs `make logs` which tails the container)
	@if [ -z "$(ID)" ]; then \
		echo "Usage: make node-logs ID=<job_id>"; \
		echo "(For container-wide logs use 'make logs' / 'make logs-jobs'.)"; \
		exit 2; \
	fi
	docker exec $(CONTAINER) sh -c "cd /code/cli && python -m scaffold_cli.main logs $(ID)"

config: ## Show orchestrator config (use: make config; or make config FILTER=model)
	@if [ -z "$(FILTER)" ]; then \
		docker exec $(CONTAINER) sh -c "cd /code/cli && python -m scaffold_cli.main config show"; \
	else \
		docker exec $(CONTAINER) sh -c "cd /code/cli && python -m scaffold_cli.main config show --filter $(FILTER)"; \
	fi

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

build: ## Rebuild scaffold-engine:${SCAFFOLD_IMAGE_TAG:-local} (prod) and restart orchestrator. Explicit rebuild gate — `compose up` no longer auto-rebuilds.
	$(COMPOSE) up -d --build $(CONTAINER)

build-dev: ## Rebuild scaffold-engine:dev and restart orchestrator under the dev overlay.
	$(COMPOSE) -f docker-compose.yml -f docker-compose.dev.yml up -d --build $(CONTAINER)

logs: ## Tail orchestrator logs (last 50 lines)
	docker logs $(CONTAINER) --tail=50

logs-follow: ## Follow orchestrator logs in real time
	docker logs $(CONTAINER) -f

logs-errors: ## Show only ERROR + WARNING lines from the last 500
	@docker logs $(CONTAINER) --tail=500 2>&1 | python3 -c "import json,sys; [print(l) for l in sys.stdin if any(t in l for t in ['\"error\"','\"warning\"','ERROR','WARNING','Traceback'])]"

logs-jobs: ## Show job-lifecycle events from the last 500 lines
	@docker logs $(CONTAINER) --tail=500 2>&1 | python3 -c "import sys; [print(l) for l in sys.stdin if any(t in l for t in ['job_created','job_failed','job_autocompleted','job_refined','dag_generated','pipeline_completed','node_execution','stale_job_cleaned'])]"

logs-research: ## Show research-agent events from the last 500 lines
	@docker logs $(CONTAINER) --tail=500 2>&1 | python3 -c "import sys; [print(l) for l in sys.stdin if any(t in l for t in ['research_started','research_complete','research_resumed','iteration_','search_complete','extraction_complete','ingestion_complete','convergence','awaiting_reply','contradictions_detected'])]"

logs-since: ## Show logs since a given timestamp (use: make logs-since SINCE=1h, or SINCE='2026-05-07T16:00:00')
	@if [ -z "$(SINCE)" ]; then \
		echo "Usage: make logs-since SINCE=<duration-or-iso-timestamp>"; \
		echo "Examples: make logs-since SINCE=1h    make logs-since SINCE=10m"; \
		exit 2; \
	fi
	docker logs $(CONTAINER) --since=$(SINCE)

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

status: ## Query /status: status counts table + recent jobs + next-step hint
	@if [ -z "$(API_KEY)" ]; then \
		echo "ERROR: Set SCAFFOLD_API_KEY env var first"; \
		exit 1; \
	fi
	@curl -s $(API_URL)/status -H "X-API-Key: $(API_KEY)" | python3 scripts/render_status.py

status-raw: ## Query /status and dump raw JSON (machine-readable form of `make status`)
	@if [ -z "$(API_KEY)" ]; then \
		echo "ERROR: Set SCAFFOLD_API_KEY env var first"; \
		exit 1; \
	fi
	@curl -s $(API_URL)/status -H "X-API-Key: $(API_KEY)" | python3 -m json.tool

health: ## Query /health endpoint (no auth required)
	curl -s $(API_URL)/health | python3 -m json.tool

## ──────────────────────────────────────────────
## Help
## ──────────────────────────────────────────────

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

.DEFAULT_GOAL := help
