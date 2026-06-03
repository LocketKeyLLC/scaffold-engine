# Scaffold Engine — Developer Makefile
# Run targets from project root: ~/scaffold-engine/

CONTAINER := scaffold-orchestrator
COMPOSE   := docker compose
API_KEY   ?= $(SCAFFOLD_API_KEY)
API_URL   ?= http://localhost:8000

.PHONY: _ensure_dev test test-cli test-sdk agent eval bench bench-rag bench-embed bench-check bench-check-rag bench-check-embed bench-check-pipeline build build-dev logs logs-follow logs-errors logs-jobs logs-research logs-since restart dev-up migrate clean clean-pyc status status-raw health ci help bootstrap bootstrap-host bootstrap-host-check doctor doctor-explain init sync-valves sync-api-key costs reindex openapi-snapshot openapi-check sync-schemas check-schemas sync-sse-events check-sse-events sync-next-actions check-next-actions check-rerank-drift ci-tier-0 ci-tier-2 hooks-install idea resume explain whatnow confirm retry skip node-logs config audit

## ──────────────────────────────────────────────
## Testing
## ──────────────────────────────────────────────

# Audit B5 — `make test` (and siblings that exec pytest) must run in the
# dev image. The prod runtime image strips tests/, pytest, and the
# pipelines/ tree (§17.62 hermetic compose), so running pytest there
# silently skips ~22 cases and emits PytestCacheWarning against the
# read-only rootfs. This guard auto-switches the orchestrator container
# to scaffold-engine:dev (via the dev compose overlay) when needed; it's
# a no-op when dev is already loaded. After the test run the user can
# flip back to prod via `make build` (or `docker compose up -d`).
_ensure_dev:
	@if docker inspect $(CONTAINER) --format '{{.Config.Image}}' 2>/dev/null | grep -q ':dev$$'; then \
		printf '\033[2m✓ dev image already loaded\033[0m\n'; \
	else \
		printf '\033[1;36m→ switching scaffold-orchestrator to dev image (B5)\033[0m\n'; \
		$(COMPOSE) -f docker-compose.yml -f docker-compose.dev.yml up -d $(CONTAINER); \
		until docker ps --filter name=^$(CONTAINER)$$ --format '{{.Status}}' | grep -qE 'healthy|Up'; do sleep 2; done; \
	fi

test: _ensure_dev ## Run all tests in dev image (~1340 passing, ~8 skipped post-§17.63)
	docker exec $(CONTAINER) pytest tests/ --timeout=30 -v

test-cli: _ensure_dev ## Run scaffold CLI tests (cli/tests/) inside the dev container
	docker exec $(CONTAINER) sh -c "cd /code/cli && python -m pytest tests/ --timeout=10 -v"

test-sdk: _ensure_dev ## Run scaffold SDK tests (sdk/tests/) inside the dev container
	docker exec $(CONTAINER) sh -c "cd /code/sdk && python -m pytest tests/ --timeout=10 -v"

agent: _ensure_dev ## Run execution agent tests only (dev image)
	docker exec $(CONTAINER) pytest tests/test_execution_agent.py -m smoke --timeout=30 -v

# §17.358 — `make eval` removed. tests/eval_retrieval.py + tests/ground_truth.json
# were retired (Tier-2 #15 from §17.29). The canonical retrieval eval is now
# `scripts/score_retrieval.py` against `tests/fixtures/golden_set.json`,
# wired into `make ci-tier-2` step 4/5 and the §17.354 quarterly cron.

bench: ## Run full e2e performance benchmark (~43 min)
	docker exec $(CONTAINER) python3 tests/benchmarks/bench_pipeline.py

bench-rag: ## Run RAG retrieval micro-bench (no LLM, ~30s)
	docker exec $(CONTAINER) python3 tests/benchmarks/bench_rag.py

bench-embed: ## Run embedder + cache micro-bench (~30s)
	docker exec $(CONTAINER) python3 tests/benchmarks/bench_embed.py

bench-check-rag: _ensure_dev ## Gate: fail if bench_rag warm_mean_ms regressed >1.5x median of last 3
	docker exec $(CONTAINER) python3 tests/benchmarks/bench_check.py \
		--file tests/benchmarks/bench_rag_results.jsonl \
		--metric summary.warm_mean_ms --threshold 1.5 --direction up

# §17.352 — per-stage gates. summary.stage.* keys land on schema_version
# 1.1 (bench_rag.py post-§17.352); pre-1.1 runs lack the keys so
# bench_check.py's "insufficient history" skip kicks in until two
# post-§17.352 runs accumulate.
bench-check-rag-embed: _ensure_dev ## Gate: bench_rag embed-stage warm mean regressed >1.5x median
	docker exec $(CONTAINER) python3 tests/benchmarks/bench_check.py \
		--file tests/benchmarks/bench_rag_results.jsonl \
		--metric summary.stage.embed_warm_mean_ms --threshold 1.5 --direction up

bench-check-rag-search: _ensure_dev ## Gate: bench_rag Milvus parallel-search warm mean regressed >1.5x median
	docker exec $(CONTAINER) python3 tests/benchmarks/bench_check.py \
		--file tests/benchmarks/bench_rag_results.jsonl \
		--metric summary.stage.search_parallel_warm_mean_ms --threshold 1.5 --direction up

bench-check-rag-rerank: _ensure_dev ## Gate: bench_rag reranker per-pair warm mean regressed >1.5x median
	docker exec $(CONTAINER) python3 tests/benchmarks/bench_check.py \
		--file tests/benchmarks/bench_rag_results.jsonl \
		--metric summary.stage.rerank_per_pair_warm_mean_ms --threshold 1.5 --direction up

bench-check-embed: _ensure_dev ## Gate: fail if bench_embed cold_mean_ms regressed >1.5x median of last 3
	docker exec $(CONTAINER) python3 tests/benchmarks/bench_check.py \
		--file tests/benchmarks/bench_embed_results.jsonl \
		--metric summary.cold_mean_ms --threshold 1.5 --direction up

bench-check-pipeline: _ensure_dev ## Gate: fail if bench_pipeline total_pipeline_s regressed >1.5x median of last 3
	docker exec $(CONTAINER) python3 tests/benchmarks/bench_check.py \
		--file tests/benchmarks/results.jsonl \
		--metric pipeline.total_pipeline_s --threshold 1.5 --direction up

# Audit I4 — aggregate gate. Runs all three core regression checks; fails
# on the first regression. Each sub-gate skips gracefully (exit 0) when
# its JSONL file is missing or has fewer than 2 prior runs, so this
# target is safe to wire into `make ci` / `make ci-tier-2` even on a
# fresh repo with no bench history yet.
#
# §17.352 — per-stage rag gates (embed / search / rerank-per-pair) are
# included so stage-level drift catches before the aggregate moves. They
# skip on schema_version 1.0 rows; activate once two 1.1+ runs land.
bench-check: bench-check-rag bench-check-rag-embed bench-check-rag-search bench-check-rag-rerank bench-check-embed bench-check-pipeline ## Gate: run every bench-check; skips gates whose JSONL file is missing or sparse

rebaseline: _ensure_dev ## §17.354 — Quarterly perf re-baseline. Runs bench-rag + bench-embed + bench-pipeline + bench-check in sequence. ~20-45 min wall-clock depending on hardware. See docs/rebaseline-runbook.md.
	@set -euo pipefail; \
	printf '\033[1;36m== §17.354 quarterly perf re-baseline ==\033[0m\n'; \
	printf '\033[2m(see docs/rebaseline-runbook.md for what to do on regression)\033[0m\n'; \
	t_start=$$(date +%s); \
	printf '\n\033[1;36m-- step 1/4: bench-rag (component, ~10 min) --\033[0m\n'; \
	$(MAKE) bench-rag; \
	printf '\n\033[1;36m-- step 2/4: bench-embed (embedder + cache, ~30 s) --\033[0m\n'; \
	$(MAKE) bench-embed; \
	printf '\n\033[1;36m-- step 3/4: bench-pipeline (e2e, ~5 min) --\033[0m\n'; \
	$(MAKE) bench; \
	printf '\n\033[1;36m-- step 4/4: bench-check (regression gate) --\033[0m\n'; \
	$(MAKE) bench-check; \
	t_end=$$(date +%s); \
	printf '\n\033[1;32m✓ rebaseline complete in %d min %d s\033[0m\n' $$(( (t_end - t_start) / 60 )) $$(( (t_end - t_start) % 60 )); \
	printf '\033[2mNext steps: review the new rows in tests/benchmarks/{results,bench_rag_results,bench_embed_results}.jsonl;\033[0m\n'; \
	printf '\033[2mif bench-check flagged a regression, see docs/rebaseline-runbook.md \"On regression\".\033[0m\n'

ci-smoke: ## Cloud-CI smoke tests — host pytest on `-m smoke`, no docker, no live services. Used by .github/workflows/ci.yml.
	# §17.177 — SCAFFOLD_PREWARM_RERANKER=false skips the lifespan
	# CrossEncoder prewarm. Tests that use `with TestClient(app) as c:`
	# trigger the lifespan; in cloud CI without sentence_transformers
	# (not in requirements-ci.txt) + no pre-warmed HF cache, the prewarm
	# can stall past pytest-timeout (30 s). Reranker has no role in
	# smoke tests, so prewarm is pure waste.
	SCAFFOLD_CI_SMOKE_MODE=1 SCAFFOLD_PREWARM_RERANKER=false pytest tests/ -m smoke --timeout=30 -v

ci: _ensure_dev ## Run CI-safe tests (no live services; dev image) + bench regression gates (skip on missing/sparse history)
	docker exec $(CONTAINER) pytest tests/ --timeout=30 -v \
		-m "not validate"
	@printf '\n--- Audit I4: bench regression gates ---\n'
	$(MAKE) bench-check

ci-tier-0: check-schemas check-sse-events check-next-actions check-rerank-drift ## §17.393 — Fast static-parity gates (NO docker, NO live services, ~2s). Pre-push hook target. The 4 prereqs are byte-equal/grep gates; the recipe adds the host static-scan inventory tests. Bypass a one-off push with `git push --no-verify`.
	@printf '\033[1m▶ static-scan inventory tests (host pytest, --noconftest)\033[0m\n'
	@if command -v pytest >/dev/null 2>&1; then \
		PYTHONPATH=$(CURDIR):$(CURDIR)/sdk pytest \
			tests/test_sse_event_inventory.py \
			tests/test_sdk_schema_parity.py \
			--noconftest -o addopts="" -p no:cacheprovider -q || exit 1; \
	else \
		printf '\033[1;33m⚠ host pytest not found — skipped the 2 inventory scans (byte-equal gates above still ran). Full coverage: make test\033[0m\n'; \
	fi
	@printf '\033[1;32m✓ ci-tier-0 passed (static parity gates green)\033[0m\n'

audit: _ensure_dev ## §17.97 — CVE scan against pinned deps via pip-audit (dev image). Scans requirements.txt + requirements-ci.txt + requirements-dev.txt. Non-zero exit on a known vulnerability; pass a tag through ARGS to ignore one (e.g. ARGS="--ignore-vuln GHSA-xxxx").
	@for f in requirements.txt requirements-ci.txt requirements-dev.txt; do \
		printf '\n\033[1;36m== pip-audit -r %s ==\033[0m\n' "$$f"; \
		docker exec $(CONTAINER) pip-audit --strict --disable-pip -r "/code/$$f" $(ARGS) || exit $$?; \
	done

## ──────────────────────────────────────────────
## Setup
## ──────────────────────────────────────────────

bootstrap: ## First-time setup: generate .env, create network/volumes, build + start stack
	@bash scripts/bootstrap.sh $(BOOTSTRAP_ARGS)

bootstrap-host: ## Audit I1: host-level setup audit (SSD mount, daemon.json, ai-network pin, volume chown). Run BEFORE `make bootstrap` on a fresh host.
	@bash scripts/bootstrap-host.sh

bootstrap-host-check: ## Same as bootstrap-host, but read-only — no changes applied.
	@bash scripts/bootstrap-host.sh check

doctor: ## Health audit: probe every dep + verify key sync + cold-backup mount guard (read-only, 11 sections)
	@bash scripts/doctor.sh

doctor-explain: ## Same as doctor, but with a one-liner per check explaining what it verifies
	@bash scripts/doctor.sh --explain

init: ## Provider/model wizard: pick per-role provider + collect API keys, update .env
	@bash scripts/init.sh

hooks-install: ## §17.393 — Activate repo git hooks (sets core.hooksPath=.githooks). Run once per clone. Pre-push then runs `make ci-tier-0`.
	@git config core.hooksPath .githooks
	@printf '\033[1;32m✓ git hooks activated\033[0m (core.hooksPath=.githooks). Pre-push now runs `make ci-tier-0`. Bypass a one-off with `git push --no-verify`.\n'

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

check-schemas: ## §17.186 — Verify sdk/scaffold_client/schemas.py is byte-equal to app/schemas.py (CI gate)
	@if ! diff -q app/schemas.py sdk/scaffold_client/schemas.py >/dev/null 2>&1; then \
		printf '\033[1;31m✗ sdk/scaffold_client/schemas.py has drifted from app/schemas.py.\033[0m\n'; \
		printf '\033[2m  Diff (first 40 lines):\033[0m\n'; \
		diff -u app/schemas.py sdk/scaffold_client/schemas.py | head -40 || true; \
		printf '\033[1;33m  Fix: `make sync-schemas` then commit the regenerated file.\033[0m\n'; \
		exit 1; \
	fi
	@echo "✓ sdk/scaffold_client/schemas.py is in sync with app/schemas.py."

sync-sse-events: ## §17.190 — Refresh pipelines/_vendor/_sse_events.py from app/sse_events.py (byte-equal vendor)
	cp app/sse_events.py pipelines/_vendor/_sse_events.py
	@echo "Vendored pipelines/_vendor/_sse_events.py from app/sse_events.py."

check-sse-events: ## §17.190 — Verify pipelines/_vendor/_sse_events.py is byte-equal to app/sse_events.py (CI gate)
	@if ! diff -q app/sse_events.py pipelines/_vendor/_sse_events.py >/dev/null 2>&1; then \
		printf '\033[1;31m✗ pipelines/_vendor/_sse_events.py has drifted from app/sse_events.py.\033[0m\n'; \
		printf '\033[2m  Diff (first 40 lines):\033[0m\n'; \
		diff -u app/sse_events.py pipelines/_vendor/_sse_events.py | head -40 || true; \
		printf '\033[1;33m  Fix: `make sync-sse-events` then commit the regenerated file.\033[0m\n'; \
		exit 1; \
	fi
	@echo "✓ pipelines/_vendor/_sse_events.py is in sync with app/sse_events.py."

sync-next-actions: ## §17.195 — Refresh pipelines/_vendor/_next_actions.py from sdk/scaffold_client/next_actions.py (byte-equal vendor)
	cp sdk/scaffold_client/next_actions.py pipelines/_vendor/_next_actions.py
	@echo "Vendored pipelines/_vendor/_next_actions.py from sdk/scaffold_client/next_actions.py."

check-next-actions: ## §17.195 — Verify pipelines/_vendor/_next_actions.py is byte-equal to sdk/scaffold_client/next_actions.py (CI gate)
	@if ! diff -q sdk/scaffold_client/next_actions.py pipelines/_vendor/_next_actions.py >/dev/null 2>&1; then \
		printf '\033[1;31m✗ pipelines/_vendor/_next_actions.py has drifted from sdk/scaffold_client/next_actions.py.\033[0m\n'; \
		printf '\033[2m  Diff (first 40 lines):\033[0m\n'; \
		diff -u sdk/scaffold_client/next_actions.py pipelines/_vendor/_next_actions.py | head -40 || true; \
		printf '\033[1;33m  Fix: `make sync-next-actions` then commit the regenerated file.\033[0m\n'; \
		exit 1; \
	fi
	@echo "✓ pipelines/_vendor/_next_actions.py is in sync with sdk/scaffold_client/next_actions.py."

ci-tier-2: ## §17.247 — Integration check: full-stack doctor + drift gate + golden retrieval sidecar + bench regression gates (§17.352). Runs locally OR via self-hosted CI; requires the orchestrator + Milvus + Postgres + Redis + Ollama stack to be live.
	@set -euo pipefail; \
	printf '\033[1;36m== §17.247 tier 2 — full-stack integration ==\033[0m\n'; \
	printf '\033[1;36m-- step 1/5: orchestrator /health --\033[0m\n'; \
	if ! curl -sf --max-time 5 http://localhost:8000/health >/dev/null; then \
		printf '\033[1;31m✗ orchestrator /health unreachable\033[0m  Fix: docker compose up -d scaffold-orchestrator\n'; \
		exit 1; \
	fi; \
	echo "  ✓ orchestrator healthy"; \
	printf '\033[1;36m-- step 2/5: make doctor (whole-cloth) --\033[0m\n'; \
	$(MAKE) doctor; \
	printf '\033[1;36m-- step 3/5: make check-rerank-drift --\033[0m\n'; \
	$(MAKE) check-rerank-drift; \
	printf '\033[1;36m-- step 4/5: golden retrieval sidecar --\033[0m\n'; \
	mkdir -p /tmp/ci-tier-2; \
	docker run --rm \
		--network ai-network \
		--env-file .env \
		--memory 6g \
		--user 1000:1000 \
		-e HF_HUB_OFFLINE=1 \
		-e HF_HOME=/sidecar-hf \
		-v "$$PWD:/code:ro" \
		-v /tmp/ci-tier-2:/host-tmp \
		-v scaffold-engine_hf-cache:/sidecar-hf:ro \
		-w /code \
		scaffold-engine:dev \
		python3 scripts/score_retrieval.py \
			--output /host-tmp/retrieval_report_ci_tier_2.json \
		2>&1 | grep -vE "reranker_decision|provenance_fetch_failed|Loading weights" | tail -15; \
	python3 -c "import json,sys; d=json.load(open('/tmp/ci-tier-2/retrieval_report_ci_tier_2.json')); \
	print(f\"  ✓ coverage_at_5={d['coverage_at_5']:.1%}  coverage_at_10={d['coverage_at_10']:.1%}  mean_mrr={d['mean_title_mrr']:.3f}\")"; \
	printf '\033[1;36m-- step 5/5: bench regression gates (§17.352) --\033[0m\n'; \
	$(MAKE) bench-check; \
	printf '\033[1;32mAll tier 2 checks passed.\033[0m\n'

check-rerank-drift: ## §17.245 — Verify MODEL_RERANKER default matches across Dockerfile ARG ↔ app/config.py ↔ .env.example (CI gate; mirrors doctor section 12)
	@DKR=$$(grep -E '^ARG MODEL_RERANKER=' Dockerfile | head -1 | sed 's/^ARG MODEL_RERANKER=//'); \
	CFG=$$(grep -E '^    model_reranker: str = ' app/config.py | head -1 | sed 's/^    model_reranker: str = "\(.*\)"$$/\1/'); \
	ENV=$$(grep -E '^# MODEL_RERANKER=' .env.example | head -1 | sed 's/^# MODEL_RERANKER=//'); \
	if [ -z "$$DKR" ] || [ -z "$$CFG" ] || [ -z "$$ENV" ]; then \
		printf '\033[1;31m✗ failed to extract MODEL_RERANKER from one of the 3 sites:\033[0m\n'; \
		printf '  Dockerfile  : [%s]\n' "$$DKR"; \
		printf '  config.py   : [%s]\n' "$$CFG"; \
		printf '  .env.example: [%s]\n' "$$ENV"; \
		printf '\033[1;33m  Fix: a grep regex has drifted; restore the canonical line shape or update this make target.\033[0m\n'; \
		exit 1; \
	fi; \
	if [ "$$DKR" = "$$CFG" ] && [ "$$CFG" = "$$ENV" ]; then \
		printf '✓ MODEL_RERANKER default agrees across 3 sites: %s\n' "$$DKR"; \
	else \
		printf '\033[1;31m✗ MODEL_RERANKER default drift across 3 sites:\033[0m\n'; \
		printf '  Dockerfile  : %s\n' "$$DKR"; \
		printf '  config.py   : %s\n' "$$CFG"; \
		printf '  .env.example: %s\n' "$$ENV"; \
		printf '\033[1;33m  Fix: pick the canonical value (typically settings.model_reranker in app/config.py:173) and update the other 2.\033[0m\n'; \
		exit 1; \
	fi

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
	@# §17.247 — added 0-9 to the target-name character class so digit-bearing
	@# names (ci-tier-2, etc.) surface in `make help`.
	@grep -E '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

.DEFAULT_GOAL := help
