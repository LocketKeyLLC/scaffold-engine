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

### #24 — "Reconcile DAG prompt 3-10 steps with code enforcement ≤10 only"
**Original audit framing:** Prompt says 3-10 but code only enforces ≤10; undersize DAGs slip through.
**Why it was wrong:** `_enforce_node_count` has `min_count=3` and was only logging at undercount — but that's a separate bug (#23). The prompt and the `_enforce_node_count` bounds already agreed on 3-10. With #23 fixed (undercount now raises), the prompt and code are fully aligned.
**Resolution:** Subsumed by #23. Marked resolved as part of Phase B.

### #100 — "Share adjacency structure between validate_dag and _build_edges"
**Original audit framing:** Duplicate graph traversal.
**Why it was wrong:** The two functions produce genuinely different output shapes. `validate_dag` builds a successors map (`dict[str, list[str]]` keyed by source node id) as input to Kahn's cycle-detection algorithm. `_build_edges` builds an edge-record list (`list[{"from": x, "to": y}]`) consumed by Mermaid rendering and root/disconnected checks. They share underlying edge information but serve different consumers — merging them would require a transformation layer that adds complexity without reducing traversal cost.
**Resolution:** Won't-fix as specified. Logged in fix-list as reframed.

### #107 — "isinstance(raw, dict): continue silently skips non-dict tasks"
**Original audit framing:** Add to errors list.
**Why it was stale:** Already implemented before the Module 12 audit pass. Line check on `_normalize_tasks` shows `errors.append(f"Task {i}: must be an object")` followed by `continue`.
**Resolution:** No code change needed; marked resolved as stale.

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

### `PytestUnraisableExceptionWarning: Invalid file descriptor: -1` — RESOLVED
**Context:** Emitted by `tests/test_rag_pipeline.py` during full-suite runs.
**Resolution:** `tests/test_rag_pipeline.py:_run()` was leaking event loops
via `asyncio.new_event_loop().run_until_complete(...)` without a
matching `loop.close()`. Wrapped in try/finally; the descriptor warning
no longer fires.

---

## Audit metadata

- **Original fix lists:** 251 items (155 Phase 1–5 + ~96 Phase 6–10)
- **Audit corrections from drift:** #116 reframed, #120 won't-fix,
  #120-inverse added
- **Resolved-by-drift-session items:** 3


---

### Attempted fix for #9.8 / #9.9 — reverted (2026-04-21)

**Audit item:** `conftest.py` eager-imports `app` and `app.model_router`, forcing
pipeline tests to use `--noconftest`.

**Attempted fix:** Removed the two eager imports on the theory that no test
module actually depended on them.

**Result:** 16 collection errors, all of the shape
`ModuleNotFoundError: No module named 'app.config'; 'app' is not a package`.

**Root cause:** Because `tests/` is not configured as a proper package (no
`tests/__init__.py`, no explicit rootdir), pytest's path-based module finder
discovers `app/` via `sys.path` manipulation. The eager `import app` in
`conftest.py` is what causes `app` to be materialized as a proper package
object before test modules reference `app.utils.x`, `app.modules.x`, etc.

**Decision:** Keep the imports. Annotate them as load-bearing with a pointer
to this finding. Future fix would require converting `tests/` to a proper
package — scoped larger than an audit item.

**Items marked [x] with caveat:** #9.8, #9.9 — addressed via documentation
rather than code change.

---

### Update to #9.8 / #9.9 (2026-04-21, after #9.6 split)

When the file splits for #9.6 added `tests/__init__.py` to make
`from tests._scaffold_router_setup import ...` work, this reduced the
collection errors from 16 down to 2 on removing the eager imports. The
2 remaining errors are specifically in `test_main.py` and
`test_integration.py`, both of which do `from app.model_router import
close_client` — and pytest's path finder still fails to resolve
`app.model_router` as a package path in those two files without the eager
import priming it.

**Decision:** Still keep the eager imports. The workaround isn't 100%
complete, and chasing the last 2 files would require refactoring
test_main / test_integration.

