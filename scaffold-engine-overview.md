# Scaffold Engine — Project Overview

**Last Updated:** April 13, 2026 (Vector DB migration — Milvus 2.5.27, TOON schema, Redis cache)
**Repo:** `LocketKeyLLC/scaffold-engine` on GitHub | `~/scaffold-engine` locally
**Latest Commit:** `e114662` — `feat: model validation — pre-flight check against Ollama /api/tags, 422 on missing models`
**Test Suite:** 230 collected, 201 passed, 29 skipped, 0 failed
**Codebase:** ~5,900 lines of application Python across 26 source files + ~974 lines in `scaffold_router.py` (pipeline)

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
| Vector Store | Milvus 2.5.27 (standalone, embedEtcd) | `milvus-standalone` | 19530 |
| UI | Open WebUI (pinned by SHA256 digest) | `open-webui` | 3000→8080 |
| Pipelines | Open WebUI Pipelines (pinned by SHA256 digest) | `open-webui-pipelines` | 9099 |
| Web Search | SearXNG (pinned by SHA256 digest) | `searxng` | 8888→8080 |
| Redis Cache | redis:8-alpine | `scaffold-redis` | 6379 |
| Inference | Ollama (host-installed, CPU-only) | N/A (host) | 11434 |

All service images are pinned by SHA256 digest in `docker-compose.yml`. The Python base image is pinned to `3.12.13-slim`. All pip dependencies are pinned to exact versions.

**Database credentials:** `POSTGRES_USER=scaffold`, `POSTGRES_PASSWORD=scaffold_dev_pw`, `POSTGRES_DB=scaffold_engine`

**Timeout configuration:**
- Open WebUI `AIOHTTP_CLIENT_TIMEOUT=7200` (2 hours)
- `scaffold_router.py` triage timeout: **900s** (15 min)
- `scaffold_router.py` auto-chain `/ideate` timeout: **1800s** (30 min)
- `scaffold_router.py` `/ideate/confirm` timeout: **1800s**
- `scaffold_router.py` DAG timeout: configurable via `dag_timeout` valve (default **1800s**)
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
| `app/main.py` | ~575 | FastAPI app with lifespan, health checks, middleware, all endpoints |
| `app/model_router.py` | 306 | Ollama API routing with retry cascade, persistent `httpx.AsyncClient` connection pool |
| `app/config.py` | ~38 | Pydantic Settings configuration (all env vars with defaults aligned to docker-compose) |
| `app/auth.py` | 33 | API key authentication via `X-API-Key` header |
| `app/database.py` | 26 | Async SQLAlchemy engine and session management |
| `app/schemas.py` | ~369 | Pydantic request/response models for all endpoints |
| `app/rerankers.py` | 156 | CrossEncoder reranker with RRF (Reciprocal Rank Fusion) fallback |
| `app/logging_config.py` | 86 | Structured JSON logging via structlog |

### Execution Pipeline

| File | Lines | Purpose |
|------|-------|---------|
| `app/modules/execution_agent.py` | ~1,139 | DAG node execution, SSE streaming, tool dispatch, verification, compiled output, concurrent execution guard, upstream prompt restructuring, auto-retry. Uses short-lived database sessions |
| `app/modules/dag_generator.py` | ~615 | DAG creation with Kahn's cycle detection, numeric-sort truncation (max 10 nodes). JSON parsing via shared `llm_parsing.py` |
| `app/modules/rag_pipeline.py` | 451 | RAG retrieval: embed → parallel vector + keyword search → RRF merge → CrossEncoder rerank. Includes `ingest_entries()` |
| `app/modules/ideation_workflow.py` | ~265 | Ideation-to-Workflow pipeline: Phase 1 (refine + feasibility + confirmation gate), Phase 2 (research → ingest → compile). 8 smoke tests |
| `app/modules/idea_refinement.py` | ~177 | Refines raw user ideas into structured briefs |
| `app/modules/prompt_optimizer.py` | 210 | Prompt optimization: strip → LLM optimize → verify |
| `app/modules/gt_extractor.py` | 457 | Ground truth extraction: SearXNG → LLM distillation → TOON formatting → optional GitHub push |
| `app/modules/gt_browser.py` | ~170 | Ground truth browsing and search. All Milvus calls wrapped in `run_in_executor` |
| `app/modules/prompt_inspector.py` | 116 | Prompt analysis and inspection |
| `app/modules/execution_handler.py` | ~75 | Execution status only (`execution_status()`). Dead code (`retry_node()`) removed in audit |
| `app/modules/cleanup.py` | 65 | Periodic stale-job reaper (15-min interval), active-node-aware |

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

---

## Open WebUI Pipelines

5 pipelines in `pipelines/` route user interactions from Open WebUI through to Scaffold Engine:

| Pipeline | Lines | Purpose |
|----------|-------|---------|
| `scaffold_router.py` | ~974 | Main pipeline (v3.1): conversational triage, transcript-based synthesis, auto-chains `/go` → Phase 1 → confirmation gate, and `/confirm` → Phase 2 → DAG → execute. SSE streaming with keepalive. **30 smoke tests** |
| `gt_browser.py` | 205 | Ground truth browsing |
| `execution_handler.py` | 201 | Direct execution control |
| `prompt_inspector.py` | 178 | Prompt analysis |
| `dag_viewer.py` | 111 | DAG visualization |

### scaffold_router.py — Valves (Admin-Configurable)

| Valve | Default | Purpose |
|-------|---------|---------|
| `api_key` | `""` | Scaffold Engine API key |
| `orchestrator_url` | `http://scaffold-orchestrator:8000` | Orchestrator endpoint |
| `dag_timeout` | `1800` | Seconds to wait for DAG generation |
| `keepalive_interval` | `10` | Seconds between keepalive zero-width spaces |
| `triage_model` | `qwen3:4b` | Model for conversational triage and synthesis |
| `triage_timeout` | `900` | Seconds to wait for triage model responses |
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

8 tables in the `scaffold_engine` database:

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

8 incremental migrations in `db/migrations/` (002–008).

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

248 tests collected (excluding 4 flaky live golden-retrieval tests):

- **227 passed, 21 skipped** (skips: files not in container, 7 from deleted `jobs_cleanup.py`)
- **`test_scaffold_router.py`** — 30 smoke tests (pure functions, SSE parsing, command dispatch). Run with `--noconftest`
- **`test_ideation_workflow.py`** — 8 smoke tests (Phase 1 + Phase 2 with full dep mocking)
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

---

## Known Issues

1. **Triage model latency on long conversations** — `qwen3:4b` on CPU can take several minutes per turn as context grows
2. **`/ideate/confirm` returned 500** on one occasion — root cause unknown, may be transient Milvus/LLM error
3. **End-to-end pipeline validated** — Full auto-chain (triage → synthesis → Phase 1 → confirm → Phase 2 → DAG → execute) confirmed working on CPU-only hardware (April 11, 2026)

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
│       └── llm_parsing.py         # Shared JSON parsing
├── pipelines/
│   ├── scaffold_router.py         # Main pipeline (v3.1)
│   ├── gt_browser.py
│   ├── execution_handler.py
│   ├── prompt_inspector.py
│   └── dag_viewer.py
├── db/
│   ├── init.sql
│   └── migrations/                # 002–008
├── docs/
│   ├── toon/                      # TOON spec + validator reference
│   ├── CI.md
│   └── logging-events.md
├── tests/                         # 248 tests
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
3. **`milvus-config/milvus.yaml`** — embedded etcd configuration for standalone mode
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
8. **Milvus 2.5.27 standalone requires env vars** — `ETCD_USE_EMBED=true`, `COMMON_STORAGETYPE=local`, `seccomp:unconfined`. The milvus.yaml port-override approach caused startup panics; env vars are the correct method.

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
