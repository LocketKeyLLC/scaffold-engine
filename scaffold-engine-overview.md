# Scaffold Engine — Project Overview

**Last Updated:** April 19, 2026
**Repo:** `LocketKeyLLC/scaffold-engine` on GitHub | `~/scaffold-engine` locally
**Test Suite:** 362 passed + 22 skipped in-container + 49 pipeline + 18 valve + 3 gt_browser, 0 failed
**Codebase:** ~6,700 lines of application Python across 27 source files + ~2,100 lines across 5 pipelines

---

## What Scaffold Engine Does

Scaffold Engine is a self-hosted **DAG orchestration engine for multi-step LLM workflows**. A user submits an idea or prompt, and the system:

1. **Triages** the idea via a lightweight conversational model
2. **Refines** it into a structured brief
3. **Assesses feasibility** and halts for user confirmation
4. **Researches** the topic via SearXNG (or directly from URL/GitHub/OpenAPI/PDF), distills facts via LLM, and ingests them into Milvus RAG
5. **Compiles** a high-fidelity prompt and workflow
6. **Generates a DAG** of execution nodes, each assigned a tool (LLM, CodeGen, SearXNG, Milvus)
7. **Executes each node** in dependency order with SSE streaming, injecting RAG context or web search results as grounding
8. **Compiles** the final output from all completed nodes
9. **Streams** real-time progress to the UI via Server-Sent Events

Runs entirely on local hardware (Pop!_OS, CPU-only inference) with no cloud dependencies for generation (heavy cloud models available as opt-in).

---

## Architecture

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│  Open WebUI     │────▶│  Pipelines       │────▶│  Scaffold       │
│  (port 3000)    │     │  (port 9099)     │     │  Orchestrator   │
│                 │     │  scaffold_router │     │  (port 8000)    │
└─────────────────┘     └──────────────────┘     └────────┬────────┘
                              │                           │
                              │ triage calls     ┌────────┼────────┐
                              ▼                  ▼        ▼        ▼
                        ┌───────────┐     ┌──────────┐  ┌────────┐ ┌────────┐
                        │  Ollama   │     │PostgreSQL│  │ Milvus │ │ Redis  │
                        │  (host)   │     │    16    │  │ 2.5.27 │ │   8    │
                        │  CPU-only │     │          │  │  512d  │ │        │
                        └─────┬─────┘     └──────────┘  └────────┘ └────────┘
                              │
                        ┌─────┴─────┐
                        │  SearXNG  │
                        │(port 8888)│
                        └───────────┘
```

All containers run on the Docker `ai-network` bridge. Pipelines reach host Ollama via the bridge gateway (`172.18.0.1:11434`).

---

## Complete User Workflow

One deliberate pause point (confirmation gate). Everything else auto-chains.

```
User types plain message
  │  ▼ Triage conversation (qwen3:4b via Ollama)
  │      Assistant proposes options → user selects → scope locked
  │
User types /go or /run
  │  ▼ Synthesis (qwen3:4b) — transcript-based, extracts final plan
  │
/ideate (Phase 1) — auto-chained
  │  ▼ Refine + feasibility assessment → halt at 'awaiting_confirmation'
  │
User reviews → /confirm <job_id>
  │  ▼ Phase 2 → DAG → Execute/all (all auto-chained)
  │     Phase 2: SearXNG research → LLM distill → Milvus ingest → compile
  │     DAG: generate nodes (T1..Tn) with Kahn's cycle check
  │     Execute: dependency order, SSE streamed, verifier-gated, auto-retry
  │
Compiled output displayed in chat
```

**Job status flow:** `pending → refining → awaiting_confirmation → researching → planning → executing → running → completed/failed/cancelled/blocked`

---

## Commands

| Command | Description |
|---|---|
| *(plain message)* | Discuss your idea with the triage assistant |
| `/go` or `/run` | Synthesize conversation → Phase 1 → confirmation gate |
| `/confirm <job_id>` | Approve → Phase 2 → DAG → execute all nodes |
| `/confirm <job_id> <feedback>` | Approve with modifications |
| `/execute <job_id>` | Manually execute all nodes |
| `/idea <text>` | Submit idea directly to Phase 1 (skip triage) |
| `/dag <job_id>` | Generate DAG for a job in `planning` state |
| `/skip <job_id> <node_key>` | Skip a specific DAG node |
| `/optimize <prompt>` | Optimize a prompt |
| `/rag <query>` | Query the Milvus knowledge base |
| `/model <sub>` | Manage models: `list`, `available`, `set`, `reset`, `help` |
| `/research <topic>` | Autonomous web research |
| `/research <url>` | Direct URL ingestion |
| `/research github:<owner>/<repo>` | Ingest README + docs + module docstrings |
| `/research openapi:<url>` | Ingest OpenAPI/Swagger spec as per-endpoint entries |
| `/research/reply <session_id> <msg>` | Resume paused research session |
| `/schedule <sub>` | Manage scheduled research: `list`, `add`, `delete`, `help` |
| `/status` | List active jobs |
| `/help` | Show command list |

Additional endpoint (not a chat command): `POST /research/pdf` — direct PDF upload (use the self-hosted upload form at `GET /research/pdf` or `curl -F file=@spec.pdf`).

---

## Infrastructure

| Component | Image / Version | Container | Port |
|---|---|---|---|
| Orchestrator | `python:3.12.13-slim` | `scaffold-orchestrator` | 8000 |
| Database | `postgres:16` | `scaffold-postgres` | 5432 |
| Vector Store | Milvus 2.5.27 (standalone, embedded ETCD) | `milvus-standalone` | 19530 |
| UI | Open WebUI (pinned by SHA256) | `open-webui` | 3000 |
| Pipelines | Open WebUI Pipelines (pinned by SHA256) | `open-webui-pipelines` | 9099 |
| Web Search | SearXNG (pinned by SHA256) | `searxng` | 8888 |
| Cache | `redis:8-alpine` | `scaffold-redis` | 6379 |
| Inference | Ollama (host-installed, CPU-only) | N/A | 11434 |

All service images pinned by SHA256 digest. All pip dependencies pinned. Migrations run manually (no auto-migration on startup).

**Key timeouts:** Open WebUI `AIOHTTP_CLIENT_TIMEOUT=7200`; triage `3600s`; `/ideate` `1800s`; DAG `3600s`; orchestrator Ollama `600s`.

**Networking:** All containers on `ai-network` (172.18.0.0/16). Pipelines reach host Ollama via bridge gateway `172.18.0.1:11434`. `host.docker.internal` is NOT available (Pop!_OS, native Docker).

---

## Model Stack

All roles routable via Open WebUI admin valves. Priority: **valve > env var > config.py default**.

| Role | Default Model | Env Var | Notes |
|---|---|---|---|
| Generation | `qwen3-vl:235b-instruct-cloud` | `MODEL_GENERAL` | Cloud-routed, 600s timeout |
| Triage | `qwen3:4b` | (valve only) | Direct to Ollama, not orchestrator |
| Verifier | `qwen2.5:7b` | `MODEL_VERIFIER` | Validates LLM outputs |
| Code | `qwen2.5-coder:7b` | `MODEL_CODER` | CodeGen tool nodes |
| Embeddings | `qwen3-embedding:8b` | `MODEL_EMBEDDER_PIPELINE` | **Config-level only** (512d lock) |
| Reranker | `tomaarsen/Qwen3-Reranker-0.6B-seq-cls` | `MODEL_RERANKER` | **Config-level only** (CrossEncoder singleton) |
| Query/Router | `qwen3:4b` | `MODEL_ROUTER` | DAG planning, gap analysis |
| Fallback | `qwen3.5:latest` | `MODEL_FALLBACK` | Cascade fallback |
| Cloud alt | `qwen3.5:397b-cloud` | `MODEL_CLOUD_ALT` | Heavy alternative |

> ⚠️ Embedder and reranker valves are config-level references only — dimension and singleton constraints prevent per-request swap.
> ⚠️ `validate_models()` runs at `/ideate`, `/ideate/confirm`, `/dag`, `/execute/all`, `/research`. Missing models → HTTP 422 with list.

---

## Application Modules

### Core
| File | Lines | Purpose |
|---|---:|---|
| `main.py` | 729 | FastAPI app, all endpoints, health checks, lifespan, middleware |
| `model_router.py` | 344 | Ollama API routing with retry cascade, persistent `httpx.AsyncClient` |
| `config.py` | ~50 | Pydantic Settings (env vars with defaults) |
| `auth.py` | 33 | API key auth via `X-API-Key` |
| `database.py` | 26 | Async SQLAlchemy engine + session |
| `schemas.py` | ~400 | Pydantic request/response models |
| `rerankers.py` | 164 | CrossEncoder reranker + RRF fallback |
| `scheduler.py` | 245 | APScheduler: per-schedule tz, job timeout, idempotent init, graceful shutdown, real session_id capture |
| `logging_config.py` | 86 | structlog setup |

### Modules
| File | Lines | Purpose |
|---|---:|---|
| `modules/execution_agent.py` | 1,261 | DAG node execution, SSE streaming, tool dispatch, verification, auto-retry |
| `modules/dag_generator.py` | 575 | DAG creation, Kahn's cycle check, numeric-sort truncation |
| `modules/rag_pipeline.py` | 596 | Embed → parallel vector+keyword → RRF → rerank; ingest with dedup + version chains |
| `modules/research_agent.py` | 2,188 | Autonomous research + URL/GitHub/OpenAPI/PDF direct modes + pause/resume |
| `modules/ideation_workflow.py` | 275 | Phase 1 (refine + feasibility) + Phase 2 (research → compile) |
| `modules/idea_refinement.py` | 172 | Raw idea → structured brief |
| `modules/prompt_optimizer.py` | 201 | Strip → optimize → verify |
| `modules/gt_extractor.py` | 435 | SearXNG → distill → TOON formatting → optional GitHub push |
| `modules/gt_browser.py` | 184 | GT browsing/search/detail/stats (async-safe) |
| `modules/prompt_inspector.py` | 116 | Prompt analysis + revision |
| `modules/execution_handler.py` | 73 | Execution status queries |
| `modules/cleanup.py` | 143 | Stale-job reaper (15-min loop, unified `reap_stale_jobs`) |

### Routers & Middleware
| File | Lines | Purpose |
|---|---:|---|
| `routers/status.py` | 206 | `/status` and `/logs` |
| `middleware/performance.py` | 104 | Request timing |
| `middleware/error_logging.py` | 83 | Error capture |

### Utilities
| File | Lines | Purpose |
|---|---:|---|
| `utils/llm_parsing.py` | 115 | Shared `strip_think_tags()`, `parse_json_object()`, `parse_json_array()` |
| `utils/http_clients.py` | 78 | Shared SearXNG + GitHub async clients with pooling |
| `utils/milvus_utils.py` | 139 | `get_collection()` with auto-create of `toon_v2` schema |
| `utils/embedding_cache.py` | ~80 | Two-tier: in-memory LRU + Redis |
| `utils/staleness.py` | 77 | TTL-per-source-type sweep |
| `utils/github_ingest.py` | 154 | GitHub repo fetch + rate-limit guard |
| `utils/openapi_ingest.py` | 241 | OpenAPI/Swagger fetch, validate, flatten per-endpoint |

---

## Open WebUI Pipelines

| Pipeline | Lines | Purpose |
|---|---:|---|
| `scaffold_router.py` | ~1,380 | Main pipeline: triage, synthesis, `/go`/`/confirm` auto-chain, `/research`, `/research/reply`, `/schedule`, `/model` |
| `gt_browser.py` | 205 | GT browsing |
| `execution_handler.py` | 201 | Direct execution control |
| `prompt_inspector.py` | 178 | Prompt analysis |
| `dag_viewer.py` | 111 | DAG visualization (Mermaid) |

### scaffold_router Valves (admin-configurable)
- **Connection:** `api_key`, `orchestrator_url`, `dag_timeout=3600`, `keepalive_interval=10`, `triage_timeout=3600`, `ollama_url`
- **Triage:** `triage_model=qwen3:4b`
- **Model overrides (8 roles):** `model_general`, `model_verifier`, `model_coder`, `model_embedder`, `model_reranker`, `model_router`, `model_fallback`, `model_cloud_alt`

---

## API Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/ideate` | Phase 1: Refine + feasibility → halt at `awaiting_confirmation` |
| `POST` | `/ideate/confirm` | Phase 2: Research → ingest → compile → `planning` |
| `POST` | `/ideas` | Direct idea refinement (skips ideation workflow) |
| `POST` | `/dag` | Generate DAG from refined idea |
| `GET` | `/dag/{job_id}` | Retrieve DAG + job status |
| `POST` | `/execute` | Execute next pending DAG node |
| `POST` | `/execute/all` | Execute all pending nodes (SSE streaming) |
| `POST` | `/rag` | Query RAG knowledge base |
| `POST` | `/research` | Autonomous research (SSE streaming). Dispatches to topic/url/github/openapi mode |
| `POST` | `/research/reply` | Resume paused research with user clarification |
| `POST` | `/research/pdf` | Direct PDF upload (multipart) |
| `GET` | `/research/pdf` | Drag-and-drop upload form |
| `POST` | `/schedule` | Create recurring research schedule |
| `GET` | `/schedule` | List schedules |
| `DELETE` | `/schedule/{id}` | Remove schedule |
| `GET` | `/rag/dedup` | Near-duplicate rejection log |
| `POST` | `/optimize` | Optimize a prompt |
| `POST` | `/gt` | Extract ground truths via SearXNG + LLM |
| `GET` | `/gt/list`, `/gt/detail/{id}`, `/gt/stats` | GT browsing |
| `POST` | `/gt/search` | Semantic GT search |
| `GET` | `/exec/status/{job_id}` | Execution status + compiled_output |
| `POST` | `/exec/retry` | Retry failed node |
| `POST` | `/jobs/cleanup` | Active-node-aware stale-job reap |
| `GET` | `/status` | List active jobs |
| `GET` | `/logs` | Execution logs |
| `GET` | `/prompts/{job_id}[/{node_key}]` | Prompt inspection |
| `POST` | `/prompts/{job_id}/{node_key}` | Update node prompt |
| `GET` | `/health` | Postgres + Ollama + Milvus + Redis |

---

## Database Schema (PostgreSQL 16)

**13 tables** in the `scaffold_engine` database:

| Table | Purpose |
|---|---|
| `jobs` | Job lifecycle (status, compiled_output, research_data, workflow_summary) |
| `dag_nodes` | Execution nodes (tool, domain, confidence, depends_on, output, **is_output_node** [since migration 017]) |
| `execution_logs` | Per-node execution records |
| `error_logs` | Error tracking with resolution status |
| `performance_logs` | Model performance metrics |
| `artifacts` | Generated artifacts |
| `blockers` | Dependency blockers |
| `benchmark_results` | Benchmarking data |
| `dedup_log` | Near-duplicate rejection log |
| `research_sessions` | `/research` session state (status, snapshot, pause fields) |
| `scheduled_jobs` | User-facing schedule metadata |
| `apscheduler_jobs` | APScheduler internal jobstore |
| *(+ 1 legacy/unused)* | — |

**Migrations:** `db/migrations/002_*.sql` through `017_dag_nodes_is_output_node.sql`. Applied manually — no runner on startup.

---

## RAG Pipeline

1. **Embed query** — `qwen3-embedding:8b`, MRL-truncated to 512d, instruction-prefixed, Redis-cached
2. **Parallel search** — vector (COSINE + HNSW_SQ8) + keyword, via `asyncio.gather`
3. **RRF merge** — Reciprocal Rank Fusion combines results
4. **CrossEncoder rerank** — `Qwen3-Reranker-0.6B-seq-cls`, in thread executor
5. **Return top-K** with scores

All blocking Milvus and reranker calls wrapped in `run_in_executor`.

**Ingestion (3-tier):**
- cosine > 0.95 → reject
- cosine 0.90–0.95 → version chain (new entry, `supersedes` link)
- cosine < 0.90 → new entry

**Retrieval filters superseded entries** by default; `include_history=true` for full chain.

---

## Milvus: `toon_v2` Collection

- **16 TOON fields** (entry_id, title, content, source_url, source_type, tags, domain, confidence_score, version, supersedes_id, expires_at, etc.)
- **512d FLOAT_VECTOR** (embedding field)
- **HNSW_SQ8 COSINE** (M=16, efConstruction=256, SQ8+BF16 refine)
- **Partition key isolation** on `domain` (64 partitions)
- **Scalar indexes:** content_hash, domain_tags, source_type, confidence_score, created_at, version
- **TTL by source_type:** real_time=7d, news=30d, community=90d, tech_docs=180d, curated/official=1y, ai_generated=180d
- **Current state:** ~501 entries (April 18, 2026); grows organically through `/research` runs

---

## Research Agent — 5 Modes

Single endpoint `/research` dispatches based on topic prefix:

| Prefix | Mode | Behavior |
|---|---|---|
| `http(s)://...` | URL | Robots check → bounded fetch (5MB) → trafilatura → chunk → distill → ingest |
| `github:owner/repo` | GitHub | Fetch README + `docs/**/*.md` + module docstrings (50-file cap) → ingest |
| `openapi:<url>` | OpenAPI | Fetch + validate → one entry per endpoint (200-endpoint cap) → ingest |
| *(other)* | Topic | Decompose → SearXNG queries → trafilatura full-page → chunk → distill → gap analysis → iterate |
| *(via `POST /research/pdf`)* | PDF | pypdf (fallback pdfplumber) → chunk → distill → ingest |

**Pause/Resume:** Gap analyzer may request clarification. Session transitions to `paused_awaiting_reply` with 1h TTL. User resumes via `/research/reply <session_id> <msg>`. Reply injected as gap_query.

**Depth levels (topic mode):** shallow=1 iteration, medium=2, deep=4.

**Two-tier model strategy:** 4b for decompose/gap-analysis (fast), 7b for extract/summary (accurate). 235b explicitly avoided in research loops.

**SSE events:** `research_started`, `decomposition`, `search_complete`, `research_fetch`, `extraction_complete`, `ingestion_complete`, `contradictions_detected`, `gap_analysis`, `awaiting_reply`, `research_resumed`, `research_complete`.

---

## Scheduled Research

APScheduler (in-process, SQLAlchemyJobStore) runs recurring `/research` jobs based on cron expressions.

- Schedules persist across orchestrator restarts (rehydrated from `scheduled_jobs` on lifespan startup)
- `POST /schedule` validates cron, inserts row, registers with APScheduler
- Pipeline command: `/schedule add "0 9 * * 1" <topic>`
- **Per-schedule timezone** — `POST /schedule` accepts IANA `timezone` field (defaults to UTC); stored in `scheduled_jobs.timezone`, threaded through `CronTrigger.from_crontab`
- **Job timeout** — each scheduled run bounded by `scheduler_job_timeout` (default 3600s); timeout marks `last_status='timeout'`
- **last_job_id** — populated with the real `research_sessions.id` captured from the SSE stream
- **Graceful shutdown** — bounded by `scheduler_shutdown_timeout` (default 30s); in-flight jobs get a grace window before being dropped
- **Current limitation:** scheduled depth hardcoded shallow

---

## Retrieval Quality Metrics

`scripts/score_retrieval.py` computes `recall@5`, `recall@10`, `mrr`, `coverage` against `tests/fixtures/golden_set.json`.

**Baseline (April 18, 2026, KB=501 entries):**
- Coverage: 95%
- Mean Recall@5: 0.95
- Mean MRR: 0.86

CI workflow `retrieval-quality.yml` runs unit tests on PRs touching retrieval code. Live scoring is local/manual (GitHub runners lack Milvus + Ollama).

---

## Key Design Decisions

1. **Async-first** — all I/O async; blocking libs (PyMilvus, CrossEncoder) wrapped in `run_in_executor`
2. **Persistent HTTP client** — module-level `httpx.AsyncClient` with pooling for Ollama, SearXNG, GitHub
3. **Centralized JSON parsing** — 4-step fallback chain in `utils/llm_parsing.py`
4. **Think-tag stripping** — shared utility removes `<think>`/`<thinking>` from all LLM outputs
5. **Confirmation gate** — ideation halts at `awaiting_confirmation`, requires explicit user confirm
6. **SSE keepalive** — zero-width spaces emitted at intervals to prevent proxy timeouts
7. **Two-model pipeline routing** — triage 4b (fast), full pipeline 235b (deep)
8. **Transcript-based synthesis** — single-message transcript prevents replay confusion
9. **Auto-chain on `/confirm`** — Phase 2 → DAG → execute all
10. **Concurrent execution guard** — atomic `UPDATE` prevents duplicate job/session runs
11. **Active-node-aware cleanup** — stale reaper skips jobs with running nodes
12. **Numeric DAG truncation** — sorts T1, T2, T3... numerically, not alphabetically
13. **Upstream-last prompt assembly** — mandatory upstream context prepended, task instruction last
14. **Env-first model configuration** — docker-compose env vars override config.py defaults
15. **Short-lived database sessions** — independent session per operation, not request-scoped
16. **Auto-retry on verify fail** — up to `max_retries`, then blocked with manual `/exec/retry`
17. **Tool-constrained DAG** — only LLM, CodeGen, SearXNG, Milvus; no Human/FileSystem
18. **Model valve system** — 8 roles switchable via admin panel; overrides threaded per-request
19. **3-tier ingestion** — dedup > 0.95, version chain 0.90–0.95, new < 0.90
20. **Latest-version-by-default retrieval** — `include_history=True` opts into full chain
21. **Autonomous research agent** — decompose → fan out → distill → ingest → gap-analyze → iterate
22. **LLM-gated pause** — gap analyzer decides if a clarifying question is warranted
23. **Direct ingestion modes** — URL/GitHub/OpenAPI/PDF bypass SearXNG discovery
24. **Trafilatura full-page extraction** — per-URL text + paragraph-aware chunking
25. **Session state snapshots** — written at each iteration boundary for pause/resume
26. **Explicit output-node marker** — DAG generator flags leaf nodes with `is_output_node=TRUE` at INSERT time. `_compile_output` prefers explicit markers (Strategy 0) before falling through to title-heuristic / last-CodeGen / concatenation strategies. Replaces fragile string-matching on node titles.
27. **Endpoint-layer model validation** — `_require_valid_models` runs only at user-reachable endpoints (`/execute/all`, `/ideate`, `/ideate/confirm`, `/dag`, `/research`), never inside `execute_next_node`. Prevents N redundant Ollama `/api/tags` calls per DAG run and keeps the internal function library-callable.

---

## Known Open Issues

### Infrastructure / Config
1. **No migration runner on startup** — migrations must be applied manually via `psql`. Risk of drift between code and DB schema.
2. *(resolved Apr 18 2026)* ~~Scheduler timezone hardcoded UTC~~ — Per-schedule `timezone` column added (migration 016); threads through `CronTrigger.from_crontab`. Defaults to UTC.
3. **Model stack drift** — `/health` surfaces Ollama models not in the documented table (qwen3.5 variants, glm-5.1, extra reranker quantizations). Reconciliation pass needed.

### Runtime behavior
4. **Triage model latency on long conversations** — qwen3:4b on CPU scales with context; several minutes per turn once history grows.
5. **Research duration on CPU** — shallow `/research` topic mode: 20–30 min. Medium/deep proportionally longer.
6. **Reranker cold-load** — first invocation ~13.6s (HF cache check + CPU load).
7. **Version chain filter is result-set scoped** — `query_rag()` only filters superseded entries when both versions appear in the same result set.
8. **Open WebUI file routing intermittent** — after container restarts, sometimes stops forwarding. Hard refresh + new chat resolves.
9. **Context stripping depends on `</context>` tag** — regex in `pipe()` needs updating if Open WebUI format changes.

### Known bugs (see fix list)
10. **Distillation uses `model_general` (235b) instead of `model_router` (4b)** — regression from changelog April 14 #26. Confirmed in `ideation_workflow.py` and `gt_extractor.py`.
11. **`idea_refinement.refine_idea` hardcodes `status="planning"`** — ignores `target_status` parameter.
12. **`execution_handler` pipeline field mismatches** — reads `model`/`output_preview` but orchestrator returns `model_used`/`output`.
13. **`test_pipeline_complete.py` tautologies** — ~6 of 10 tests validate their own literals.
14. **`conftest_ci.py` is dead code** — filename not auto-loaded by pytest.
15. **Client-disconnect leaves orphan research sessions** — `run_research()` generator cancelled without finalize. Reaper catches at 30 min.
16. **Scheduler timestamp type mismatch** — `apscheduler_jobs.next_run_time` (DOUBLE PRECISION) → `scheduled_jobs.next_run_at` (TIMESTAMPTZ) without `to_timestamp()` cast.

---

## Observed Performance (CPU-only)

| Operation | Duration | Notes |
|---|---|---|
| Triage turn (qwen3:4b) | 30–300s+ | Scales with conversation length |
| Idea synthesis (qwen3:4b) | 30–120s | Scales with conversation length |
| `/ideate` (Phase 1) | 100–547s | Refinement + feasibility LLM calls |
| `/ideate/confirm` (Phase 2) | 512–1,450s | Research loop + distill + embed + ingest |
| `/dag` | 416–504s | Close to timeout threshold |
| `/execute` (single node) | ~893s | RAG retrieval + reranker + gen + verify |
| `/research` shallow | 18–27 min | Dominated by 7b extraction |
| `/research <url>` | 3–8 min | Single-page, no gap analysis |
| `/research github:...` | 1–5 min | Fetch + ingest; no LLM distill |
| `/research/pdf` 1-page | ~6 min | Cold-start dominated |
| `/health` | ~43ms | Postgres + Ollama + Milvus + Redis |

---

## Test Suite

**382 tests** across 33 files, ~9,300 lines.
- **360 in-container (+22 skipped):** core orchestrator modules
- **49 pipeline (local):** `test_scaffold_router.py`, `test_schedule_command.py`
- **18 valve (local):** `test_model_valves.py`
- **3 gt_browser (local):** `test_gt_browser.py`

**Markers:** `smoke` (fast unit), `validate` (integration, requires stack)
**Run:** `make test` / `make test-ci`
**Pipeline tests require `--noconftest`** due to conftest.py eager-loading `app`.

---

## CI/CD

- **Workflows:** `.github/workflows/test.yml`, `ci.yml`, `retrieval-quality.yml`
- **Secrets:** `SCAFFOLD_API_KEY`, `GITHUB_TOKEN`
- **`.dockerignore`** excludes git, venvs, logs, caches
- **All dependencies pinned** (production in `requirements.txt`, dev in `requirements-dev.txt`, CI in `requirements-ci.txt`)

---

## Audit Status (April 18, 2026)

Full 10-phase code audit completed:
- **~251 findings** documented across two fix lists:
  - Phases 1–5b: `docs/audit/scaffold-engine-fix-list.md` (155 items)
  - Phases 6–10: `docs/audit/scaffold-engine-fix-list-phase-6-10.md` (96 items)
- **12 critical items** prioritized for first pass
- **13 modules without dedicated tests** flagged for coverage expansion

---

## File Structure

```
scaffold-engine/
├── app/
│   ├── main.py, config.py, auth.py, database.py
│   ├── model_router.py, schemas.py, rerankers.py
│   ├── scheduler.py, logging_config.py
│   ├── modules/          # 12 files, orchestration logic
│   ├── routers/          # status.py
│   ├── middleware/       # performance.py, error_logging.py
│   └── utils/            # 7 shared utilities
├── pipelines/            # 5 Open WebUI pipelines
├── db/
│   ├── init.sql
│   └── migrations/       # 002–013
├── docs/
│   ├── audit/            # fix lists
│   ├── toon/             # TOON spec + validator reference
│   └── CI.md, logging-events.md
├── scripts/              # score_retrieval.py, create_toon_v2.py
├── tests/                # 33 files, 382 tests + fixtures/
├── docker-compose.yml, Dockerfile
├── requirements.txt, requirements-dev.txt, requirements-ci.txt
├── Makefile
└── .github/workflows/
```

---

*Changelog has been moved to git log. Use `git log --oneline` for history.*
