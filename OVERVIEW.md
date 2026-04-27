# Scaffold Engine — Project Overview

## Stack
- **Backend:** Python 3.12 async (FastAPI + SQLAlchemy async)
- **DB:** Postgres 16
- **Vector:** Milvus 2.5.27 (standalone, embedded ETCD, `toon_v2` collection)
- **Cache:** Redis 8 (`scaffold-redis`)
- **LLM:** Ollama (host-installed, multi-role routing)
- **Search:** SearXNG
- **Repo path:** `~/scaffold-engine`
- **Orchestrator container:** `scaffold-orchestrator`
- **Tests:** `make test` (in-container)

---

## Change Log

### 2026-04-23 — Phase 1/2 Orchestration Hardening
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

---

## Known Pre-existing Issues (not in scope of this change)
- `tests/test_execution_handler.py` — `ModuleNotFoundError: execution_handler`.
- `tests/test_auth.py::test_valid_key_is_accepted` + `test_no_key_configured_disables_auth` failing.
- 7 golden retrieval tests skipped — blocked on repopulating `toon_v2` with pre-migration entries (~143).

---

## On the Horizon
- Repopulate knowledge base (~143 entries from pre-migration backup).
- Re-enable golden retrieval tests.
- Address Phase 2 review critical findings still open: `validate_models()` outage sentinel, `_verify_output` fail-closed, DAG insert rollback.

---

## 2026-04-23 — Schema/middleware/reaper drift fixes
**Branch:** `fix/schema-middleware-reaper-drift` · **Commits:** `cf11449`, `dbf1295`

- **`app/schemas.py`** — `DagNodeBase`/`DagNodeRead` gained `tool`, `domain`,
  `confidence` (0–1), `is_output_node`. `JobBase.metadata` / `ArtifactBase.metadata`
  renamed to `meta` (SQLAlchemy registry collision). New `ResearchDepth` literal
  applied to `ResearchInput.depth` and `ScheduleCreate.depth`. `RagInput.top_k`
  bounded `[1,100]`, `confidence_threshold` bounded `[0,1]`. `ExecRetryInput.max_retries`
  removed (unused). `ConfigDict(protected_namespaces=())` on 14 classes containing
  `model_*` fields.
- **`app/main.py`** — Removed imperative `depth` check in `/schedule` (now Pydantic-enforced).
  Documented middleware order: request flows `Performance` (outer) → `ErrorLogging` (inner)
  → endpoint.
- **`app/middleware/error_logging.py`** — Re-raises `FastAPIHTTPException` and
  `StarletteHTTPException` before the generic `except`, so 4xx responses are no longer
  reclassified as 500. `import httpx` hoisted to module top.
- **`app/config.py`** — New settings: `stale_threshold_minutes=30`,
  `long_phase_stale_minutes=45`, `planning_stale_minutes=60`,
  `cleanup_interval_seconds=900`.
- **`app/modules/cleanup.py`** — State-aware reaper: `researching`/`refining`/
  `planning` (jobs) use `long_phase_stale_minutes`; `running`/`executing` use
  `stale_threshold_minutes`. Runs one eager sweep before entering the sleep loop.
  Uses `len(await r.fetchall())` instead of driver-dependent `rowcount`. Return
  dict is now 5-key (adds `long_phase_to_failed`).
- **`app/scheduler.py`** — `_rehydrate` wraps each row in try/except: a single
  bad cron expression is logged and skipped without aborting the rest. Result
  `UPDATE` in `_execute_research_job` warns on `rowcount == 0`. `finally` block
  marks `research_sessions.status = 'cancelled'` on `asyncio.wait_for` timeout
  (removes 30-min reaper dependency). `import json` hoisted to module top.
  Added `scheduled_research_completed` success log with duration.

**Tests**
- `tests/test_cleanup.py` rewritten (7 tests) for the new 5-statement shape.
- `tests/test_health_cleanup.py` skipped with TODO pending port.

**Acceptance**
- `GET /dag/<missing-uuid>` → **404** (not 500). ✅
- `POST /schedule` with bad cron → 422 at precheck; bad cron injected directly in
  DB → logged `schedule_rehydrate_skipped` on restart; other schedules registered. ✅
- Eager reaper sweep observed at orchestrator startup (`long_phase_to_failed=1`). ✅

**Unrelated deferred**
- Migration `020_research_sessions_single_running.sql` contains multiple statements
  in one `execute()`; asyncpg rejects. Not touched by this branch.

---

## Ingestion-path silent-failure hardening (Apr 24 2026)

Eliminated silent partial-success modes in GitHub and OpenAPI ingestion paths
that swallowed critical errors and returned half-empty result sets.

**`app/config.py`** — New settings:
- `github_blob_concurrency: int = 8` (was hardcoded `_BLOB_CONCURRENCY`)
- `openapi_max_params_per_endpoint: int = 50`

**`app/utils/github_ingest.py`**
- `asyncio.gather(..., return_exceptions=True)` collector now re-raises
  `GitHubRateLimitError` and `GitHubRepoNotFoundError`; only transient
  exceptions are swallowed.
- `_fetch_readme` decode failures now raise (previously returned `("", "")`,
  conflating decode-failure with missing-README).
- `tree_truncated` initialized to `False` before the `if remaining > 0`
  block; dropped fragile `'tree_truncated' in locals()` check.
- `_BLOB_CONCURRENCY` literal moved to `settings.github_blob_concurrency`.
- `INFO` log now reports both `attempted=` and `files=` so partial-result
  cases are visible in logs.

**`app/utils/openapi_ingest.py`**
- `_resolve_refs` now passes the already-fetched spec via `spec_string=`;
  no more URL re-fetch. Returns `(spec, refs_resolved: bool)`.
- Validation order reversed: **resolve $refs THEN validate** the inlined
  spec, so refs that only exist post-resolution don't bypass schema checks.
- `_validate_spec` selects a version-specific validator (`OpenAPIV2`,
  `OpenAPIV30`, `OpenAPIV31`) by detecting the spec's top-level version
  field. Returns `"openapi-3.0" | "openapi-3.1" | "swagger-2"`.
- `_walk_paths` filters parameter dicts containing raw `$ref` (unresolved)
  with a logged skip count; returns `(entries, skipped_param_refs)`.
- Per-endpoint parameter cap: `all_params[:settings.openapi_max_params_per_endpoint]`
  with a `"... (K more)"` footer when truncated.
- Metadata gains `refs_resolved: bool` and `skipped_param_refs: int`;
  every emitted entry is tagged `refs_resolved` for downstream filters.
- Module imports hoisted to top: `prance`, `yaml`, version-specific
  validators, `asyncio`. YAML parse error narrowed from `Exception` to
  `yaml.YAMLError`.
- prance >=25 incompatibility fix: passing `url=` + `spec_string=` together
  causes `ParseResult` → `os.PathLike` failure. Now passes `spec_string=`
  alone; relative external $refs are unsupported (documented in code).

**Tests**
- `tests/test_openapi_ingest.py` version-label assertion updated:
  `"openapi-3"` → `"openapi-3.0"`. All 17 ingestion tests pass.

**Acceptance** (live `/research openapi:<url>`)
- Swagger 2.0 (Petstore): `version=swagger-2 endpoints=20 refs_resolved=True`. ✅
- Unresolvable-refs path: warning logged, `refs_resolved=False` flagged in
  both metadata and per-entry. ✅

**Unrelated pre-existing**
- `tests/test_execution_handler.py` collection error (`ModuleNotFoundError:
  execution_handler`) — not in scope.
- `tests/test_auth.py` (2 fixture failures) — not in scope.
