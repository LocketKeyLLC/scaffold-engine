# Scaffold Engine — Audit Fix List (Phases 6–10)

**Companion to:** `scaffold-engine-fix-list.md` (Phases 1–5b, 155 items)
**Generated:** April 18, 2026
**Scope:** Ideation, GT extraction, utilities, Open WebUI pipelines, test suite
**Total items:** ~140

---

## 🔴 Critical (runtime bugs / data integrity / security)

### Phase 6 — Ideation + GT
- [x] **#6.1** `ideation_workflow.py` — distillation uses `model_general` (235b cloud) instead of `model_router` (4b). Overview changelog April 14 #26 says this was fixed; either regression or doc drift. 200–500s CPU cost per run. — ✅ `02bdc22`
- [x] **#6.2** `idea_refinement.refine_idea` hardcodes return `status="planning"`, ignoring the `target_status` parameter. Callers pass `"awaiting_confirmation"` but it never takes effect. — ✅ `02bdc22`
- [x] **#6.3** `gt_extractor.py` — same `model_general` distillation regression as #6.1. — ✅ `02f7ecb`
- [x] **#6.4** `gt_detail` returns HTTP 200 with `{"found": false}` on miss instead of HTTP 404. — ✅ `02f7ecb`

### Phase 7 — Utilities
- [x] **#7.1** `http_clients.get_searxng_client` declares `global _searxng_client, _github_client` but `_github_client` is not defined at module scope in that function — stray global referencing wrong name.

### Phase 8a — scaffold_router
- [x] **#8.1** `/help` advertises `/results` command but `_handle_command` doesn't implement it. User-visible broken link. — ✅ `2989f0b`
- [x] **#8.2** `_handle_sse_event` silently drops SSE events of type `"error"` — orchestrator errors never surface in chat. — ✅ `2989f0b`

### Phase 8b — Other pipelines
- [x] **#8.3** `gt_browser._handle_detail` displays `source_url` twice (as "Source:" and "URL:") — duplicate render. — ✅ `94f74bb`
- [x] **#8.4** `execution_handler._approve` reads `d.get("model")` but orchestrator returns `model_used` → silent fallback to "default" on every call. — ✅ `cd039ce`
- [x] **#8.5** `execution_handler._approve` reads `d.get("output_preview")` but orchestrator returns `output` → preview section never renders. — ✅ `cd039ce`

### Phase 9 — Tests
- [x] **#9.1** `test_pipeline_complete.py` — ~6 of 10 tests are tautologies (e.g., `assert "pipeline_complete" == "pipeline_complete"`). Effectively zero real coverage.
- [x] **#9.2** `test_tasks_13_14_15_16.py::test_cache_dir_matches_compose` asserts `/app/.cache/huggingface`; actual Dockerfile path is `/code/.cache/huggingface`. Test is broken.

---

## 🟡 Medium (code quality / consistency / maintainability)

### Phase 6 — Ideation
- [x] **#6.5** `ideation_workflow.py` — mid-module imports (inside functions) violate PEP 8. Move to top. — ✅ `02bdc22`
- [x] **#6.6** Private function imports from `gt_extractor` (e.g., `_detect_topic_id`). Breaks encapsulation. — ✅ `02bdc22`
- [x] **#6.7** Misleading comment `"4b distillation"` next to code that calls 235b model.
- [x] **#6.8** Hardcoded caps (research query count, URL cap) with no config reference. — ✅ `02bdc22`
- [x] **#6.9** No concurrent-execution guard for Phase 2 (`research_and_compile`) — two simultaneous `/confirm` calls for same job would race. — ✅ `02bdc22`
- [x] **#6.10** Dead imports: `JobCreate`, `JobRead`. — ✅ `02bdc22`
- [x] **#6.11** `optimize_prompt` endpoint missing `model_overrides` param. — ✅ `02bdc22`
- [x] **#6.12** `_llm_verify` verify-fail path returns `intent_preserved=True` regardless of actual outcome.
- [x] **#6.13** `_llm_verify` substring `"true"` fallback in text — fragile heuristic. — ✅ `02bdc22`

### Phase 6 — GT
- [x] **#6.14** `gt_extractor._push_to_github` uses hardcoded `owner="LocketKeyLLC"` and `repo="smokieRAGs"`. Should be config. — ✅ `02f7ecb`
- [x] **#6.15** `_push_to_github` uses ephemeral `httpx.AsyncClient` — not shared client. — ✅ `02f7ecb`
- [x] **#6.16** No GitHub rate-limit check before push (unlike `github_ingest.py` which does check). — ✅ `02f7ecb`
- [x] **#6.17** `sanitize_toon_content` doesn't escape `
`, `	` — newlines in extracted content break TOON row format. — ✅ `02f7ecb`
- [x] **#6.18** `DISTILL_SYSTEM` prompt references legacy `"topic"` field; schema uses `"title"`. — ✅ `02f7ecb`
- [x] **#6.19** `gt_extractor` imports private `_embed_query` from `rag_pipeline`. — ✅ `02f7ecb`
- [x] **#6.20** `gt_list` and `gt_search` don't filter out superseded (version-chained) entries. — ✅ `02f7ecb`
- [x] **#6.21** `gt_stats` silently returns incomplete data when Milvus query truncates at 100k limit. — ✅ `02f7ecb`

### Phase 7 — Utilities
- [x] **#7.2** `cleanup.py` writes reaper messages to `jobs.compiled_output` field. Should use `error_summary` or a dedicated reap reason column. — ✅ `306fabf`
- [x] **#7.3** `cleanup.py::STALE_THRESHOLD_MINUTES=30` constant declared but never referenced — SQL uses hardcoded literal. — ✅ `306fabf`
- [x] **#7.4** `reap_stale_jobs` docstring says "2 categories" but code handles 4. Drift. — ✅ `306fabf`
- [x] **#7.5** SearXNG client in `http_clients` missing `follow_redirects=True` — 301/302 responses fail silently.
- [x] **#7.6** `execution_handler.deps_met` logic inconsistent with `execute_next_node` — excludes `'skipped'` status.
- [x] **#7.7** `actionable` filter in `execution_handler` includes `'failed'` status, but `/execute` won't re-pick failed nodes without explicit `/exec/retry`.
- [ ] **#7.8** No prompt revision history (Phase 8 #119 workflow) — edits overwrite without audit trail.

### Phase 8a — scaffold_router
- [x] **#8.6** Command prefix matching uses `startswith()` without word boundary — `/executor` matches `/exec`, `/confirmation` matches `/confirm`. — ✅ `2989f0b`
- [x] **#8.7** Threading pattern for SSE reader duplicated 3× across `_go_synthesize_and_stream`, `_confirm_and_stream`, `_execute_and_stream`, `_research_and_stream_raw`. DRY candidate. — ✅ `2989f0b`
- [x] **#8.8** Inconsistent timeout sources (6 different values: triage_timeout, dag_timeout, request_timeout on valves, 30s hardcoded in some helpers, 60s in others, 310s in execution_handler). — ✅ `2989f0b`
- [x] **#8.9** `/schedule add` doesn't expose `depth` param — scheduled runs hardcoded to shallow. — ✅ `2989f0b`
- [x] **#8.10** `_MODEL_DEFAULTS` class attribute duplicates the `Valves` default values. Single source of truth violated. — ✅ `2989f0b`
- [x] **#8.11** `print()` statements used instead of `logger` in several debug paths. — ✅ `2989f0b`
- [x] **#8.12** SSE reader thread has no read timeout on `iter_lines()` — stuck stream → stuck thread. — ✅ `2989f0b`
- [x] **#8.13** `TRIAGE_SYSTEM_PROMPT` (~60 lines) hardcoded inside Pipeline class. Move to constant or external file. — ✅ `2989f0b`

### Phase 8b — Other pipelines
- [x] **#8.14** `gt_browser` uses `httpx.Client` while other pipelines use `requests` — mixed HTTP libraries. — ✅ `94f74bb`
- [x] **#8.15** `execution_handler.request_timeout=310` magic number — undocumented. — ✅ `cd039ce`
- [x] **#8.16** `execution_handler._status` direct dict access (`d["counts"]`, `d["nodes"]`, `d["job_status"]`, `d["job_title"]`) — no `.get()` guards. — ✅ `cd039ce`
- [x] **#8.17** Status icon map duplicated across 5 pipelines (scaffold_router, execution_handler, prompt_inspector, gt_browser, dag_viewer). Extract shared constant.
- [x] **#8.18** `/prompt edit` → `/prompt save` two-step flow is fragile (comment in code acknowledges this). Single-step preferred.
- [x] **#8.19** `_save` joins new prompt with single space via `" ".join(parts[4:])` — newlines in prompt lost.
- [x] **#8.20** `/dag` command collision between `scaffold_router` and `dag_viewer` pipelines.
- [x] **#8.21** `dag_viewer` Mermaid escaping only handles `"` (line: `title.replace('"', "'")`) — breaks on `[`, `]`, `(`, `)`, `|`, `{`, `}`, `#`.

### Phase 9 — Tests (structural)
- [x] **#9.3** `conftest_ci.py` — filename not auto-loaded by pytest. Fixtures unreachable. Dead code.
- [x] **#9.4** No `asyncio_mode = "auto"` in `pyproject.toml` — every async test needs `@pytest.mark.asyncio` individually.
- [x] **#9.5** No default `--timeout` in pytest addopts.
- [ ] **#9.6** Oversized test files: `test_scaffold_router.py` (35 KB), `test_research_agent.py` (30 KB), `test_execution_agent.py` (21 KB), `test_ideation_workflow.py` (20 KB). Split.
- [x] **#9.7** Rename legacy `test_tasks_13_14_15_16.py` to module-based name.
- [x] **#9.8** `conftest.py` eager-imports `app` and `app.model_router` — blocks `test_scaffold_router.py` (must run with `--noconftest`).
- [x] **#9.9** Pipeline tests require `--noconftest`, splitting CI and local runs.
- [x] **#9.10** `make_mock_db` helper covers only `.mappings().all()` — tests using `.scalar()`, `.first()`, `.rowcount` must build mocks manually.
- [x] **#9.11** Replace custom `_run(coro)` helper in `test_verify_extraction.py` with pytest-asyncio.
- [x] **#9.12** Remove dead `app.settings` stub from `test_pipeline_complete.py` (code uses `app.config`).
- [x] **#9.13** Isolate `sys.modules` stubbing in `test_pipeline_complete.py` to fixtures, not module-level.
- [x] **#9.14** Convert source-grep tests in `test_tasks_13_14_15_16.py` to behavioral tests (per Phase Y guidance, missed).
- [x] **#9.15** Loosen `test_download_before_copy` (assumes exact `COPY app/` syntax).
- [x] **#9.16** Dead helper `_load_module()` in `test_tasks_13_14_15_16.py` — defined, never called.
- [x] **#9.17** Dead `_status_spec`/`_status_mod` objects — created then discarded.
- [x] **#9.18** `conftest_ci.py` uses `localhost` hostnames — from inside container, Milvus is at `scaffold-milvus:19530`, not localhost. Dead-on-arrival even if activated.

### Phase 9 — Tests (coverage gaps — add tests for these modules)
- [x] **#9.19** `auth.py` — API key validation, health exemption.
- [x] **#9.20** `llm_parsing.py` — 4-step fallback chain, edge cases.
- [x] **#9.21** `model_router.py` — retry cascade, timeout handling.
- [x] **#9.22** `prompt_optimizer.py` — filler strip, verify loop.
- [x] **#9.23** `gt_extractor.py` — SearXNG → distill → TOON flow.
- [x] **#9.24** `rerankers.py` — CrossEncoder path, RRF fallback, reset.
- [x] **#9.25** `embedding_cache.py` — hit/miss, eviction, Redis roundtrip.
- [x] **#9.26** `milvus_utils.py` — auto-create, `raise_on_missing`.
- [x] **#9.27** `staleness.py` — TTL policy, `sweep_expired`.
- [x] **#9.28** `http_clients.py` — lazy init, `close_clients`.
- [x] **#9.29** `prompt_inspector.py` + `execution_handler.py` (orchestrator modules).
- [x] **#9.30** `cleanup.py` — separate from health, or clarify combined scope.

---

## 🔵 Low (nits / style / future consideration)

### Phase 6
- [x] **#6.22** `ideation_workflow` docstrings incomplete on newer async methods. — ✅ `02bdc22`
- [x] **#6.23** Inconsistent `logger.bind(session_id=...)` usage across phases. — ✅ `02bdc22`

### Phase 7
- [ ] **#7.9** `prompt_inspector` (orchestrator module) returns prompt history data structured as flat dict — consider structured model.

### Phase 8
- [x] **#8.22** `gt_browser` pagination lacks "previous page" hint. — ✅ `94f74bb`
- [x] **#8.23** `per_page=20` hardcoded in offset calc in `gt_browser`. — ✅ `94f74bb`
- [x] **#8.24** `execution_handler` — `resp.json()` can raise unhandled. — ✅ `cd039ce`
- [x] **#8.25** `prompt_inspector` direct dict access — `.get()` guards missing.
- [x] **#8.26** No client-side prompt-length validation in prompt_inspector.
- [x] **#8.27** `dag_viewer` missing `on_startup`/`on_shutdown` stubs.
- [x] **#8.28** `dag_viewer._render` has 3 separate loops — could combine.
- [x] **#8.29** `dag_viewer` no truncation on large DAG rendered output.

### Phase 9
- [x] **#9.31** Remove duplicate `import pytest` in `test_verify_extraction.py`.
- [x] **#9.32** Add test for `_verify_output` exception-from-chat path.
- [x] **#9.33** Remove `test_todo_removed` (fragile comment-absence check).

---

## Summary by Phase

| Phase | Critical | Medium | Low | Total |
|---|---|---|---|---|
| 6 (ideation + GT) | 4 | 17 | 2 | 23 |
| 7 (utilities) | 1 | 7 | 1 | 9 |
| 8a (scaffold_router) | 2 | 8 | — | 10 |
| 8b (other pipelines) | 3 | 8 | 8 | 19 |
| 9 (tests) | 2 | 30 | 3 | 35 |
| **TOTAL** | **12** | **70** | **14** | **~96** |

> Note: totals understate; some items aggregate multiple sub-issues (e.g. #9.6 lists 4 files to split, counted as 1).

---

## Recommended Attack Order

**Week 1 — Critical fixes (12 items):**
1. #6.1, #6.3 — model regression (saves hundreds of seconds per research/ideation run)
2. #8.4, #8.5 — execution_handler field mismatches (silently broken UX)
3. #6.2 — target_status hardcode (ideation workflow state bug)
4. #6.4, #8.1 — HTTP status and broken `/results` link
5. #8.2 — SSE error events swallowed (debugging blind spot)
6. #8.3 — duplicate source_url render
7. #7.1 — stray global var
8. #9.1, #9.2 — test suite health

**Week 2 — Medium (70 items, tackle by module):**
- GT module cleanup (#6.14–6.21)
- Pipeline consistency (#8.6–8.13, #8.17)
- Test suite structure (#9.3–9.18)

**Week 3+ — Coverage gaps (#9.19–9.30):** 12 modules need dedicated test files. Prioritize `auth`, `model_router`, `llm_parsing` first (security/core).

**Ongoing — Low (14 items):** fold into relevant PRs.

---

*End of Phases 6–10 fix list.*
