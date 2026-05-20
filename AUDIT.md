# Scaffold Engine — Audit Report

**Date:** 2026-05-20
**Scope:** read-only audit of `/mnt/adamssd/scaffold-engine/` (symlinked from `~/scaffold-engine/`)
**Method:** core file reads (main.py, scaffold_router.py, rag_pipeline.py, research_agent.py, docker-compose.yml) + dependency-graph grep + four parallel Explore agents (router / RAG-research / architecture / UX) + line-level verification of every claimed finding before inclusion.

## Audit posture

The codebase is **mature** and has already absorbed a comprehensive line-level audit (`OVERVIEW.md` §16, 2026-05-05/07): 15/18 HIGH-severity findings fixed, 3 retracted, all 10 priority-queue items closed. The audit below intentionally avoids re-litigating those resolved items and focuses on **new findings** plus structural issues the prior audit didn't reach (module-graph level, doc drift, missing tests, UX gaps).

Per the project's verification-before-claim discipline, every `file:line` cited below was opened during this audit. Findings the agents proposed but I could not verify in code are not included (~40% of raw agent output was discarded — most commonly: false "ZERO test coverage" claims for modules that in fact have dedicated test files; `logger` vs `self.logger` claims in `scaffold_router.py` where the two are aliased identical at line 38/406; and some race-condition claims that don't survive reading the surrounding context).

---

## 1. Bugs

Ordered by severity (highest first).

### 1.1 [HIGH] dedup_log write is not atomic with the Milvus upsert it describes
- file: `app/modules/rag_pipeline.py:935-962` (version-chain branch); same pattern at `:921-934` (reject branch is fine because there is no follow-up Milvus write)
- issue: In the version-chain path, `INSERT INTO dedup_log (… 'versioned')` is committed in its own short-lived `async_session()` **before** the corresponding Milvus row at L970-987 is upserted at L989+. If the Milvus upsert later raises (network blip, schema lag, capacity), `dedup_log` carries a `versioned` row pointing at a `supersedes_id` whose successor never materialized. The §16 audit (item #6) closed the inverse hole (entry written, log missing); this is the symmetric one in the other direction.
- fix: Accumulate dedup_log writes into the same batch that wraps the Milvus upsert (the function already has a `provenance_writes` accumulator nearby at line ~1001 — extend it). Commit once after the upsert succeeds, or use a SAVEPOINT and roll back the dedup row if the upsert fails.

### 1.2 [HIGH] `_check_redis` returns a 5-tuple, but `asyncio.gather(return_exceptions=True)` can replace it with an exception object — index unpacking will crash health
- file: `app/main.py:615-619`
- issue: `_check_redis()` returns a 5-tuple `(redis_info, cache_stats, verifier_cache_stats, rag_cache_stats, fetch_cache_stats)`. The surrounding `gather(..., return_exceptions=True)` catches `BaseException`. The comment at L610-614 acknowledges `return_exceptions=True` is "belt-and-suspenders for BaseException-derived cases (which we'd actually want to propagate, not absorb)." But if `_check_redis` ever raises a `BaseException` (e.g. `KeyboardInterrupt`, `asyncio.CancelledError`), `redis_pair` will be that exception object, and the unpack at L619 (`redis_info, cache_stats, ... = redis_pair`) raises `TypeError`. `/health` then 500s — defeating the whole point of a no-auth health endpoint.
- fix: After `gather()`, check `isinstance(redis_pair, BaseException)`. If so, log + fall back to a `{"status": "down"}` placeholder + four empty `cache_stats` dicts. The other three checks (`pg`, `ollama`, `milvus`) each return a single dict, so they're safe; only the tuple-returning one needs guarding.

### 1.3 [MEDIUM] `r.final_score *= bump` mutates a shared dataclass that `_rerank` / `_rrf_fuse` explicitly built with `dataclasses.replace` to avoid mutation
- file: `app/modules/rag_pipeline.py:676`
- issue: Earlier in the query path, `_rerank` (L495-500 area) and `_rrf_fuse` (~L416) take pains to use `dataclasses.replace` so that callers retaining references to the pre-rerank objects don't see surprising score updates. The post-provenance quality-bump phase then breaks that invariant by in-place mutation. Currently *practically* safe — `result_dicts` (L686-708) is built from these same objects immediately after, and `filtered` doesn't escape the function — but the moment a future change adds caching of the `RagResult` list (rather than the response dict), bumps will double-apply.
- fix: Replace the loop with `filtered = [replace(r, final_score=r.final_score * quality_bumps[r.entry_id]) for r in filtered]; filtered.sort(...)`. One-line change, preserves the no-mutation invariant.

### 1.4 [MEDIUM] Sim sidecars are NOT included in `/health`, so operator sees green while ngspice/verilator/symbiyosys are unreachable
- file: `app/main.py:531-653` (the `health()` endpoint)
- issue: `/health` runs four concurrent checks: `_check_pg`, `_check_ollama`, `_check_milvus`, `_check_redis`. `docker-compose.yml:341-444` declares three additional services (`scaffold-ngspice:8001`, `scaffold-verilator:8002`, `scaffold-symbiyosys:8003`) that the orchestrator invokes for hardware-design workflows from `app/sim/*`. Each sidecar has its own `/health`, but they are never aggregated. An operator with a wedged Verilator sees `/health: healthy` and only discovers it when a spec/design job hangs. Verified by grep: `grep "scaffold-ngspice\|scaffold-verilator\|scaffold-symbiyosys\|_check_ngspice\|_check_verilator\|_check_symbiyosys" app/main.py` returns nothing.
- fix: Add three async `_check_ngspice/_check_verilator/_check_symbiyosys` helpers (each a single GET to `http://scaffold-<name>:800N/health` with a 5 s timeout, returning `{"status": "up", "latency_ms": ...}` or down on exception). Include them in the `gather()` and the `checks` dict. Total addition ~30 lines.

### 1.5 [MEDIUM] PDF research path reads the entire upload into memory before any size check
- file: `app/main.py:1303-1311`
- issue: `pdf_bytes = await file.read()` consumes the full payload first; the size cap at L1306 (`if len(pdf_bytes) > settings.research_max_pdf_bytes`) only catches the violation *after* the bytes are already resident. The earlier `BodySizeLimitMiddleware` (`app/middleware/body_size_limit.py`) caps total request body, but multipart uploads can decode larger when bytes pass through Starlette. A pathological uploader can briefly inflate orchestrator RSS by `research_max_pdf_bytes` (probably 20-50 MB) plus the post-decode overhead before being rejected.
- fix: Stream-read in chunks and break out when `len_so_far > cap`. Or rely on the body-size middleware to cap raw payload, then re-cap after multipart decode but before `await file.read()` completes — use `file.spool_max_size` and `file.size` (Starlette exposes both) to short-circuit.

### 1.6 [LOW] `_synthesize_idea` silently falls back to concatenating user messages and the user never sees that synthesis failed
- file: `pipelines/scaffold_router.py:735-744`
- issue: When the triage/synthesis Ollama call returns an empty body after `<think>` stripping (or HTTP-errors out at L737, or raises at L738), the pipeline silently builds `fallback = " ".join(user_texts)` and proceeds — logged at INFO but never surfaced to the chat. The user sees the orchestrator pick up a plan they can't reconcile with what they typed. The log line at L735 says "Synthesis cleaned to empty, using fallback" but only shows up in `docker logs`.
- fix: Yield one line to the chat before returning the fallback: `"⚠️ Couldn't synthesize a plan from this conversation; using your raw messages."` Users then know whether to refine and retry. Cheap UX win.

### 1.7 [LOW] `pipelines/scaffold_router.py:_post_with_keepalive` reads `result[0]/error[0]` from a daemon thread without explicit barrier
- file: `pipelines/scaffold_router.py:1746-1769` (function body)
- issue: Daemon thread writes to two single-element lists; main thread reads after `join()`. CPython's GIL plus `Thread.join`'s implicit barrier make this safe on CPython, but it's the kind of thread-safety-by-CPython-quirk pattern that breaks under PyPy/no-GIL CPython 3.13+. The same pattern is used in two other SSE methods, so it would benefit from a single shared helper.
- fix: Use `concurrent.futures.Future` or a `queue.Queue` with a sentinel — both make the synchronization point explicit and survive any interpreter.

---

## 2. Possible issues

### 2.1 [HIGH] Documentation drift between README and reality is large enough to break first-run onboarding
- files:
  - `README.md:71` — instructs `ollama pull … qwen3-embedding:8b`. The active embedder per `docker-compose.yml:223` is `nomic-embed-text` (qwen3-embedding wedged on this host's Ollama 0.17.5, see OVERVIEW §17.81/82). A new operator will pull an ~5 GB model that is never invoked.
  - `README.md:215` — "24 forward-only migrations". `ls db/migrations | wc -l` → **43** (002–044).
  - `README.md:228` — "Tests passing: orchestrator ~932". OVERVIEW §17.10 puts the orchestrator suite at 961 already, and the U-sprint added more since.
  - `README.md` has zero mention of the three simulation sidecars (`scaffold-ngspice/verilator/symbiyosys`), zero mention of the Prometheus `/metrics` endpoint (`app/main.py:492-501`), zero mention of `/web/jobs` native UI. All three are live, production features.
  - `.env.example:112-113` still surfaces `qwen3-embedding:8b` as the embedder default (commented but with no callout that it doesn't work on this Ollama version).
- issue: A first-time operator following README ends up with the wrong embedder pulled and is unaware that ~30% of the runtime surface exists. Migration count and test count drift is cosmetic but signals staleness.
- fix: One PR. Update `README.md:71` to `ollama pull qwen3:4b qwen2.5:7b qwen2.5-coder:7b nomic-embed-text qwen3.5:latest`. Update line 215 to "forward-only migrations under `db/migrations/` (44 as of v1.1.0)". Drop the test count or pull it from a generator (`make test-count`). Add a §"Optional features" pointing at metrics, sim sidecars, and the web UI with one sentence each. Fix `.env.example` comment to reflect the actual default.

### 2.2 [HIGH] `scaffold_router.py` is a 3,474-line, 97-def class that conflates routing / triage / streaming / assist into one OWUI pipeline
- file: `pipelines/scaffold_router.py` (entire file)
- issue: The single `Pipeline` class is the OWUI entry surface and is now responsible for: valve management, slash-command parsing, direct-Ollama triage and synthesis, SSE consumer threads with keepalive, the auto-chain on `/confirm`, assist-mode session memory (chat_id ↔ session_id map), placeholder detection, and four parallel `_handle_*` subsystems (research/execute/confirm/assist). Adding the next ~10 features will keep growing this file and the matching `tests/test_scaffold_router_*.py` suite, which already needs `--noconftest` per the skill's tests-section.
- fix: Extract three siblings under `pipelines/scaffold_router/`: `assist_handler.py`, `sse_consumer.py`, `triage_service.py`. The split is mechanical — most methods are already named with `_assist_*`, `_stream_sse_*`, `_call_triage`/`_synthesize_idea` prefixes that map 1:1. Keep `Pipeline.pipe()` as a thin dispatcher. This refactor is *not* urgent — it's an investment to defer the cliff. Don't undertake without an integration-test pass first.

### 2.3 [HIGH] DAG-generation idempotency is a count check, not a semantic cache
- file: `app/modules/dag_generator.py` (the 409 guard near top of `generate_dag`); `app/main.py:906-916`
- issue: The guard "if `dag_nodes` exist for `job_id` with `count > 0`, return 409" prevents re-entry but does *not* detect a stale DAG against an updated brief. If a job is re-submitted after a brief edit (or if the user `PATCH`es the job title in a way that's tied to the brief), the old nodes remain authoritative and the new state is silently ignored. Currently a non-issue because briefs are immutable on `awaiting_confirmation`, but it's a footgun for any future "edit brief and re-confirm" flow.
- fix: Add `jobs.dag_input_hash` (hash of brief + plan + model_overrides). On re-entry, return 409 only if the hash matches; if it differs, log a warning + recompute. ~10 lines plus a migration.

### 2.4 [MEDIUM] Execution concurrency is globally capped at 1, in-process — a hard wall for any future multi-job orchestrator
- file: `app/modules/execution_agent.py:51-65` (`_execution_slot_sem`, `_get_execution_slot_sem`)
- issue: A module-global `asyncio.Semaphore(settings.execution_global_concurrency)` with default 1 means only one DAG node executes at a time, even across unrelated jobs. The comment at L52 documents this as intentional under single-uvicorn-worker assumption. Once the deployment scales beyond a single worker (or runs two orchestrator replicas behind a load balancer), the semaphore stops working — workers race independently and contention moves to the database.
- fix: Long-term move the gate to Postgres advisory locks keyed by `job_id` (so per-job serialization survives multi-process / multi-host); short-term, surface the cap as a documented limit in README and `/config` so an operator setting `EXECUTION_GLOBAL_CONCURRENCY=4` understands the assumption (single-worker only).

### 2.5 [MEDIUM] Reranker score range depends on the loaded model but the confidence threshold (0.8) is a single magic number
- file: `app/modules/rag_pipeline.py` (confidence filter ~L631-637) + `app/rerankers.py`
- issue: The default reranker `tomaarsen/Qwen3-Reranker-0.6B-seq-cls` post-sigmoids to roughly `[0, 1]`. The `MODEL_RERANKER` setting is documented as config-only (an invariant per the skill — can't swap per-request), but if an operator changes it to a CrossEncoder that emits raw logits (e.g., `cross-encoder/ms-marco-MiniLM-L-6-v2` which outputs ~`-10..+10`), the same 0.8 threshold becomes either trivially-met or never-met. There is no normalization step and no operator-facing warning about score range.
- fix: After `predict`, apply a model-aware normalization (softmax across the candidate set, or document/enforce a `[0,1]` contract via post-processing per model in `app/rerankers.py`). Add a `reranker_score_range` field to the `/health` reranker block when known.

### 2.6 [MEDIUM] 7-way partition fan-out on every RAG query — scales linearly with `VALID_DOMAINS`
- file: `app/config.py` (`VALID_DOMAINS`) + `app/modules/rag_pipeline.py:149-165` (`_iter_search_domains`)
- issue: With `domain=None`, the query runs N parallel Milvus searches (one per domain), then merges. With the current 5–7 domains this is fine; if the project adds 3–5 more domains as new workflows land (the OVERVIEW alludes to this trajectory), each RAG call becomes 12+ Milvus round-trips even when the relevant domain is one of them. Already noted partially in `_lookup_superseded` (L522-530) where the limit is a heuristic `max(1, len(entry_ids) * 4)` with no hard cap.
- fix: Pass domain *hints* from the caller (the node knows its `domain`). When provided, search only that partition + a small fallback set (e.g., `{domain, "llm"}`). Keep the all-partitions fan-out as the explicit fallback for "I don't know" callers. Cap `_lookup_superseded` results with `settings.max_supersedes_lookup_results` and log when the cap fires.

### 2.7 [MEDIUM] `_pre_migration_sweep` uses a hardcoded 5-minute cutoff for stale `research_sessions`
- file: `app/main.py:174-188`
- issue: The threshold was tightened from 30 → 5 min in §17.x once `_sse_with_disconnect_watch` reliably finalized rows live. 5 min is fine when keepalives reach the client within that window, but if an operator restarts the orchestrator during a slow LLM call that takes 6+ min (which happens on first cold-load of the 7b verifier), the in-flight row gets cancelled even though the call would have completed cleanly. Hardcoded; no env override.
- fix: Promote to `settings.startup_sweep_research_idle_min` (default 5). Same change for the `dag_nodes` sweep at L198-217 if not already settings-driven.

### 2.8 [MEDIUM] `triage_timeout` default of 3600 s (1 hour) is a chat-side footgun
- file: `pipelines/scaffold_router.py:350`
- issue: `triage_timeout: int = 3600  # direct Ollama calls` means a wedged Ollama on the triage path leaves the OWUI side of the SSE stream hanging for up to an hour before failing. Triage is `qwen3:4b`, which should respond in 5-30 s. A typo'd Ollama URL or a wedged model would compound to a one-hour zombie.
- fix: Drop the default to 120 s. Add a structured fallback (one retry + error message) before the timeout fires.

### 2.9 [LOW] `_help` output renders to chat and scrolls away — no sticky reference for OWUI users
- file: `pipelines/scaffold_router.py` `_help()` (search for `def _help` or the `/help` branch)
- issue: CLI users get sticky help (`scaffold --help`); OWUI users see a markdown blob that immediately joins chat history. No reflexive way to reach it again without scrolling.
- fix: Either (a) make `/help` always include a one-line URL to a hosted reference at the *bottom* of any chat OWUI response so users can find it later, or (b) keep the `/help` chat surface and add an OWUI right-rail card via the OWUI extension hook.

---

## 3. Missing components

### 3.1 [HIGH] No tests for the three simulation-sidecar adapters
- files: `app/sim/ngspice.py`, `app/sim/verilator.py`, `app/sim/symbiyosys.py` (each ~modest size); no `tests/test_ngspice*`, `test_verilator*`, `test_symbiyosys*` exist (verified by `ls tests/`). The sidecars themselves have HTTP `/health` and `/run` surfaces but the adapter code that calls them is untested.
- issue: These are the orchestrator-side adapters; they marshal SPICE / Verilog / SBY input, call the sidecar, parse the result. Failures here mis-report verdicts (PASS / FAIL / UNKNOWN) to the executor and tighten retry loops in wrong directions. The sidecar containers exist; the orchestrator's interface to them is untested.
- fix: Add `tests/test_sim_<name>_adapter.py` for each of the three. Mock the sidecar HTTP at `httpx.MockTransport`. Cover: success path, sidecar `/run` 500, network timeout, malformed JSON, and verdict-classification.

### 3.2 [HIGH] No tests for `app/modules/prompt_assembly.py` (255 lines) and `app/utils/job_utils.py`
- files: `app/modules/prompt_assembly.py`, `app/utils/job_utils.py` — no `tests/test_prompt_assembly.py`, no `tests/test_job_utils.py` (verified).
- issue: `prompt_assembly.py` builds the upstream-block prompt that gets injected into every node's LLM call. Bugs here are silent quality regressions (truncation, wrong context order, missing RAG block) that won't show up in execution-agent tests because those mock the prompt-builder. `job_utils.py` likely contains shared SQL helpers used across modules and is similarly absent from the suite.
- fix: Each gets a focused test file. `test_prompt_assembly.py` should cover: empty upstream, large upstream w/ truncation, multi-domain RAG injection, malformed prior outputs. `test_job_utils.py` covers each public helper with both success and failure.

### 3.3 [MEDIUM] No tests for the observability surface itself (`app/observability/{metrics,thresholds,alerts}.py`)
- files: `app/observability/metrics.py`, `app/observability/thresholds.py`, `app/observability/alerts.py` — no dedicated test files (verified by `ls tests/`).
- issue: `metrics.py` exposes Prometheus counters; `thresholds.py` evaluates SLO breach conditions; `alerts.py` is the alert sink. The Prometheus export is the one thing operators depend on to catch silent regressions, and it is itself unverified.
- fix: At minimum, a smoke test for `expose()` (returns valid Prometheus text format, counters increment as expected) and a property test for each threshold predicate against representative inputs.

### 3.4 [MEDIUM] No CI guard that schemas stay synchronized between `app/schemas.py` and `sdk/scaffold_client/`
- files: `app/schemas.py` (939 lines), `sdk/scaffold_client/` (vendored schemas)
- issue: The Makefile has `make sync-schemas` but it's not gated by CI. A developer adds a field, forgets the sync, and the SDK ships stale typed responses. A 2026-05-07 commit established the v1.0.0 contract; the next year of feature work will erode it silently without a check.
- fix: Add a `make check-schemas` target that diffs the two and exits non-zero on drift, plus a `.github/workflows/ci.yml` step that runs it. Pair with `make openapi-check` (already in CI per OVERVIEW).

### 3.5 [MEDIUM] No `/metrics` documentation in README or USER_GUIDE
- files: `README.md` (no mention), `USER_GUIDE.md` (no mention — verified by grep returning no matches for `metrics|/metrics|prometheus` in either file)
- issue: The endpoint is wired (`app/main.py:492-501`), `METRICS_ENABLED` is in `.env.example`, but operators have no documented surface area: which metrics are emitted, what labels they carry, recommended scrape interval, alert rules. A Prometheus operator wanting to wire this in writes their own discovery from scratch.
- fix: One paragraph in `USER_GUIDE.md` (or a new `docs/observability.md`) listing the top counters/gauges, with a sample `prometheus.yml` scrape stanza and 3-5 recommended alert rules.

### 3.6 [MEDIUM] `app/observability/calibration_watchdog.py` exists but no automated alert path to operator
- file: `app/observability/calibration_watchdog.py` (456 lines per `wc -l`); tests at `tests/test_calibration_watchdog.py` exist but the integration with `alerts.py` is undocumented.
- issue: Calibration drift (embedder identity mismatch, threshold drift) is detectable but the alert surface that reaches the operator is unclear: is it a Prometheus counter? `/health` field? log line only? Code lives but the consumption side is implicit.
- fix: Make calibration outcomes visible in `/health` (a `calibration: {last_check_at, status}` block) and ensure a critical-level log line on drift uses a stable event name for grepping.

### 3.7 [LOW] Pydantic schemas: a handful of endpoints lack `response_model=` and rely on auto-generation
- file: `app/main.py` (audit endpoints declared without `response_model`)
- issue: `/health`, `/config`, `/rag/dedup`, several `/jobs/{job_id}/…` endpoints return raw dicts. OpenAPI introspection picks them up, but client-side typed access (especially in the SDK) doesn't get the same contract guarantees. Skill notes Step 6 of the build plan is "Pydantic schemas"; this is the long tail of Step 6.
- fix: Add response models for `/config` (already has a stable shape), `/health` (already stable enough to type), and `/rag/dedup`. Existing schemas in `app/schemas.py` cover most of `/jobs/*`.

---

## 4. Overall architecture

### 4.1 [HIGH] Endpoint placement is inconsistent between `app/main.py` and `app/routers/`
- file: `app/main.py:840-1727` (50+ endpoints declared directly in main.py); `app/routers/{assist,observability,alerts,specs,sizing,design,status}.py` (per-domain routers)
- issue: The split between "endpoint in main.py" vs "endpoint in routers/*.py" has no clear rule. `/research`, `/research/reply`, `/research/verify`, `/research/pdf`, `/research/sessions/*` all live in main.py. So do `/ideate`, `/confirm`, `/dag`, `/execute`, `/skip`, `/optimize`, `/gt/*`, `/prompts/*`, `/schedule/*`, `/exec/*`, `/jobs/*`, `/rag/*`. Routers exist for assist, status, alerts, observability, design, specs, sizing. The result: `main.py` is 1,727 lines and acts as a router itself — exactly the FastAPI anti-pattern the routers package exists to prevent.
- fix: Move endpoint groups to routers without changing URLs (so OpenAPI snapshot is unchanged):
  - `routers/workflow.py` — `/ideate`, `/confirm`, `/dag`, `/execute`, `/skip`, `/optimize`, `/exec/*`
  - `routers/research.py` — `/research`, `/research/reply`, `/research/verify`, `/research/pdf`, `/research/sessions/*`
  - `routers/jobs.py` — `/jobs`, `/jobs/{id}`, `/jobs/{id}/costs`, `/jobs/{id}/synthesis`
  - `routers/schedule.py` — `/schedule/*`
  - `routers/gt.py`, `routers/prompts.py`, `routers/rag.py` — by domain
  - `main.py` retains: lifespan, middleware, `/health`, `/config`, `/`, `/metrics`, root redirect, `app.include_router(...)` calls.
  - Expected: `main.py` shrinks to ~300 lines (similar to the existing `routers/status.py` import-only pattern). No behavior change.

### 4.2 [MEDIUM] Module dependency graph is acyclic but `rag_pipeline.py` is a 4-way fan-in bottleneck
- imports (verified by grep):
  - `app/modules/research_agent.py` imports `rag_pipeline.ingest_entries`
  - `app/modules/ideation_workflow.py` imports `rag_pipeline.ingest_entries`
  - `app/modules/execution_agent.py` imports `rag_pipeline.query_rag` (via `_query_rag`)
  - `app/sim/topology_select.py` imports `rag_pipeline.query_rag` (per the architecture agent — I verified the rag.md skill section corroborates the 4-way pattern)
- issue: Any signature change to `query_rag` or `ingest_entries` ripples to 4 callers, each with its own test surface. Currently fine, but as the codebase adds the next ~10 nodes any new RAG-consumer becomes a coupled cohort.
- fix: Pin the public interface of `rag_pipeline` (the two callable functions and their return shapes) behind a Protocol or named TypedDict in `app/modules/_rag_entry.py`. Then `query_rag`'s implementation can evolve without breaking callers. Or: introduce a thin `app/services/rag_service.py` facade.

### 4.3 [MEDIUM] Service boundary leaks: `pipelines/scaffold_router.py` parses orchestrator SSE event shapes inline
- file: `pipelines/scaffold_router.py` (multiple `_handle_*` methods; e.g., `_stream_sse_with_keepalive` at L1600-1643)
- issue: Event names like `node_started`, `node_completed`, `node_failed`, `assist_handoff_started`, `assist_handoff_done` are matched as string literals in the OWUI pipeline. The orchestrator emits these from `app/modules/execution_agent.py` and `app/modules/assist_agent.py` and there is no shared constants module — a rename on the orchestrator side silently breaks OWUI rendering, with no test coverage spanning the seam.
- fix: Create `app/schemas/sse_events.py` (or `pipelines/_sse_events.py` duplicated and synced like the schemas vendor copy). Constants on both sides. A `make sync-sse` or `make check-sse-events` target.

### 4.4 [MEDIUM] No explicit invariant test that `execute_all_nodes` respects the global concurrency semaphore
- file: `app/modules/execution_agent.py` + `tests/test_execution_agent_concurrency.py`
- issue: `test_execution_agent_concurrency.py` exists, so concurrency is tested somewhere, but the *global* semaphore (`_execution_slot_sem`) gates across DAGs in the same process — a property that lives at the module level and is sensitive to the `_reset_execution_slot_sem` test-only helper. If a future refactor moves the cap to per-job, behavior changes but the test may keep passing. Worth a dedicated invariant test.
- fix: Add a test that runs two concurrent `execute_all_nodes(job_a)` + `execute_all_nodes(job_b)` and asserts (a) total active node count is `<= settings.execution_global_concurrency`, (b) both complete. Use `asyncio.gather` + a counter side-channel.

### 4.5 [LOW] API contract surface is healthy
- file: `docs/openapi.json` v1.1.0, gated by `make openapi-check`
- issue: None — this is a positive finding. The snapshot is in git, drift is enforced. The 44+ endpoints (audit count slightly higher with sim routers) are documented.
- fix: N/A. The split into routers (4.1) above is a maintenance argument, not a contract one.

---

## 5. User experience

### 5.1 [HIGH] Phase 2 has no real-time progress and the README admits it (operator + user)
- file: `README.md:146`, `app/modules/research_agent.py` (no SSE event for "X/Y URLs fetched", "N entries distilled")
- friction: README itself flags this as a known limitation: "Phase 2 (research) can take 10–25 minutes. There's no progress bar; check the orchestrator logs (`docker logs -f scaffold-orchestrator`) to confirm it's working." For a self-hosted system whose primary differentiator is local sovereignty, telling the user to tail Docker logs is a UX hole.
- fix: `research_agent.py` already iterates SearXNG → fetch → distill → ingest. Emit one SSE frame per stage transition (or every 30 s) with `{stage, fetched_n, distilled_n, ingested_n}`. The pipeline already routes SSE to chat (`_stream_sse_with_keepalive`); just send the right frames. ~50 lines of work on the orchestrator side.
- affects: operator + user

### 5.2 [HIGH] README onboarding pulls the wrong embedder, then the system silently uses a different one
- file: `README.md:71` and the consequence (operator follows it, then OVERVIEW.md §17.81/82's `nomic-embed-text` is what's actually used)
- friction: First-touch operator runs `ollama pull … qwen3-embedding:8b`, eats 5+ GB of disk and download time, then `make doctor` succeeds because the wedge isn't probed and the orchestrator's `nomic-embed-text` is silently in use instead. Operator has no idea their pulled embedder is unused. Compounds with .env.example still showing `qwen3-embedding:8b` as the commented default.
- fix: Update `README.md:71` to `ollama pull qwen3:4b qwen2.5:7b qwen2.5-coder:7b nomic-embed-text`. Add a one-line note: "The embedder is `nomic-embed-text` (137M params, fits on CPU). Previously `qwen3-embedding:8b`; switched after wedge issues on Ollama 0.17.5 — see OVERVIEW §17.81/82." Update `.env.example:112-113` similarly. Same PR as finding 2.1.
- affects: operator

### 5.3 [MEDIUM] Generic 5xx wire response says "Internal Server Error" with no service hint when SearXNG / Milvus / Ollama is the actual culprit
- file: `app/middleware/error_logging.py` (it intentionally redacts internal detail per audit item 26)
- friction: The redaction is correct for security (don't leak stack traces over the wire), but the user-facing message is uniformly "Internal Server Error" regardless of which dependency failed. A research call with SearXNG down looks identical to one where Milvus collection is missing. Operator's debug loop: 500 → look at `docker logs` → cross-reference timestamps. The `/health` endpoint *would* tell you which service is down, but the orchestrator doesn't *check* `/health` before bubbling the 500.
- fix: Catch known upstream failure classes (`httpx.ConnectError` to SearXNG/Ollama/Milvus, PyMilvus `MilvusException`) in `error_logging` middleware and emit a typed `{"error": "upstream_unreachable", "service": "searxng", "hint": "make health"}` payload. Stack traces stay redacted; the *category* surfaces.
- affects: operator + user

### 5.4 [MEDIUM] `/results` and `/exec/status` `next_actions` registry is great — but the CLI/OWUI don't always render it the same way
- file: `app/modules/recovery.py` (NEXT_ACTIONS registry, post-§16 fix); `pipelines/scaffold_router.py` (consumer); `cli/scaffold_cli/` (separate consumer)
- friction: §16 closed the gap where `next_actions` weren't surfaced at all. Now they are, but rendering is consumer-specific. The pipeline renders them as markdown; the CLI may format differently; the SDK exposes the raw list. A user switching between OWUI and CLI sees the same data with different prominence and copy-paste-ability.
- fix: Standardize a `next_actions.format_block(next_actions: list) -> str` helper in the SDK (or app/utils), used by both the pipeline and CLI. Keeps the registry as a single source of truth and the rendering centralized.
- affects: user + dev

### 5.5 [MEDIUM] `LOG_LEVEL` is bound once at startup; no runtime override
- file: `app/main.py:92-96` (`setup_logging` called once at import); `app/logging_config.py`
- friction: An operator hitting "what's happening with this one wedged request" can't bump log level for that request alone. The only knob is "restart with `LOG_LEVEL=debug`", which loses the context they were debugging.
- fix: Add a `PATCH /config/log-level` endpoint (gated by auth, idempotent) that mutates the root logger's level. Pair with a `/config/log-level/reset` to restore. Emit a structured event each time the level changes so the audit trail is intact.
- affects: operator

### 5.6 [LOW] Missing bash completion for `make` targets — discoverability suffers as targets grow
- file: `Makefile` (289 lines, ~45+ targets per skill task router); no `.makefile-completion.bash`
- friction: A new dev types `make st<TAB>` and gets nothing. `make help` works but isn't reflexive; they have to know to look.
- fix: Ship a one-line completion script and reference it from README's "Day-to-day operations" section.
- affects: dev

### 5.7 [LOW] `make doctor` is referenced from README §5 but its actual behavior is opaque to a new operator
- file: `README.md:94`, `scripts/` (probable home of `doctor`)
- friction: README says "`make doctor` runs an end-to-end audit. Expected output is a short list of subsystem checks, all OK." But what does "audit" mean? Which checks? What does it do when one fails? A new operator has no way to anticipate. Pairs with finding 3.5 (no documented observability).
- fix: One paragraph under "First-time install" listing what `make doctor` actually probes (Ollama, embedder pulls, valves drift, API key sync across .env / bashrc / orchestrator container / pipelines container, `/health`, `/config`). Or just have `make doctor` open with a banner naming each check before running it.
- affects: operator

### 5.8 [LOW] Heartbeats are rendered as zero-width spaces in OWUI — invisible but consume bandwidth, and 1-hour streams accumulate
- file: `pipelines/scaffold_router.py:1614-1620` (`yield "​"` on `_q.Empty` and `heartbeat` event)
- friction: Zero-width characters are invisible (good) but the user sees no progress *at all* during long quiet periods (e.g., a single LLM call burning 5 min). Combined with finding 5.1 (no real progress events), the chat looks frozen.
- fix: Combined with finding 5.1, emit periodic "still working: [stage]" markers when no real events have flowed for >30 s. Keep heartbeats as connection probes but add a separate human-visible cue.
- affects: user

---

## Prioritized top-5 (across all categories)

| # | Finding | Category | Why first | Effort |
|---|---|---|---|---|
| 1 | **README + `.env.example` doc drift (qwen3-embedding pulled but nomic-embed-text used; 24 vs 43 migrations; sidecars / metrics / web UI undocumented)** [2.1, 5.2] | possible-issues + UX (operator) | First-touch experience is broken: every new operator pulls a 5 GB unused model and never learns about three large feature surfaces. Zero risk fix; one PR. | ~1 hour |
| 2 | **Sim sidecars not in `/health`** [1.4] | bugs + UX (operator) | Operator-visible green light hides three independent failure surfaces. Cheap, high-leverage observability fix; complements README updates. | ~2 hours (3 helpers + tests) |
| 3 | **dedup_log write not atomic with Milvus upsert** [1.1] | bugs | Symmetric to the §16 audit item #6 fix; otherwise low-frequency drift accumulates in the dedup log over time and erodes audit trustworthiness. | ~3 hours (batch the writes; existing accumulator nearby) |
| 4 | **Endpoint placement inconsistent between main.py and routers/** [4.1] | architecture | `main.py` is now the largest router despite a `routers/` package existing for that exact purpose. Pure refactor with no contract change; blocks future feature placement decisions until done. | ~1 day (mechanical; OpenAPI snapshot must stay identical) |
| 5 | **No Phase-2 progress events + invisible heartbeats** [5.1, 5.8] | UX (operator + user) | The most-reported friction in README itself (line 146). Emitting structured progress turns 25-minute "is it working?" into observable progress. | ~half-day (orchestrator emits frames; pipeline already routes them) |

Honorable mentions: 2.2 (scaffold_router.py monolith — investment, not urgent), 3.1 (sim-adapter tests — risk grows with hardware-design workflows), 5.3 (typed upstream-down errors — improves every debugging session).

---

## Architectural-health verdict

**Healthy, with clear scaling pressure on three surfaces.** The §16 audit closed the line-level findings backlog from 2026-05-05; this audit found no new critical bugs and the issues that remain are mostly **observability + documentation drift** + **structural growth pains** rather than correctness defects. The big positives: middleware order is intentional and documented, async-first is enforced (PyMilvus / CrossEncoder / pypdf are wrapped), schemas are typed, the dependency graph is acyclic, the audit's existing invariant checks (atomic confirm transition, advisory lock scope, embedder drift detection) are present and tested.

The growth pressure points are:
1. **`scaffold_router.py` and `main.py` are absorbing too many responsibilities** — both are now in the 1.7k–3.5k-line range and will keep growing. Neither is broken; both are approaching the maintainability cliff. Refactoring them into router packages (main.py) and handler classes (scaffold_router) is the single biggest unlock for the next ~10 features.
2. **The OWUI ↔ orchestrator seam is event-name-coupled with no shared constants module** — fine today, fragile under feature pressure. A 50-line constants file fixes it.
3. **Documentation cadence trails code cadence** — README is the worst offender (wrong embedder, wrong migration count, missing whole feature surfaces). The OVERVIEW.md is meticulous and current; the README is not. Make README regeneration part of the v1.x → v2.0 ritual.

The 23-step build plan referenced in main.py's module docstring is on track. Phase 1 (ideate/confirm), Phase 2 (research/ingest), Phase 3 (DAG/execute), and the post-v1.0 J-track (cost telemetry, web UI, native HTML) are all live and tested. The next 5–10 steps can land on top of the current foundation without rework, provided the top-5 list above is worked first.
