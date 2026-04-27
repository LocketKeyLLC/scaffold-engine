# Scaffold Engine — Developer Makefile
# Run targets from project root: ~/scaffold-engine/

CONTAINER := scaffold-orchestrator
COMPOSE   := docker compose
API_KEY   ?= $(SCAFFOLD_API_KEY)
API_URL   ?= http://localhost:8000

.PHONY: test agent eval bench build logs clean status health ci help

## ──────────────────────────────────────────────
## Testing
## ──────────────────────────────────────────────

test: ## Run all tests in Docker (~547 passing, 31 skipped)
	docker exec $(CONTAINER) pytest tests/ --timeout=30 -v

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
## Build & Ops
## ──────────────────────────────────────────────

build: ## Rebuild and restart the orchestrator container
	$(COMPOSE) up -d --build $(CONTAINER)

logs: ## Tail orchestrator logs (last 50 lines)
	docker logs $(CONTAINER) --tail=50

logs-follow: ## Follow orchestrator logs in real time
	docker logs $(CONTAINER) -f

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

migrate: ## Apply pending DB migrations inside the orchestrator container
	docker exec scaffold-orchestrator python -m app.migrations
