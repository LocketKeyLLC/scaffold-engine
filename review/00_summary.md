# Scaffold-Engine Full Review — Synthesis

**Source files:** `01_foundation.md`, `02a_infra.md`, `02b_runtime.md`, `03a_modules.md`, `03b_modules.md`, `03c_modules.md`, `04_pipelines.md`, `05_schema.md`. Tests phase skipped per user direction.

## Coverage and counts

| phase | source LOC | HIGH | MED | LOW | DEAD |
|---|---|---|---|---|---|
| 1 — Foundation | ~2.6k | 2 | 13 | 10 | 5 |
| 2a — Infra utils | ~3k | 2 | 2 | 1 | 0 |
| 2b — Scheduler + routers | ~750 | 2 | 1 | 2 | 0 |
| 3a — Research/ingest | ~3k | 3 | 9 | 13 | 0* |
| 3b — Ideation/DAG | ~3k | 2 | 7 | 10 | 0* |
| 3c — Execution/assist | ~3k | 2 | 5 | 4 | 2 |
| 4 — OWUI pipelines | ~4k | 4 | 6 | 7 | 0* |
| 5 — Schema | ~750 | 1 | 8 | 7 | 5 |
| **Total** | **~20k read** | **18** | **51** | **54** | **12** |

*Phase 3a/3b/4 emitted DEAD-tagged findings inside the LOW table rather than a separate DEAD section; counted under LOW above.

Grand total: **135 distinct findings** across the application + pipelines + schema. Tests (~14.8k LOC) explicitly skipped.

---

## Severity-ranked HIGH list (18)

Ranked by **blast radius first, fix-effort second**.

### Tier 1 — production hot paths

1. **`app/migrations.py:173-189` (CORRECT/ARCH)** — Postgres advisory lock is acquired inside `async with db.begin():` but the per-file apply loop runs **outside** that block. Two replicas booting concurrently can both compute the same `pending` list, drop the lock, and both apply migrations. Phase 5 amplifies: 7 of 22 migrations contain `BEGIN/COMMIT` (011, 013, 014, 015, 016, 022, 023); 022 and 023 always run live on established DBs.
2. **`app/migrations.py:138-147` (CORRECT)** — Own-transaction migration path wraps asyncpg's raw `execute(BEGIN; …; COMMIT;)` inside an outer `async with db.begin():`. asyncpg refuses `BEGIN` inside an active transaction. Migrations 022 and 023 hit this on every established DB.
3. **`app/scheduler.py:143-165` (ARCH) + `app/main.py:865-874` (CORRECT)** — APScheduler ↔ DB ordering bug, **both directions**: `add_schedule` registers in APScheduler before committing the DB row (in-memory ghost on DB failure); `delete_schedule` commits the DB DELETE before calling `remove_schedule` (DB row gone, scheduler still firing). Cross-direction symmetry confirms a shared design oversight.
4. **`app/modules/ideation_workflow.py:252` (ARCH/CORRECT)** — `await db.close()` is called mid-Phase-2 before research/compile I/O completes; subsequent `db.execute()` calls (L254-356) run on a closed/recycled session. This is a live race-condition hazard during /ideate/confirm.
5. **`app/modules/execution_agent.py:642` (CORRECT)** — Timeout/exception path marks `dag_nodes.status='failed'` without persisting `optimized_prompt`. The retry path (`/exec/retry`) then has no prompt to run from; manual recovery required.

### Tier 2 — auditability / data-integrity gaps

6. **`app/modules/rag_pipeline.py:819-831` (DEAD/CORRECT)** — Version-chain (supersede) entries skip the `dedup_log` audit row. Invariant #9 expects parity with rejected duplicates. Audit incomplete.
7. **`app/modules/assist_agent.py:369` (DEAD/CORRECT)** — `assist_steps.status='applied'` exists in the migration-023 CHECK constraint but no code path writes it. Either reserved for unimplemented behavior or refactor residue.
8. **`app/utils/github_ingest.py:209` (CORRECT)** — `gather(return_exceptions=True)` followed by a generic `isinstance(item, Exception)` swallows `CancelledError`. Breaks task cancellation propagation; long /research GitHub ingests can't be cleanly cancelled.

### Tier 3 — invariant violations

9. **`app/modules/ideation_workflow.py:46` (ARCH)** — Module imports `structlog.stdlib.get_logger` instead of `logging.getLogger("scaffold...")`. Direct violation of invariant #2.
10. **`app/model_router.py:36-38` (ARCH)** — Constructs ad-hoc `httpx.AsyncClient` instead of using `app.utils.http_clients`. Bypasses the shared client invariant.
11. **`app/main.py:93-97, 172` (ARCH)** — Sync PyMilvus `connect`/`disconnect` directly inside async `lifespan`. Blocks event loop on startup/shutdown.

### Tier 4 — UX / OWUI integration

12. **`pipelines/scaffold_router.py:893-938` (ARCH)** — Auto-chain `/ideate/confirm → /dag → /execute/all` has no recovery state machine; mid-chain failures leave jobs orphaned without surfaced retry path.
13. **`pipelines/{execution_handler,dag_viewer,gt_browser,prompt_inspector}.py` (SEC)** — API-key drift between `valves.json` and `SCAFFOLD_API_KEY` env warned via `print()` to stdout — invisible in OWUI UI; users debug stale-key 401s blind.
14. **`pipelines/scaffold_router.py:1460` (PERF)** — SSE consumer hardcodes `timeout=(30, 120)`; 120-second read timeout aborts long-running `/research` and `/execute/all` streams regardless of the `stream_timeout` valve (3600s default).
15. **`pipelines/*` (PERF)** — All 5 pipelines call bare `requests.get/post` per call instead of a module-level Session; every command opens a fresh TCP connection to the orchestrator.

### Tier 5 — orchestrator bugs surfaced incidentally

16. **`app/main.py:740` (CORRECT)** — `/research/pdf` crashes with `AttributeError` when `UploadFile.filename` is None (legal per Starlette).
17. **`app/main.py:441-459` (CORRECT)** — `GET /dag/{job_id}` returns 500 instead of 400 on malformed UUID (no validation before SQL).
18. **`app/main.py:666-678` (CORRECT)** — `/execute` returns dict-error responses without HTTPException conversion, breaking client-side status-code parsing relative to /ideas, /dag, /rag.

---

## Cross-phase patterns

### Pattern A — Logger identity is broken in ≥5 places
Invariant #2 says "stdlib `logging.getLogger("scaffold...")` only; structlog is the formatter, not the runtime logger." Violations:
- `ideation_workflow.py:46` — uses `structlog.stdlib.get_logger` (HIGH).
- `execution_handler.py:78` — uses `logging.getLogger(__name__)` → resolves to `app.modules.execution_handler`, not `scaffold.*`.
- `prompt_optimizer.py:16`, `prompt_inspector.py:11` — same `__name__` pattern.
- `prompt_assembly.py:32` — uses `logging.getLogger("scaffold")` with no submodule suffix; logs unattributable to source.

### Pattern B — HTTP-client pool is bypassed in 6+ places
Invariant #3 says shared httpx clients live in `app.utils.http_clients`. Bypasses:
- `app/model_router.py:36-38` — ad-hoc Ollama client (HIGH).
- `app/main.py:221-230` (`/health` `_check_ollama`) — fresh `httpx.AsyncClient(timeout=5)` per probe.
- All 5 OWUI pipelines — bare `requests` calls per command (HIGH).

### Pattern C — Postgres ↔ APScheduler state ordering bugs in **both** directions
- `add_schedule`: APScheduler-first then DB (Phase 2b HIGH).
- `delete_schedule` in main.py: DB-first then APScheduler (Phase 1 HIGH).
- Indicates the original design did not adopt a consistent "register-with-rollback" pattern.

### Pattern D — Schema CHECK enum members never written in code
Phase 1 flagged `error_logs.error_type IN (..., 'model_failure', 'structural')` as suspicious because the `_classify_error` middleware emits only 4 of 6. Phase 3c grep confirms: **no execution/assist module emits those values directly either**. Both CHECK members are confirmed dead at the application layer (Phase 5 DEAD finding).

### Pattern E — Pydantic ↔ DB column name drift
- `JobBase.meta` (schemas.py:88) vs. `jobs.metadata` (init.sql:23) — no alias, dropped silently.
- `ArtifactBase.meta` (schemas.py:241) vs. `artifacts.metadata` (init.sql:101) — same drift.

### Pattern F — Placeholder/UUID validation inconsistent at the boundary
Foundation:
- `/dag/{job_id}` GET — no UUID validation (MED).
- `/research/pdf` — `file.filename.lower()` crashes when filename is None (MED).
- `/jobs/*` paths — validate consistently.

Pipelines:
- `/research <topic>` — placeholder check at scaffold_router:863.
- `/confirm <job_id> [feedback]` — no placeholder check on feedback (L888).
- `/research/reply [session_id]` — no placeholder check on session_id (L826).

### Pattern G — Cancellation safety is uneven
- Research path: `_run_with_session_lifecycle` correctly catches `CancelledError` and finalizes session as `cancelled` (Phase 3a confirmation).
- GitHub ingest: swallows CancelledError as transient (Phase 2a HIGH).
- Pipelines SSE consumer: 120s timeout aborts long streams instead of forwarding cancellation (Phase 4 HIGH).

### Pattern H — Field-name dual-aliases in ingest code
RAG ingest accepts `source`/`source_url`, `content`/`canonical_text`, `title`/`topic`, `tags`/`domain_tags` (Phase 3a LOW × 4). Each pair compounds the test surface and obscures the canonical schema.

---

## Retractions and carry-forward resolutions

| origin | claim | resolution |
|---|---|---|
| Phase 3c (HIGH #6) | `app/modules/execution_agent.py:642` failure path leaves `optimized_prompt=NULL` | **Retracted during cluster D verification.** Both the timeout (L629) and general-exception (L642) paths explicitly pass `optimized_prompt=exec_prompt` to `_set_node_status`, which `COALESCE`s the value into the row (L120). Adjacent real gap exists: prompt-build / RAG-injection at L530-L595 is unwrapped, so an exception there leaves the node `'running'` until the 60-min orphan reaper resets it — flagged for future work, not part of cluster D. |
| Phase 1 (DEAD) | `app/schemas.py:601-605` `ResearchSessionSummary.{domain,depth}` typed non-Optional but DB may permit NULL | **Retracted.** Phase 5 confirms migration 010 declares both columns `NOT NULL` with defaults (`'medium'`, `'eng'`). Pydantic types are correct. |
| Phase 1 (DEAD) | `error_logs` CHECK enum has unused members (`model_failure`, `structural`) | **Confirmed.** Phase 3c grep finds no module writes those values directly; classifier only emits 4 of 6. |
| Phase 1 (DEAD) | `PromptUpdateInput.reason` declared but never threaded through | **Partially confirmed.** Phase 3b notes prompt_inspector accepts `source` but optimizer never writes `source='optimizer'`; the `reason` field on the API input remains unused. |
| Phase 1 carry-forward | `/jobs` whitelists status, `/research/sessions` does not | **Confirmed at API layer** (Phase 1 MED); no schema-level CHECK on `research_sessions.status` widens the gap. |
| Phase 2b carry-forward | Scheduler add path symmetric to delete-path bug? | **Confirmed.** Phase 2b HIGH on `scheduler.py:143-165` mirrors the main.py:865-874 bug in the opposite direction. |

---

## Prioritized fix queue (review-only — no patches per user direction)

Order = "what to fix first if I had to pick a sprint."

1. **Migration runner (`app/migrations.py:138-189`)** — both the lock-release-before-apply race AND the BEGIN-inside-asyncpg-txn defect. Migrations 022 and 023 hit this on every fresh production DB.
2. **APScheduler/DB ordering** (both `app/scheduler.py:143-165` and `app/main.py:865-874`) — adopt one register-with-rollback pattern in both directions.
3. **`app/modules/ideation_workflow.py:252`** — remove the mid-Phase-2 `await db.close()`; restructure session lifecycle.
4. **`app/modules/execution_agent.py:642`** — persist `optimized_prompt` on failed nodes so /exec/retry has something to retry.
5. **`app/utils/github_ingest.py:209`** — narrow the `Exception` catch to exclude `CancelledError` (or special-case re-raise).
6. **`app/modules/rag_pipeline.py:819-831`** — write a `dedup_log` row for `action_taken='versioned'` to satisfy invariant #9.
7. **OWUI auto-chain (`pipelines/scaffold_router.py:893-938`)** — add a recovery surface for partial failures (likely a state-bearing job-status read at chain start).
8. **OWUI SSE timeout (`pipelines/scaffold_router.py:1460`)** — derive the read timeout from the `stream_timeout` valve, not a hardcoded 120s.
9. **OWUI HTTP session reuse** — module-level `requests.Session()` per pipeline.
10. **Logger identity sweep** — ideation_workflow, execution_handler, prompt_optimizer, prompt_inspector, prompt_assembly all need `logging.getLogger("scaffold.<sub>")`.
11. **Foundation MED list** — `/research/pdf` filename guard, `/dag/{id}` UUID validation, `/execute` HTTPException conversion, `/rag/dedup` + `/gt/list` limit caps, `list_research_sessions` status whitelist, X-Request-ID sanitization.
12. **Schema rebaseline** — refresh `init.sql` to reflect the post-023 union, OR document explicitly that init.sql is post-008-baseline and the runner advances it.
13. **Dead enum cleanup** — drop `error_logs.error_type IN ('model_failure','structural')` from the CHECK constraint (and `assist_steps.status='applied'`) once code-side audit completes.
14. **Pydantic ↔ DB alias drift** — either add `Field(alias="metadata")` + `populate_by_name=True` for JobBase/ArtifactBase, or drop the `meta` field.

---

## Items NOT covered (out of scope)

- **Tests phase skipped** per user direction — no coverage matrix produced. Risk: untested code paths in execution_agent's retry loop, ideation_workflow's session-lifecycle, and scheduler's misfire handling are not enumerated here.
- **Performance benchmarking** — review identifies likely PERF issues but does not measure them. The `tests/benchmarks/` directory was not audited.
- **Observability completeness** — log-line fan-out, metric coverage, and alerting hooks were not systematically audited beyond foundation-level middleware review.
- **Deployment surface** — Dockerfile, docker-compose.yml, .env.example were not audited as part of this review.
- **OWUI valves.json** content was not read; only pipeline code that consumes them was reviewed.

---

## Confidence note

Findings cite `file:line` and a short evidence quote so each can be independently verified. The synthesis trusts the per-phase findings files; if any phase agent fabricated a citation, it propagates here. Spot-check by opening any cited line and confirming the claim against the source. Phase 1 was main-thread; Phases 2a/2b/3a/3b/3c/4 were Explore-agent work; Phase 5 was main-thread. Phase 3a's reported logger violation in `ideation_workflow.py:46` is the same as Phase 3b's report — corroborated by two independent agents.
