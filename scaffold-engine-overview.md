# Scaffold Engine — Project Overview
**Last Updated:** April 15, 2026 (v0.3.0 — /research command, autonomous topic research agent)
**Repo:** `LocketKeyLLC/scaffold-engine` on GitHub | `~/scaffold-engine` locally
**Latest Commit:** `f6a72c2` — `feat: add /research command`
**Test Suite:** 210 passed, 20 skipped, 0 failed in container (+ 43 pipeline + 18 valve + 3 gt_browser locally = 274 total)
**Codebase:** ~6,700 lines of application Python across 27 source files + ~1,050 lines in `scaffold_router.py` (pipeline)




**Test Suite:** 210 passed, 20 skipped, 0 failed in container (+ 43 pipeline + 18 valve + 3 gt_browser locally = 274 total)
**Codebase:** ~6,400 lines of application Python across 26 source files + ~974 lines in `scaffold_router.py` (pipeline)

---

## What Scaffold Engine Does

Scaffold Engine is a self-hosted **DAG orchestration engine for multi-step LLM workflows**. A user submits an idea or prompt, and the system:

1. **Triages** the idea via a lightweight conversational model — the user discusses scope, goals, and options collaboratively before committing to the full pipeline
2. **Refines** the idea into a structured brief (idea refinement)
3. **Assesses feasibility** and halts for user confirmation (ideation workflow — Phase 1)
4. **Researches** the topic via SearXNG, distills facts via LLM, and ingests them into Milvus RAG (ideation workflow — Phase 2)
5. **Compiles** a high-fidelity prompt and step-by-step workflow from the research
6. **Generates a DAG** — a directed acyclic graph of execution nodes, each assigned a tool (LLM, CodeGen, SearXNG, Milvus) and domain
7. **Executes each node** in dependency order with SSE streaming, injecting RAG context (vector search + keyword search + reranking) or web search results as grounding
8. **Compiles** the final output from all completed nodes
9. **Streams** real-time progress to the UI via Server-Sent Events

The system runs entirely on local hardware (Pop!_OS, CPU-only inference) with no cloud API dependencies for generation.

---

## Architecture

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│  Open WebUI      │────▶│  Pipelines       │────▶│  Scaffold       │
│  (port 3000)     │     │  (port 9099)     │     │  Orchestrator   │
│                  │     │  scaffold_router  │     │  (port 8000)    │
└─────────────────┘     └──────────────────┘     └────────┬────────┘
                              │                           │
                              │ triage calls     ┌────────┤
                              ▼                  ▼        ▼
                        ┌───────────┐     ┌──────────┐  ┌──────────┐
                        │  Ollama   │     │PostgreSQL│  │  Milvus  │
                        │  (host)   │     │ 16       │  │  2.5.27  │
                        │  CPU-only │     │ 8 tables │  │  512d    │
                        └─────┬─────┘     └──────────┘  └──────────┘
                              │
                        ┌─────┴─────┐
                        │  SearXNG   │
                        │ (port 8888)│
                        └───────────┘
```

All containers run on the Docker `ai-network` bridge network. The Pipelines container reaches Ollama on the host via the bridge gateway (`172.18.0.1:11434`).

---

## Complete User Workflow

The system has one deliberate pause point (the confirmation gate). Everything else auto-chains.

```
User types plain message
  │
  ▼
Triage conversation (qwen3:4b via Ollama)
  ├── Assistant proposes options with pros/cons
  ├── User selects direction
  ├── Assistant refines scope — nails down specifics:
  │     what exactly is being built, what hardware/software,
  │     what "done" looks like, key constraints
  ├── Repeats until scope is clear
  └── Assistant: "Type /go when ready"
  │
  ▼
User types /go or /run
  │
  ▼
Synthesis (qwen3:4b)
  ├── Builds plain-text transcript of full conversation
  ├── Sends transcript as single user message (not replayed chat turns)
  ├── Extracts final agreed-upon plan as 3-6 sentence idea statement
  └── Strips <think>/<thinking> tags, falls back to raw user messages if empty
  │
  ▼
Phase 1: POST /ideate (auto-chained from /go)
  ├── Refine idea → structured brief (title, description, goals, constraints, domain)
  ├── LLM feasibility assessment (confidence %, risks, clarifications)
  └── Halt at 'awaiting_confirmation' → present to user
  │
  ▼
User reviews feasibility, risks, and clarifications
  ├── Proceed as-is: /confirm <job_id>
  ├── Proceed with changes: /confirm <job_id> <feedback>
  └── Start over: describe new idea → /go
  │
  ▼
/confirm triggers auto-chain (Phase 2 → DAG → Execute):
  │
  ├── Phase 2: POST /ideate/confirm
  │     ├── SearXNG research loop (up to 5 queries)
  │     ├── LLM distillation → discrete knowledge entries
  │     ├── Milvus RAG ingestion (embed + insert)
  │     ├── TOON formatting (optional GitHub push via PR)
  │     └── Prompt compilation + workflow generation
  │
  ├── DAG Generation: POST /dag
  │     ├── Creates execution nodes (T1, T2, T3...) from workflow
  │     ├── Each node gets: tool, domain, dependencies, prompt
  │     ├── Kahn's algorithm validates no cycles
  │     └── Truncation keeps first N nodes by numeric sort (T1, T2, ... not alphabetical)
  │
  └── Execution: POST /execute/all (SSE streaming)
        ├── Uses short-lived database sessions (grab, use, release) to avoid pool exhaustion
        ├── Concurrent execution guard (atomic check-and-set to 'running')
        ├── Runs each node in dependency order
        ├── Each node: upstream context prepended → task instruction last → tool dispatch → LLM/CodeGen/SearXNG/Milvus
        ├── Verifier model (qwen2.5:7b) validates each output
        ├── RAG context injected as grounding where relevant
        ├── Failed nodes are auto-retried up to max_retries (default 3), then skipped; blocked downstream nodes reported
        ├── Progress streamed to chat via SSE
        └── Final compiled output displayed in chat
```

**Job status flow:** `pending → refining → awaiting_confirmation → researching → planning → executing → running → completed/failed/cancelled/blocked`

---

## Commands

All interaction happens through the Open WebUI chat interface.

| Command | Description |
|---|---|
| *(plain message)* | Discuss your idea with the triage assistant |
| `/go` or `/run` | Synthesize conversation → auto-chain: Phase 1 → confirmation gate |
| `/confirm <job_id>` | Approve and auto-chain: Phase 2 → DAG → execute all nodes |
| `/confirm <job_id> <feedback>` | Approve with modifications, then auto-chain |
| `/execute <job_id>` | Manually execute all nodes for a job (streams via SSE) |
| `/results <job_id>` | Fetch and display a completed job's output |
| `/idea <text>` | Submit idea directly to Phase 1 (skip triage) |
| `/dag <job_id>` | Manually generate DAG for a job in `planning` state |
| `/skip <job_id> <node_key>` | Skip a specific DAG node |
| `/optimize <prompt>` | Optimize a prompt (independent of any job) |
| `/rag <query>` | Query the Milvus knowledge base directly |
| `/model <sub>` | Manage models: `list`, `available`, `set <role> <model>`, `reset`, `help` |
| `/research <topic>` | Research a topic autonomously and ingest into knowledge base |
| `/status` | List active jobs |
| `/help` | Show command list |

**Typical workflow:** Plain messages → `/go` → review feasibility → `/confirm <job_id>` → done.

**Manual control:** `/idea`, `/dag`, `/execute`, `/skip` let you run each phase independently.

---

## Where Output Goes

All output displays in the Open WebUI chat. There is no separate output folder. The compiled final output from all nodes gets assembled by the orchestrator and streamed back through the pipeline into chat.

Data stored in the backend (accessible via commands or API):

- **PostgreSQL** — job records, DAG nodes, execution results, status history. Query with `/status` or `/results <job_id>`.
- **Milvus** — toon_v2 collection (2 test entries; knowledge base pending repopulation after schema migration). Query with `/rag <query>`.
- **Orchestrator logs** — detailed timing and error info. Check with `docker logs scaffold-orchestrator`.

---

## DAG Nodes Explained

Each node in the DAG is a single task created from your project description. Nodes are labeled T1, T2, T3, etc.

Each node has:
- **title** — what the task does (e.g., "Define network topology")
- **tool** — which tool runs it: LLM generation, code generation, SearXNG web search, or Milvus RAG query
- **domain** — which knowledge domain it falls under (eng, rag, llm, prompt, spec)
- **dependencies** — which nodes must finish before this one can start
- **prompt** — the actual instruction sent to the assigned model

Nodes execute in dependency order. When a node has upstream dependencies, their outputs are **prepended** as mandatory context with explicit framing ("MANDATORY CONTEXT — your output MUST build on and be consistent with this work"), and the node's own task instruction is placed **last** under a `## YOUR TASK` header with instruction to build on upstream outputs.

After each node runs, the verifier model (qwen2.5:7b) checks the output before moving on. If a node fails verification, it is auto-retried up to `max_retries` (default 3). If retries are exhausted, it can be manually retried via `/exec/retry` or skipped with `/skip <job_id> <node_key>`.

DAG truncation enforces a maximum node count (currently 10). When the LLM generates more nodes than allowed, the truncator keeps the first N by **numeric sort** (T1, T2, T3...) and removes references to dropped nodes from remaining dependency lists.

---

## Infrastructure

| Component | Image / Version | Container | Port |
|-----------|----------------|-----------|------|
| Orchestrator | `python:3.12.13-slim` (custom build) | `scaffold-orchestrator` | 8000 |
| Database | `postgres:16` | `scaffold-postgres` | 5432 |
| Vector Store | Milvus 2.5.27 (standalone, embedEtcd, compose-managed) | `milvus-standalone` | 19530 |
| UI | Open WebUI (pinned by SHA256 digest) | `open-webui` | 3000→8080 |
| Pipelines | Open WebUI Pipelines (pinned by SHA256 digest) | `open-webui-pipelines` | 9099 |
| Web Search | SearXNG (pinned by SHA256 digest) | `searxng` | 8888→8080 |
| Redis Cache | redis:8-alpine | `scaffold-redis` | 6379 |
| Inference | Ollama (host-installed, CPU-only) | N/A (host) | 11434 |

All service images are pinned by SHA256 digest in `docker-compose.yml`. The Python base image is pinned to `3.12.13-slim`. All pip dependencies are pinned to exact versions.

**Database credentials:** `POSTGRES_USER=scaffold`, `POSTGRES_PASSWORD=scaffold_dev_pw`, `POSTGRES_DB=scaffold_engine`

**Timeout configuration:**
- Open WebUI `AIOHTTP_CLIENT_TIMEOUT=7200` (2 hours)
- `scaffold_router.py` triage timeout: **3600s** (60 min)
- `scaffold_router.py` auto-chain `/ideate` timeout: **1800s** (30 min)
- `scaffold_router.py` `/ideate/confirm` timeout: **1800s**
- `scaffold_router.py` DAG timeout: configurable via `dag_timeout` valve (default **3600s**)
- Orchestrator Ollama timeout: **600s** for generation model

**Networking:**
- All containers on Docker `ai-network` bridge (`172.18.0.0/16`)
- Pipelines container reaches host Ollama via bridge gateway: `172.18.0.1:11434`
- `host.docker.internal` is **not** available (Pop!_OS, native Docker — not Docker Desktop)

---

## Model Stack

| Role | Model Tag | Configured Via | Notes |
|------|-----------|----------------|-------|
| Generation | `qwen3-vl:235b-instruct-cloud` | `model_general` valve + `MODEL_GENERAL` env var | Valve overrides env var per-request. 600s timeout |
| Triage | `qwen3:4b` | `triage_model` valve in scaffold_router.py | Direct to Ollama, not via orchestrator |
| Verifier | `qwen2.5:7b` | `model_verifier` valve + `MODEL_VERIFIER` env var | Validates LLM outputs |
| Code | `qwen2.5-coder:7b` | `model_coder` valve + `MODEL_CODER` env var | CodeGen tool nodes |
| Embeddings | `qwen3-embedding:8b` | `model_embedder` valve + `MODEL_EMBEDDER_PIPELINE` env var | Config-level only (512d dimension constraint). MRL truncated, L2-normalized |
| Reranker | `tomaarsen/Qwen3-Reranker-0.6B-seq-cls` | `model_reranker` valve + `MODEL_RERANKER` env var | Config-level only (singleton loaded at startup). CrossEncoder, runs in-container |
| Query gen | `qwen3:4b` | `model_router` valve + `MODEL_ROUTER` env var | DAG planning / query routing |
| Fallback | `qwen3.5:latest` | `model_fallback` valve + `MODEL_FALLBACK` env var | Cascade fallback |
| Cloud alt | `qwen3.5:397b-cloud` | `model_cloud_alt` valve + `MODEL_CLOUD_ALT` env var | Alternative heavy model |

> ⚠️ Short tag `qwen3-vl:235b` does **not** exist — always use full tag `qwen3-vl:235b-instruct-cloud`

> ⚠️ **Model override priority:** Open WebUI valve > `docker-compose.yml` env var > `config.py` default. The `get_model()` helper in `config.py` enforces this chain. Embedder and reranker valves are config-level references only — they do not override per-request due to dimension and singleton constraints.

---

## Application Modules

### Core Application

| File | Lines | Purpose |
|------|-------|---------|
| `app/main.py` | ~571 | FastAPI app with lifespan, health checks, middleware, all endpoints |
| `app/model_router.py` | 344 | Ollama API routing with retry cascade, persistent `httpx.AsyncClient` connection pool |
| `app/config.py` | ~38 | Pydantic Settings configuration (all env vars with defaults aligned to docker-compose) |
| `app/auth.py` | 33 | API key authentication via `X-API-Key` header |
| `app/database.py` | 26 | Async SQLAlchemy engine and session management |
| `app/schemas.py` | ~369 | Pydantic request/response models for all endpoints |
| `app/rerankers.py` | 164 | CrossEncoder reranker with RRF (Reciprocal Rank Fusion) fallback |
| `app/logging_config.py` | 86 | Structured JSON logging via structlog |

### Execution Pipeline

| File | Lines | Purpose |
|------|-------|---------|
| `app/modules/execution_agent.py` | ~1,153 | DAG node execution, SSE streaming, tool dispatch, verification, compiled output, concurrent execution guard, upstream prompt restructuring, auto-retry. Uses short-lived database sessions |
| `app/modules/dag_generator.py` | ~615 | DAG creation with Kahn's cycle detection, numeric-sort truncation (max 10 nodes). JSON parsing via shared `llm_parsing.py` |
| `app/modules/rag_pipeline.py` | 583 | RAG retrieval: embed → parallel vector + keyword search → RRF merge → CrossEncoder rerank. Includes `ingest_entries()` |
| `app/modules/ideation_workflow.py` | ~265 | Ideation-to-Workflow pipeline: Phase 1 (refine + feasibility + confirmation gate), Phase 2 (research → ingest → compile). 8 smoke tests |
| `app/modules/research_agent.py` | ~350 | Autonomous research loop: decompose → SearXNG search → LLM extraction → Milvus ingestion → gap analysis → iterate. SSE streaming with heartbeat keepalives |
| `app/modules/idea_refinement.py` | ~172 | Refines raw user ideas into structured briefs |
| `app/modules/prompt_optimizer.py` | 201 | Prompt optimization: strip → LLM optimize → verify |
| `app/modules/gt_extractor.py` | 435 | Ground truth extraction: SearXNG → LLM distillation → TOON formatting → optional GitHub push |
| `app/modules/gt_browser.py` | 178 | Ground truth browsing and search. All Milvus calls wrapped in `run_in_executor` |
| `app/modules/prompt_inspector.py` | 116 | Prompt analysis and inspection |
| `app/modules/execution_handler.py` | ~75 | Execution status only (`execution_status()`). Dead code (`retry_node()`) removed in audit |
| `app/modules/cleanup.py` | 107 | Periodic stale-job reaper (15-min interval), unified reap_stale_jobs(), active-node-aware |

### Routers & Middleware

| File | Lines | Purpose |
|------|-------|---------|
| `app/routers/status.py` | 206 | `/status` and `/logs` endpoints with proper HTTP error codes |
| `app/middleware/performance.py` | 104 | Request timing middleware |
| `app/middleware/error_logging.py` | 83 | Error capture middleware |

### Utilities

| File | Lines | Purpose |
|------|-------|---------|
| `app/utils/llm_parsing.py` | 120 | Shared LLM output parsing: `strip_think_tags()`, `parse_json_object()` and `parse_json_array()` with 4-step fallback chain |
| `app/utils/http_clients.py` | 44 | Shared SearXNG async client with connection pooling, lazy init, clean shutdown |
| `app/utils/milvus_utils.py` | 114 | Shared Milvus collection accessor: `get_collection(raise_on_missing)` with auto-creation of toon_v2 schema |
| `app/utils/embedding_cache.py` | ~80 | Two-tier embedding cache: in-memory LRU (10K entries) + Redis persistent |
| `app/utils/staleness.py` | ~60 | TTL-per-source-type sweep and `compute_expires_at()` helper |

---

## Open WebUI Pipelines

5 pipelines in `pipelines/` route user interactions from Open WebUI through to Scaffold Engine:

| Pipeline | Lines | Purpose |
|----------|-------|---------|
| `scaffold_router.py` | ~974 | Main pipeline (v3.1): conversational triage, transcript-based synthesis, auto-chains `/go` → Phase 1 → confirmation gate, and `/confirm` → Phase 2 → DAG → execute. SSE streaming with keepalive. **36 smoke tests** |
| `gt_browser.py` | 205 | Ground truth browsing |
| `execution_handler.py` | 201 | Direct execution control |
| `prompt_inspector.py` | 178 | Prompt analysis |
| `dag_viewer.py` | 111 | DAG visualization |

### scaffold_router.py — Valves (Admin-Configurable)

| Valve | Default | Purpose |
|-------|---------|---------|
| `api_key` | `""` | Scaffold Engine API key |
| `orchestrator_url` | `http://scaffold-orchestrator:8000` | Orchestrator endpoint |
| `dag_timeout` | `3600` | Seconds to wait for DAG generation |
| `keepalive_interval` | `10` | Seconds between keepalive zero-width spaces |
| `triage_model` | `qwen3:4b` | Model for conversational triage and synthesis |
| `triage_timeout` | `3600` | Seconds to wait for triage model responses |
| `ollama_url` | `http://172.18.0.1:11434` | Ollama endpoint (host via bridge gateway) |
| `model_general` | `qwen3-vl:235b-instruct-cloud` | Generation model (overrides env var per-request) |
| `model_verifier` | `qwen2.5:7b` | Verifier model |
| `model_coder` | `qwen2.5-coder:7b` | Code generation model |
| `model_embedder` | `qwen3-embedding:8b` | Embedding model (config-level only) |
| `model_reranker` | `tomaarsen/Qwen3-Reranker-0.6B-seq-cls` | Reranker model (config-level only) |
| `model_router` | `qwen3:4b` | Query/DAG planning model |
| `model_fallback` | `qwen3.5:latest` | Cascade fallback model |
| `model_cloud_alt` | `qwen3.5:397b-cloud` | Alternative cloud model |

---

## API Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/ideate` | Phase 1: Refine idea + feasibility assessment → halt at `awaiting_confirmation` |
| `POST` | `/ideate/confirm` | Phase 2: Research → ingest → compile → transition to `planning` |
| `POST` | `/ideas` | Legacy: Direct idea refinement (skips ideation workflow) |
| `POST` | `/dag` | Generate DAG from refined idea |
| `GET` | `/dag/{job_id}` | Retrieve DAG nodes + job status |
| `POST` | `/execute` | Execute next pending DAG node (single node) |
| `POST` | `/execute/all` | Execute all pending DAG nodes (SSE streaming) |
| `POST` | `/rag` | Query the RAG knowledge base |
| `POST` | `/research` | Autonomous research: decompose → search → extract → ingest → iterate (SSE streaming) |
| `GET` | `/rag/dedup` | List near-duplicate rejection log for manual review |
| `POST` | `/optimize` | Optimize a prompt |
| `POST` | `/gt` | Extract ground truths via SearXNG + LLM |
| `GET` | `/gt/list` | List ground truth entries |
| `POST` | `/gt/search` | Search ground truths |
| `GET` | `/gt/detail/{entry_id}` | Get ground truth detail |
| `GET` | `/gt/stats` | Ground truth statistics |
| `GET` | `/exec/status/{job_id}` | Execution status (includes compiled_output and node details) |
| `POST` | `/exec/retry` | Retry failed node (smart: checks max_retries, cascade-resets downstream, increments retry count) |
| `POST` | `/jobs/cleanup` | Clean stale jobs (active-node-aware) |
| `GET` | `/status` | List active jobs |
| `GET` | `/health` | Health check |

---

## Database Schema (PostgreSQL 16)

9 tables in the `scaffold_engine` database:

| Table | Purpose |
|-------|---------|
| `jobs` | Job lifecycle tracking. Includes `compiled_output TEXT`, `research_data JSONB`, `workflow_summary TEXT` |
| `dag_nodes` | Individual DAG execution nodes with `tool`, `domain`, `confidence`, `depends_on`, `status`, `output_text` |
| `execution_logs` | Per-node execution records |
| `error_logs` | Error tracking with resolution status |
| `performance_logs` | Model performance metrics |
| `artifacts` | Generated artifacts from node execution |
| `blockers` | Dependency blockers between nodes |
| `benchmark_results` | Performance benchmarking data |
| `dedup_log` | Near-duplicate rejection log (content_hash, existing_entry_id, similarity_score, action_taken) |

9 incremental migrations in `db/migrations/` (002–009).

---

## RAG Pipeline

1. **Embed query** → `qwen3-embedding:8b` (MRL truncated to 512d, instruction-prefixed, Redis-cached)
2. **Parallel search** — vector search + keyword search via `asyncio.gather`
3. **RRF merge** — Reciprocal Rank Fusion combines results
4. **CrossEncoder rerank** — `Qwen3-Reranker-0.6B-seq-cls` re-scores (runs in thread executor)
5. **Return top-K** results with scores

All blocking Milvus and reranker calls are wrapped in `run_in_executor`.

---

## TOON Schema

TOON (Token-Optimized Object Notation) is the serialization format used for knowledge entries. Specification and validator reference are in `docs/toon/`:
- `TOON_SPEC.md` — quick reference (format rules, field layout)
- `TOON_Optimal_Schema.md` — full specification
- `toon_validator_reference/` — validation gates (contradiction, freshness, URL validation) from the retired smokieRAGs repo, preserved for future integration

TOON formatting is used in `gt_extractor.py` and `ideation_workflow.py` for ingesting research into Milvus.

---

## Test Suite

263 tests collected (excluding 4 flaky live golden-retrieval tests):

- **210 passed, 20 skipped** (skips: files not in container, router/valve tests outside container, golden retrieval pending repopulation)
- **`test_scaffold_router.py`** — 43 smoke tests (pure functions, SSE parsing, command dispatch, /go + /confirm flow, context stripping, /model commands). Run with `--noconftest`
- **`test_ideation_workflow.py`** — 8 smoke tests (Phase 1 + Phase 2 with full dep mocking)
- **`test_model_valves.py`** — 18 tests (get_model priority chain, _model_overrides mapping, payload inclusion). Run with `--noconftest`
- **`test_gt_browser.py`** — 3 smoke tests (field name mappings post-fix). Run with `--noconftest`
- CI via GitHub Actions (`.github/workflows/test.yml` and `ci.yml`)

---

## CI/CD

- GitHub Actions workflows in `.github/workflows/`
- `SCAFFOLD_API_KEY` stored in GitHub Secrets, `~/.bashrc`, and `~/scaffold-engine/.env`
- `.dockerignore` excludes git, venvs, logs, caches from build context
- All dependencies pinned (production in `requirements.txt`, dev in `requirements-dev.txt`, CI in `requirements-ci.txt`)

---

## Key Design Decisions

1. **Async-first** — All I/O is async. Blocking libraries (PyMilvus, CrossEncoder) are wrapped in `run_in_executor`
2. **Persistent HTTP client** — Module-level `httpx.AsyncClient` with connection pooling for Ollama
3. **4-step JSON parsing (centralized)** — All LLM JSON parsing consolidated in `app/utils/llm_parsing.py` via `parse_json_object()` and `parse_json_array()`
4. **Think-tag stripping** — Shared utility removes `<think>`/`<thinking>` blocks from all LLM outputs
5. **Proper HTTP errors** — All endpoints return appropriate status codes (not 200-with-error-body)
6. **Reproducible builds** — All pip deps pinned, base images pinned by digest, reranker weights pre-downloaded at build time
7. **Confirmation gate** — Ideation workflow halts at `awaiting_confirmation`, requiring explicit user confirmation
8. **SSE keepalive in pipelines** — Zero-width spaces emitted at intervals to prevent idle disconnects
9. **Conversational triage** — Lightweight `qwen3:4b` handles multi-turn planning before the expensive pipeline runs
10. **Two-model pipeline routing** — Triage uses 4b (fast), full pipeline uses 235b (deep)
11. **Transcript-based synthesis** — Single-message transcript prevents model confusion from replayed chat turns
12. **Auto-chain after /confirm** — Phase 2 → DAG → execute all in one flow
13. **Language pinning** — "Respond ONLY in English" in triage/synthesis prompts
14. **Concurrent execution guard** — Atomic UPDATE prevents duplicate job execution
15. **Active-node-aware cleanup** — Stale-job reaper skips jobs with running nodes
16. **Numeric DAG truncation** — Sorts T1, T2, T3... numerically, not alphabetically
17. **Upstream-last prompt assembly** — Mandatory upstream context prepended, task instruction last under `## YOUR TASK` with explicit instruction to build on upstream work
18. **Environment-first model configuration** — docker-compose env vars override config.py defaults
19. **Short-lived database sessions** — `execute_all_nodes()` uses independent sessions per operation, not request-scoped
20. **importlib-based test loading** — Avoids Docker `/app` package shadowing
21. **Pipeline tests run independently** — `test_scaffold_router.py` uses `--noconftest`
22. **Auto-retry on failure** — Failed nodes are automatically retried in `execute_all_nodes()` using existing `retry_failed_node()`, up to `max_retries`
23. **Tool-constrained DAG generation** — Only LLM, CodeGen, SearXNG, and Milvus are valid tools; Human and FileSystem removed to prevent unexecutable nodes
24. **Anti-redundancy DAG rules** — Prompt instructs LLM to produce distinct, non-overlapping nodes that extend rather than duplicate prior work
25. **Clean clarification display** — Feasibility clarifications shown without generic boilerplate suffixes
26. **Model valve system** — All model roles switchable via Open WebUI admin valves; overrides threaded per-request through `model_overrides` dict with `get_model()` helper enforcing priority chain (valve > env var > default). Embedder/reranker are config-level only due to dimension and singleton constraints
27. **Three-tier ingestion logic** — dedup threshold (>0.95) rejects, version-chain window (0.90–0.95) creates linked versions, everything below is a new entry. Version check runs after content-hash dedup and after embedding.
28. **Latest-version-by-default retrieval** — `query_rag()` strips superseded entries from results unless `include_history=True`, keeping responses current without breaking callers that don't pass the flag.
29. **Autonomous research agent** — `/research` decomposes topics via LLM, fans out SearXNG searches, distills facts through 7b model, ingests into Milvus with existing dedup pipeline, then gap-analyzes for iterative deepening
30. **Two-tier research model strategy** — 4b for decomposition/gap analysis (fast), 7b for extraction/summary (accurate). Avoids 235b model entirely for research to keep runtime under 30 min
31. **SSE heartbeat keepalives** — research agent emits heartbeat events every 8s during long LLM calls; pipeline renders as zero-width spaces to prevent Open WebUI's aiohttp proxy from timing out

---

## Known Issues

1. **Triage model latency on long conversations** — `qwen3:4b` on CPU can take several minutes per turn as context grows
2. **Knowledge base has 8 entries** — toon_v2 contains 8 entries from E2E test runs. Old 143 entries not recoverable; grows organically through pipeline usage
3. **Golden retrieval tests skipped** — 7 tests blocked on knowledge base repopulation
4. **Version chain filter is result-set scoped** — `query_rag()` only filters superseded entries when both versions appear in the same result set. Stricter Milvus-side filtering deferred
5. **Open WebUI file routing intermittent** — after container restarts, Open WebUI sometimes stops forwarding to the pipeline. Hard browser refresh + new chat resolves
6. **Context stripping depends on `</context>` tag** — if Open WebUI changes its context injection format, the regex in `pipe()` needs updating

---

## Observed Performance (CPU-only, April 8–9 2026)

| Operation | Observed Duration | Notes |
|-----------|------------------|-------|
| Triage turn (qwen3:4b) | ~30s – 300s+ | Scales with conversation length |
| Idea synthesis (qwen3:4b) | ~30s – 120s | Depends on conversation length |
| `/ideate` (Phase 1) | 100s – 547s | Includes refinement + feasibility LLM calls |
| `/ideate/confirm` (Phase 2) | ~512s – 1,450s | Research loop, distillation, embedding, ingestion |
| `/dag` | ~416s – 504s | Close to timeout threshold |
| `/execute` (single node) | ~893s | Includes RAG retrieval, reranker, generation, verification |
| `/health` | ~43ms | PostgreSQL + Ollama + Milvus checks |

---

## Commit History

| Phase | Scope | Commit |
|-------|-------|--------|
| A — Schema Alignment | Migrations, indexes, dockerignore | `582e32b`..`a105fc6` |
| B — Functional Fixes | SSE compiled_output, field name fix | `ff77c4e` |
| C — Code Quality | Think-tag stripping, json_repair, async, dead code, HTTP errors | `7996172`..`c7d6503` |
| D — DevOps Hardening | Config alignment, CI, connection pooling, dependency pinning | `c601079`..`1360ffa` |
| E — Ideation Workflow | Ideation pipeline with confirmation gate, Milvus ingestion | `897b29a` |
| F–U — Full Audit | Triage v3.1, synthesis rewrite, execution fixes, model/prompt fixes, dead code removal, schema fixes, async audit, JSON parsing consolidation, retry unification, TOON docs, 38 new tests, file cleanup | `156789c` |
| V — Test Fixes | Aligned test signatures with `execute_all_nodes()` (removed stale `db` arg, added `async_session` mock), updated DAG truncation tests from max 5→10 | `cb1ecc1` |
| W — Version Chains | Version chain ingestion (0.90–0.95 similarity), latest-version retrieval filtering, include_history param | `05f040a` |
| X — Refactoring | 65 issues: bug fixes, code quality, HTTP client consolidation, API schema fixes, dead code removal | `c43b3c4`..`9c2ca34` |
| Y — Test Rewrites | Behavioral tests replacing source-grep, test fixes, /model + valve + gt_browser coverage | `25f573e`..`23c64d2` |
| Z — /model Command | Chat-based model management: list, set, reset, available | `b409575` |

---

## File Structure (post-audit)

```
scaffold-engine/
├── app/
│   ├── main.py                    # FastAPI app, all endpoints
│   ├── config.py                  # Pydantic Settings
│   ├── auth.py                    # API key auth
│   ├── database.py                # Async SQLAlchemy
│   ├── model_router.py            # Ollama routing with retry cascade
│   ├── schemas.py                 # Request/response models
│   ├── rerankers.py               # CrossEncoder + RRF
│   ├── logging_config.py          # structlog config
│   ├── modules/
│   │   ├── execution_agent.py     # DAG execution, SSE streaming, auto-retry
│   │   ├── dag_generator.py       # DAG creation, Kahn's algorithm
│   │   ├── rag_pipeline.py        # RAG retrieval + ingestion
│   │   ├── ideation_workflow.py   # Phase 1 + Phase 2
│   │   ├── idea_refinement.py     # Idea → structured brief
│   │   ├── prompt_optimizer.py    # Prompt optimization
│   │   ├── gt_extractor.py        # Ground truth extraction + TOON
│   │   ├── gt_browser.py          # GT browsing (async-safe)
│   │   ├── prompt_inspector.py    # Prompt analysis
│   │   ├── execution_handler.py   # execution_status() only
│   │   └── cleanup.py             # Stale-job reaper
│   ├── routers/
│   │   └── status.py              # /status, /logs
│   ├── middleware/
│   │   ├── performance.py         # Request timing
│   │   └── error_logging.py       # Error capture
│   └── utils/
│       ├── llm_parsing.py         # Shared JSON parsing
│       ├── http_clients.py        # Shared SearXNG async client with connection pooling
│       ├── embedding_cache.py     # Two-tier embedding cache (LRU + Redis)
│       ├── staleness.py           # TTL-per-source-type sweep
│       └── milvus_utils.py        # Shared Milvus collection accessor + auto-create
├── pipelines/
│   ├── scaffold_router.py         # Main pipeline (v3.1)
│   ├── gt_browser.py
│   ├── execution_handler.py
│   ├── prompt_inspector.py
│   └── dag_viewer.py
├── db/
│   ├── init.sql
│   └── migrations/                # 002–009
├── docs/
│   ├── toon/                      # TOON spec + validator reference
│   ├── CI.md
│   └── logging-events.md
├── tests/                         # 274 tests
├── docker-compose.yml
├── Dockerfile
├── requirements.txt               # Production deps (pinned)
├── requirements-dev.txt            # Dev deps
├── requirements-ci.txt             # CI deps
├── Makefile
└── .github/workflows/             # CI/CD
```

---

## Changelog — April 12, 2026 (Triage file-awareness + /go flexibility)

### Changes to `pipelines/scaffold_router.py`

1. **`_extract_text()` — file/document content handling**
   - Previously only extracted `type: "text"` blocks from multimodal content lists
   - Now also captures `type: "file"` and `type: "document"` blocks, plus catch-all for any block with a `text` field (excluding images)
   - Fixes: triage model now sees uploaded file content instead of ignoring it

2. **Triage system prompt — file awareness**
   - Added instruction: when user provides a document/file/specification, treat it as primary context
   - Triage no longer asks user to re-explain what's already in the document
   - If document defines scope clearly, triage summarizes and suggests `/go` immediately

3. **`/go` command matching — accepts trailing text**
   - Changed from exact match (`msg in ("/go", "/run")`) to prefix match
   - `/go keep as the with separate proxy container` now triggers the pipeline
   - Chat history filter updated to match (excludes all `/go`-prefixed messages)

4. **Context stripping — Open WebUI `<context>` injection**
   - Open WebUI Model skins prepend file content as `<context>...</context>` to `user_message`
   - Added regex to extract the real user command after the last `</context>` tag
   - Without this, `/go` was buried inside context and never matched

### Changes to `app/config.py`

5. **Cloud timeout increased** — `cloud_timeout` bumped from 600s to 3600s (1 hour)

### Known Issues (updated)

4. **Open WebUI file routing intermittent** — after container restarts, Open WebUI sometimes stops forwarding requests to the pipeline. Hard browser refresh (`Ctrl+Shift+R`) and new chat usually resolves. Root cause under investigation.
5. **Context stripping depends on `</context>` tag** — if Open WebUI changes its context injection format, the regex in `pipe()` will need updating.


---

## Changelog — April 13, 2026 (Vector DB migration — Milvus 2.5.27, TOON schema, Redis cache)

### Phase 1: Milvus upgrade + Redis
1. **Milvus 2.4.17 → 2.5.27** — patches CVE-2026-26190 (CVSS 9.8 auth bypass). Fresh volume (`milvus-data-v2`); old data backed up in `milvus-data-backup-20260413`
2. **Redis cache added** — `redis:8-alpine` on `ai-network`, 2GB maxmemory, LRU eviction, RDB persistence
3. **`milvus-config/milvus.yaml`** — embedded etcd configuration for standalone mode (file retained as reference; bind-mount removed from compose due to segfault — env vars are the correct method)
4. **`app/config.py`** — added `redis_url`, `embedding_dim` (512), `model_embedder_id`

### Phase 2: TOON v2 schema + 512d HNSW_SQ8
5. **`toon_v2` collection created** — 16 TOON fields, 512d FLOAT_VECTOR, HNSW_SQ8 index (M=16, efConstruction=256, SQ8, BF16 refine), partition key isolation on `domain` (64 partitions), scalar indexes on content_hash/domain_tags/source_type/confidence_score/created_at/version
6. **`app/utils/embedding_cache.py`** (new) — two-tier cache: in-memory LRU (10K entries) + Redis persistent. Keys include model ID for auto-invalidation on model change
7. **`app/modules/rag_pipeline.py`** — full rewrite: collection `toon_v2`, 512d MRL-truncated embeddings, COSINE metric, HNSW_SQ8 search params, instruction-prefix query embedding, `_build_embedding_text()` for ingestion, `_content_hash()` for dedup, backward-compatible `ingest_entries()`
8. **`app/modules/gt_browser.py`** — updated to toon_v2 fields, COSINE search, `_embed_query()` from rag_pipeline

### Phase 3: Hardening
9. **Content-hash dedup** — exact hash check before embedding; skips insertion if identical content exists
10. **Semantic near-duplicate detection** — ANN search on new embedding; entries with cosine > 0.95 logged as near-duplicates
11. **`app/utils/staleness.py`** (new) — TTL-per-source-type sweep (real_time=7d, news=30d, community=90d, tech_docs=180d, curated=1y, official_docs=1y, ai_generated=180d)
12. **`app/modules/cleanup.py`** — staleness sweep wired into existing 15-min cleanup loop
13. **`app/main.py`** — health endpoint: Redis status, embedding cache hit stats, toon_v2 collection count

### Test updates
14. **`tests/test_integration.py`** — `test_rag_query_round_trip` updated to query `domain="eng"`
15. **`tests/test_tasks_13_14_15_16.py`** — collection name assertion updated to `toon_v2`
16. **`tests/test_retrieval_golden.py`** — skipped until knowledge base repopulated

### Known Issues (updated)
6. **Knowledge base empty** — toon_v2 has 2 test entries. Old 143 entries from `technical_knowledge` need re-ingestion through the pipeline. Old data preserved in `milvus-data-backup-20260413` volume.
7. **Golden retrieval tests skipped** — blocked on knowledge base repopulation (issue 6).
8. **Milvus 2.5.27 standalone config** — runs in docker-compose with `ETCD_USE_EMBED=true`, `COMMON_STORAGETYPE=local`, `seccomp:unconfined`. The `milvus.yaml` bind-mount causes segfaults and has been removed; env vars are the correct method. `milvus-config/milvus.yaml` retained as reference only.

---

## Changelog — April 13, 2026 (Model Valve System)

### Pipeline — scaffold_router.py
1. **8 new model valves** — `model_general`, `model_verifier`, `model_coder`, `model_embedder`, `model_reranker`, `model_router`, `model_fallback`, `model_cloud_alt`. All switchable from Open WebUI admin panel
2. **`_model_overrides()` helper** — builds override dict from current valve values
3. **All 9 orchestrator API calls** now pass `model_overrides` in request body (`/ideate`, `/ideate/confirm`, `/dag` ×2, `/execute/all`, `/idea`, `/dag` manual, `/optimize`, `/rag`)

### Orchestrator
4. **`app/config.py`** — `get_model(role, overrides)` helper: valve override > env var > default
5. **`app/main.py`** — `IdeaInput`, `DagInput` accept `model_overrides`; all endpoints extract and pass downstream
6. **`app/schemas.py`** — `ExecuteNextInput` gains `model_overrides: dict | None`
7. **`app/modules/ideation_workflow.py`** — `analyze_and_confirm()` and `research_and_compile()` accept and use overrides via `get_model()`
8. **`app/modules/dag_generator.py`** — `generate_dag()` accepts overrides
9. **`app/modules/execution_agent.py`** — `execute_all_nodes()` and `execute_next_node()` thread overrides to generation, verifier, and coder model selection
10. **`app/modules/idea_refinement.py`** — `refine_idea()` accepts overrides

### Design decisions
- **Embedder and reranker are config-level only** — embedder is tied to 512d vector dimensions (per-request swap risks dimension mismatch); reranker is a CrossEncoder singleton loaded at startup. Valves exist for these as configuration reference but do not override per-request
- **Override priority chain:** Open WebUI valve > docker-compose env var > config.py default
- **Backward compatible** — all `model_overrides` fields are optional; omitting them preserves existing behavior

### Test results
- **162 passed, 45 skipped, 0 failed** in container
- **30 passed** for scaffold_router.py locally
- No regressions from valve changes

### Known Issues (updated)
9. **Backup volume empty** — `milvus-data-backup-20260413` contains orphaned segments with no collection metadata in etcd. The 143 old entries are not recoverable from this volume. Knowledge base will be repopulated through the pipeline.

---

## Changelog — April 13, 2026 (Model Validation)

### Orchestrator
1. **`app/model_router.py`** — `validate_models(overrides)` queries Ollama `/api/tags`, resolves all 6 Ollama-routed roles (`model_general`, `model_verifier`, `model_coder`, `model_router`, `model_fallback`, `model_cloud_alt`) via `get_model()`, returns list of missing tags. Handles `:latest` suffix matching both directions
2. **`app/main.py`** — `_require_valid_models(overrides)` helper raises HTTP 422 with `missing_models` list and hint. Wired into `/ideate`, `/ideate/confirm`, `/dag`, `/execute/all`

### Design decisions
- **Validate at job start, not per-node** — avoids redundant Ollama calls during execution
- **Embedder and reranker excluded** — embedder is dimension-locked (512d), reranker is a HuggingFace CrossEncoder singleton; neither routes through Ollama generate
- **Silent pass on Ollama unreachable** — returns empty list so health check handles connectivity separately
- **Uses persistent `_get_client()`** — no new HTTP connections

### Known Issues (updated)
- Removed #10 (model validation not implemented) — resolved by this commit
11. **Version chain filter is result-set scoped** — `query_rag()` only filters superseded entries when both old and new versions appear in the same result set. If only the old version is retrieved (new one outside top-K), it still returns. Acceptable for now; stricter Milvus-side filtering deferred.

---

## Changelog — April 13, 2026 (Version Chains)

### `app/modules/rag_pipeline.py`
1. **`RagResult` dataclass** — added `version: int = 1` and `supersedes_id: str = ""` fields
2. **Vector + keyword search** — both now fetch `version` and `supersedes_id` from Milvus and populate `RagResult`
3. **`ingest_entries()` — 3-branch dedup/version logic:**
   - cosine > 0.95 → reject (semantic duplicate, unchanged)
   - cosine 0.90–0.95 → version chain: `version = old.version + 1`, `supersedes_id = old.entry_id`
   - cosine < 0.90 → new entry (`version=1`, `supersedes_id=""`)
4. **`query_rag()` — latest-version filtering:** filters out entries whose `entry_id` appears as a `supersedes_id` in another result. New `include_history: bool = False` parameter returns all versions when True

### `app/main.py`
5. **`RagInput`** — added `include_history: bool = False`, passed through to `query_rag()`

### Commit
- `05f040a` — `feat: version chains`

### Test results
- **202 passed, 29 skipped, 0 failed** — no regressions

---

## Changelog — April 14, 2026 (Milvus into docker-compose)

### Migration
1. **Milvus moved into `docker-compose.yml`** — previously ran via standalone `docker run`; now compose-managed with digest-pinned image (`sha256:ea3b924dfb2129fa2daca569eab657b4fcebc4baaed5706cf30d51e19d99b0c9`), healthcheck (`curl -f http://localhost:9091/healthz`), `start_period: 60s`
2. **`milvus.yaml` bind-mount removed** — caused segfault (SIGSEGV) on startup. `ETCD_USE_EMBED=true` and `COMMON_STORAGETYPE=local` env vars handle all required config. File retained in `milvus-config/` as reference
3. **`milvus-data-v2` volume declared `external: true`** — preserves existing data across compose lifecycle
4. **Orchestrator `depends_on` updated** — now waits for `milvus-standalone` and `scaffold-redis` healthy conditions (in addition to existing `scaffold-postgres`)
5. **Compose comment updated** — removed outdated note about Milvus running outside compose

---

## Changelog — April 13, 2026 (Model Valve Tests)

### Tests
1. **`tests/test_model_valves.py`** (new) — 17 tests across 3 classes, all run with `--noconftest`
   - `TestGetModel` (8 tests) — `get_model()` priority chain: override > settings default, falsy values (None, `""`) fall through, partial dicts work, valve-only keys (`model_embedder`) work via override
   - `TestModelOverrides` (4 tests) — `_model_overrides()` returns all 8 keys, values match valves, reflects changes, no extras
   - `TestPayloadInclusion` (5 tests) — `/idea`, `/dag`, `/optimize`, `/rag` payloads include `model_overrides`; custom valve values propagate

### Test count
- **248 collected, 219 passed, 29 skipped, 0 failed** (17 new tests added)

---

## Changelog — April 14, 2026 (E2E Verification + Partition Key Fix)

### Pre-flight
1. **toon_v2 collection missing** — lost when Milvus moved into docker-compose (volume survived but collection metadata did not). Recreated via `scripts/create_toon_v2.py`. Consider adding auto-create logic to `_get_collection()` for resilience against future volume resets.

### E2E Pipeline Verification
2. **Full auto-chain verified** — triage → `/go` → synthesis → Phase 1 (ideate, 27s) → `/confirm` → Phase 2 (research, 110s, 3 SearXNG queries, 8 entries ingested) → DAG (4 nodes, 9s) → execute (T1–T4, ~33min total) → compiled output (3,722 chars). All on CPU-only hardware.
3. **T1 auto-retry exercised** — verifier rejected T1's first attempt (scope mismatch: generated full CLI tool instead of HTML structure design). Auto-retry produced correct output on second attempt. Working as designed.
4. **RAG pipeline validated** — vector search (top scores 0.99+), keyword search, CrossEncoder reranker (13.6s cold load, ~4-6s warm), upstream context injection all functioning.
5. **Reranker cold-load** — `Qwen3-Reranker-0.6B-seq-cls` took 13.6s on first invocation (HuggingFace cache check + CPU load), ~4-6s on subsequent calls.

### Bug fix: partition key isolation
6. **`app/modules/rag_pipeline.py`** — three search paths were missing the required `domain` filter expression for Milvus partition key isolation (`partitionkey.isolation: true`):
   - **Vector search** in `_vector_search()` (~line 146): `if domain:` → `search_domain = domain or "eng"`; always includes `domain == "{search_domain}"` in expr
   - **Keyword search** in `_keyword_search()` (~line 205): same pattern
   - **Semantic dedup search** in `ingest_entries()` (~line 522): added `expr=f'domain == "{domain}"'` to the ANN search call
   - Without these, Milvus rejected every search with `partition key not found in expr`, silently disabling semantic dedup and degrading retrieval (keyword-only fallback)
7. **Commit:** `c43b3c4` — `fix: add domain expr to all Milvus searches — partition key isolation requires it`

### Test results
8. **In-container:** 202 passed, 30 skipped, 0 failed (26.98s)
9. **Pipeline (local):** 30 passed (0.11s)
10. **Model valves (local):** 17 passed (0.12s)
11. **Total:** 249 passed, 30 skipped, 0 failed — no regressions from partition key fix

### Known Issues (updated)
12. **Knowledge base has 8 entries** — toon_v2 now contains 8 entries from the E2E test run. Old 143 entries are not recoverable (backup volume had orphaned segments). Knowledge base will grow organically through pipeline usage.
13. **~~Collection not auto-created~~** — RESOLVED in `671214f`. `get_collection()` in `app/utils/milvus_utils.py` auto-creates toon_v2 with full TOON schema if missing.

---

## Changelog — April 14, 2026 (Code Quality — Issues 5, 8, 25)

### Issue 8: Pydantic config fix
1. **`app/config.py`** — `semantic_dedup_threshold` replaced raw `os.getenv()` with a standard Pydantic `float` field (`default=0.95`). Pydantic Settings auto-reads `SEMANTIC_DEDUP_THRESHOLD` from env. Removed unused `import os`
2. **Commit:** `a3bd8f0`

### Issue 25: Dead env var
3. **`docker-compose.yml`** — removed `MODEL_EMBEDDER: qwen3-embedding:0.6b` (never read by any code). `MODEL_EMBEDDER_PIPELINE` retained (used by orchestrator)
4. **Commit:** `589feee`

### Issue 5: Consolidated `_get_collection()` + auto-create (resolves Known Issue #13)
5. **`app/utils/milvus_utils.py`** (new, 114 lines) — single `get_collection(raise_on_missing=False)` function:
   - Connects to Milvus if needed
   - Auto-creates `toon_v2` with full TOON schema (16 fields, 512d HNSW_SQ8 COSINE, partition key isolation, 6 scalar indexes) if collection is missing — replicates `scripts/create_toon_v2.py` exactly
   - `raise_on_missing=True`: raises `RuntimeError` on failure (gt_browser pattern)
   - `raise_on_missing=False` (default): returns `None` on failure (rag_pipeline/staleness pattern)
6. **`app/modules/rag_pipeline.py`** — old `_get_collection()` replaced with `_get_collection = get_collection` alias. Removed direct `connections`/`utility` imports
7. **`app/modules/gt_browser.py`** — old `_get_collection()` now delegates to `get_collection(raise_on_missing=True)`. Removed direct `connections`/`utility` imports
8. **`app/utils/staleness.py`** — old `_get_collection()` replaced with `_get_collection = get_collection` alias. Removed direct `connections`/`utility` imports
9. **Commit:** `671214f`

---

## Changelog — April 14, 2026 (Bug Fixes)

### Issue 1 (CRITICAL): RAG context used wrong field name
1. **`app/modules/execution_agent.py`** — `r['topic']` → `r['title']` in three locations: `_fetch_rag_context()` (lines 231, 233) and `_milvus_search()` (line 390). The `query_rag()` response dict has both `topic` (always `""`) and `title` (actual value); all three references were reading the empty field
2. **`tests/test_execution_agent.py`** — updated mock data in `test_success_formats_results` to use `title` key

### Issue 12: Dead L2 distance check replaced with COSINE threshold
3. **`app/modules/execution_agent.py`** — `_fetch_rag_context()`: removed `if vec_score == 0.0 or vec_score > 1.0` (impossible under COSINE metric, leftover from L2 era). Replaced with `if vec_score < 0.3` to skip low-relevance results. Log message updated from `L2=` to `cosine=`

### Issue 4: Reranker model name hardcoded
4. **`app/rerankers.py`** — `_get_cross_encoder()` now reads `settings.model_reranker` instead of hardcoding `"tomaarsen/Qwen3-Reranker-0.6B-seq-cls"`. Import added for `app.config.settings`

### Issue 3: Ingested entries never expired
5. **`app/modules/rag_pipeline.py`** — `ingest_entries()`: replaced `"expires_at": 0` with `"expires_at": compute_expires_at(source_type, now)`. Import added for `app.utils.staleness.compute_expires_at`. Entries now get TTLs based on source_type (real_time=7d, news=30d, community=90d, tech_docs=180d, curated=1y, official_docs=1y, ai_generated=180d)

### Housekeeping
6. **`pipelines/failed/`** — stale backup of `scaffold_router.py` removed from repo, path added to `.gitignore`

### Commits
- `e914ad8` — `fix: topic→title in RAG context, cosine threshold, config-driven reranker, computed expires_at`
- `9142080` — `chore: remove stale pipelines/failed/ from repo`

### Test results
- **202 passed, 30 skipped, 0 failed** — no regressions

---

## Changelog — April 14, 2026 (Secondary Pipeline Bug Fixes)

### Issue 39: execution_handler.py — Wrong retry endpoint
1. **`_retry()` method** — endpoint was `/retry` (404), corrected to `/exec/retry` matching the orchestrator's actual route

### Issue 40: gt_browser.py — Field name mismatches
2. **`_handle_list()`** — `e.get("topic")` → `e.get("title")`
3. **`_handle_search()`** — `r.get("topic")` → `r.get("title")`
4. **`_handle_detail()`** — `data.get('topic')` → `data.get('title')`
5. **`_handle_detail()`** — `data.get('source_file')` → `data.get('source_url')`

### Issue 41: gt_browser.py — Wrong stats keys
6. **`_handle_stats()`** — `data.get("topics")` → `data.get("domains")`
7. **`_handle_stats()`** — `data.get("source_files")` → `data.get("source_types")`

### Commit
- `2ceba26` — `fix: correct endpoint and field names in secondary pipelines (#39, #40, #41)`
- 2 files changed, 7 insertions, 7 deletions

### Test results
- Container restart confirmed (`docker restart open-webui-pipelines`)
- All field names now match orchestrator response schemas

---

## Changelog — April 14, 2026 (SSE node_retry handler + pipeline summary refactor)

### Pipeline — pipelines/scaffold_router.py
1. **`_handle_sse_event()` — `node_retry` handler added** — when `execution_agent.py` auto-retries a failed node, the user now sees `🔄 Step T1: Retrying — <title> (attempt 2)...` instead of a confusing fail-then-silent-rerun sequence
2. **Test added** — `test_node_retry` in `TestHandleSSEEvent` (31 smoke tests total)

### Orchestrator — app/modules/execution_agent.py
3. **`_build_pipeline_summary()` extracted** — async helper that builds the `pipeline_complete` SSE payload (fetches compiled_output, computes pass/fail counts, assembles failed_node_details). Replaces two near-identical ~30-line blocks:
   - "terminal: all nodes done" path — calls with `extra_fields={"status": "completed"}`
   - "early exit: auto-completion" path — calls without extra fields
4. **Net change:** +76 / -63 lines — added a feature while reducing duplication

### Test results
5. **Pipeline (local):** 31 passed (0.09s)
6. **Model valves (local):** 17 passed (0.04s)
7. **In-container:** 202 passed, 30 skipped, 0 failed (20.91s)

### Commit
- `b409575` — `fix: add node_retry SSE handler + extract _build_pipeline_summary helper`

---

## Changelog — April 14, 2026 (Model Command System)

### Pipeline — scaffold_router.py
1. **`/model list`** — Shows current model assignments for all 8 roles with default status
2. **`/model available`** — Queries Ollama `/api/tags` and lists all available models
3. **`/model set <role> <model>`** — Validates model exists on Ollama, updates valve in-memory, shows old → new value. Accepts short role names (e.g., `general` instead of `model_general`)
4. **`/model reset`** — Resets all 8 roles to their default values, shows what changed
5. **`/model help`** — Usage instructions
6. **`_MODEL_DEFAULTS` class attribute** — Single source of truth for default model values
7. **`_SINGLETON_ROLES` class attribute** — Identifies embedder and reranker as singleton/dimension-locked; `/model set` warns that changes require container restart
8. **`_handle_command()` routing** — Added `/model` branch dispatching to `_handle_model()`
9. **`_help()` updated** — `/model <sub>` added to command table

### Design decisions
- **Ollama validation before set** — Prevents assigning nonexistent models (skipped for reranker since it's a HuggingFace model)
- **In-memory only** — Valve changes persist for the session but reset on container restart (consistent with Open WebUI valve behavior)
- **No orchestrator changes** — Purely pipeline-side; model overrides flow through existing `_model_overrides()` dict
- **`:latest` suffix matching** — Accepts both `qwen3` and `qwen3:latest` when validating against Ollama tags

### Commit
- `feat: add /model command system — list/set/reset/available from chat`

---

## Changelog — April 14, 2026 (Code Quality Refactor — Issues 9, 13, 14, 15, 16)

### Issue 9: Model selection consolidation
1. **`app/modules/idea_refinement.py`** — replaced manual override chain `model or (model_overrides or {}).get("model_general", model_router.settings.model_general)` with `get_model("model_general", model_overrides)` from `app/config.py`
2. **`app/modules/dag_generator.py`** — same replacement

### Issue 13: ConfirmInput Pydantic model
3. **`app/main.py`** — added `ConfirmInput(BaseModel)` with fields: `job_id` (str), `feedback` (str|None), `push_to_github` (bool=False), `model_overrides` (dict|None). `/ideate/confirm` endpoint now uses typed model instead of raw `request.json()`

### Issue 14: refine_idea target_status parameter
4. **`app/modules/idea_refinement.py`** — `refine_idea()` gains `target_status: str = "planning"` parameter; hardcoded `'planning'` in UPDATE SQL replaced with parameterized `:target_status`
5. **`app/modules/ideation_workflow.py`** — `analyze_and_confirm()` passes `target_status="awaiting_confirmation"` to `refine_idea()`, removing redundant status overwrite UPDATE

### Issue 15: Stale cleanup consolidation
6. **`app/modules/cleanup.py`** — new `reap_stale_jobs(db)` function covers all status sets: running/executing > 30min → failed (with active-node guard), planning > 60min → cancelled. Returns `{"running_to_failed": N, "planning_to_cancelled": N}`
7. **`app/main.py`** — startup cleanup block and `/jobs/cleanup` endpoint both call `reap_stale_jobs()` instead of inline SQL

### Issue 16: _parse_entries consolidation
8. **`app/modules/gt_extractor.py`** — 26-line `_parse_entries()` replaced with 1-line call to `parse_json_array()` from `app/utils/llm_parsing`, gaining `json_repair` fallback

### Commit
- `06dd410` — `refactor: consolidate model selection, stale cleanup, JSON parsing, and endpoint validation`

### Test results
- **In-container:** 202 passed, 30 skipped, 0 failed (23.20s)
- **Pipeline (local):** 31 passed (0.06s)
- **Model valves (local):** 17 passed (0.04s)
- **Total:** 250 passed, 30 skipped, 0 failed — no regressions

---

## Changelog — April 14, 2026 (HTTP Client Consolidation)

### Issue 10: `list_models()` ephemeral client
1. **`app/model_router.py`** — `list_models()` replaced `async with httpx.AsyncClient(timeout=10)` with persistent `_get_client()`. Eliminates per-call connection overhead

### Issue 11: `embed()` retry
2. **`app/model_router.py`** — `embed()` switched from `_call_ollama()` (single attempt) to `_dispatch_with_retry()`. Transient Ollama failures now retry with the same cascade as `generate()` and `chat()`

### Issue 17: Overly-aggressive filler patterns
3. **`app/modules/prompt_optimizer.py`** — Removed 9 patterns from `FILLER_PATTERNS` that stripped semantically meaningful words: `just`, `basically`, `essentially`, `actually`, `simply`, `maybe`, `perhaps`, `try to`, `attempt to`. These can be meaningful in technical prompts (e.g., "just return the first 5", "simply output JSON"). Remaining patterns target genuine filler (politeness phrases, AI self-references)

### Issue 22: Shared SearXNG client
4. **`app/utils/http_clients.py`** (new) — Module-level `httpx.AsyncClient` with `base_url`, connection pooling (`max_connections=10`, `max_keepalive=5`), lazy initialization, and `close_clients()` shutdown hook
5. **`app/modules/execution_agent.py`** — `_searxng_search()` replaced ephemeral client with `get_searxng_client()`, paths changed to relative (`/search`)
6. **`app/modules/gt_extractor.py`** — `_search_searxng()` replaced ephemeral client with `get_searxng_client()`, fixed indentation bug from migration
7. **`app/main.py`** — `close_clients()` wired into lifespan shutdown

### Issue 29: Redis health check
8. **`app/main.py`** — Health endpoint replaced sync `redis.from_url()` with async Redis connection from `EmbeddingCache._get_redis()`. Eliminates sync `redis` import and per-call connection

### Bonus: Startup Ollama check
9. **`app/main.py`** — Lifespan startup Ollama verification replaced ephemeral `httpx.AsyncClient` with `_get_client()` from `model_router`

### Test results
- **202 passed, 30 skipped, 0 failed** in container (33.05s)
- No regressions from any changes

### Commit
- `db85efa` — `refactor: consolidate HTTP clients, add embed retry, fix filler patterns`

---

## Changelog — April 14, 2026 (API Schema & Code Quality — Issues 6, 19, 20, 23, 24)

### Issue 6: RagInput missing domain parameter
1. **`app/main.py`** — Added `domain: str | None = None` to `RagInput` Pydantic model. Passed `domain=body.domain` through to `query_rag()`. Users can now query non-"eng" partitions via the `/rag` API

### Issue 19: ExecutionResult missing tool field
2. **`app/schemas.py`** — Added `tool: str | None = None` to `ExecutionResult`. Pydantic was silently dropping the `tool` key from `execute_next_node()` return dicts via `response_model`

### Issue 20: Silent auth disable warning
3. **`app/auth.py`** — Added `logging.getLogger(__name__)` and a module-level `_logger.warning()` when `SCAFFOLD_API_KEY` is empty. Previously auth was silently disabled with no indication in logs

### Issue 23: Dead FileSystem tool routing
4. **`app/modules/execution_agent.py`** — Changed `if tool in ("CodeGen", "FileSystem")` to `if tool == "CodeGen"`. FileSystem was removed from `VALID_TOOLS` in the DAG generator; the routing case was unreachable dead code

### Issue 24: Duplicate Kahn's cycle detection
5. **`app/modules/dag_generator.py`** — Removed the full Kahn's algorithm block (adjacency build, in-degree tracking, cycle check) from `_validate_graph()`. `validate_dag()` already runs Kahn's and raises `ValueError` on cycles before `_validate_graph()` is called, making its cycle check unreachable. Retained roots/leaves and disconnected-node checks. Updated docstring accordingly

### Test results
- **202 passed, 30 skipped, 0 failed** in container (24.61s)
- `/rag` endpoint verified with `domain="eng"` — returns results correctly
- No regressions

### Commit
- `4516306` — `fix: RagInput domain param, ExecutionResult tool field, auth warning, dead FileSystem route, dedup cycle detection (#6, #19, #20, #23, #24)`

---

## Changelog — April 14, 2026 (Code Quality — Issues 21, 27, 28, 30, 31, 32, 34, 36, 43)

### Bug fixes
1. **Issue 21: `app/modules/rag_pipeline.py`** — removed duplicate `topic_slug = title.lower().replace(" ", "-")[:60]` line (~line 550)
2. **Issue 28: `pipelines/scaffold_router.py`** — removed `model_overrides` from `/rag` POST payload. RAG uses config-level embedder only; overrides were accepted but never consumed. Test updated to assert absence
3. **Issue 31: `app/modules/prompt_optimizer.py`** — default optimizer model changed from `model_general` (235b) to `model_verifier` (7b). Prompt rewriting doesn't need the heavy model
4. **Issue 34: `app/modules/gt_extractor.py`** — removed unused `start_id` parameter from `_format_toon_rows()`, hardcoded `eid = i + 1`

### Improvements
5. **Issue 27: `app/rerankers.py`** — added `reset_reranker()` function that clears `_cross_encoder` and resets `_load_failed`, allowing retry after transient load failures
6. **Issue 32: `app/modules/gt_browser.py`** — `gt_stats()` query limit increased from 16,384 to 100,000 with a log warning if results are truncated
7. **Issue 36: `app/modules/cleanup.py`** — replaced `async for db in get_db()` with `async with async_session() as db`, consistent with `execute_all_nodes()` pattern

### Typing / docs
8. **Issue 43: `pipelines/dag_viewer.py`** — `pipe()` return type changed from `str` to `Optional[str]` (returns `None` for non-matching messages)
9. **Issue 30: `scaffold-engine-overview.md`** — corrected `triage_timeout` (900→3600) and `dag_timeout` (1800→3600) to match actual valve defaults

### Housekeeping
10. **`pipelines/failed/`** — stale backup directory removed from git tracking (994 lines deleted)

### Commit
- `9c2ca34` — `fix: remove dup topic_slug, reset_reranker, cleanup async_session, gt_stats limit, prompt opt default, dag_viewer typing, drop dead start_id/model_overrides, doc timeouts`

### Test results
- **In-container:** 202 passed, 30 skipped, 0 failed
- **Pipeline (local):** 31 passed
- **Model valves (local):** 17 passed
- **Total:** 250 passed, 30 skipped, 0 failed — no regressions

---

## Changelog — April 14, 2026 (Issues 18, 26, 33)

### Issue 18: gt_search domain filter
1. **`app/modules/gt_browser.py`** — `gt_search()` gains `domain: str | None = None` parameter. When provided, adds `expr=f'domain == "{domain}"'` to the Milvus search call for partition key isolation compliance. When `None` (default), no expr is set — searches all partitions. Backward-compatible
2. **`app/main.py`** — `GtSearchInput` gains `domain: str | None = None` field; `/gt/search` endpoint passes `domain=body.domain` through to `gt_search()`

### Issue 26: Distillation model downgrade
3. **`app/modules/ideation_workflow.py`** — `research_and_compile()` distillation LLM call changed from `get_model("model_general", ...)` (235b, ~500s on CPU) to `get_model("model_router", ...)` (4b). Summarizing web snippets doesn't need the heavy model. Compilation call remains on `model_general`

### Issue 33: Pagination scaling comment
4. **`app/modules/gt_browser.py`** — added comment to `gt_list()` documenting offset-based pagination scaling limitation and suggesting cursor-based pagination for future growth beyond ~1K entries

### Commit
- `8d58b50` — `fix: gt_search domain filter, distillation model_router, pagination comment (#18, #26, #33)`

### Test results
- **In-container:** 202 passed, 30 skipped, 0 failed
- **Pipeline (local):** 31 passed
- **Total:** 233 passed, 30 skipped, 0 failed — no regressions

---

## Changelog — April 14, 2026 (Behavioral Test Rewrite — Issues 48, 49, 50, 60, 61, 65)

### test_sse_streaming.py (Issues 48/49/50)
1. **Replaced 18 source-grep tests with 16 behavioral tests** — all tests now call `execute_all_nodes()` with mocked dependencies and parse actual SSE output
2. **TestSSEWireFormat** (3 tests) — verifies `event:`/`data:` lines, valid JSON, double-newline termination
3. **TestEventSequenceContract** (3 tests) — happy path ordering, `node_failed` emission, `pipeline_complete` always last
4. **TestPipelineCompleteStructure** (6 tests) — `total_nodes`, `passed`/`failed` counts, `duration_ms`, `compile_status`, `failed_nodes` array on partial
5. **TestConcurrentGuard** (2 tests) — guard failure yields error, error references job ID
6. **TestNodeStartEvent** (1 test) — verifies `node_key`, `title`, `tool` fields
7. **TestHeartbeatCharacter** (1 test) — verifies zero-width space in router (skipped in container)

### test_idea_refinement.py (Issue 60)
8. **Replaced 6 signature-inspection tests with 12 behavioral tests** — all tests call `refine_idea()` with mocked `model_router.generate` and DB
9. **TestRefineIdeaHappyPath** (5 tests) — output dict structure, `status=planning`, `refined_brief`, `model_used`, prompt contains idea text
10. **TestRefineIdeaLLMFailure** (2 tests) — LLM error returns `status=failed`, unparseable JSON returns `status=failed`
11. **TestRefineIdeaDomainOverride** (2 tests) — user-supplied domain applied, absent domain keeps LLM value
12. **TestRefineIdeaTargetStatus** (1 test) — custom `target_status` parameter
13. **TestRefineIdeaModelOverrides** (1 test) — `model_overrides` passed through to `get_model()`
14. **TestRefineIdeaDBInteractions** (1 test) — verifies INSERT + UPDATEs + commits

### test_rag_pipeline.py (Issue 61)
15. **Replaced 10 AST/source-analysis tests with 12 behavioral tests** — all tests call `query_rag()` or `_rrf_fuse()` with mocked Milvus, embedder, and reranker
16. **TestQueryRagHappyPath** (4 tests) — result structure, required fields, score sub-fields, metadata
17. **TestQueryRagErrors** (2 tests) — collection unavailable, embedding failure
18. **TestRRFFusion** (3 tests) — combines both sources, preserves disjoint results, sorted by RRF score
19. **TestConfidenceThreshold** (1 test) — `too_strict` fallback returns up to 3 results
20. **TestVersionFiltering** (2 tests) — superseded entries removed, `include_history=True` keeps all

### test_health_cleanup.py (Issue 65)
21. **Replaced 20 source-grep tests (7 targeting nonexistent `jobs_cleanup.py`) with 15 behavioral tests**
22. **TestHealthEndpointResponse** (6 tests) — calls `health()` directly with mocked PG/Ollama/Milvus/Redis; verifies dict structure, status, timestamp, checks dict
23. **TestHealthDegradedStates** (3 tests) — `degraded` when Milvus down, `unhealthy` when PG or Ollama down
24. **TestReapStaleJobs** (6 tests) — calls `reap_stale_jobs()` with mocked DB; verifies counts, both types, commits

### Commit
- `25f573e` — `test: replace source-grep tests with behavioral tests (#48, #49, #50, #60, #61, #65)`

### Test results
- **In-container:** 210 passed, 20 skipped, 0 failed (26.78s)
- **Pipeline (local):** 31 passed
- **Model valves (local):** 17 passed
- **Total:** 258 passed, 20 skipped, 0 failed — no regressions


---

## Changelog — April 14, 2026 (Test Fixes — Issues 46, 47, 57, 62, 63, 64)

### test_scaffold_router.py (Issues 46, 47)
1. **TestGoCommand** (2 tests) — `test_go_with_empty_history` verifies "Nothing to launch yet" when no user messages exist; `test_go_triggers_synthesis` mocks `_synthesize_idea` and `requests.post`, verifies synthesis is called and "Synthesizing" appears in output
2. **TestConfirmCommand** (1 test) — `test_confirm_usage_error` verifies `/confirm` with no job_id yields usage message
3. **TestContextStripping** (2 tests) — `test_context_tags_stripped` verifies `<context>...</context>` prefix is stripped and `/go` is recognized; `test_no_context_tags_passes_through` verifies plain messages work unmodified

### test_dag_generator.py (Issue 57)
4. **VALID_TOOLS constant** — removed `"Human"` and `"FileSystem"` to match production code. Now `{"LLM", "CodeGen", "SearXNG", "Milvus"}`

### test_integration.py (Issue 62)
5. **RAG result assertion** — `assert "topic" in first or "title" in first` simplified to `assert "title" in first` matching the Issue 1 field name fix

### test_retrieval_golden.py (Issues 63, 64)
6. **Golden retrieval field name** — 3 occurrences of `r["topic"]` changed to `r["title"]`; assertion message updated from "Expected topic containing" to "Expected title containing"

### Commit
- `6cc2e15` — `test: fix test issues #46, #47, #57, #62, #63, #64`

### Test results
- **Pipeline (local):** 36 passed (0.11s)
- **DAG (local):** 15 passed (0.06s)
- **Model valves (local):** 17 passed
- **Integration/golden:** syntax verified (require container for execution)
- **Total local:** 68 passed, 0 failed — no regressions

---

## Changelog — April 14, 2026 (Test Coverage — /model commands, valve overrides, gt_browser field mappings)

### tests/test_scaffold_router.py
1. **`TestModelCommand`** (7 tests) — covers all `/model` subcommands:
   - `test_model_help`: verifies all 8 roles mentioned in help output
   - `test_model_list`: markdown table with all role assignments
   - `test_model_set_valid`: mocked Ollama, valve updated
   - `test_model_set_invalid_role`: error with valid role list
   - `test_model_set_model_not_found`: mocked Ollama missing model, valve unchanged
   - `test_model_reset`: restores defaults, reports changes
   - `test_model_available`: mocked Ollama, lists models with count

### tests/test_model_valves.py
2. **`test_model_set_updates_overrides`** — after valve change, `_model_overrides()` reflects new value while other roles stay at defaults

### tests/test_gt_browser.py (new)
3. **`TestFieldMappings`** (3 tests) — verifies Prompt 3 fixes (Issues 40, 41):
   - `test_handle_list_uses_title`: data reads from `title` key, not `topic`
   - `test_handle_search_uses_title`: search results use `title`
   - `test_handle_stats_uses_domains`: stats use `domains`/`source_types`, not `topics`/`source_files`

### Commit
- `23c64d2` — `test: add /model command tests, valve-override test, gt_browser field mapping smoke tests`

### Test results
- **Pipeline (local):** 43 passed (0.07s)
- **Model valves (local):** 18 passed (0.05s)
- **gt_browser (local):** 3 passed (0.03s)
- **Total local:** 64 passed, 0 failed — no regressions

---

## Changelog — April 15, 2026 (/research Command — Autonomous Topic Research Agent)

### New file: `app/modules/research_agent.py` (~350 lines)
1. **`run_research()` async generator** — main research loop yielding SSE events. Phases: decompose → search → extract → ingest → gap analyze → iterate
2. **`_decompose_topic()`** — LLM decomposes topic into 3-8 keyword-based SearXNG queries with facet tracking. Uses `model_router` (4b) for speed
3. **`_search_queries()`** — sequential SearXNG searches with 1.5s delay, URL dedup, max 20 URLs per iteration
4. **`_extract_entries()`** — LLM distills search results into atomic knowledge entries with confidence scores. Uses `model_verifier` (7b). Batches of 10 results
5. **`_analyze_gaps()`** — LLM compares collected knowledge against topic outline, identifies uncovered facets, generates follow-up queries
6. **`_generate_summary()`** — produces human-readable summary of all collected research. Uses `model_verifier` (7b)
7. **`ResearchState` dataclass** — tracks iteration count, search/URL history, entries, coverage, gap queries
8. **Heartbeat keepalives** — `asyncio.create_task()` wrapper emits SSE heartbeat events every 8s during long LLM calls to prevent proxy timeouts
9. **Convergence detection** — stops iterating when: all entries are duplicates, coverage ≥ 85%, or max iterations reached
10. **Depth control** — `shallow=1`, `medium=2`, `deep=4` max iterations

### Orchestrator changes
11. **`app/main.py`** — `POST /research` endpoint with SSE streaming, model validation via `_require_valid_models()`
12. **`app/schemas.py`** — `ResearchInput(topic, depth, domain, model_overrides)`
13. **`app/config.py`** — 6 new settings: `research_max_iterations`, `research_max_queries`, `research_max_urls_per_iteration`, `research_searxng_delay`, `research_chunk_size`, `research_timeout`

### Pipeline changes
14. **`pipelines/scaffold_router.py`** — `/research <topic> [--depth shallow|medium|deep]` command routing
15. **`_research_and_stream()`** — SSE consumer with threaded reader, heartbeat rendering as zero-width spaces, progress display for all research phases
16. **`/help` updated** — `/research` added to command table

### Design decisions
- **Two-tier model strategy** — `model_router` (4b) for decomposition/gap analysis, `model_verifier` (7b) for extraction/summary. Keeps total research time under 30 min on CPU
- **Reuses existing `ingest_entries()`** — inherits 3-tier dedup (>0.95 reject, 0.90-0.95 version chain, <0.90 new entry), content-hash dedup, TTL-per-source-type, partition key isolation
- **model_overrides threaded through** — same valve system as all other commands
- **No job/DB tracking** — research runs are stateless; results persist only in Milvus. Job-based tracking deferred

### Test results
- **E2E verified** — `/research HNSW index tuning for Milvus --depth shallow`: 9 entries extracted, 9 ingested, 22 min
- **E2E verified** — `/research Python asyncio patterns --depth shallow`: 10 entries extracted, 10 ingested, 27 min
- **RAG retrieval confirmed** — ingested entries retrievable via `/rag` with 0.9999 rerank scores
- **Knowledge base** — grew from 8 to 27 entries across test runs

### Known Issues
14. **Research duration on CPU** — shallow runs take 20-30 min (dominated by 7b extraction LLM calls). Medium/deep runs proportionally longer
15. **SearXNG snippets only** — extraction uses ~200-char search snippets, not full page content. Trafilatura integration would improve quality significantly
16. **No concurrent research guard** — multiple `/research` calls can run simultaneously. Atomic check-and-set (like `execute_all_nodes`) deferred

### Commit
- `f6a72c2` — `feat: add /research command`

---

## Changelog — April 16, 2026 (Trafilatura Full-Page Extraction)

### app/modules/research_agent.py
1. **`_fetch_and_extract(results)`** (new) — fetches URLs concurrently via `httpx.AsyncClient` with `asyncio.Semaphore(5)`, 15s timeout, `follow_redirects=True`. Extracts clean text via `trafilatura.extract(output_format='txt', with_metadata=False)` in `asyncio.to_thread()`. Skips results with `len(text) < 100`. Returns `[{"url", "content"}]`
2. **`_chunk_text(text, max_tokens=1500, overlap_tokens=200)`** (new) — splits long articles at `\n\n` paragraph boundaries, ~4 chars/token estimate, with 200-token overlap between chunks
3. **`_extract_entries()` modified** — before batching, calls `_fetch_and_extract()` to get full-page text. Builds `url_to_text` map; for each result, chunks full-page text or falls back to SearXNG snippet. Batch size reduced from 10→5 when full-page content is available (larger per-item context). If all URLs fail, falls back to snippet-only behavior (existing path unchanged)

### requirements.txt
4. **`trafilatura==2.0.0`** pinned

### Design decisions
- **Short-lived httpx client for external URLs** — acceptable per constraints (not an internal service); reused across all URLs within a single `_fetch_and_extract()` call
- **Snippet fallback is per-URL** — if trafilatura fails for one URL but succeeds for others, only the failed URL uses its snippet
- **Total fallback preserved** — if `_fetch_and_extract()` returns empty, `_extract_entries()` behaves identically to pre-patch (batch_size=10, raw snippets)

### Verification
- **Tests:** 227 passed, 21 skipped, 0 failed in container — no regressions
- **Live smoke test:** `research_fetch: 19/20 URLs extracted via trafilatura` on "httpx connection pooling" shallow research. 16 entries extracted and ingested

### Commit
- `6a0167b` — `feat: trafilatura full-page extraction for research loop (#3.1)`

### Test suite totals (updated)
- **In-container:** 227 passed, 21 skipped, 0 failed
- **Pipeline (local):** 43 passed
- **Model valves (local):** 18 passed
- **gt_browser (local):** 3 passed
- **Total:** 291 passed, 21 skipped, 0 failed
---

## Changelog — April 16, 2026 (Research Agent Performance — #3.5)

### `app/modules/research_agent.py`
1. **Extraction snippet truncation** — `r['content']` → `r['content'][:600]` in batch prompts. Smaller prompts = faster 7b inference per batch
2. **Extraction `max_tokens`: 4096 → 1024** — extraction rarely produces >1K tokens; saves generation time
3. **Dict guard in entry loop** — `entries = [e for e in entries if isinstance(e, dict)]` before `entry.get()`. Fixes `AttributeError: 'str' object has no attribute 'get'` crash when LLM returned a list of strings
4. **Redis SearXNG cache** — new `_searxng_cache_key/get/set` helpers; `searxng:{sha256(query)[:16]}` key namespace; 1h TTL; reuses `EmbeddingCache._get_redis()` connection
5. **`_search_queries` cache-first path** — checks Redis before HTTP; on cache hit, skips both the SearXNG request and the `research_searxng_delay` sleep; on miss, persists result after successful fetch

### Measurements (shallow depth, CPU-only Ollama)
- Baseline: 22:40
- After (cache miss): 18:34 (**−18%**, 4m6s faster)
- After (cache hit): 23:31 — post-extraction variance dominates; cache itself saves ~10s per repeated query

### Design decisions
- **Cache keys are content-hashed**, not literal query strings — keeps Redis keys short and collision-safe
- **No new Redis client** — `get_cache()._get_redis()` reused to avoid a second connection pool
- **Silent cache errors** (debug-level log, return `None`) — Redis being down should not break research
- **Cache hit skips rate-limit sleep** — the `asyncio.sleep(research_searxng_delay)` only matters when actually hitting SearXNG

### Known post-extraction bottleneck (unchanged, tracked for future)
- Embedding loop + dedup search + ingest + gap analysis + summary = ~15–23 min of a shallow run. Target of 15 min total is not reachable without touching this path. Candidates for future work: batch embedding, skip gap analysis on `depth=shallow`, reduce summary `max_tokens`.

### Commit
- `ee5d6ba` — `perf: reduce batch size, token limits, add SearXNG cache (#3.5)`

---

## Changelog — April 16, 2026 (Contradiction Detection — #3.4)

### `app/modules/research_agent.py`
1. **`_check_contradictions(entries)`** (new) — async helper placed before `_extract_entries()`. Scans entry pairs; two entries whose titles share 2+ words (lowercased, whitespace-split) are flagged as candidates. Returns `[{"entry_a", "entry_b", "shared_concepts"}]`. Capped at 5 pairs to avoid O(n²) blowup on large batches.
2. **`run_research()` — wire-in** — after `extraction_complete` / before `ingest_entries()` call, runs contradiction check on the batch. When pairs are found, emits `contradictions_detected` SSE event with `count` and `pairs` payload. Entries are ingested regardless — informational only, user decides.

### Design decisions
- **Heuristic over ML** — title word overlap is cheap, deterministic, and good enough for human review. No embedding, no LLM call.
- **Informational only** — ingestion proceeds unconditionally. Contradictions surface via SSE for user visibility; no auto-resolution.
- **Capped at 5 pairs** — prevents large batches from producing unbounded candidate lists. Worst case: C(n,2) comparisons, short-circuited on 5th hit.

### Tests
3. **`tests/test_research_agent.py`** — 3 new tests:
   - `test_check_contradictions_flags_shared_words` — 2 entries sharing "python is language" → 1 pair returned
   - `test_check_contradictions_skips_low_overlap` — disjoint titles → empty list
   - `test_check_contradictions_caps_at_five` — 6 entries with shared words → exactly 5 pairs

### Test results
- **In-container:** 20 passed (5.24s) — 17 pre-existing + 3 new, 0 regressions
- Full test suite not re-run; no changes outside `research_agent.py` + test file

### Commit
- `c5451b0` — `feat: lightweight contradiction detection in research loop (#3.4)`


---

## Changelog — April 16, 2026 (Pipeline Handoff: /research → /go)

### `pipelines/scaffold_router.py`
1. **`research_complete` SSE handler** — replaced single vague "continue with your project" line with a three-option next-steps block:
   - `/go` — build a project plan from this research
   - `/research <subtopic> --depth deep` — explore further
   - `/rag <query>` — query the ingested knowledge

### Design decisions
- **Zero new code paths** — the research summary is already streamed to chat during `research_complete`, so when the user later types `/go`, `_synthesize_idea()` picks it up from the chat transcript naturally. No new commands, no orchestrator changes, no schema changes.
- **Informational, not coercive** — suggestion only. User can still type anything; `/go` is just surfaced as the obvious default.
- **Reuses Phase 1 entry point** — `_synthesize_idea()` → `/ideate` → confirmation gate flow is unchanged.

### Tests
2. **`tests/test_scaffold_router.py`** — 1 new test added to `TestResearchCommand`:
   - `test_research_complete_suggests_go` — mocks `requests.post` with a fake SSE stream carrying `research_complete`, asserts output contains `/go`, "build a project plan", and "Research Complete"

### Test results
- **Pipeline (local):** 48 collected, 47 passed, 1 pre-existing failure (`TestContextStripping::test_no_context_tags_passes_through` — unrelated; `/help` yields empty `combined` under the importlib-loaded module, root cause not in this scope)
- No regressions from this change

### Commit
- `bea6a33` — `feat: suggest /go after /research completes`

## Changelog — April 16, 2026 (Ingestion breakdown in /research SSE)

### `app/modules/rag_pipeline.py`
1. **`ingest_entries()` return type** — changed from `int` to `dict` with shape `{"new": N, "versioned": M, "rejected": K, "skipped_hash": S}`. `new + versioned` = successfully inserted rows. Existing dedup logic untouched; counters added at the four decision points (exact-hash skip, semantic reject, version chain insert, new insert).

### `app/modules/research_agent.py`
2. **`ResearchState`** — gained `total_new`, `total_versioned`, `total_skipped_hash` (existing `total_ingested` / `total_rejected` retained for back-compat; `total_ingested` now = new + versioned)
3. **Ingest call site** — unpacks dict into per-bucket state counters. Fixes prior accounting bug where hash-skipped entries were lumped into `total_rejected`
4. **`research_complete` SSE event** — adds `new`, `versioned`, `rejected`, `skipped_hash` fields so users see the three-bucket breakdown per research run
5. Per-iteration `ingestion_complete` event left unchanged (final rollup only, per spec)

### `app/modules/ideation_workflow.py`
6. **Caller updated** — `ingest_count = _ingest_stats["new"] + _ingest_stats["versioned"]` preserves the existing `milvus_ingested` semantics in Phase 2 output

### Tests
7. **`tests/test_research_agent.py`** — new `TestIngestionBreakdown` class (2 tests): `test_research_complete_contains_breakdown_fields` asserts final SSE payload shape with mocked stats; `test_breakdown_totals_accumulate_across_iterations` asserts counters sum correctly across a 2-iteration run
8. **`tests/test_dedup_rejection.py`** — assertion updated from `result == 0` to `result["new"] + result["versioned"] == 0` and `result["rejected"] == 1`
9. **`tests/test_ideation_workflow.py`** — 3 `AsyncMock(return_value=N)` mocks updated to return the new dict shape
10. **Mock in `test_research_agent.py` `TestRunResearch`** — updated from `return_value=1` to dict shape

### Test results
- **232 passed, 21 skipped, 0 failed** (container)
- 2 new behavioral tests added; no regressions

### Commit
- `2c8598c` — `feat: surface ingestion breakdown (new/versioned/rejected) in /research SSE`

---


## Changelog — April 16, 2026 (Scheduled Research — Phase 1 Scaffolding)

### Decision
1. **Chose APScheduler (in-process) over cron sidecar** — matches existing `cleanup.py` async loop pattern, reuses structlog observability, single container, direct `run_research()` call (no HTTP hop). Sidecar's isolation win judged theoretical since `run_research()` runs in orchestrator either way.

### Dependency
2. **`requirements.txt`** — added `apscheduler==3.10.4` (stable 3.x, async support; 4.x rewrite not yet production-ready)

### Migration 010 (`db/migrations/010_scheduled_jobs.sql`)
3. **`scheduled_jobs` table** — user-facing schedule metadata: `id`, `topic`, `depth` (shallow/medium/deep), `cron_expression`, `enabled`, `last_run_at`, `last_status`, `last_job_id`, `next_run_at`, `run_count`, `failure_count`, timestamps
4. **`apscheduler_jobs` table** — APScheduler's internal jobstore (pre-created to keep migrations in our control rather than runtime DDL): `id VARCHAR(191)`, `next_run_time DOUBLE PRECISION`, `job_state BYTEA`
5. **Partial indexes** on `enabled = TRUE` for `scheduled_jobs` (fast scans as disabled entries accumulate)

### docker-compose.yml
6. **Added env vars to scaffold-orchestrator service:**
   - `SCHEDULER_ENABLED: "true"` — toggle for tests / one-off runs
   - `SCHEDULER_TIMEZONE: "America/New_York"` — cron expression interpretation TZ
   - `SCHEDULER_JOBSTORE_URL: postgresql+psycopg2://...` — sync driver for `SQLAlchemyJobStore` (coexists with async asyncpg elsewhere)

### Phase 1 status
7. **No container rebuild yet** — Phase 2 will add `psycopg2-binary`, the scheduler module, lifespan integration, and `/schedule` endpoints before rebuilding once
8. **Database state:** 12 tables total (up from 10); both new tables verified empty

### Open design question for Phase 2
9. **Inline vs. fire-and-forget execution** — should scheduled jobs block the APScheduler worker until `run_research()` completes, or kick off as a background task? Fire-and-forget safer for deep research (which can run 30+ min on CPU); inline gives cleaner status capture. Defer to implementation phase.
---

## Changelog — April 17, 2026 (Scheduled Research Jobs)

### New capability
Users can schedule recurring `/research` runs via cron expressions. Schedules survive orchestrator restarts (APScheduler rehydrates from `scheduled_jobs` on startup).

### Migration
- **`db/migrations/011_scheduled_jobs.sql`** — `scheduled_jobs` (user-facing metadata) + `apscheduler_jobs` (APScheduler jobstore, pre-created so migrations stay in our control). Renamed from 010 to resolve duplicate-number conflict with `010_research_sessions.sql`.

### Dependencies (new)
- `apscheduler==3.10.4`, `tzlocal==5.2`, `psycopg2-binary==2.9.9` (sync driver for `SQLAlchemyJobStore`)

### Orchestrator
1. **`app/scheduler.py`** (new, 116 lines) — AsyncIOScheduler + SQLAlchemyJobStore. `init_scheduler()` rehydrates enabled rows from DB; `_execute_research_job()` calls `run_research()` directly in-process via asyncio, updates `last_run_at`/`last_status`/`run_count`/`failure_count` via the `apscheduler_jobs.next_run_time` foreign reference.
2. **`app/config.py`** — `sync_database_url` property (asyncpg→psycopg2 swap for jobstore); new settings `scheduler_enabled`, `scheduler_timezone`, `scheduler_jobstore_url` (all env-configurable via `SCHEDULER_*`).
3. **`app/main.py`** — lifespan starts/stops scheduler. New endpoints:
   - `POST /schedule` — cron validation, row insert, APScheduler add, returns full record
   - `GET /schedule` — lists schedules
   - `DELETE /schedule/{id}` — removes DB row and APScheduler job
4. **`app/schemas.py`** — `ScheduleCreate`, `ScheduleResponse` Pydantic models.
5. **`docker-compose.yml`** — `SCHEDULER_ENABLED=true`, `SCHEDULER_TIMEZONE=UTC`, `SCHEDULER_JOBSTORE_URL=""` (derives from `DATABASE_URL` when blank).

### Pipeline (`scaffold_router.py`)
6. **`/schedule` chat command** — subcommands `list` / `add "<cron>" <topic>` / `delete <id>` / `help`. Add uses `shlex` to parse quoted cron expressions.
7. **Command dispatcher fix** — `pipe()` now dispatches unhandled `/`-commands to `_handle_command()`. **This was dormant dead code before** — `/model`, `/idea`, `/rag`, `/status`, `/help`, `/optimize`, `/dag`, `/skip` were all silently broken in chat but undetected because tests called `_handle_command` directly. All working post-fix (verified in chat).
8. **Help table** — new row for `/schedule <sub>`.

### Design decisions
- **In-process asyncio execution** — scheduler awaits `run_research()` directly rather than self-POSTing `/research`. Cleaner cancellation, no extra HTTP hop.
- **UTC-only cron** — start simple; per-schedule TZ can be added later.
- **`apscheduler_jobs` pre-created in migration** — keeps schema in our control rather than runtime DDL from `SQLAlchemyJobStore`.
- **`scheduled_jobs.enabled = TRUE` is source of truth** — APScheduler jobstore is a warm cache; rehydrate rebuilds it on startup.

### Tests (new — 21 total)
- **`tests/test_scheduler.py`** (7) — lifecycle, rehydration, add/remove, cron validation. Includes `sys.modules` cleanup guard for sqlalchemy pollution from `test_domain_filtering.py`.
- **`tests/test_schedule_command.py`** (14) — help/list/add/delete subcommands with mocked HTTP.

### Test counts post-merge
- **In-container:** 239 passed, 18 skipped, 0 failed
- **Local pipelines:** 83 passed, 0 failed
- **Total:** 322 passed, 0 failed

### Verified end-to-end
- `POST /schedule` → row persisted → `next_run_at` computed → orchestrator restart → `jobs=1` on rehydrate → `DELETE /schedule/1` → empty.
- Chat: `/schedule list`, `/schedule add "0 9 * * 1" test`, `/schedule delete 1`, `/model list`, `/help` all render correctly.

### Known issues (updated)
- **#14 (new): `/confirm` block in `pipe()` missing terminal `return`** — latent bug predating this work; after DAG generation the block falls through instead of calling `/execute/all`. Deferred. Workaround: type `/execute <job_id>` manually.

---

## Changelog — April 17, 2026 (Fix /confirm auto-chain — #14)

### `pipelines/scaffold_router.py`
1. **`/confirm` handler in `pipe()` — missing `/execute/all` call** — after DAG generation the handler yielded "running N steps..." and returned without invoking execution. Jobs stuck in `planning`. Added `yield from self._execute_and_stream(job_id, num_nodes, headers)` before the terminal `return`.

### Tests
2. **`tests/test_scaffold_router.py`** — new `test_confirm_invokes_execute_all` asserts three POSTs fire in sequence: `/ideate/confirm`, `/dag`, `/execute/all`. Suite: 49 passed, 0 failed.

### Known Issues (updated)
- Removed #14 (/confirm block missing terminal execute call) — resolved.
- **Pre-existing:** `TestConfirmCommand` is defined twice in `tests/test_scaffold_router.py` (lines 473 and 567); second shadows first. Non-blocking; worth a separate cleanup pass.

### Commit
- `78376ad` — `fix: /confirm auto-chains into /execute/all (#14)`

---

## Changelog — April 17, 2026 (/research <url> — Direct URL Ingestion)

### New capability
`/research <url>` skips SearXNG discovery and ingests a specific page directly. URL vs topic detected automatically inside `run_research()`; no new endpoint, no schema change.

### `app/modules/research_agent.py`
1. **`_is_url(s)`** (new) — validates `http(s)://` + netloc via `urllib.parse`. Rejects bare domains, non-http schemes, empty strings; tolerates leading/trailing whitespace
2. **`_robots_allowed(url, user_agent)`** (new) — stdlib `urllib.robotparser` check. Fail-open on any error (missing robots.txt = allowed). Fetches `/robots.txt` via short-lived `httpx.AsyncClient` (10s timeout)
3. **`_fetch_url_bounded(url, max_bytes=5MB)`** (new) — streaming fetch with hard byte cap. Checks `content-length` header first, aborts mid-stream if exceeded. 30s timeout, `User-Agent: ScaffoldEngine/1.0`, follows redirects
4. **`_run_research_url_mode(url, state, session_id, extract_model, summary_model, t0)`** (new, ~150 lines) — URL-mode pipeline: robots check → bounded fetch → trafilatura extract → chunk → LLM distill (batches of 5) → ingest → summary → finalize. Single iteration, no gap analysis
5. **`run_research()` branch** — inserted at top, after `ResearchState` construction: if `_is_url(topic)`, emits `research_started` with `mode="direct_url"`, delegates to `_run_research_url_mode()`, returns. Normal topic flow untouched

### Design decisions
- **Auto-detect in `run_research()`** — no new endpoint, no schema change. Router passes URL through as `topic`; orchestrator branches transparently
- **Stdlib `urllib.robotparser`** — no new dependency. Fail-open (missing robots.txt = allowed per RFC)
- **5MB cap via streaming** — both `content-length` pre-check and mid-stream abort via `aiter_bytes()` accumulator
- **Reuses `_chunk_text`, `ingest_entries`, `_generate_summary`, `_score_source`** — all existing infrastructure (dedup >0.95 reject, version chain 0.90-0.95, TTL by source_type, partition key isolation)
- **Batch size 5** — same as topic-mode full-page path. `facet="direct_url"` tags all entries
- **Skips decompose/search/gap-analysis** — single URL = nothing to discover

### Tests — `tests/test_research_url_mode.py` (new, 20 tests)
- `TestIsUrl` (8) — http/https/path+query accepted; bare domain, non-URL text, empty, ftp rejected; whitespace tolerated
- `TestRobotsAllowed` (4) — fail-open on 404, fail-open on exception, disallowed path blocked, allowed path passes
- `TestFetchUrlBounded` (4) — oversize `content-length` rejected, small page fetched, non-200 rejected, mid-stream cap enforced
- `TestRunResearchUrlMode` (4) — happy path reaches `research_complete` with `depth=direct_url`, robots-blocked emits error + no ingest, fetch-failed emits error, non-URL topic still hits `_decompose_topic`

### Commit
- `da9e2ae` — `feat: /research <url> direct URL ingestion (#4.5a)`

### Test results
- **In-container:** 259 passed, 21 skipped, 0 failed (20 new + 239 existing)
- **Pre-existing collection error in `test_schedule_command.py`** (unrelated): file-path-based pipeline load fails in container; same pattern as 3 other pipeline tests but lacks their `FileNotFoundError` guard. Tracked for future cleanup

### Known Issues (new)
- **#15 (new)**: `test_schedule_command.py` fails collection in container (missing `FileNotFoundError` guard around pipeline file read at import time). Pre-existing; unrelated to this change. Workaround: `--ignore=tests/test_schedule_command.py`

---

## Changelog — April 17, 2026 (/research/pdf — Direct PDF Ingestion)

### New capability
Users can upload a PDF directly to the orchestrator (bypassing Open WebUI's built-in document RAG, which intercepts file blocks before they reach the pipeline). PDFs are extracted, chunked, LLM-distilled, and ingested into Milvus with the same dedup + version chain + TTL infrastructure as `/research <url>`.

### Architectural context — why a new endpoint
Diagnostic instrumentation confirmed Open WebUI's RAG layer captures PDF uploads on its side and sends the pipeline only post-composed meta-prompts (query generation, citation-based response, follow-up suggestions). The raw PDF bytes never reach the scaffold router. To retain control over extraction quality, scanned-PDF detection, and table handling, upload goes directly to the orchestrator via a new multipart endpoint — no new router-side changes required.

### New endpoints (`app/main.py`)
1. **`POST /research/pdf`** — multipart upload (`file` field, required). Query params: `extractor` (`auto`/`pypdf`/`plumber`, default `auto`), `domain` (optional partition key). Validates `.pdf` extension, 20MB cap, non-empty. Returns SSE stream (same event vocabulary as `/research`).
2. **`GET /research/pdf`** — renders a self-contained drag-and-drop HTML upload page (inline CSS/JS, no frameworks). Streams SSE events live into a terminal-style log. Dropdowns for extractor + domain.

### New helpers (`app/modules/research_agent.py`)
3. **`_extract_pypdf(pdf_bytes) -> (text, page_count)`** — text extraction via pypdf. Per-page try/except skips malformed pages without aborting.
4. **`_extract_pdfplumber(pdf_bytes) -> (text, page_count)`** — same contract, pdfplumber implementation for multi-column / structured PDFs.
5. **`_extract_threshold(page_count) -> int`** — `max(200, page_count * 50)`. Floors at 200 chars; scales linearly for multi-page docs.
6. **`_extract_pdf_text(pdf_bytes, extractor="auto")`** — cascade: `"auto"` tries pypdf first, falls back to pdfplumber if below threshold; `"pypdf"`/`"plumber"` force. Both sync libs run via `asyncio.to_thread`. Both failing → raises `RuntimeError` with "scanned or unreadable" message.
7. **`_run_research_pdf_mode(pdf_bytes, filename, extractor, state, session_id, ...)`** — PDF-mode pipeline: 20MB check → extract → chunk → LLM distill (batches of 5) → ingest → summary → finalize. Single iteration, no gap analysis, no search. Entries tagged `facet="direct_pdf"`, source `pdf://<filename>`, source_type `tech_docs`, confidence 0.8.
8. **`run_research_pdf(pdf_bytes, filename, extractor, domain, model_overrides)`** — entry point symmetric with `run_research`. Concurrent-research guard, session creation, `_run_research_pdf_mode` dispatch, error finalize.

### Design decisions
- **Bypass Open WebUI entirely** — multipart upload straight to the orchestrator. Diagnostic showed OWU's RAG layer absorbs all file blocks before the pipeline sees them.
- **pypdf default, pdfplumber opt-in fallback** — pypdf is 5-10× faster on prose and zero transitive deps; pdfplumber only runs when pypdf underproduces. LLM distillation downstream flattens structured data anyway.
- **Threshold `max(200, pages*50)`** — catches both fully-scanned PDFs and partially-degraded extractions.
- **20MB cap enforced twice** — at endpoint boundary (HTTP 413) and inside `_run_research_pdf_mode` (SSE error).
- **`pdf://<filename>` virtual URL** — keeps TOON schema consistent, avoids collision with real http URLs.
- **Sync SSE default, same pattern as `/research`** — fire-and-forget variant (`?async=true`) can be added later without endpoint changes.

### Dependencies (new, pinned)
- `pypdf==5.1.0`, `pdfplumber==0.11.4`, `python-multipart==0.0.17`

### Tests — `tests/test_research_pdf_mode.py` (new, 17 tests)
- `TestExtractThreshold` (2), `TestExtractPypdf` (2), `TestExtractPdfplumber` (1)
- `TestExtractPdfTextCascade` (6) — pypdf success, auto-fallback, force pypdf, force plumber, both-fail raises, invalid-extractor defaults
- `TestRunResearchPdf` (6) — happy path, oversize rejection, scanned error, concurrent blocked, extractor propagation, domain override

All tests use a dep-free inline raw-PDF byte generator — no reportlab required.

### E2E verification (live)
- `scaffold_test.pdf` (2 KB, 1 page, 577 chars extracted) via curl
- SSE flow: `research_started → decomposition → search → extraction (5 entries) → ingestion (5 new) → summary → research_complete`
- Total duration: **5m 54s** on CPU
- RAG query `"HNSW index Milvus"` → top result cites `pdf://scaffold_test.pdf` with rerank score **0.9998**

### Test results
- **In-container:** 276 passed, 21 skipped, 0 failed (+17 new, no regressions)

### Commit
- `51f308c` — `feat: /research/pdf direct PDF ingestion (#4.5b)`

### Known Issues (new)
- **#16**: First LLM call on PDF content is slow (~3 min for 1-page on CPU, cold-start on qwen2.5:7b). Subsequent calls faster. Batched warm-up deferred.
- **#17**: Large multi-page PDFs (50+ pages) will produce multi-hour runtimes on CPU. Consider `?async=true` mode next iteration.
- **OCR for scanned PDFs** explicitly deferred — `_extract_pdf_text` surfaces "scanned or unreadable" error; tesseract/paddleocr integration tracked as future work.
