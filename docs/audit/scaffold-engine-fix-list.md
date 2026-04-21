# Scaffold Engine — Audit Fix List

**Generated:** April 18, 2026
**Coverage:** Phases 1–5b (Foundation, Core API, Execution Engine, RAG Stack, Research Agent + ingestion/scheduling)
**Phases remaining:** 6 (Ideation/GT), 7 (Utilities), 8 (Pipelines), 9 (Tests), 10 (Final report + overview rewrite)

Total items: **155**

---

## CRITICAL (9)

### Phase 2 — main.py
- [x] **#1** **Missing `await` on `_require_valid_models` in `/schedule`** — coroutine never awaited → model validation silently skipped — ✅ `4a3c1d7`

### Phase 3a — execution_agent.py
- [x] **#2** **Job status can stick at 'running'** — `execute_all_nodes` has no `try/finally` wrapping the loop; abnormal exit (error, blocked, client disconnect) leaves job in 'running' until reaper at 30 min — ✅ `d653af1`

### Phase 5a — research_agent.py
- [x] **#3** **`resume_research` bypasses concurrent guard** — allows two simultaneous 'running' research sessions — ✅ `5fc820e`
- [x] **#4** **Session leak on unexpected exception in dispatch short-circuits** — URL/GitHub/OpenAPI mode exceptions outside known types leave session in 'running' — ✅ `5fc820e`
- [x] **#5** **`run_research` exception handler drops error_message** — `_finalize_session` called without `error_message` parameter; failure reason lost — ✅ `5fc820e`
- [x] **#6** **LLM-provided `confidence_score` systematically overridden** — prompt tells model to assign confidence, code discards and uses `_score_source(url)` instead — ✅ `5fc820e`

### Phase 5b — scheduler.py
- [x] **#7** **Type mismatch: `scheduled_jobs.next_run_at` UPDATE** — writes DOUBLE PRECISION to TIMESTAMPTZ without `to_timestamp(...)`; likely runtime error on every scheduled job completion — ✅ `ad06f3d`
- [x] **#8** **Cron timezone hardcoded UTC** — `settings.scheduler_timezone` effectively dead; crons fire at UTC regardless of config — ✅ `ad06f3d`

### Phase 2 — main.py (UX-adjacent)
- [ ] **#9** *(see #1)*

---

## HIGH (1)

### Phase 1 — Foundation
- [ ] **#10** **No migration runner on orchestrator startup** — `init.sql` runs on fresh DB but migrations 002–013 don't auto-apply; `docker compose down -v && up` produces incomplete schema

---

## MEDIUM (71)

### Phase 1 — Foundation
- [ ] **#11** Remove dead `MODEL_CLOUD_HEAVY` env var from `docker-compose.yml`
- [ ] **#12** Set `SCHEDULER_JOBSTORE_URL: ""` in compose, let `config.py` derive it

### Phase 2 — main.py
- [x] **#13** Move 6 inline Pydantic models to `schemas.py` (`IdeaInput`, `ConfirmInput`, `DagInput`, `RagInput`, `GtInput`, `GtSearchInput`) — ✅ `4a3c1d7`
- [x] **#14** Convert `/prompts/{job_id}/{node_key}` POST and `/exec/retry` to Pydantic models (currently use raw `request.json()`) — ✅ `4a3c1d7`
- [x] **#15** Add `_require_valid_models` to `/ideas` and `/gt` endpoints (or document why not) — ✅ `4a3c1d7`
- [x] **#16** Extract `_PDF_UPLOAD_HTML` (~80 lines) to static file / Jinja2 template — ✅ `4a3c1d7`

### Phase 3a — execution_agent.py
- [x] **#17** Fix `/execute/all` guard to also exclude `'completed'` status — ✅ `d653af1`
- [x] **#18** Remove dead `_SEARXNG_URL`, `import httpx`, `import os as _os` — ✅ `bcada1e`
- [x] **#19** Remove dead `model_override` (str) param from `execute_next_node` — ✅ `f59e459`
- [x] **#20** Rename parameter/loop var `text` to avoid shadowing `sqlalchemy.text` — ✅ `d653af1`
- [x] **#21** Update stale module docstring (mentions phi4-mini-reasoning; actual verifier is qwen2.5:7b) — ✅ `bcada1e`
- [x] **#22** Cache `_compile_output` result for blocked jobs (currently recomputed per call) — ✅ `d653af1`

### Phase 3b — dag_generator.py
- [x] **#23** Fix `_enforce_node_count` to actually enforce `min_count` or remove the parameter — ✅ `9b49e0d`
- [x] **#24** Reconcile DAG prompt "3-10 steps" with code enforcement "≤10 only" — 🔄 reframed: audit-wrong (prompt matches code) (see drift-findings.md)
- [x] **#25** Warn/reject when Milvus node is missing required `domain` — ✅ `9b49e0d`
- [x] **#26** Surface `_normalize_tasks` coercion warnings to caller's `warnings` list — ✅ `9b49e0d`
- [x] **#27** Remove dead `leaves` variable in `_validate_graph` — ✅ `e5dd575`
- [x] **#28** Expand `_safe_label` Mermaid escaping to cover `( ) { } | " #` — ✅ `9b49e0d`

### Phase 4a — rag_pipeline.py
- [x] **#29** Fix `confidence_threshold` mismatch when `skip_rerank=True` (RRF scores top at ~0.03, threshold 0.8 always triggers `too_strict`) — ✅ `b1c862c`
- [x] **#30** Remove dead `RagResult.topic` and `RagResult.source_file` fields — ✅ `15e6d7a`
- [x] **#31** Remove dead `_get_collection()` fallback in `_vector_search`/`_keyword_search` — ✅ `15e6d7a`
- [x] **#32** Rename `text` param in `_embed_content` to avoid shadowing `sqlalchemy.text` — ✅ `b1c862c`
- [x] **#33** Extract magic numbers in `_rerank` (candidate cap=20, char truncation=500, latency thresholds 5000/15000) to module constants — ✅ `ee46bf3`
- [x] **#34** Clean up formatting mess at lines 47-52 (double separator, misplaced comments, floating alias) — ✅ `15e6d7a`
- [x] **#35** `/rag` should raise HTTPException on error (not return 200 with `{"status": "error"}` body) — ✅ `b1c862c`

### Phase 4b — rerankers + utils
- [x] **#36** Add thread/async lock around reranker singleton init (rerankers.py)
- [x] **#37** Add auto-retry with backoff to reranker load failure path
- [x] **#38** Extract reranker `max_pairs=20` to module constant
- [x] **#39** Make reranker prompt template config-driven (decouple from Qwen3)
- [x] **#40** Cache Milvus connection liveness — don't `utility.list_collections()` RPC on every `get_collection` call
- [x] **#41** Cache Milvus `load()` status — don't RPC on every call
- [x] **#42** Consolidate toon_v2 schema (remove duplication between `milvus_utils.py` and `scripts/create_toon_v2.py`)
- [x] **#43** Move `num_partitions=64` to config
- [x] **#44** Add async lock to `EmbeddingCache._get_redis()` lazy init
- [x] **#45** Consider binary encoding for cached embeddings (replace JSON; 2× smaller/faster)
- [x] **#46** Add explicit Redis TTL on cache entries
- [x] **#47** Use `aclose()` instead of `close()` on redis client (deprecation-proof)
- [x] **#48** Fix `staleness.sweep_expired()` `entry_id in {list}` quoting (build expression explicitly with double quotes)
- [x] **#49** Paginate staleness sweep instead of hard 1000 cap

### Phase 5a — research_agent.py
- [x] **#50** Extract `_execute_iteration_loop` helper (deduplicates run_research/resume_research, ~90% duplication) — ✅ `5fc820e`
- [x] **#51** Extract `_run_direct_mode` helper (deduplicates URL/GitHub/OpenAPI/PDF modes) — ✅ `5fc820e`
- [x] **#52** Extract `_await_with_heartbeat` helper (replaces 8× duplicated heartbeat boilerplate) — ✅ `5fc820e`
- [x] **#53** Fix dispatch-mode comment indentation (comments indented 8 spaces, code at 4) — ✅ `5fc820e`
- [x] **#54** Remove stale `# PATCH_GITHUB_OPENAPI_DISPLAY` marker comment — ✅ `5fc820e`
- [x] **#55** Unify stats persistence pattern across all modes (URL/PDF use `_update_session_iteration`, GitHub/OpenAPI use `_persist_session_stats`) — ✅ `5fc820e`
- [x] **#56** Populate `state.all_entries` in GitHub and OpenAPI modes — ✅ `5fc820e`
- [x] **#57** Stop truncating content to 8000 chars in GitHub/OpenAPI modes, or emit truncation SSE — ✅ `5fc820e`
- [x] **#58** Move hardcoded caps/semaphores/timeouts to config (`_MAX_URL_BYTES`, `_MAX_PDF_BYTES`, `Semaphore(5)`, `timeout=15/30`) — ✅ `5fc820e`
- [x] **#59** Fix PDF mode mislabel: `Title: {filename} (p. {page_count})` — reads as page marker but is total — ✅ `5fc820e`
- [x] **#60** Emit SSE for `_extract_pdf_text` pypdf→plumber fallback (currently only logged) — ✅ `5fc820e`
- [x] **#61** Make `_check_contradictions` sync (currently `async def` with no `await`) — ✅ `5fc820e`
- [x] **#62** Remove dead `_embed_query` import from top of file — ✅ `5fc820e`
- [x] **#63** Remove duplicate `urlparse` imports (triple: top + 2 function-level) — ✅ `5fc820e`
- [x] **#64** Hoist function-level `httpx`/`trafilatura`/`pypdf`/`pdfplumber` imports to module top — ✅ `5fc820e`
- [x] **#65** Remove re-imports of `async_session` and `text` inside `_persist_session_stats` — ✅ `5fc820e`
- [x] **#66** Move `_detect_topic_id` into a shared util (decouple research_agent from gt_extractor private API) — ✅ `5fc820e`
- [x] **#67** Move `TOPIC_TO_DOMAIN` mapping to config — ✅ `5fc820e`

### Phase 5b — github_ingest, openapi_ingest, scheduler
- [x] **#68** Make tree-truncated fail or emit SSE warning (github_ingest.py)
- [x] **#69** Parallelize blob fetches with semaphore (github_ingest.py)
- [x] **#70** Unify 429 handling in github_ingest (raise `GitHubRateLimitError` consistently, not `HTTPStatusError`)
- [x] **#71** Widen `_extract_docstring` exception catch (add `ValueError`, `TypeError`)
- [x] **#72** Narrow `_validate_spec` exception catch (currently `except Exception` masks unrelated bugs)
- [x] **#73** Simplify `_build_entry` title logic (self-comparison and string rebuilding)
- [x] **#74** Extract content-cap constants or emit truncation signals (openapi_ingest 5 places)
- [x] **#75** Add `$ref` resolution to openapi_ingest (via `prance` or explicit resolver)
- [x] **#76** Use shared HTTP client in `_fetch_spec` (ephemeral client per call currently)
- [x] **#77** Fix `init_scheduler` return type hint (`AsyncIOScheduler | None`, returns None when disabled) — ✅ `ad06f3d`
- [x] **#78** Make `init_scheduler` idempotent (shutdown existing before re-init) — ✅ `ad06f3d`
- [x] **#79** Either correlate `last_job_id` to real `research_sessions.id` or remove the field — ✅ `ad06f3d`
- [x] **#80** Add timeout wrapper around `_execute_research_job` (no timeout currently) — ✅ `ad06f3d`
- [x] **#81** Move `misfire_grace_time=300` to config — ✅ `ad06f3d`

---

## LOW (74)

### Phase 1 — Foundation
- [ ] **#82** Pin `requirements-ci.txt` to exact versions matching production
- [ ] **#83** Fix migration 011 internal header comment ("Migration 010" → "Migration 011")
- [ ] **#84** Remove unused `LOCAL_TIMEOUT` from `.env` (compose hardcodes value)
- [ ] **#85** Split dev deps out of production Docker image (future optimization)
- [ ] **#86** Verify `sentence-transformers==5.3.0` major version bump is intentional
- [ ] **#87** Reconcile `init.sql` "8 tables" comment vs overview "9 tables" claim
- [ ] **#88** Note: `Dockerfile` doesn't copy `requirements-ci.txt` (informational; CI uses venv)

### Phase 2 — main.py
- [x] **#89** Fix `cache_stats` fallback in `/health` (uses sketchy `dir()` check; initialize `cache_stats = {}` before try) — ✅ `4a3c1d7`
- [x] **#90** Remove stale "Endpoint stubs — each will be implemented as a separate module" comment — ✅ `4a3c1d7`
- [x] **#91** Remove duplicate `UUID` import in `research_history_detail` (`from uuid import UUID as _UUID`) — ✅ `4a3c1d7`
- [x] **#92** Clean up lifespan `async for db in get_db(): ... break` pattern — use `async with async_session()` — ✅ `4a3c1d7`

### Phase 3a — execution_agent.py
- [x] **#93** Clean up `_verify_output` `'resp' in locals()` pattern (set `resp = None` before try) — ✅ `d653af1`
- [x] **#94** Convert `_build_prompt` from `async def` to sync (has no `await`) — ✅ `bcada1e`
- [x] **#95** Verify `skip_node` return shape matches `ExecutionResult` response_model — ✅ `f5cb9ad`
- [x] **#96** Unify type annotation style (`Optional[T]` vs `T | None` — pick one) — ✅ `bcada1e`
- [x] **#97** `_compile_output` heuristics are fragile (e.g., any node with "output" in title triggers Strategy 1) — consider explicit output-node marking — ✅ `d653af1`
- [x] **#98** `execute_next_node` doesn't call `_require_valid_models` — only `/execute/all` endpoint does — consider moving validation into the function — ✅ `f5cb9ad`

### Phase 3b — dag_generator.py
- [x] **#99** Use `continue` after missing-name/non-dict errors for consistency — ✅ `e5dd575`
- [x] **#100** Share adjacency structure between `validate_dag` and `_build_edges` (duplicate graph traversal) — 🔄 reframed: audit-wrong (different output shapes) (see drift-findings.md)
- [x] **#101** Move `VALID_DOMAINS` / `VALID_TOOLS` to `config.py` — ✅ `f777d05`
- [x] **#102** Remove arbitrary ≤2 task skip in `_render_mermaid` — ✅ `6afc039`
- [ ] **#103** Update overview line counts (dag_generator 615→575)
- [x] **#104** Enforce task-name length limit (≤5 words or ≤80 chars; prompt says "max 5 words" but no enforcement) — ✅ `f777d05`
- [ ] **#105** Consider bulk INSERT for DAG nodes (deferred, perf only)
- [x] **#106** Remove unnecessary `from __future__ import annotations` (Python 3.12) — ✅ `f777d05`
- [x] **#107** `isinstance(raw, dict): continue` silently skips non-dict tasks — add to errors list — 🔄 reframed: stale (already implemented) (see drift-findings.md)
- [x] **#108** Deduplicate `str(raw_domain).strip().lower()` calls in `_normalize_tasks` — ✅ `6afc039`

### Phase 4a — rag_pipeline.py
- [x] **#109** Hardcoded stopwords list → move to module constant or config — ✅ `ee46bf3`
- [x] **#110** Document Milvus `like` case-sensitivity or switch to case-insensitive pattern — ✅ `0a0006b`
- [x] **#111** Document silent 5-keyword cap in `_keyword_search` — ✅ `ee46bf3`
- [x] **#112** `_rrf_fuse` should dedup on `entry_id`, not `content[:200]` — ✅ `2a3d466`
- [x] **#113** `too_strict` fallback should scale with `top_k` (e.g., `min(3, top_k)`) — ✅ `b1c862c`
- [x] **#114** Sanitize `topic_slug` for URL-safe chars (current: `title.lower().replace(" ", "-")[:60]` leaves punctuation/unicode) — ✅ `0a0006b`
- [ ] **#115** Consider batched ingestion (deferred, perf only)
- [x] **#116** Use parameter binding for Milvus expressions (consistency; low-risk injection) — ✅ `0a0006b`
- [x] **#117** Add `skipped_empty` counter to ingestion stats — ✅ `0a0006b`
- [x] **#118** Document canonical field names; plan migration off legacy aliases (content/canonical_text, title/topic, etc.) — ✅ `0a0006b`
- [x] **#119** Add upper bound cap on `top_k` (unbounded input currently) — ✅ `0a0006b`
- [ ] **#120** Remove unnecessary `run_in_executor` around `_get_collection`
- [x] **#121** Document `confidence_threshold=0.0` as disable-filter option — ✅ `b1c862c`
- [ ] **#122** Update overview line counts (rag_pipeline 583→596)

### Phase 4b — rerankers + utils
- [x] **#123** Remove unused `Optional` import from rerankers.py
- [x] **#124** Document why `rerank_rrf` omits `query` param (order-based, intentional)
- [x] **#125** Document reranker score range (model-dependent)
- [x] **#126** Document `raise_on_missing=False` pitfall in `get_collection` (callers forget to check)
- [x] **#127** Raise Redis cache error log level above debug on repeated failures
- [x] **#128** Add eviction counter to `EmbeddingCache.stats`
- [x] **#129** Move `MEMORY_MAX_SIZE=10_000` to config
- [x] **#130** Share text-normalization helper between `embedding_cache` and `rag_pipeline`
- [x] **#131** Move `TTL_POLICY` to config
- [x] **#132** Replace `created_at=0` sentinel with `None` in `compute_expires_at`
- [x] **#133** Log warning on unknown `source_type` in staleness (currently silent 180d default)
- [x] **#134** Cap `deleted: titles` length in `sweep_expired` return

### Phase 5a — research_agent.py
- [x] **#135** Extract heartbeat `sleep(8)` to module constant — ✅ `5fc820e`
- [x] **#136** Clean up `_extract_entries` parameter mutation (`results = expanded`) — ✅ `5fc820e`
- [x] **#137** Handle oversized single paragraphs in `_chunk_text` (currently passes through) — ✅ `5fc820e`
- [x] **#138** Consider tokenizer-based chunking (deferred, perf — current uses 4 chars/token estimate) — ✅ `5fc820e`
- [x] **#139** Normalize query text for `search_history` dedup (case-insensitive) — ✅ `5fc820e`
- [x] **#140** Add retry to `_analyze_gaps` (asymmetric — `_decompose_topic` retries once) — ✅ `5fc820e`
- [x] **#141** Unify `research_complete` payload shape across 4 modes (main/URL/GitHub/OpenAPI) — ✅ `5fc820e`
- [x] **#142** Replace 2-query reply seed in `resume_research` with `_decompose_topic(reply)` call — ✅ `5fc820e`
- [x] **#143** Populate `content_hash` in snapshot entries_projection (currently empty) — ✅ `5fc820e`
- [x] **#144** Improve URL-mode page-title fallback (use explicit `<title>` tag extraction, not sentinel) — ✅ `5fc820e`
- [x] **#145** Bump `research_sessions.depth` to VARCHAR(32) for future "direct_X" modes (`direct_pdf` is exactly 10 chars now) — ✅ `5fc820e`
- [x] **#146** Harden `_parse_github_ref` to raise on malformed input (currently relies on `_is_github_ref` caller discipline) — ✅ `5fc820e`
- [x] **#147** Update overview line count (research_agent 350→2413) — ✅ `d1a1c84`
- [x] **#148** Add prompt versioning (e.g., `DECOMPOSE_SYSTEM_V1`) for 4 system prompts — ✅ `5fc820e`

### Phase 5b — github_ingest, openapi_ingest, scheduler
- [x] **#149** Log warning when empty README is dropped (github_ingest)
- [x] **#150** Document "top-level *.py only" design in github_ingest
- [x] **#151** Consider Redis cache for GitHub trees (deferred)
- [x] **#152** Document `_fetch_spec` text-decoding assumption (openapi_ingest)
- [x] **#153** Use `Literal["openapi-3", "swagger-2"]` type for `_validate_spec` return
- [x] **#154** Add explicit `scheduler_enabled` check in `get_scheduler()` call sites — ✅ `ad06f3d`
- [x] **#155** Consider graceful shutdown with timeout (`wait=True, timeout=N`) in scheduler — ✅ `ad06f3d`

---

## Notes

**On prioritization:** Critical items are actual bugs with user-visible impact (validation skipped, data corrupted, sessions leak). High = infrastructure gaps that will bite on fresh setup. Medium = code quality / maintainability / consistency with stated design. Low = informational, style, or deferred perf.

**On the "shrinking list" pattern:** During Phases 1 and 3a I initially dropped findings I judged "not worth acting on." Corrected after user feedback — every finding now lands on the list, user decides what's worth doing.

**Overview file drift tracked separately:** Phase 10 will rewrite the overview. Line-count updates in this list (items #103, #122, #147) are examples; full reconciliation deferred to Phase 10.
