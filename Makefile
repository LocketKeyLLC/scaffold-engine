# Scaffold Engine — In-container test runner
# Usage: docker exec scaffold-orchestrator make -C /app smoke
SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c

PYTEST := python -m pytest
PYTEST_FLAGS := --strict-markers --strict-config -ra --tb=short

ARGS ?=

.DEFAULT_GOAL := smoke

.PHONY: smoke validate full eval clean

# Tier 1: Fast sanity — unit tests, extraction pipeline (<2 min)
smoke:
	$(PYTEST) $(PYTEST_FLAGS) -m smoke $(ARGS)

# Tier 2: Integration — API, CRUD, reranker, verifier (<15 min)
validate:
	$(PYTEST) $(PYTEST_FLAGS) -m validate $(ARGS)

# Tier 3: Everything including unmarked tests (<45 min)
full:
	$(PYTEST) $(PYTEST_FLAGS) $(ARGS)

# Retrieval evaluation (ground-truth fixture, 40 queries)
eval:
	python /app/tests/eval_retrieval.py --verbose

clean:
	rm -rf .pytest_cache
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
# ──────────────────────────────────────────────────────────
# CI TARGETS — append these to the existing Makefile
# ──────────────────────────────────────────────────────────

# Tier 1: Cloud-safe unit tests (no Milvus, no Postgres, no Ollama)
# Runs: test_verify_extraction.py (24 tests)
# Skips: anything marked @pytest.mark.validate or requiring live services
# Safe for: GitHub Actions free runners, `act`, bare laptop
ci-smoke:
	PYTHONPATH=. pytest tests/test_verify_extraction.py \
		-m "smoke" \
		-x -v \
		--timeout=30 \
		--tb=short \
		-q

# Tier 2: Full stack via docker exec (local convenience)
# Requires: milvus-standalone + scaffold-orchestrator + scaffold-postgres running
ci-local:
	docker exec scaffold-orchestrator make -C /app smoke
	docker exec scaffold-orchestrator make -C /app validate

# Tier 3: Full eval via docker exec
ci-eval:
	docker exec scaffold-orchestrator make -C /app eval

# All tiers local
ci-full: ci-local ci-eval

.PHONY: ci-smoke ci-local ci-eval ci-full

# ── Benchmarks ──────────────────────────────────────────────────────────────

.PHONY: bench bench-compare bench-history

bench: ## Run full performance benchmark (raw inference + pipeline)
	@echo "═══ Running Scaffold Engine benchmark... ═══"
	python3 tests/benchmarks/bench_pipeline.py
	@echo ""
	@echo "Results appended to tests/benchmarks/results.jsonl"

bench-compare: ## Compare last 2 benchmark runs for regressions
	python3 tests/benchmarks/bench_compare.py --last 2

bench-history: ## Show last 5 benchmark runs
	python3 tests/benchmarks/bench_compare.py --last 5
