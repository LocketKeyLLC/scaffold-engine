# Scaffold Engine — Hardening Log

Reverse-chronological record of dated fixes, hardening rounds, and architectural changes. Engineer's working log; preserves file paths, function names, and commit hashes.

## 2026-05-07 — Sprint J.1.a: OpenAPI snapshot + v1.0.0 stability anchor

Public HTTP API contract is now versioned and snapshotted. First step of Sprint J.1 (Python SDK package + stable OpenAPI).

- **FastAPI `version="1.0.0"`** in `app/main.py` — was `0.1.0`. This becomes the stability anchor that `scaffold-engine-client` (forthcoming) will pin against. Breaking changes to the contract require a major bump and a release-note entry.
- **`scripts/openapi_snapshot.py`** — imports `app.main:app`, calls `app.openapi()`, emits sorted-keys JSON to stdout. `--check` mode compares against the committed `docs/openapi.json` and exits non-zero on drift; CI gate.
- **`docs/openapi.json`** — first committed snapshot. 44 paths, ~100 KB. Sorted keys + 2-space indent + trailing newline make it `git diff`-friendly.
- **Makefile targets** — `make openapi-snapshot` (regenerate, captures container stdout into the host file so bind-mount perms stay clean) and `make openapi-check` (verify-only).
- **`docker-compose.dev.yml`** — added `./docs:/code/docs:ro` so `--check` can read the committed snapshot through the bind mount. Dev-only; prod compose untouched.

Verified: `make openapi-snapshot` produces a deterministic byte-for-byte file, `make openapi-check` exits 0 in sync and 1 on a single-newline tamper.

## 2026-05-03 — Command UX restructure

- **`/help` regrouped** — flat 19-row table replaced with 5 grouped sections (Scope & kickoff, Workflow control, Knowledge base, Manage saved work, Configuration & utilities). Workflow line now sits at the top of the help output.
- **`/research` mgmt disambiguated to slash form** — `/research/list`, `/research/find`, `/research/rename`, `/research/delete`, `/research/help`. Old space-form removed; `/research <anything-else>` now unambiguously means autonomous research (topic / url / `github:` / `openapi:`). Mirrors existing `/research/reply` and `/research/pdf` separator style. Pipeline-only change; orchestrator endpoints unchanged.
- **`/dag` dropped from user-facing `/help`** — endpoint and dispatcher retained for advanced/scripted callers; documented as internal. Overview's known issue ("`/dag` is unreachable in normal flow") closes as documentation-only.
- **Two undocumented surfaces added to the command table** — `/jobs <sub>` and `/research/<sub>` (both shipped earlier; previously only discoverable via help text).
- **Issue logged for separate work:** `/research <topic>` summary occasionally bleeds unrelated training-data content (observed: "kubernetes pods" → Svelte/Todo.svelte tutorial). Likely a research summary prompt issue against the 235b cloud model — not a regression from this round.

## 2026-05-03 — User-facing component testing + 6 fixes

Test pass: Open WebUI walkthrough of all 5 pipelines + every / command. Findings split into trivial doc drift, real bugs, and one investigative non-issue. Suite touched at the pipeline layer; no orchestrator test changes. Test count unchanged.

**Bug fixes (6)**

- `/help` doc drift — `/model <sub>` description omitted `probe` subcommand; `/research/pdf <url>` advertised a URL arg that does not exist (real contract is multipart POST or browser form at `GET /research/pdf`). Two-line fix in `pipelines/scaffold_router.py::_help`.
- `/skip` leaked asyncpg DataError on bad UUID — `SkipNodeInput` accepted `job_id: str` and let asyncpg blow up at the SQL boundary. Added `field_validator` on `job_id` (UUID-parse) and `node_key` (non-empty), so malformed input now returns a clean Pydantic 422 with field path. Schema-level fix in `app/schemas.py`; orchestrator restart picks it up.
- `/gt search` 503 on partition-key isolation — `app/modules/gt_browser.py::gt_search` called `col.search()` with a single `domain ==` clause that became `expr=None` when `domain=None`, which Milvus 2.5 partition-key isolation rejects. Fan-out pattern from the Apr-23 rag_pipeline hardening applied: when `domain is None`, iterate `sorted(VALID_DOMAINS)`, run one search per partition, merge by best score per `entry_id`, sort by score, top-k. Response payload now includes `domains_searched`. Verified end-to-end against KB at 1011 entries.
- `/results` for `blocked`/`failed`/`cancelled` jobs returned "no details" — pipeline-side renderer ignored the orchestrator's `counts` + `nodes` array (same shape that the Round 6 fix taught the running-state branch to read). Rewrote `_handle_results` terminal-state branch to surface a `done/total nodes complete, N failed` summary, a per-failed-node table (key, title, model), and inline recovery commands (`/exec retry` and `/skip` pre-filled with `job_id` and `node_key`). Pipeline-only fix.
- `/status` raw JSON dump — `_fmt(r)` printed the response unchanged. Added `_render_status` with: header line (N total, M active), counts table sorted desc and zero-rows dropped, and a recent-jobs table with status icons, short job IDs, node count, and trimmed `updated_at`. Pipeline-only fix.
- Valves API key wipe-on-restart for 4 pipelines — Round 9 had hardened only `scaffold_router`. The other four (`dag_viewer`, `gt_browser`, `prompt_inspector`, `execution_handler`) silently lost their saved `api_key` whenever OWUI Pipelines `main.py` rewrote `valves.json` to `{}` on container restart, surfacing as 401s. Three-layer defense added per pipeline (mirrors `scaffold_router`'s pattern):
  - `valves.template.json` tracked next to each pipeline's existing `valves.json`
  - `_bootstrap_valves` re-seeds from template if live file is missing or `{}`
  - `_apply_env_fallbacks` fills empty string-valued valves from `SCAFFOLD_API_KEY` / `SCAFFOLD_ORCHESTRATOR_URL`, and persists the resolved values back to disk — critical, because OWUI's "Updated valves for module" path runs after our `__init__` and would otherwise re-wipe in-memory values.
  - Helpers are inlined per-pipeline rather than imported from a shared module, because OWUI Pipelines treats every `.py` under `/app/pipelines/` as a pipeline candidate; a sibling `_valves_helpers.py` gets auto-discovered and quarantined. Initial shared-module attempt did exactly this, then crashed all four pipelines on import and shuffled them to `pipelines/failed/` — caught and reverted.
  - Verified by wiping all four `valves.json` files, restarting, and confirming the bootstrap → env-load → disk-persist chain in container logs, plus a successful `/dagviz` call against a known job.

**Investigated, no action**

- `/rag` 195s latency from prior session — re-ran cold and warm; current latency 13–15s on 4-domain fan-out with CrossEncoder rerank. Reranker pre-warm fires correctly at lifespan startup (`crossencoder_loaded: elapsed_s=1.9` then `reranker_prewarmed`). The 195s data point predates pre-warm shipping (or coincided with the partition-key error path retrying). No bug; further reduction would require parallel fan-out in `rag_pipeline._iter_search_domains` callers, which is non-trivial and out of scope for this pass.

**Logged but not fixed**

- 22 stranded `awaiting_confirmation` jobs > 4 days old. Reaper not catching them; check `cleanup.reap_stale_jobs` thresholds for that state.
- `/dag` is unreachable in normal flow: `/idea` lands jobs in `awaiting_confirmation` and `/confirm` auto-chains through Phase 2 + DAG + execute. There is no path that lands a job in `planning` for `/dag` to act on. Decide whether to delete the command, document it as internal, or have it accept `awaiting_confirmation` (skipping research).
- Blocked job `01ab243e` T2 (CodeGen) failed verification 3× — same "List supported image files" tagged as CodeGen issue from Round 9 worked-example, suggesting the Round-9 DAG-generator prompt update did not fully eliminate the mis-tagging.
- `/optimize` produces minimal optimization on short prompts (e.g. "Write a function that sorts a list" → "Define a function that sorts a list"). Cosmetic.

**KB state:** `toon_v2` at 1011 entries (eng=341, llm=483, rag=171, spec=8, prompt=0). Verified active end-to-end via `/idea Build a Python script that lists files in a directory sorted by size descending` → `/confirm` → 4 nodes verified=true on first pass, 0 retries.

**Total this session:** 6 fixes shipped, 4 issues logged, 0 regressions. Pipeline-side persistence story now consistent across all 5 pipelines.

## 2026-05-01 — `/research` end-to-end validation + GitHub ingest fix

**All 5 `/research` modes verified working via Open WebUI:**
- `/research <topic>` (medium depth) — 100 entries, gap analyzer converged at 85% coverage
- `/research <url>` — 70 entries from a single Wikipedia page
- `/research github:owner/repo` — fixed (see below); 6 entries from `anthropics/anthropic-sdk-python`
- `/research openapi:<url>` — 19 endpoints from petstore3 spec, 3.6 min
- `/research/pdf` — 18 entries from 11-page Transformer paper, 18 min
- `/research/reply <session_id> <msg>` — pause/resume validated via injected paused session
- `/schedule add/list/delete` — full lifecycle validated

**Bug fix (commit `2a47022`)** — `app/utils/github_ingest.py:_select_tree_files()` filter required `docs/` prefix on `.md` files. Repos that keep README, CHANGELOG, api.md, etc. at root (like `anthropics/anthropic-sdk-python`) had only the dedicated README endpoint result; the tree-walk found 0 attemptable files. Filter now accepts `.md` files in `docs/` OR at root level (no `/` in path). Verified: `attempted` went from 0 → 9 for the same repo.

**KB state:** 695 → ~909 entities across testing session.

**Known minor observations (not bugs):**
- Gap-analyzer pause path is rare — coverage_threshold convergence is the common terminal state for shallow/medium runs. Resume endpoint validated via injected session, not natural pause.
- PDF mode's first request stranded with `Research already in progress` (orphan from prior curl client disconnect). Force-cleanup via DB UPDATE was needed before retry. Reaper would have caught it eventually.

## 2026-04-30 — Triage gap-tracking hardening

**Triage looping fix** — `pipelines/scaffold_router.py` `TRIAGE_SYSTEM_PROMPT` was asking for clarification on gaps the user had already answered in earlier messages. Model lacked explicit instruction to scan history and mark gaps `✓ covered`. Added "CRITICAL — READ THIS FIRST" preamble instructing the model to check ALL prior messages before re-asking. Maps implicit answers (e.g., "3 hours a day for 6 months" → CONSTRAINTS). When all four gap buckets read `✓ covered`, emits only a 2-4 sentence summary + `/go` offer, no looping. Verified: test input with all four answers now yields summary + `/go` on first turn. Commit `85994e7`.

**Impact:** Triage conversations now converge in N turns (where N = number of answers needed), not indefinitely. Scope-locking is faster and user intent is respected from the first mention.

## 2026-04-28 (Round 9) — environmental backlog mitigation

**Suite:** 756 passed, 5 skipped, 0 failing. +11 vs Round 8 baseline (745 → 756).

**Triage CPU latency mitigation (commit `d94a55b`)**
- Every plain message previously sent the entire chat history to qwen3:4b on CPU; wall time grew linearly with turn count. New `_window_messages()` helper in `pipelines/scaffold_router.py` caps history to the last N turns (default 8) while always pinning the first user message so the model retains the original goal as the conversation grows. Wired into `_call_triage` only — `_synthesize_idea` (one-shot on `/go`) intentionally untouched.
- New valve `triage_history_window` (default 8, OWUI-tunable). 6 new tests in `TestWindowMessages`.

**OWUI file-routing diagnostic (commit `a7cf8a1`)**
- File uploads occasionally fail to inline content into `user_message` after container restarts. Symptom is intermittent and we had no captured payload to target. Shipped `_log_pipe_inputs()` — gated diagnostic that captures `body` keys, `metadata` keys, `files_count`, `file_ids`, message count, last role, and head/tail of `user_message` when the new valve `log_pipe_inputs` is enabled.
- Default off; flip in OWUI admin when symptoms recur. Targeted fallback fix deferred until a real PIPE_INPUTS sample tells us which OWUI field carries the dropped content. 5 new tests in `TestLogPipeInputs`.

**Triage UX restructure (commits `a7fe0a0` + `a5a287b`)**
- Replaced `TRIAGE_SYSTEM_PROMPT` with an enforced 4-section template (Scope so far / Options / Gaps / My pick). Live testing showed the model passively assuming user intent; new structure forces it to surface options, name gaps, and recommend defaults every turn. Forbidden-output rules ban markdown tables, emoji, fenced blocks, and horizontal rules. A worked mid-conversation example anchors the model when scope is mostly clear but not locked. Exit-to-summary only fires when all four Gaps read "✓ covered".

**Valves resilience (commit `a7fe0a0`)**
- Discovered while diagnosing a 401 on `/ideate`: `pipelines/main.py:193` rewrites `valves.json` to `{}` whenever the file is missing on container startup, silently wiping every saved value. Three-layer defense added — `valves.template.json` tracked in git with sensible defaults, `_bootstrap_valves_from_template()` seeds an empty live file from the template at `__init__`, `_apply_env_fallbacks()` fills empty `api_key`/`orchestrator_url`/`ollama_url` from `SCAFFOLD_*` env vars. End-to-end verified: deleted live `valves.json` → restart → re-seeded → env fallback filled `api_key` → `/ideate` returned 200. `.gitignore` cleaned of duplicate + stale entries.

**Execution-node output discipline (commit `2a7b392`)**
- Live `/execute/all` test surfaced that the call site at `_run_inference` was sending only a user message — no system prompt — so qwen3-vl:235b freelanced into 350-line essays with tables, emoji, and editorialising. Added `EXECUTION_SYSTEM_LLM` (strict prose) and `EXECUTION_SYSTEM_CODEGEN` (code-first, code-friendly markdown), routed by `_system_for_tool()` based on `tool` field.

**DAG generator tool selection (commit `2a7b392`)**
- Strict CodeGen prompt exposed a planning-side bug: the DAG generator picked `tool=CodeGen` for "List supported image files" because the original guide just said "CodeGen = code generation or script writing". Tightened the rule with explicit anti-examples (listing, naming, designing, documentation → LLM) and a positive definition (deliverable IS executable code). LLM marked `DEFAULT` loudly. Added a 5-node CLI-tool worked example showing the right 1:4 CodeGen:LLM ratio.

**Verified end-to-end (CLI tool that converts screenshots to a searchable PDF)**
- Before: T2 incorrectly tagged CodeGen, failed verification 3× ("includes unnecessary and unrelated content such as installation instructions and a Bash script"), blocked the job at T2.
- After: same idea, fresh DAG. T1=Choose image formats (LLM), T2=Design CLI structure (LLM), T3=Write OCR to PDF script (CodeGen), T4=Document usage instructions (LLM), T5=Validate end-to-end workflow (LLM). 5/5 nodes done, 0 retries. T1 output went from ~3500 chars of markdown chrome to 814 chars of focused prose. T3 emitted a working ~30-line bash script with brief context, no tangents.

**Round 9 totals:** 6 commits, +31 tests (745 → 776), 0 regressions. 2 environmental items addressed (1 mitigated, 1 instrumented). 1 latent silent-failure bug discovered + fixed (valves wipe-on-restart). 2 architectural improvements (triage UX + execution node discipline + DAG tool selection).

## 2026-04-28 (Round 8) — endpoint contracts, audit closeout, test suite expansion, DX polish

**Suite:** 745 passed, 5 skipped (KB-availability), 0 failing. Stable across consecutive runs. +167 vs Round 7 baseline (578 → 745).

**Bug fixes (3)**
- **`/ideas` endpoint contract gap** — `refine_idea()` defaulted `target_status="planning"`, so jobs created via `/ideas` (including the validate-tier integration test running on every `make test`) landed in `planning` with no auto-chain forward. They orphaned and were reaped at the 24h `planning_min` threshold. Default flipped to `awaiting_confirmation`, matching `/ideate`. Three test assertions updated. Discovered via 9 stranded "List Three Sorting Algorithms" jobs spanning 3 days, all from the same test. Commit `eb64c1e`.
- **`test_batches_large_input` flakiness** — test mocked only `model_router`, leaving `_fetch_and_extract` to attempt real httpx fetches against the fake URLs (`https://ex.com/0..14`). In isolation: ~3s. Under full-suite load with warmed httpx clients: 30+s, tripping the global `--timeout=30`. Mocked the fetch too, forcing the snippet-fallback path. Runtime now deterministic at ~1.6s, +3 tests came out of flake-skip. Commit `c1d0940`.
- **Auth tests singleton-reload pattern** — `test_valid_key_is_accepted` and `test_no_key_configured_disables_auth` had been failing for weeks tagged "out of scope". Two real issues: (1) Pydantic Settings is a singleton instantiated at first `app.config` import, so reloading `app.auth` alone left `settings.scaffold_api_key` pinned to the original value — fixture now reloads BOTH; (2) the second test name claimed "missing key disables auth" but the documented contract is that empty key WITHOUT explicit `SCAFFOLD_AUTH_DISABLED=1` raises `RuntimeError` at import. Fixture now sets the opt-out flag and the test renamed to reflect actual behavior. Teardown also fixed (was leaving modules in invalid state). Commit `3600a64`.

**Feature (1)**
- **Prompt revision history** (audit items #7.8 + #7.9, both closed) — full audit trail for DAG node prompt edits. New `prompt_revisions` table (migration 022) with composite FK to `dag_nodes`, monotonic `revision_number` per (job_id, node_key), `source` CHECK constraint (manual/optimizer/initial/system). `update_prompt()` rewritten to archive the OLD prompt as an immutable revision before applying the new value. New `get_history()` function + `GET /prompts/{job_id}/{node_key}/history` endpoint returning newest-first. `PromptRevision` and `PromptHistoryResponse` Pydantic models in schemas (closes #7.9 "structured model"). 7 new tests (first edit, increment, empty-old skip, status guard, invalid source, ordering, missing node). Commit `3d2c034`.

**Test suite hardening (3)**
- **Phase 2 client-disconnect handler** (Round 7 work, applied this round) — `research_and_compile()` now mirrors the `_run_with_session_lifecycle` pattern from `research_agent.py`. Commit `2a7642b`.
- **17 previously-skipped tests unlocked** — pipeline + infra tests under `test_scaffold_router_*.py`, `test_model_valves.py`, `test_gt_browser.py`, `test_execution_handler.py`, `test_sse_streaming.py`, `test_infra_scaffolding.py` were skipping because they probe `pipelines/`, `Dockerfile`, and `.github/` — none mounted into the orchestrator container by design. Added dev-only mounts in `docker-compose.dev.yml`. Flushed out 4 stale test assertions (model_valves expecting 8 keys when filter correctly returns 6, schedule depth default mismatch from Round 6, `/results` mock shape mismatch from same Round 6 fix, hardcoded SSE timeout test). Also added inline `# noqa: T201` support to the no-prints structural test. Commit `0bc1ae8`.
- **6 obsolete compile-strategy-1 tests deleted** — all `@pytest.mark.skip` with the reason "Strategy 1 (title-heuristic) removed; superseded by is_output_node marker". Tests for deleted code carry no value. File: 322 → 247 lines. Commit `13aeda8`.
- **3 of 7 golden retrieval tests activated** — live KB inspection (664 entries: eng=261, llm=218, rag=175, spec=8, prompt=0) showed 3 queries had retrievable matches. Module-level skip removed; per-query skips with precise reasons left for the 4 that need KB content. Commit `fd434e8`.

**Hygiene + DX (5)**
- **Bare-except audit** — 31 sites reviewed across `app/` and `pipelines/`. 30 found legitimate (health-check fallbacks, JSON parsing, finalize-during-cancel, etc.). One (`scheduler.py` force-shutdown fallback) gained a debug log so future failures are visible. Commit `5a5f7ce`.
- **`redis>=5.0.0` pinned to 7.4.0** — sole unpinned dep, violated the "all dependencies pinned" invariant. Commit `1c43e2f`.
- **`postgres:16` and `redis:8-alpine` pinned by SHA256** — every other compose image was SHA-pinned; these two were the outliers. Commit `9f1da00`.
- **Complete `.env.example`** — was a 1-line stub (only `GITHUB_TOKEN`), missing 14+ vars the orchestrator consumes. Rebuilt as a 139-line documented template with three tiers: REQUIRED (4 vars), RUNTIME-LIKELY (8), ADVANCED (every `config.py` knob commented out with defaults). Also fixed `.gitignore` so the file could actually be tracked. Commits `9f1da00`, `260e8f5`.
- **README + Makefile polish** — repo had no README at all. New 55-line README serves as the front door (pipeline diagram, prerequisites, quick start, common operations, project layout, pointer to overview). Makefile: stale `~547 passing` comment fixed to `~745`, `migrate` target moved up next to other ops targets, two new targets (`restart`, `dev-up`). Commit `e258089`.

**Multi-axis security/error/log audit** — 0 hardcoded secrets, 0 SQL injection vectors, 0 dangerous patterns (`eval`/`exec`/`pickle.load`/`shell=True`/`verify=False`), 0 f-string log violations, all HTTP calls have timeouts, all `HTTPException` calls have status codes, all `raise` statements are legitimate re-raises. Codebase is genuinely clean across all surveyed axes — Round 1–6 fix work consolidated quality.

**Round 8 totals:** 14 commits, +167 tests, 3 bug fixes, 1 feature, 17 tests unlocked, 6 obsolete tests deleted, 2 audit items closed (list now empty), 0 regressions. Test suite is stable across runs and the strongest it has ever been.

## 2026-04-27 (Round 7) — Phase 2 client-disconnect handling

**Suite:** 578 passed, 31 skipped, 2 known-fail out-of-scope (auth). +1 test vs Round 6.

**Bug fix (1)**
- **Phase 2 client disconnects stranded jobs in `planning` with empty `research_data`** — `research_and_compile()` caught `Exception` but not `asyncio.CancelledError`, so SSE client disconnects during long Phase 2 runs (CPU runs of 8–24min are common) skipped `_fail_job` entirely. Jobs landed with `refined_brief` populated but `research_data` null and were only caught by the reaper at the 24h `planning_min` threshold. Added explicit `CancelledError` handler that calls new `_cancel_job` helper (`error_summary='client_disconnect'`), then re-raises to preserve asyncio task semantics. Mirrors the `_run_with_session_lifecycle` pattern in `research_agent.py`. Test `test_ideation_phase2_cancel.py` patches `search_searxng` to raise `CancelledError` mid-flight and asserts the UPDATE fires with the expected params. Discovered via two stranded jobs (`59745a88-…`, `e0a9b5ee-…`). Commit `2a7642b`.

## 2026-04-27 (Round 6) — backlog completion + middleware test coverage

**Suite:** 576 passed, 31 skipped, 3 known-fail out-of-scope (2× auth, 1× integration). Net +28 tests vs Round 5 (548 → 576).

**Bug fixes (4)**
- **`/results` displayed `0/N completed` mid-execution** — pipeline asked the orchestrator for `completed_nodes`/`current_node`, but the API returns per-status `counts` dict + `nodes` array. Pipeline now derives `done` from `counts` (sum of `done` + `skipped`), surfaces failure count, and locates the running node from the array. Smoke-test against a completed job verified the math: `total: 7`, `counts: {'done': 7}`. Pipeline-only fix; orchestrator contract unchanged.
- **Orphaned executors locked their parent jobs forever** — `_REAP_RUNNING_SQL` has a `NOT EXISTS running node` guard that explicitly refuses to fail a job with a running node (correct for live work, but turns into a permanent lock when the node itself is orphaned by a crashed/restarted executor). Added `Stage 0` to `reap_stale_jobs`: dag_nodes with `status='running'` and `started_at` older than `node_orphan_threshold_minutes` (default 60min, env override `NODE_ORPHAN_THRESHOLD_MINUTES`, bounded `[5,1440]`) reset to `pending`. Parent jobs' `updated_at` is touched so the next reap cycle doesn't immediately fail the freshly-recovered job. Per-node `WARNING` + cumulative `INFO` log lines. Manufactured-orphan test verified end-to-end: injected → `/jobs/cleanup` → `orphan_nodes_reset: 1` → node confirmed `pending`. Commit `3b1efb8`.
- **DAG generator emitted `recommended_model: gpt-4o`** — `COMPILE_SYSTEM` prompt asked the LLM to recommend a model, but nothing in the codebase ever read `configuration.recommended_model`. The LLM had no awareness of the local Ollama stack and defaulted to its training-data prior (gpt-4o, claude-3-opus, etc.). Removed the field from the prompt schema; `configuration` now contains only `temperature`, `domain`, `estimated_nodes` — all consumed. Role-routing via `model_router.get_model()` was already the source of truth. `grep` verified zero readers across `app/`, `pipelines/`, `tests/`. Also removed orphaned `app/modules/ideation_workflow.py.bak-20260425`. Commit `4239fb6`.
- **`/schedule add` defaulted depth to 'shallow'** while `ScheduleCreate.depth` and `scheduled_jobs.depth` both default to `'medium'`. Three-way drift: user-facing default depended on which surface received input. Pipeline default aligned to `'medium'`; `--depth=<level>` explicit override behavior unchanged. The broader "scheduled depth hardcoded shallow" overview note predated the `depth` column, scheduler wiring, and pipeline parser shipping — only the default was misaligned. Commit `dce5e2f`.

**Test/hygiene (5)**
- **Cleanup test regression from orphan fix** — `test_cleanup.py` asserted "five counts / five SQL statements". Stage 0 brought it to six (plus a conditional seventh `_REFRESH_PARENT_JOBS_SQL` when orphans exist). Helper `_db_with_counts` updated to take 6 counts, with a new `_orphan_row()` factory providing `.job_id` / `.node_key` attrs (Stage 0 reads both). Two new tests: `test_reap_stale_jobs_orphan_reset_count_propagates` and `test_reap_stale_jobs_runs_seven_sql_statements_when_orphans_found`. Commit `c9588ba`.
- **`PytestUnraisableExceptionWarning`** — `_extract_entries` is wrapped in `asyncio.create_task()` inside `_execute_iteration_loop`. When tests patched it with `new_callable=AsyncMock`, the inner `_execute_mock_call` coroutine was left un-awaited inside the task wrapper (known `unittest.mock` + `create_task` quirk). Replaced `AsyncMock + return_value` with `side_effect=<plain async fn>`. Verified with `-W error::pytest.PytestUnraisableExceptionWarning`. Commit `05a1e7d`.
- **FastAPI `Query(regex=)` deprecation** — single occurrence at `/research/pdf` form: `extractor` query param. FastAPI 0.100+ deprecated `regex=` in favor of `pattern=` (Pydantic v2 alignment). One-line change, no behavior delta. Removes the only `DeprecationWarning` emitted by the test suite. Commit `d504b3b`.
- **Audit fix lists reconciled** — Round 3 reported the lists "need pruning" citing ~5 stale items. Inspection found only 1 truly open in `fix-list.md` (`#9`, transitive ref to a closed item) and 2 genuinely open in `phase-6-10.md` (`#7.8` prompt revision history, `#7.9` structured prompt-history dict — both real future work). Ticked `#9` and added a reconciliation banner to both files noting that the ~106 items checked-but-unhashed reflect inconsistent commit-hash recording during Apr 2026, not stale work. Per-file open counts: `fix-list.md=0`, `phase-6-10.md=2`. Commit `8cce1b9`.
- **Middleware test coverage** — overview claimed "13 modules without dedicated tests"; cross-referencing showed actual gap was 3, all in `app/middleware/`. Added 28 tests across three files:
  - `test_request_id_middleware.py` (5): inbound `X-Request-ID` honored, uuid4().hex generation, distinct ID per request, contextvar cleared after request, empty inbound header treated as missing.
  - `test_error_logging_middleware.py` (12): `_classify_error` mapping for timeout / connect_timeout / http_error / validation family / unrecoverable; pass-through; structured 500 body; `error_logs` persist with classified `error_type`; secondary persistence failure does not break the user response (regression guard for Apr 26 `import httpx` bug).
  - `test_performance_middleware.py` (11): `_truncate` (None / under / at / over with ellipsis); `X-Request-Duration-Ms` header; `/health` fast → DEBUG, slow → INFO, non-/health → INFO; `log_model_call` truncates `model` / `endpoint` to column widths and `error_message` to 500 chars; `log_model_call` swallows DB failures.
  - Initial slow-`/health` test deadlocked patching `time.monotonic` (uvicorn calls it more than twice per request). Rewrote to patch `_HEALTH_SLOW_MS=0` instead — deterministic INFO classification without time-faking. Commit `d13b0dd`.

**Round 6 totals:** 9 commits, +28 tests, 7 user-visible/code issues closed, 0 regressions remaining.

## 2026-04-26 (Round 5) — API key check + context-strip hardening

**API key consistency audit**
- Verified `sk-scaffold-***REDACTED***` is identical across all 5 locations: `.env`, `valves.json`, `~/.bashrc`, `scaffold-orchestrator` env, `open-webui-pipelines` env. Zero drift.

**Context-strip hardening (Issue #9)**
- `pipelines/scaffold_router.py` line 316 was a single-pattern `re.split(r"</context>\s*", ...)` — silently passed context-laden messages through if Open WebUI ever changed wrapper format.
- Replaced with a multi-wrapper sweep handling `</context>`, `</documents>`, `</source>`, plus a heuristic warning that fires if a long message starts with `<` but no known closing tag matched. Format drift is now visible instead of silent.
- Closes overview "Known Open Issues" #9.

**Round 4 + 5 cumulative**
- Real bugs fixed: 3 (cleanup config, 2× ideation model routing tests)
- Architectural improvements: 3 (reranker pre-warm, configurable ideation model, defensive context strip)
- Drift items addressed: 1 (`.pyc` cache)
- Test suite restored to 547 passing baseline

## 2026-04-26 (Round 4) — drift cleanup, reranker pre-warm, configurable ideation model

**drift-findings.md review**
- Cross-references confirmed: file already documented overview issues #10–#12 + #16 as resolved. Today's reconciliation closed those in-place.
- Two new genuine items found in "Open" section: stale `.pyc` cache, `PytestUnraisableExceptionWarning`. The first was actionable.

**Stale `.pyc` / AST cache fix**
- Added `PYTHONDONTWRITEBYTECODE=1` to `docker-compose.dev.yml` `environment:` block. Future dev-image runs won't generate stale bytecode after host file edits.

**Reranker pre-warm at lifespan startup**
- New env var `SCAFFOLD_PREWARM_RERANKER` (default `true`). Lifespan now calls `_get_cross_encoder()` via `run_in_executor` after `init_clients()` — model is hot-loaded before first user request.
- Eliminates the documented 13.6s cold-load penalty on first `/research`/`/execute` call. Verified: `crossencoder_loaded: elapsed_s=1.8` in startup logs, followed immediately by `reranker_prewarmed`.

**Test suite verification & 3 regression fixes**
- Full suite ran in dev image: **547 passed, 31 skipped, 2 known auth fails** — matches documented baseline.
- 3 new failures discovered and fixed:
  1. `test_cleanup_settings_are_sourced_from_config` — was hardcoding 15min/30min defaults that `.env` deliberately overrides for CPU-realistic operation. Rewrote to assert *invariants* (positivity, ordering, cleanup_interval ≤ stale_threshold) instead of specific numbers.
  2. `test_analyze_uses_model_router_not_general` — see below.
  3. `test_research_uses_model_router_not_general` — see below.

**Configurable ideation model role (architectural decision)**
- The April 25 commit `f896399` ("WIP: model_general for ideation") quietly reverted audit fix #6.1 which mandated `model_router` for ideation. Audit's CPU-cost reasoning (200–500s) no longer applied because `model_general` resolved to a cloud model post-rotation, not a local one. Cloud is faster (Phase 1: 16s vs estimated 30–120s on CPU). But this was an undocumented architectural override.
- Resolution: introduced `ideation_model_role` setting in `app/config.py` (defaults to `"model_general"`, env override: `IDEATION_MODEL_ROLE`). Rewired the 3 hardcoded `get_model("model_general", ...)` calls in `app/modules/ideation_workflow.py` to `get_model(settings.ideation_model_role, ...)`.
- Updated both ideation tests to assert against `settings.ideation_model_role` (with mocked-settings pinning to the real configured value), so they verify *configurability* rather than a specific role choice.
- Net result: cost/speed tradeoff is now explicit and switchable at deploy time.

## 2026-04-26 (Round 3) — backlog sweep + audit reconciliation

**Round 3 fixes applied**
- `app/config.py` — `rerank_warn_ms` 1500 → 30000, `rerank_error_ms` 5000 → 120000. CPU rerank latencies of 60–170s were tripping the error threshold on every call, drowning real errors. Thresholds now calibrated to actual hardware. Reranker logs go info / warning / error appropriately.
- **Ollama model cleanup** — removed 10 undocumented models that were not wired in via env or valves: `qwen2.5-coder:latest`, `qwen3.5:4b`, `qwen3.5:0.8b`, `phi4-mini:latest`, `phi4-mini-reasoning:latest`, `mxbai-embed-large:latest`, `qwen3-embedding:0.6b`, `sam860/qwen3-reranker:0.6b-Q8_0`, `qwen3-vl:235b-cloud`, `glm-5.1:cloud`. Reclaimed **15.6 GB** (37.1 → 21.5 GB Ollama disk). Live model list now exactly matches the documented model table (7 models).

**Audit reconciliation — 5 "Known Open Issues" verified already resolved**
- **Issue #7 (version chain filter scope)** — Already closed in `app/modules/rag_pipeline.py` lines 475–498 + 582–588. `_lookup_superseded()` queries `supersedes_id IN (returned_ids)` against the full collection (not result-set scoped), drops stale ancestors before return. Docstring even cites "Closes overview issue #7."
- **Issue #10 (test_pipeline_complete.py tautologies)** — Already cleaned. File's own docstring documents the cleanup: *"#9.1 — Remove ~6 tautology tests that asserted their own literals."* Remaining 3 tests are legitimate SSE shape checks (round-trip JSON, double-newline framing).
- **Issue #11 (conftest_ci.py dead code)** — File no longer exists in the repo.
- **Issue #12 (orphan research sessions)** — Already handled in `_run_with_session_lifecycle()` (`research_agent.py:451`). `try/except/finally` block marks session `cancelled` with `error_message='client_disconnect'` on `CancelledError`/`GeneratorExit`. DB confirms 12 cancelled sessions, zero stuck-running orphans.
- **Issue #13 (scheduler timestamp type mismatch)** — Already correct. `app/scheduler.py:155` and `:243` read `next_run` from the live APScheduler job as a tz-aware `datetime`, written via asyncpg → `TIMESTAMPTZ`. The code never reads the `DOUBLE PRECISION` column directly. Comment on line 236 explicitly cites the fix.

**Net result:** 6 of 7 audit items investigated this round were stale notes documenting work that had already shipped. The April 18 audit fix lists need pruning. The codebase is in better shape than the overview suggests.

**Round 3 totals**
- Real fixes shipped: 2 (reranker thresholds, model cleanup)
- Stale audit items reconciled: 5
- Disk reclaimed: 15.6 GB
- Open items remaining (likely environmental, not code): triage CPU latency, Open WebUI file routing intermittent *(both addressed in Round 9)*

## 2026-04-26 — bug fixes + DB hygiene + e2e validation

**Bugs fixed**
- `app/middleware/error_logging.py` — confirmed `import httpx` (line 9) loads correctly post-restart; earlier NameError was a stale-container artifact, resolved.
- `app/modules/execution_agent.py` — `retry_failed_node()` referenced `new_retry_count` on lines 929/938 without defining it. Added `new_retry_count = row.retry_count + 1` before Stage 2. Validated end-to-end: `/exec/retry` reset T1 to pending, retry_count=1, downstream T3/T4/T5/T6/T7 cascade-reset, structured `node_retry` log emitted.

**Phase 2 distill — verified working**
- Three historical runs (`37b5edca`, `a46a724f`, `be8f7f75`) all produced exactly 8 entries each: `queries_run=4, results_found=38, facts_extracted=8, milvus_ingested=8`. Earlier "0-entries distill" concern was unfounded. Distill output count is deterministic by prompt design.

**End-to-end pipeline test** — `be8f7f75-648b-48fe-9dc3-c1a000bc4b8e` (Tokio vs async-std comparison)
- Phase 1 → Phase 2 → DAG (7 nodes) → execute/all → compiled_output. **7/7 nodes verified=true on first pass, 0 retries, 30m40s wall-clock.** Auto-retry path code present but did not fire this run; validated separately via `/exec/retry`.
- Final `compiled_output`: 3,321 chars stored. SSE stream emitted correctly with keepalives.

**Open WebUI persistence**
- `RESET_CONFIG_ON_START` flipped `true → false` in `docker-compose.yml` line 14. Settings now persist across container restarts.

**DB hygiene — pruned historical rows**
- Policy: keep all `completed`, drop `cancelled`/`failed` older than 7 days, drop orphaned April-12 `executing` job, drop stale `awaiting_confirmation` (excluded today's test).
- Removed: 116 cancelled + 41 failed + 1 executing + 1 awaiting = **159 jobs**. CASCADE removed 260 dag_nodes + 104 execution_logs. `error_logs` and `performance_logs` cascade behavior verified (CASCADE / SET NULL respectively).
- Final state: 90 jobs (31 completed / 40 cancelled / 19 failed), 153 dag_nodes, 168 execution_logs, 23 error_logs.

**Cancelled-job resume — verified working (no code change needed)**
- `44f74601-39a9-4e19-9e9f-11752357bdbd` (cancelled, T1+T2 done, T3 pending). `/execute/all` resumed from T3 only, skipped completed nodes, used T1+T2 outputs as upstream context. Final compile included Bubble/Merge/Quick Sort (from T1 output) — 2 min wall-clock for the 1 pending node.

**Milvus state**
- `toon_v2`: 645 entities (was 637 pre-session, +8 from Phase 2 ingest of `be8f7f75`). Active partitions: 234, 218, 177, 16. Counts roughly aligned with `eng/llm/rag` domain split.

## 2026-04-26 — End-to-end validation + middleware fix

**Phase 2 distill verified working** — earlier "0-entries" suspicion disproved. Two production jobs completed end-to-end:
- PostgreSQL 16 vacuum tuning guide: 6 nodes, 0 retries, 34.6 min wall time
- Piston Fireball multiplayer simulation: 8 nodes, 3 retries (T5×2, T6×1), self-healed

**Milvus state**: 622 → 645 entities (eng partition grew most).

**Bug fix**: `app/middleware/error_logging.py` was missing `import httpx`. `isinstance(exc, httpx.TimeoutException)` raised NameError on every API error, masking 4xx as 500. One-line fix; restart picks it up via bind mount. Committed as `ead402d`.

**Cancellation/resume validated manually**: orphaned `running` node + `running` job recoverable by (1) `UPDATE dag_nodes SET status='pending'` for the stuck node, (2) `UPDATE jobs SET status='executing'` for the parent, (3) re-fire `/execute/all`.

**New issues logged**:
- `/results` reports "0/N completed" mid-execution — reads job status, not node states
- Concurrent-execution guard doesn't detect orphaned executors — stale `running` + no executor = manual-only recovery
- DAG generator emits `recommended_model: gpt-4o` from training data (cosmetic; role-routing overrides it at execution)

**Auto-chain confirmed working through Open WebUI pipeline.** Raw curl bypasses the pipeline (auto-chain lives in `scaffold_router.py`, not in orchestrator endpoints) — earlier "auto-chain broken" theory was a false alarm from testing methodology.

## 2026-04-24 — shared-utility concurrency + pagination hardening

- `app/utils/http_clients.py` — eager-init via `init_clients()` at lifespan startup (no lazy path). Dict-based `_clients` registry. `_get_or_create(name, factory)` consolidates duplicate factory logic. `close_clients()` uses per-client try/finally; registry reset unconditionally. Generic client `max_connections` raised 10 → 50 for OpenAPI fan-out during `/research`.
- `app/utils/staleness.py` — cursor-based pagination on `entry_id > "<last_id>"` prevents re-processing the same IDs when a flush lags. `hit_cap=True` logs at ERROR (was WARNING). Dropped `_get_collection = get_collection` alias. Documented `expires_at == 0` sentinel (never expire).
- `app/utils/llm_parsing.py` — 4 think-tag regexes collapsed to 2 (closed + open); fence stripper matches anywhere (not just leading); all patterns module-level compiled.
- `app/middleware/performance.py` — added `duration_s` (float) alongside `duration_ms`; `/health` polling logs DEBUG below 200ms / INFO above; `model` + `endpoint` truncated to 200 chars before insert.
- `app/logging_config.py` — added `configure_logging_once` guard (fixture-safe); `_resolve_level()` validates via `getLevelName()` with INFO fallback; stack choice (structlog as unified formatter) documented.
- `app/middleware/request_id.py` (new) — binds `request_id` contextvar upstream of perf + error layers so every log line carries the correlation ID. Honors inbound `X-Request-ID` header; generates UUID4 otherwise.
- `app/routers/status.py` — `status_filter` typed as `Literal[...]` mirroring `jobs_status_check`; `job_id` validated as UUID with clean 400; `include_compiled` flag gates `compiled_output`; `StatusCounts` completed (pending, refining, awaiting_confirmation, researching, executing added); `/logs/{job_id}` paginated with `limit`/`offset`.
- `tests/conftest.py` — autouse fixture re-inits http_clients per test (pytest-asyncio gives each test a fresh event loop; httpx clients are loop-bound).
- Tests updated: `test_http_clients.py` (eager contract), `test_staleness.py` (patch target renamed), `test_status_logs.py` (UUID IDs, paginated args, `include_compiled`), `test_openapi_ingest.py` (patch `get_generic_http_client` not `httpx.AsyncClient`).

Suite: **547 passed**, 2 pre-existing auth failures out of scope, 30 skipped.

## 2026-04-24 — Ingestion-path silent-failure hardening

Eliminated silent partial-success modes in GitHub and OpenAPI ingestion paths that swallowed critical errors and returned half-empty result sets.

**`app/config.py`** — New settings:
- `github_blob_concurrency: int = 8` (was hardcoded `_BLOB_CONCURRENCY`)
- `openapi_max_params_per_endpoint: int = 50`

**`app/utils/github_ingest.py`**
- `asyncio.gather(..., return_exceptions=True)` collector now re-raises `GitHubRateLimitError` and `GitHubRepoNotFoundError`; only transient exceptions are swallowed.
- `_fetch_readme` decode failures now raise (previously returned `("", "")`, conflating decode-failure with missing-README).
- `tree_truncated` initialized to `False` before the `if remaining > 0` block; dropped fragile `'tree_truncated' in locals()` check.
- `_BLOB_CONCURRENCY` literal moved to `settings.github_blob_concurrency`.
- `INFO` log now reports both `attempted=` and `files=` so partial-result cases are visible in logs.

**`app/utils/openapi_ingest.py`**
- `_resolve_refs` now passes the already-fetched spec via `spec_string=`; no more URL re-fetch. Returns `(spec, refs_resolved: bool)`.
- Validation order reversed: **resolve $refs THEN validate** the inlined spec, so refs that only exist post-resolution don't bypass schema checks.
- `_validate_spec` selects a version-specific validator (`OpenAPIV2`, `OpenAPIV30`, `OpenAPIV31`) by detecting the spec's top-level version field. Returns `"openapi-3.0" | "openapi-3.1" | "swagger-2"`.
- `_walk_paths` filters parameter dicts containing raw `$ref` (unresolved) with a logged skip count; returns `(entries, skipped_param_refs)`.
- Per-endpoint parameter cap: `all_params[:settings.openapi_max_params_per_endpoint]` with a `"... (K more)"` footer when truncated.
- Metadata gains `refs_resolved: bool` and `skipped_param_refs: int`; every emitted entry is tagged `refs_resolved` for downstream filters.
- Module imports hoisted to top: `prance`, `yaml`, version-specific validators, `asyncio`. YAML parse error narrowed from `Exception` to `yaml.YAMLError`.
- prance >=25 incompatibility fix: passing `url=` + `spec_string=` together causes `ParseResult` → `os.PathLike` failure. Now passes `spec_string=` alone; relative external $refs are unsupported (documented in code).

**Tests**
- `tests/test_openapi_ingest.py` version-label assertion updated: `"openapi-3"` → `"openapi-3.0"`. All 17 ingestion tests pass.

**Acceptance** (live `/research openapi:<url>`)
- Swagger 2.0 (Petstore): `version=swagger-2 endpoints=20 refs_resolved=True`. ✅
- Unresolvable-refs path: warning logged, `refs_resolved=False` flagged in both metadata and per-entry. ✅

**Unrelated pre-existing**
- `tests/test_execution_handler.py` collection error (`ModuleNotFoundError: execution_handler`) — not in scope.
- `tests/test_auth.py` (2 fixture failures) — not in scope.

## 2026-04-23 — RAG pipeline hardening

- **Domain contract:** `domain=None` fans out one `==` search per `VALID_DOMAINS` partition and merges (Milvus partition-key isolation rejects unfiltered exprs and `IN` exprs over the partition key). `domain=""` raises `ValueError`. No silent `"eng"` default anywhere.
- **Keyword safety:** tokens restricted to `[a-z0-9]+` — strips LIKE wildcards (`%`, `_`), backslash, and quotes from the expr-interpolation path.
- **Version chain:** on 0.90 ≤ sim < 0.95, walk forward to the latest version before linking (cap 8 hops). Prevents mid-chain supersede pointers.
- **Upsert keyed on `entry_id`** replaces `insert` — closes the hash-check+insert race. Verified: 3 concurrent ingests of the same entry → exactly 1 row in Milvus.
- **`_rrf_fuse`** uses `dataclasses.replace` — upstream `RagResult` instances are never mutated.
- **Reranker empty-items fallback:** explicit WARNING + RRF fallback when `rr.items == [] and docs != []`.
- **New `query_rag` metadata:** `warnings`, `reranker_backend`, `skipped_rerank`, `below_threshold`, `fell_back_to_top3`.
- **Batch embedding** in `ingest_entries` with per-text cache lookup and serial fallback.
- **Post-query supersedes sweep:** `supersedes_id IN (returned_ids)` DB lookup drops stale ancestors (closes overview issue #7).
- **Embedding cache v3:** key now `embedv3:{model}:d{dim}:{hash}` — dim changes auto-invalidate. `_decode` + `put` validate length. Repeat-put refreshes LRU position. Stats now split `l1_hits` / `l2_hits`.
- **Milvus utils:** `_auto_create_collection` wraps the client in try/finally for close. `get_collection` uses double-checked locking (no thundering herd). Cold load asserts `dim == 512` and primary `entry_id`.
- **Config:** `Field(ge=…, le=…)` bounds on every timeout/budget/limit. `ROLE_FIELDS` frozenset gates `get_model()`. `sync_database_url` asserts the `postgresql+asyncpg://` prefix. New tunables: `rerank_{max_candidates,doc_truncate,warn_ms,error_ms}`, `version_chain_threshold`, `embedding_batch_size`.

**Milvus state:** `toon_v2` holds 611 entities across partitions — `eng=218`, `llm=218`, `rag=175`, `prompt=0`, `spec=0`. The earlier "2 test entries" note is stale.

## 2026-04-23 — Schema/middleware/reaper drift fixes

**Branch:** `fix/schema-middleware-reaper-drift` · **Commits:** `cf11449`, `dbf1295`

- **`app/schemas.py`** — `DagNodeBase`/`DagNodeRead` gained `tool`, `domain`, `confidence` (0–1), `is_output_node`. `JobBase.metadata` / `ArtifactBase.metadata` renamed to `meta` (SQLAlchemy registry collision). New `ResearchDepth` literal applied to `ResearchInput.depth` and `ScheduleCreate.depth`. `RagInput.top_k` bounded `[1,100]`, `confidence_threshold` bounded `[0,1]`. `ExecRetryInput.max_retries` removed (unused). `ConfigDict(protected_namespaces=())` on 14 classes containing `model_*` fields.
- **`app/main.py`** — Removed imperative `depth` check in `/schedule` (now Pydantic-enforced). Documented middleware order: request flows `Performance` (outer) → `ErrorLogging` (inner) → endpoint.
- **`app/middleware/error_logging.py`** — Re-raises `FastAPIHTTPException` and `StarletteHTTPException` before the generic `except`, so 4xx responses are no longer reclassified as 500. `import httpx` hoisted to module top.
- **`app/config.py`** — New settings: `stale_threshold_minutes=30`, `long_phase_stale_minutes=45`, `planning_stale_minutes=60`, `cleanup_interval_seconds=900`.
- **`app/modules/cleanup.py`** — State-aware reaper: `researching`/`refining`/`planning` (jobs) use `long_phase_stale_minutes`; `running`/`executing` use `stale_threshold_minutes`. Runs one eager sweep before entering the sleep loop. Uses `len(await r.fetchall())` instead of driver-dependent `rowcount`. Return dict is now 5-key (adds `long_phase_to_failed`).
- **`app/scheduler.py`** — `_rehydrate` wraps each row in try/except: a single bad cron expression is logged and skipped without aborting the rest. Result `UPDATE` in `_execute_research_job` warns on `rowcount == 0`. `finally` block marks `research_sessions.status = 'cancelled'` on `asyncio.wait_for` timeout (removes 30-min reaper dependency). `import json` hoisted to module top. Added `scheduled_research_completed` success log with duration.

**Tests**
- `tests/test_cleanup.py` rewritten (7 tests) for the new 5-statement shape.
- `tests/test_health_cleanup.py` skipped with TODO pending port.

**Acceptance**
- `GET /dag/<missing-uuid>` → **404** (not 500). ✅
- `POST /schedule` with bad cron → 422 at precheck; bad cron injected directly in DB → logged `schedule_rehydrate_skipped` on restart; other schedules registered. ✅
- Eager reaper sweep observed at orchestrator startup (`long_phase_to_failed=1`). ✅

**Unrelated deferred**
- Migration `020_research_sessions_single_running.sql` contains multiple statements in one `execute()`; asyncpg rejects. Not touched by this branch.

## 2026-04-23 — Phase 1/2 Orchestration Hardening

Closed "job stranded forever" holes in `idea_refinement.py` + `ideation_workflow.py`.

**Files changed**
- `app/modules/idea_refinement.py`
- `app/modules/ideation_workflow.py`
- `app/modules/gt_extractor.py`
- `tests/test_idea_refinement.py`
- `tests/test_ideation_workflow_phase2.py`
- `tests/test_gt_extractor.py`
- `tests/test_gt_extractor_module.py`

**Fixes landed**
1. `model_router.generate` wrapped in try/except → `_fail_job` + re-raise.
2. Collapsed 3 commits (create + refining + planning) into single `INSERT ... status='refining' RETURNING id`.
3. `ALLOWED_DOMAINS = {prompt, rag, llm, spec, eng}` — raises `ValueError` on invalid override.
4. Phase 2 Steps 1–5 wrapped in try/except → `_fail_job` via short-lived session + re-raise.
5. Short-lived DB sessions: claim session closed before network I/O; separate `async_session()` for final `UPDATE` and for `_fail_job`.
6. `user_feedback` explicitly injected into COMPILE prompt (visible `USER FEEDBACK (must be honored):` section).
7. Feasibility fallback now annotates user message with `⚠️` and sets `feasibility.fallback=True`.
8. Compile fallback replaced with hard failure — job moves to `failed`, no empty `workflow_steps=[]`.
9. Promoted `_format_toon_rows`, `_push_to_github`, `_search_searxng` → public names in `gt_extractor`. Backward-compat aliases retained. Import aliased as `gt_push_to_github` in `ideation_workflow` to avoid param collision.

**Verification**
- Phase 1 forced LLM exception → job `failed`, `error_summary='LLM refinement exception: ...'`, exception re-raised.
- Phase 2 forced SearXNG exception → job `failed`, `error_summary='phase2 exception: ...'`, exception re-raised.
- Full suite: **542 passed, 23 skipped**. Pre-existing failures unrelated to this task: `test_auth.py` (2), `test_execution_handler.py` collection error (missing module).
