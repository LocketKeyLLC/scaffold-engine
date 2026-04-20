# Scaffold Engine — Drift Findings

**Purpose:** Items discovered during the audit pass that were not on the
original fix lists. These fall into three categories:

- **Resolved** — items we fixed during the audit session
- **Reframed** — items where the original audit description turned out
  to be incorrect or incomplete; the fix was different than proposed
- **Open** — newly-discovered items that still need action

Each entry cross-references the commit (if resolved) and the audit
context in which it surfaced.

---

## Resolved

### #120-inverse — `_get_collection()` blocking the event loop in `query_rag`
**Commit:** `ca4faff`
**Context:** Surfaced during Module 11 Phase E when investigating audit
item #120.
**Summary:** Audit item #120 claimed the `run_in_executor(None, _get_collection)`
wrap in `ingest_entries` was unnecessary. Evidence showed the wrap is
required — `get_collection()` makes 3 Milvus RPCs (list_collections,
has_collection × 2). The opposite issue existed: `query_rag` called
`_get_collection()` synchronously at L360, blocking the event loop for
the duration of the RPC round-trip. Fixed by adding the executor wrap
to match `ingest_entries`.

### Docker-compose tests/ mount missing
**Commit:** `71e99ca`
**Context:** Surfaced during the drift-items session while trying to
add new RRF tests for #112.
**Summary:** `docker-compose.yml` mounted `./app:/code/app:ro` but had
no mount for `./tests:/code/tests`. The orchestrator container was
running pytest against a baked-in copy of `tests/` from image build time,
so any host-side edits to test files were invisible until image rebuild.
Fortunately `app/` mount was correct, so every committed code fix was
validated against the real container state — only test file edits were
silently ignored. Fixed by adding `./tests:/code/tests:ro` to the
orchestrator `volumes` block and force-recreating the container.

### RRF fusion test coverage gap for #112
**Commit:** `916d4ef`
**Context:** The existing `TestRRFFusion` tests all constructed
`RagResult` with explicit `entry_id="e1"`/`"e2"`, exercising the new
primary dedup path (good). But no test covered the fallback path
(entries with empty `entry_id` dedup on `content[:200]`). Added two
new tests: `test_fuse_dedup_uses_entry_id_not_content_prefix`
(positive path) and `test_fuse_falls_back_to_content_when_entry_id_missing`
(fallback).

---

## Reframed

### #116 — "Use parameter binding for Milvus expressions"
**Original audit framing:** Suggested introducing SQLAlchemy-style bind
parameters for Milvus `.query(expr=...)` and `.search(expr=...)` calls.
**Why it was wrong:** pymilvus does not support bind parameters on
`expr` strings — the audit author was thinking in SQL terms. The existing
inline escape pattern (`safe_word = word.replace("'", "\\'").replace('"', '\\"')`)
is the correct hardening for pymilvus.
**Resolution:** Converted to a doc-only fix. Module docstring now
explicitly describes the escape model. See `0a0006b`.

### #120 — "Remove unnecessary `run_in_executor` around `_get_collection`"
**Original audit framing:** Remove executor wrap in `ingest_entries`.
**Why it was wrong:** `get_collection()` makes 3 Milvus RPCs and is
blocking. Wrap is required.
**Resolution:** Won't-fix as originally specified. Opposite issue
(#120-inverse, see above) filed and resolved.

---

## Open

### Stale `.pyc` / AST cache in container
**Context:** During Module 11 drift session, Python AST parse inside
the container returned an outdated structure even after file edits
landed via the mount. Clearing `tests/__pycache__` resolved it.
**Severity:** Low — has a known workaround (`find /code/tests -name
__pycache__ -exec rm -rf {} +`).
**Possible fix:** Add `PYTHONDONTWRITEBYTECODE=1` to the dev compose
env or a Makefile target for `make clean-pyc`.

### `PytestUnraisableExceptionWarning: Invalid file descriptor: -1`
**Context:** Emitted by `tests/test_rag_pipeline.py` during full-suite
runs. Does not fail tests; benign event-loop teardown race.
**Severity:** Low — warning only.
**Possible fix:** Enable `tracemalloc` to identify which test creates
the unclosed descriptor. Likely `asyncio.new_event_loop()` in a test
helper that doesn't clean up.

### Open Overview Issue #16 (timestamp mismatch) — resolved, remove
**Context:** `scaffold-engine-overview.md` "Known Open Issues" #16
describes the `scheduled_jobs.next_run_time` DOUBLE PRECISION →
TIMESTAMPTZ mismatch. This was resolved in `ad06f3d` via Python-side
datetime retrieval.
**Severity:** Informational — overview file drift.
**Action:** Remove from overview during Phase 10 rewrite.

### Overview "Known Open Issues" #10–#12 — resolved, remove
**Context:** #10 (distillation regression, resolved in `02bdc22`),
#11 (target_status hardcode, resolved in `02bdc22`), #12 (execution_handler
field mismatches, resolved in `cd039ce`) are all listed as open.
**Severity:** Informational — overview file drift.
**Action:** Remove from overview during Phase 10 rewrite.

---

## Audit metadata

- **Original fix lists:** 251 items (155 Phase 1–5 + ~96 Phase 6–10)
- **Audit corrections from drift:** #116 reframed, #120 won't-fix,
  #120-inverse added
- **Resolved-by-drift-session items:** 3

