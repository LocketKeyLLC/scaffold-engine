# Scaffold Engine — Comprehensive Overview

The single source of truth for the project. Architecture, runtime, every module, every public function, the full database schema, all configuration, the data formats, the logging catalog, the known issues, performance benchmarks, and a glossary. (Per-sprint `§17.x` development history is kept in an operator-internal log, not this public reference; release-level history is in [CHANGELOG.md](./CHANGELOG.md).)

This file replaces the prior scattered docs (`docs/ARCHITECTURE.md`, `docs/CHANGELOG.md`, `docs/CI.md`, `docs/logging-events.md`, `docs/audit/*`, `docs/toon/TOON_*.md`, the `review/*.md` audit notes, and the per-package READMEs). For day-to-day operator commands, see `USER_GUIDE.md`. For first-touch onboarding, see `README.md`.

> **Pinned to API v1.2.0** (`docs/openapi.json`). `make openapi-check` enforces no silent contract drift.

---

## Table of contents

1. [What scaffold-engine is](#1-what-scaffold-engine-is)
2. [Container topology + port map](#2-container-topology--port-map)
3. [Request flow — happy path](#3-request-flow--happy-path)
4. [Workflows](#4-workflows)
5. [Database schema (PostgreSQL 16)](#5-database-schema-postgresql-16)
6. [Pydantic schemas](#6-pydantic-schemas)
7. [RAG pipeline](#7-rag-pipeline)
8. [Research agent](#8-research-agent)
9. [Assist Mode](#9-assist-mode)
10. [TOON data format](#10-toon-data-format)
11. [Module reference](#11-module-reference)
12. [Configuration reference](#12-configuration-reference)
13. [Logging events catalog](#13-logging-events-catalog)
14. [Testing + CI](#14-testing--ci)
15. [Conventions and invariants](#15-conventions-and-invariants)
16. [Known issues (architecture audit, 2026-05-05)](#16-known-issues)
17. [Sprint history + roadmap](#17-sprint-history--roadmap)
18. [Performance benchmarks](#18-performance-benchmarks)

---

## 1. What scaffold-engine is

A self-hosted **DAG orchestration engine for multi-step LLM workflows**. A user submits an idea or prompt; the system:

1. **Triages** via a lightweight conversational model (qwen3:4b)
2. **Refines** into a structured brief
3. **Assesses feasibility** and halts for explicit user confirmation
4. **Researches** the topic (SearXNG, or directly from URL/GitHub/OpenAPI/PDF), distills facts via LLM, ingests them into Milvus RAG
5. **Compiles** a high-fidelity prompt and workflow
6. **Generates a DAG** of execution nodes, each typed (LLM / CodeGen / SearXNG / Milvus)
7. **Executes** node-by-node in dependency order with SSE streaming, RAG context injected as upstream
8. **Compiles** the final output from leaf nodes
9. **Streams** real-time progress to the UI

Runs entirely on local hardware (Pop!_OS, CPU-only Ollama inference). Cloud models available as opt-in for heavy roles. Stack: Python 3.12 async (FastAPI + SQLAlchemy async), Postgres 16, Milvus 2.5.27 standalone, Redis 8, SearXNG, Ollama on host.

Public surface from v1.0.0:
- HTTP API at `:8000` (44 endpoints, OpenAPI snapshot at `docs/openapi.json`)
- Python SDK `scaffold-engine-client` at `sdk/` (sync `Client` + async `AsyncClient` + SSE helpers)
- Terminal CLI `scaffold-engine-cli` at `cli/` (delegates to the SDK)
- Open WebUI chat surface via 5 OWUI pipelines at `pipelines/`

---

## 2. Container topology + port map

```
┌────────────┐    ┌──────────────┐    ┌─────────────────┐
│ Open WebUI │───▶│ Pipelines    │───▶│ Orchestrator    │
│ :3000      │    │ :9099        │    │ :8000           │
└────────────┘    │ scaffold_    │    └────────┬────────┘
                  │   router.py  │             │
                  └──────────────┘    ┌────────┼────────────────┬──────────┐
                                      ▼        ▼                ▼          ▼
                                ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌────────┐
                                │ Postgres │ │  Milvus  │ │  Redis   │ │ Ollama │
                                │ :5432    │ │ :19530   │ │ :6379    │ │ host   │
                                │          │ │ 512d     │ │          │ │ :11434 │
                                └──────────┘ └──────────┘ └──────────┘ └────────┘
                                                                            ▲
                                                              SearXNG ──────┘
                                                              :8888  (research path)

                /design (engineering-design pipeline, §17.140-§17.156, §17.319)
                                     │
                       ┌─────────────┼──────────────┐
                       ▼             ▼              ▼
                ┌──────────┐  ┌────────────┐  ┌──────────────┐
                │ ngspice  │  │ verilator  │  │ symbiyosys   │
                │ :8001    │  │ :8002      │  │ :8003        │
                │ analog   │  │ digital    │  │ formal verif │
                └──────────┘  └────────────┘  └──────────────┘
```

All containers on Docker `ai-network` bridge (172.18.0.0/16). Pipelines and orchestrator reach **host Ollama** via the bridge gateway `172.18.0.1:11434`. `host.docker.internal` is unavailable on Pop!_OS native Docker.

| Service | Port | Container | Image |
|---|---|---|---|
| Open WebUI | 3000 | `open-webui` | pinned by SHA256 |
| Pipelines | 9099 | `open-webui-pipelines` | pinned by SHA256 |
| Orchestrator | 8000 | `scaffold-orchestrator` | `python:3.12.13-slim` (multi-stage Dockerfile) |
| Postgres 16 | 5432 | `scaffold-postgres` | `postgres:16` |
| Milvus 2.5.27 | 19530 | `milvus-standalone` | pinned by SHA256 (standalone, embedded ETCD) |
| Redis 7.4 | 6379 | `scaffold-redis` | `redis:8-alpine` |
| SearXNG | 8888 | `searxng` | pinned by SHA256 |
| ngspice sidecar (§17.140) | 8001 | `scaffold-ngspice` | locally-built (`python:3.12.13-slim` + apt `ngspice` 44.2), `read_only`, `cap_drop ALL`, `no-new-privileges`, `/tmp` 64m tmpfs |
| Verilator sidecar (§17.141) | 8002 | `scaffold-verilator` | locally-built (Verilator 5.024 from source), `read_only`, `cap_drop ALL`, `/tmp` 256m `rw,nosuid,nodev,exec` tmpfs (binaries must execute) |
| SymbiYosys sidecar (§17.142) | 8003 | `scaffold-symbiyosys` | locally-built (OSS CAD Suite 2026-05-12 tarball: yosys + sby + z3), `read_only`, `cap_drop ALL`, `/tmp` 512m `exec` tmpfs |
| Ollama | 11434 | (host, not containerized) | local install, CPU-only |

**EDA sidecars are loopback-only and reached over the bridge by name** (`http://scaffold-ngspice:8001`, etc.) — they have no auth surface of their own, are not in the request-path for any chat workflow, and only fire when an operator drives the `/design` flow (`design_circuit` job_type). The orchestrator wraps each one with an audit-the-attempt contract (`sim_runs` row written even on transport/timeout/non-zero exit) so failures never raise — see §17.140-§17.142.

Compose: `docker-compose.yml` is the prod runtime — image is hermetic, **zero host-source bind mounts**, only the `hf-cache` and `scaffold-logs` named volumes are mounted (post-X.27, 2026-05-09; §17.62). `docker-compose.dev.yml` is the dev override that bind-mounts `app/`, `tests/`, `cli/`, `sdk/`, `scripts/`, `db/`, `pipelines/`, `Dockerfile`, `.github/`, `docs/` (all `:ro`) plus `tests/benchmarks/` (rw) for live edit + bench writes. `make dev-up` brings up the dev overlay; `make test` requires the dev image.

All pip deps in `requirements.txt` (prod) / `requirements-dev.txt` / `requirements-ci.txt` (CI), every line pinned. All container images pinned by SHA256 digest in compose.

---

## 3. Request flow — happy path

User types `/idea X` in Open WebUI:

1. **OWUI → Pipelines.** OWUI strips wrapper context tags (`</context>`, `</documents>`, `</source>`) and forwards to `pipelines/scaffold_router.py::pipe()`.
2. **Pipelines → Orchestrator.** Router POSTs to `:8000/ideate` with the cleaned message + `X-API-Key` header.
3. **Orchestrator runs Phase 1.** `app/modules/ideation_workflow.py::refine_and_assess` calls `model_router.get_model("model_general")` for refine, then for feasibility. Job lands in `awaiting_confirmation` and the response streams back over SSE.
4. **User sends `/confirm <job_id>`.** Router auto-chains Phase 2 → DAG → execute, all over SSE. The auto-chain logic lives in `pipelines/scaffold_router.py`, **not** in orchestrator endpoints. Curl-only paths skip it.
5. **Phase 2** (`research_and_compile`): SearXNG queries → trafilatura full-page extract → LLM distill → Milvus ingest → compile.
6. **DAG** (`dag_generator.py`): produces T1..Tn with Kahn cycle check, INSERTs nodes with `is_output_node` flag on leaves.
7. **Execute** (`execution_agent.py`): topological order, RAG context injected as upstream, verifier-gated, auto-retry up to `max_retries`.
8. **Compile** (`_compile_output`): prefers explicit `is_output_node=TRUE` (Strategy 0); falls back to title heuristic / last CodeGen / concatenation.

**Job status flow:** `pending → refining → awaiting_confirmation → researching → planning → executing → running → completed | failed | cancelled | blocked`. Assist Mode branch: `... → assisted_executing → assisted_running → completed`.

**Middleware order** (declared in `app/main.py` as `ErrorLogging → Performance → RequestId`; Starlette runs in **reverse-add order**, so the runtime stack from outermost is `RequestId → Performance → ErrorLogging`). `request_id` binds first so every downstream log line carries it.

---

## 4. Workflows

### 4.1 The complete user journey

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

### 4.2 Phase 1 — `/ideate`

`POST /ideate` body: `IdeaInput { idea, domain?, model?, model_overrides? }`. Backed by `ideation_workflow.refine_and_assess`. Pipeline:

1. INSERT `jobs` row with `status='pending'`, captures `job_id`.
2. UPDATE → `'refining'`. `idea_refinement.refine_idea` calls `model_general` to produce a structured brief (problem statement, success criteria, constraints).
3. UPDATE → `'awaiting_confirmation'`. Feasibility step: same model (or `ideation_model_role`) returns `{feasible, confidence, summary, recommended_research_queries}`.
4. Response: `{job_id, status, refined_brief, feasibility, recommended_research_queries}`.

Halt point. Nothing proceeds without the user typing `/confirm`.

### 4.3 Phase 2 — `/ideate/confirm`

`POST /ideate/confirm` body: `ConfirmInput { job_id, feedback?, push_to_github=False, model_overrides? }`. Backed by `ideation_workflow.research_and_compile`. Pipeline:

1. Atomic claim: `UPDATE jobs SET status='researching' WHERE id=? AND status='awaiting_confirmation' RETURNING ...` — prevents double-execution under concurrent `/confirm` (idempotency invariant #12).
2. SearXNG research loop (uses `recommended_research_queries` from Phase 1, plus any `feedback`). 5 fetchers, 30s timeout per URL, trafilatura extraction, paragraph-aware chunking.
3. LLM distill → topic/title/content/source/tags entries.
4. Milvus ingest via `rag_pipeline.ingest_entries` (3-tier dedup).
5. Compile workflow summary; UPDATE → `'planning'`. Auto-chain in pipeline now triggers `/dag` then `/execute/all`.

Phase 2 is long-running (10–25 min on a cold corpus); pipelines must use a long stream timeout.

### 4.4 DAG generation — `/dag`

`POST /dag` body: `DagInput { job_id, model?, model_overrides? }`. Backed by `dag_generator.generate_dag`:

1. Idempotency guard: if `dag_nodes` for `job_id` already exist with count > 0, return 409 (audit-flagged hot path).
2. LLM produces a JSON list of tasks: `[{node_key, title, description, tool, domain, depends_on}, ...]`.
3. `_normalize_tasks`: clamps `tool` to `VALID_TOOLS` (LLM/CodeGen/SearXNG/Milvus), `domain` to `VALID_DOMAINS` (prompt/rag/eng/eng_design/llm/spec/code/qa — see §17.329 for the eng/eng_design split), defaults invalids with a warning event.
4. Kahn's cycle check (`validate_dag`); cycles → `dag_cycle_detected` event + 500.
5. Numeric T-key sort + `_MAX_NODES` truncation (drops keys with logged warning).
6. Determine leaf set; INSERT nodes with `is_output_node=TRUE` for leaves.
7. UPDATE jobs → `'executing'`.

### 4.5 Execution — `/execute` (single) and `/execute/all` (all, SSE)

`POST /execute` runs the next single ready node and returns synchronously (`ExecutionResult`). `POST /execute/all` (SSE) runs all pending nodes in dependency order. Both backed by `execution_agent`.

Per-node loop in `execute_next_node`:
- Resolve dependencies → fetch `output_text` of each upstream → pack as upstream block.
- RAG context injection: `rag_pipeline.query_rag` against the node's domain partition; inject as additional upstream.
- Tool dispatch: LLM (`model_router.generate`), CodeGen (`model_coder`), SearXNG (web search), Milvus (RAG fan-out).
- Verifier: `model_verifier` confirms output meets node criteria. On fail → retry (up to `max_retries`, default 3) → blocked (manual `/exec/retry`).
- Persist `optimized_prompt`, `output_text`, `confidence`, `model_used`. UPDATE → `'done'` or `'failed'`.

`_compile_output` runs at end:
- **Strategy 0 (preferred):** SELECT `output_text` from nodes where `is_output_node=TRUE`, JOIN by execution order.
- **Strategy 1 (fallback):** title heuristic — nodes with titles matching final/output/code patterns.
- **Strategy 2:** last successful `tool='CodeGen'` node.
- **Strategy 3:** concatenate all completed nodes.

Stored in `jobs.compiled_output`. Streamed back as the final SSE event.

### 4.6 Research — `/research`

Single endpoint with prefix-based dispatch. Body: `ResearchInput { topic, depth='medium', domain?, model_overrides? }`. SSE-streamed.

| Topic prefix | Mode | Behavior |
|---|---|---|
| `http(s)://...` | URL | Robots check → bounded fetch (`research_max_url_bytes`, default 5MB) → trafilatura → chunk → distill → ingest |
| `github:owner/repo` | GitHub | Fetch README + `docs/**/*.md` + module docstrings; `github_max_files` cap (default 50) |
| `openapi:<url>` | OpenAPI | Fetch + validate → one entry per endpoint; `openapi_max_endpoints` cap (default 200) |
| *(other)* | Topic | Decompose → SearXNG queries → trafilatura → chunk → distill → gap analysis → iterate |
| *(via `POST /research/pdf`)* | PDF | pypdf (fallback pdfplumber) → chunk → distill → ingest |

**Pause/Resume:** Gap analyzer may request clarification. Session transitions to `paused_awaiting_reply` with 1h TTL. User resumes via `POST /research/reply { session_id, reply }`. Reply injected as gap_query.

**Depth (topic mode):** shallow=1 iteration, medium=2, deep=4.

**Two-tier model strategy:** 4b for decompose/gap-analysis (fast), 7b for extract/summary (accurate). 235b explicitly avoided in research loops.

**SSE events:** `research_started`, `decomposition`, `iteration_started`, `search_complete`, `research_fetch`, `extraction_complete`, `ingestion_complete`, `contradictions_detected`, `gap_analysis`, `awaiting_reply`, `research_resumed`, `convergence`, `research_complete`. Plus `: keepalive` comment lines every ~2s.

### 4.7 Scheduled research

APScheduler (in-process, SQLAlchemyJobStore in Postgres) runs recurring `/research` jobs based on cron expressions.

`POST /schedule` body: `ScheduleCreate { topic, cron_expression, depth='medium', timezone='UTC', model_overrides? }`. Validates cron, INSERT row, register with APScheduler. Per-schedule timezone via IANA tz name.

- Schedules persist across restarts (rehydrated from `scheduled_jobs` on lifespan startup).
- `last_status` ∈ {`success`, `failed`, `running`, `timeout`}; `last_job_id` populated with the real `research_sessions.id` captured from the SSE stream.
- Job timeout: `scheduler_job_timeout` (default 3600s).
- Misfire grace: `scheduler_misfire_grace_time` (default 300s).
- Graceful shutdown: `scheduler_shutdown_timeout` (default 30s).
- Single-running-per-topic guard at `research_sessions.status='running'` UNIQUE partial index (migration 020).

### 4.8 Assist Mode (human-in-the-loop)

A sibling to autonomous execute. After `/dag` produces a plan, the operator opts into Assist Mode (interactive walkthrough). The two paths coexist; per-node handoff flips a single node back into the autonomous executor and returns control on the next.

**Lifecycle:**
- `assist_sessions` row per job (UNIQUE constraint — one active session per job)
- Session status: `active → paused → completed | abandoned | cancelled`
- Job status: `assisted_executing → assisted_running → completed`

**Per-step state machine:** `pending → presented → awaiting_input → received → committed`. Branches: `skipped`, `handed_off`, `escalated`. (The `applied` state was dropped in migration 024 as dead.)

**Mirror-to-dag_nodes invariant:** on commit, `assist_steps.evidence` is mirrored to `dag_nodes.output_text` and `dag_nodes.status='done'` in the same transaction. The existing `_compile_output`, `_fetch_upstream_outputs`, and downstream RAG-grounding paths see human output identical to autonomous output.

**Re-plan policies (per session):**
- `context_only` (default) — no regeneration; downstream upstream-last assembly absorbs divergence implicitly. Zero LLM cost.
- `selective` — divergence detector (qwen2.5:7b) flags major divergences; affected subgraph reset for re-walking.
- `full` — regenerate all pending nodes (discouraged).
- `disabled` — skip detection.

**Reaper interaction:** `cleanup.reap_stale_jobs` skips `assisted_*` statuses on the normal cadence. A separate idle sweep (`assist_idle_threshold_days`, default 7) cancels truly abandoned sessions.

**Friction log:** `POST /assist/{session_id}/friction` records per-step notes for post-mortem.

---

## 5. Database schema (PostgreSQL 16)

15 tables across the `scaffold_engine` database. `db/init.sql` is the post-migration-025 baseline; `db/migrations/002_*.sql` through `025_*.sql` are auto-applied by `app.migrations.run_migrations()` at lifespan startup. Tracking lives in `schema_migrations`. Opt out with `SCAFFOLD_RUN_MIGRATIONS_ON_STARTUP=false`.

### 5.1 Core tables (in init.sql)

#### `jobs`
| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | `uuid_generate_v4()` |
| `title` | TEXT NOT NULL | |
| `description` | TEXT | |
| `status` | TEXT NOT NULL | CHECK in 14 statuses (lifecycle + 3 `assisted_*`); default `'pending'` |
| `job_type` | TEXT NOT NULL DEFAULT `'legacy'` | CHECK in `('legacy','design_circuit')`; added migration 043 (§17.151). Partial index `WHERE job_type <> 'legacy'`. `design_circuit` rows are owned by the §17.140-§17.156 engineering-design pipeline (specs / topology_select / device_sizings / digital_sizings / sim_runs join via `jobs.id`) |
| `input_text` | TEXT | original idea text |
| `refined_brief` | JSONB | from idea_refinement |
| `compiled_output` | TEXT | from execution_agent._compile_output |
| `error_summary` | TEXT | populated on failure paths |
| `metadata` | JSONB DEFAULT `{}` | (Pydantic field is `meta` — alias drift, see Known Issues) |
| `created_at`, `updated_at`, `completed_at` | TIMESTAMPTZ | `updated_at` auto-trigger |

`status` lifecycle: `pending → refining → awaiting_confirmation → researching → planning → executing → running → completed | failed | cancelled | blocked` plus `assisted_executing | assisted_running | assisted_paused`.

`job_type` branch: `legacy` (default) drives the original `/ideate → /confirm → /execute/all` chain. `design_circuit` (§17.151) drives the `/design → /specs/{id}/confirm → /design/{id}/advance?stage={topology,size,report}` chain — same `status` set, different routers and per-stage audit tables (see §11.11).

#### `dag_nodes`
| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `job_id` | UUID NOT NULL | FK → `jobs.id` ON DELETE CASCADE |
| `node_key` | TEXT NOT NULL | e.g. `"T1"`, `"T2"` |
| `title`, `description` | TEXT | |
| `node_type` | TEXT | CHECK in `('task','decision','parallel_group','checkpoint')` |
| `status` | TEXT | CHECK in `('pending','running','done','failed','skipped')` |
| `depends_on` | TEXT[] | array of upstream `node_key`s |
| `assigned_model` | TEXT | model tag actually used |
| `prompt_template`, `optimized_prompt` | TEXT | |
| `output_text` | TEXT | the result; mirrored from assist_steps.evidence in Assist Mode |
| `output_artifact_id` | UUID | optional FK to artifacts |
| `confidence` | FLOAT | verifier confidence; nullable |
| `domain` | VARCHAR(10) | one of `VALID_DOMAINS` (prompt/rag/eng/eng_design/llm/spec/code/qa); see §17.329 for the eng vs eng_design semantic split |
| `tool` | VARCHAR(50) DEFAULT `'LLM'` | one of `VALID_TOOLS` (LLM/CodeGen/SearXNG/Milvus) |
| `retry_count`, `max_retries` | INT | retry budget |
| `parallel_group`, `execution_order` | INT | scheduling hints |
| `is_output_node` | BOOLEAN | added migration 017; flagged for leaves at INSERT |
| `created_at`, `updated_at`, `started_at`, `completed_at` | TIMESTAMPTZ | |
| | | UNIQUE `(job_id, node_key)` |

#### `execution_logs`
Per-node structured logs.
| Column | Type |
|---|---|
| `id` UUID PK | |
| `job_id` UUID FK | ON DELETE CASCADE |
| `node_id` UUID FK | ON DELETE SET NULL |
| `log_level` | CHECK in `(debug, info, warning, error, critical)` |
| `message` TEXT NOT NULL | |
| `details` JSONB DEFAULT `{}` | |
| `created_at` TIMESTAMPTZ | |

#### `error_logs`
Errors with recovery state.
| Column | Type |
|---|---|
| `id` UUID PK | |
| `job_id` UUID FK | ON DELETE CASCADE |
| `node_id` UUID FK | ON DELETE SET NULL |
| `error_type` | CHECK in `(transient, timeout, validation, unrecoverable)` (post-migration 025; `model_failure` and `structural` were dropped) |
| `error_message` TEXT | |
| `stack_trace` TEXT | |
| `model_used` TEXT | |
| `retry_count` INT | |
| `recovery_action` | CHECK in `(retry, model_swap, dag_replan, manual, none, NULL)` |
| `recovery_model` TEXT | |
| `resolved` BOOLEAN | |
| `resolution` TEXT | |
| `created_at`, `resolved_at` TIMESTAMPTZ | |

#### `artifacts`
Generated outputs (DAG/prompts/code/reports).
| Column | Type |
|---|---|
| `id` UUID PK | |
| `job_id` UUID FK | ON DELETE CASCADE |
| `node_id` UUID FK | ON DELETE SET NULL |
| `artifact_type` | CHECK in `(dag, prompt, toon_file, plan, code, report, mermaid, other)` |
| `title` TEXT | |
| `content` TEXT | |
| `file_path` TEXT | |
| `mime_type` TEXT DEFAULT `'text/plain'` | |
| `size_bytes` INT | |
| `metadata` JSONB DEFAULT `{}` | (Pydantic field is `meta` — alias drift) |
| `created_at` TIMESTAMPTZ | |

#### `performance_logs`
Per-call latency / token metrics.
| Column | Type |
|---|---|
| `id` UUID PK | |
| `job_id`, `node_id` UUID FK | ON DELETE SET NULL |
| `model` TEXT | model tag |
| `endpoint` TEXT | e.g. `/api/generate` |
| `request_type` | CHECK in `(generate, embed, rerank, classify)` |
| `ttft_ms` INT | time-to-first-token (nullable) |
| `total_duration_ms` INT NOT NULL | |
| `tokens_prompt`, `tokens_completion` INT | nullable |
| `tokens_per_sec` FLOAT | |
| `success` BOOLEAN | |
| `error_message` TEXT | |
| `created_at` TIMESTAMPTZ | |

#### `benchmark_results`
| Column | Type |
|---|---|
| `id` UUID PK | |
| `model`, `domain`, `benchmark_name` TEXT | |
| `score` FLOAT NOT NULL | |
| `max_score` FLOAT DEFAULT 1.0 | |
| `sample_count` INT | |
| `details` JSONB | |
| `run_at` TIMESTAMPTZ | |

#### `blockers`
Beta blocker tracking.
| Column | Type |
|---|---|
| `id` UUID PK | |
| `title`, `description` TEXT | |
| `severity` | CHECK in `(critical, high, medium, low)` |
| `category` | CHECK in `(infrastructure, model, pipeline, ui, data, performance, other, NULL)` |
| `status` | CHECK in `(open, in_progress, resolved, wont_fix)` |
| `resolution` TEXT | |
| `created_at`, `updated_at`, `resolved_at` TIMESTAMPTZ | |

#### `schema_migrations`
Tracks applied migrations.
| Column | Type |
|---|---|
| `filename` TEXT PK | |
| `applied_at` TIMESTAMPTZ DEFAULT NOW() | |

### 5.2 Tables added by migrations

#### `dedup_log` (migration 009)
Near-duplicate rejection / version-chain audit log. Logs `action_taken ∈ ('rejected','versioned')`, the cosine similarity, the candidate source URL, the matched existing entry_id. **Note:** versioned entries currently skip this audit (review HIGH #6); fix scoped for a future round.

#### `research_sessions` (migration 010, extended by 012/013/014/015/020)
| Column | Type | Notes |
|---|---|---|
| `id` UUID PK | | |
| `topic` TEXT NOT NULL | | |
| `domain` VARCHAR(50) NOT NULL DEFAULT `'eng'` | | |
| `depth` VARCHAR(32) NOT NULL DEFAULT `'medium'` | widened from VARCHAR(10) by migration 014 |
| `status` VARCHAR(32) | (widened from VARCHAR(20) by migration 015) — values: `pending`, `running`, `paused_awaiting_reply`, `completed`, `failed`, `cancelled` |
| `state_snapshot` JSONB | iteration boundary checkpoint for pause/resume |
| `pause_question`, `pause_answer` TEXT | gap-analyzer clarification flow |
| `pause_expires_at` TIMESTAMPTZ | 1h TTL |
| `error_message` TEXT | |
| `created_at`, `updated_at`, `completed_at` TIMESTAMPTZ | |

UNIQUE partial index `idx_research_sessions_running_topic` on `(topic) WHERE status='running'` — single-running-per-topic guard (migration 020).

#### `scheduled_jobs` (migration 011, fixed by 016/018)
| Column | Type | Notes |
|---|---|---|
| `id` SERIAL PK | | |
| `topic` TEXT NOT NULL | | |
| `cron_expression` TEXT NOT NULL | e.g. `"0 9 * * 1"` |
| `depth` VARCHAR(32) DEFAULT `'medium'` | |
| `timezone` TEXT DEFAULT `'UTC'` | IANA name (migration 016) |
| `enabled` BOOLEAN DEFAULT TRUE | |
| `last_run_at` TIMESTAMPTZ | |
| `last_status` TEXT | CHECK `IS NULL OR IN (success, failed, running, timeout)` (migration 018 corrected the IN-list NULL bug from 011) |
| `last_job_id` UUID | populated with the real `research_sessions.id` |
| `next_run_at` TIMESTAMPTZ | computed by APScheduler |
| `run_count`, `failure_count` INT | |
| `model_overrides` JSONB | |
| `created_at` TIMESTAMPTZ | |

#### `apscheduler_jobs` (migration 011, defensively typed)
APScheduler internal jobstore.
- `id` VARCHAR(191) PK
- `next_run_time` DOUBLE PRECISION — **DO NOT alter this type**, APScheduler's interpretation depends on the float layout
- `job_state` BYTEA — pickled job

#### `prompt_revisions` (migration 022)
Per-node prompt edit audit trail.
| Column | Type | Notes |
|---|---|---|
| `id` SERIAL PK | | |
| `job_id` UUID FK | ON DELETE CASCADE |
| `node_key` TEXT NOT NULL | |
| `revision_number` INT NOT NULL | per `(job_id, node_key)` sequence |
| `prompt_text` TEXT NOT NULL | |
| `edited_at` TIMESTAMPTZ NOT NULL | |
| `edited_by` TEXT | nullable — user/system |
| `source` TEXT DEFAULT `'manual'` | values: manual, optimizer, initial, system |

#### `assist_sessions` (migration 023)
Assist Mode top-level session.
| Column | Type | Notes |
|---|---|---|
| `id` UUID PK | | |
| `job_id` UUID FK NOT NULL | ON DELETE CASCADE; **UNIQUE** (one active session per job) |
| `status` TEXT NOT NULL | CHECK in `(active, paused, completed, abandoned, cancelled)` |
| `replan_policy` TEXT | CHECK in `(context_only, selective, full, disabled)` |
| `handoff_policy` TEXT | CHECK in `(manual, all)` |
| `last_activity_at` TIMESTAMPTZ | drives the abandoned-session reaper |
| `created_at`, `updated_at` TIMESTAMPTZ | |

#### `assist_steps` (migration 023, refined by 024)
Per-(session, node) state.
| Column | Type | Notes |
|---|---|---|
| `id` UUID PK | | |
| `session_id` UUID FK | ON DELETE CASCADE |
| `job_id`, `node_key` | NOT NULL | denormalized for query speed |
| `status` TEXT | CHECK in `(pending, presented, awaiting_input, received, committed, skipped, handed_off, escalated)` (migration 024 dropped dead `applied`) |
| `evidence` TEXT | human-supplied output; mirrored to `dag_nodes.output_text` on commit |
| `friction_notes` TEXT[] DEFAULT `{}` | |
| `created_at`, `updated_at` TIMESTAMPTZ | |
| | | UNIQUE `(session_id, node_key)` |

### 5.3 Migration history

22 migrations on top of init.sql baseline (002–025). Forward-only; no down migrations by design.

| # | Filename | Purpose |
|---|---|---|
| 002 | `add_confidence.sql` | `dag_nodes.confidence` |
| 003 | `add_compiled_output.sql` | `jobs.compiled_output` |
| 004 | `add_job_statuses.sql` | broaden `jobs.status` CHECK |
| 005 | `add_domain_tool.sql` | `dag_nodes.domain` and `tool` |
| 006 | `add_indexes.sql` | dag_nodes.domain + performance_logs.job_id (also re-declared in init.sql) |
| 007 | `ideation_workflow.sql` | `jobs.research_data` + `workflow_summary` |
| 008 | `add_ideation_statuses.sql` | further `jobs.status` broadening |
| 009 | `dedup_log.sql` | RAG dedup audit log |
| 010 | `research_sessions.sql` | session table for autonomous research |
| 011 | `scheduled_jobs.sql` | + `apscheduler_jobs` |
| 012 | `research_sessions_state.sql` | snapshot column |
| 013 | `research_pause.sql` | pause/resume columns (introduced VARCHAR(20) overflow bug) |
| 014 | `research_sessions_depth_varchar.sql` | widen `depth` to VARCHAR(32) |
| 015 | `research_sessions_status_varchar.sql` | widen `status` to VARCHAR(32) — fixes 013 bug |
| 016 | `scheduler_timezone.sql` | per-schedule IANA timezone |
| 017 | `dag_nodes_is_output_node.sql` | explicit leaf marker |
| 018 | `scheduled_jobs_last_status_check.sql` | rewrite CHECK as `IS NULL OR IN (...)` |
| 019 | `dag_nodes_unique_job_node_key.sql` | defensive UNIQUE recreate |
| 020 | `research_sessions_single_running.sql` | UNIQUE partial index on `topic WHERE status='running'` |
| 021 | `updated_at_triggers.sql` | additional updated_at trigger registrations |
| 022 | `prompt_revisions.sql` | prompt revision audit table |
| 023 | `assist_mode.sql` | + `assist_sessions` and `assist_steps` |
| 024 | `drop_assist_steps_applied_status.sql` | drops dead `'applied'` from CHECK |
| 025 | `drop_dead_error_types.sql` | drops dead `'model_failure'` and `'structural'` from `error_logs.error_type` |

### 5.4 Milvus collection: `toon_v2`

Vector store for the RAG knowledge base. All ingest goes through `rag_pipeline.py`.

- **16 TOON-aligned fields:** `entry_id` (PK), `title`, `content`, `source_url`, `source_type`, `tags` (`string`-joined), `domain` (partition key), `confidence_score`, `version`, `supersedes_id`, `expires_at`, `content_hash`, `created_at`, `updated_at`, `model_id`, `domain_tags`
- **`dense_vector`:** 512d FLOAT_VECTOR
- **Index:** HNSW_SQ8 COSINE (M=16, efConstruction=256, SQ8 + BF16 refine)
- **Partition key isolation:** `domain` (64 partitions, one per domain value plus headroom)
- **Scalar indexes:** `content_hash`, `domain_tags`, `source_type`, `confidence_score`, `created_at`, `version`
- **TTL by source_type:** `real_time=7d`, `news=30d`, `community=90d`, `tech_docs=180d`, `curated=1y`, `official_docs=1y`, `ai_generated=180d` (`config.TTL_POLICY`)

`scripts/create_toon_v2.py` creates the collection with this schema. `scripts/reindex.py` re-embeds the corpus when `MODEL_EMBEDDER_PIPELINE` changes.

---

## 6. Pydantic schemas

Source: `app/schemas.py` (~625 lines). Vendored byte-equal at `sdk/scaffold_client/schemas.py` (parity enforced by `tests/test_sdk_schema_parity.py`).

### 6.1 Shared types

```python
JobStatus = Literal[
    "pending", "refining", "awaiting_confirmation", "researching",
    "planning", "executing", "running",
    "completed", "failed", "cancelled", "blocked",
    "assisted_executing", "assisted_running", "assisted_paused",
]
JOB_STATUSES = get_args(JobStatus)  # iterable runtime mirror

ASSIST_PROTECTED_STATUSES = ("assisted_executing", "assisted_running", "assisted_paused")
RESEARCH_SESSION_STATUSES = ("pending", "running", "paused_awaiting_reply",
                             "completed", "failed", "cancelled")

NodeStatus = Literal["pending", "running", "done", "failed", "skipped"]
NodeType   = Literal["task", "decision", "parallel_group", "checkpoint"]
LogLevel   = Literal["debug", "info", "warning", "error", "critical"]
ErrorType  = Literal["transient", "timeout", "validation", "unrecoverable"]
RecoveryAction = Literal["retry", "model_swap", "dag_replan", "manual", "none"]
ArtifactType = Literal["dag", "prompt", "toon_file", "plan", "code", "report", "mermaid", "other"]
RequestType  = Literal["generate", "embed", "rerank", "classify"]
Severity = Literal["critical", "high", "medium", "low"]
ResearchDepth = Literal["shallow", "medium", "deep"]
```

### 6.2 Endpoint input models

| Model | Endpoint | Key fields |
|---|---|---|
| `IdeaInput` | `POST /ideas`, `POST /ideate` | `idea`, `domain?`, `model?`, `model_overrides?` |
| `ConfirmInput` | `POST /ideate/confirm` | `job_id`, `feedback?`, `push_to_github=False`, `model_overrides?` |
| `DagInput` | `POST /dag` | `job_id`, `model?`, `model_overrides?` |
| `RagInput` | `POST /rag` | `query`, `top_k=10` (1..100), `confidence_threshold=0.8`, `skip_rerank=False`, `include_history=False`, `domain?` |
| `GtInput` | `POST /gt` | `topic`, `queries?`, `push_to_github=False`, `target_file?`, `model?`, `github_owner?`, `github_repo?` |
| `GtSearchInput` | `POST /gt/search` | `query`, `domain?`, `top_k=10`, `include_history=False` |
| `PromptOptimizeInput` | `POST /optimize` | `prompt`, `model_optimizer?`, `model_verifier?`, `skip_verify=False`, `model_overrides?` |
| `ExecuteNextInput` | `POST /execute`, `POST /execute/all` | `job_id`, `skip_optimize=False`, `skip_verify=False`, `model_overrides?` |
| `SkipNodeInput` | `POST /skip` | `job_id` (UUID-validated), `node_key` (non-empty) |
| `ExecRetryInput` | `POST /exec/retry` | `job_id`, `node_key` |
| `PromptUpdateInput` | `POST /prompts/{job_id}/{node_key}` | `prompt` |
| `ResearchInput` | `POST /research` | `topic`, `depth='medium'`, `domain?`, `model_overrides?` |
| `ResearchReplyInput` | `POST /research/reply` | `session_id`, `reply`, `model_overrides?` |
| `ScheduleCreate` | `POST /schedule` | `topic`, `cron_expression`, `depth='medium'`, `timezone='UTC'`, `model_overrides?` |
| `JobRenameInput` | `PATCH /jobs/{id}` | `title` (1..200 chars, validated) |
| `ResearchSessionRenameInput` | `PATCH /research/sessions/{id}` | `topic` |

### 6.3 Response models

| Model | Endpoint | Key fields |
|---|---|---|
| `PromptOptimizeResult` | `POST /optimize` | `original_prompt`, `optimized_prompt`, `pre_cleaned`, `token_count_before/after`, `token_reduction_pct`, `clarity_score`, `intent_preserved`, `issues_found`, `issues_resolved`, `model_used`, `verifier_used` |
| `ExecutionResult` | `POST /execute`, `POST /skip` | `status`, `job_id?`, `node_key?`, `title?`, `output?`, `verified?`, `verification_reason?`, `confidence?`, `model_used?`, `prompt_used?`, `awaiting_approval?`, `message?`, `tool?`, `error?` |
| `JobSummary` | (composed in JobListResponse) | `id`, `title`, `status`, `node_count`, `created_at`, `updated_at` |
| `JobListResponse` | `GET /jobs` | `jobs[]`, `total`, `limit`, `offset` |
| `DeleteResponse` | various DELETE endpoints | `deleted`, `id` |
| `ScheduleResponse` | `POST/GET /schedule` | full schedule row |
| `PromptRevision` | inside PromptHistoryResponse | `revision_number`, `prompt_text`, `edited_at`, `edited_by?`, `source` |
| `PromptHistoryResponse` | `GET /prompts/{id}/{key}/history` | `job_id`, `node_key`, `current_prompt`, `revision_count`, `revisions[]` |
| `ResearchSessionSummary` | composed in ResearchSessionListResponse | full session row |
| `ResearchSessionListResponse` | `GET /research/sessions` | `sessions[]`, `total`, `limit`, `offset` |

---

## 7. RAG pipeline

Source: `app/modules/rag_pipeline.py` (~647 lines).

### 7.1 Query path

1. **Embed query** — `qwen3-embedding:8b` via `model_router`, MRL-truncated to 512d, instruction-prefixed (`"Represent this query for retrieval: "`), Redis-cached at `embedv3:{model}:d{dim}:{hash}` (in-memory LRU + Redis two-tier).
2. **Parallel search** — vector (COSINE + HNSW_SQ8) + keyword (TextMatch on `content`), via `asyncio.gather`. PyMilvus calls wrapped in `run_in_executor`.
3. **RRF merge** — Reciprocal Rank Fusion combines results by `_key()` (entry_id; falls back to `content[:200]` when missing — flagged as collision risk).
4. **CrossEncoder rerank** — `Qwen3-Reranker-0.6B-seq-cls`, in thread executor (`run_in_executor`). Pre-warmed at lifespan startup.
5. **Return top-K** with scores + source URLs + tags.

Domain fan-out: when `domain=None`, iterates `sorted(VALID_DOMAINS)` and fans out one search per partition. Merges by best score per `entry_id`. Single-domain queries use partition-key isolation (one Milvus call).

### 7.2 Ingest path (3-tier)

For each candidate entry:
1. Compute `content_hash` (SHA-256 of normalized content).
2. Exact-hash filter: SELECT entries with same hash → reject as identity duplicate (logs `dedup_log`).
3. Embed candidate, semantic search top-1 in same domain partition.
4. Branch on cosine similarity:
   - **> `semantic_dedup_threshold` (0.95):** reject as near-duplicate (logs `dedup_log` with `action_taken='rejected'`).
   - **0.90–0.95 (`version_chain_threshold`..`semantic_dedup_threshold`):** insert as new entry with `supersedes_id = matched.entry_id`, `version = matched.version + 1`. Older entry remains queryable via `include_history=true`.
   - **< 0.90:** insert as new entry, `version=1`.

**Note:** versioned entries currently skip the `dedup_log` audit row. Review HIGH #6.

### 7.3 Retrieval defaults

`include_history=False` filters superseded entries by querying only entries where no other entry has `supersedes_id = this.entry_id`. `include_history=True` returns the full chain (useful for diff/audit views).

### 7.4 Embedder portability

Embedding dim is locked at 512 by config (`embedding_dim: int = Field(default=512, ge=512, le=512)`). Swapping `MODEL_EMBEDDER_PIPELINE` (or its provider) requires re-embedding the whole corpus — `make reindex` runs `scripts/reindex.py` which fans out across partitions, paginates by `entry_id` cursor, preserves every non-vector field, and upserts.

---

## 8. Research agent

Source: `app/modules/research_agent.py` (~2,189 lines), `app/modules/research_extractors.py`, `app/modules/research_state.py`.

### 8.1 Modes

`/research` is mode-dispatched on the topic prefix. See workflow §4.6 for the table.

### 8.2 Topic mode loop (most complex)

```
decompose(topic) → facets + queries + estimated cost
  for iteration in range(depth):           # shallow=1, medium=2, deep=4
    for query in queries:
      results = searxng_search(query)
      pages   = trafilatura.extract(parallel, max=research_max_urls_per_iteration)
      entries = distill(pages)              # 7b LLM, structured JSON
      ingest(entries)                       # rag_pipeline 3-tier
    contradictions = detect_contradictions(state.all_entries)
    summary = summarize(state.all_entries[:60])
    gap = analyze(state, summary)
    if gap.needs_clarification:
      pause(gap.question)                   # → research_sessions.status='paused_awaiting_reply'
      return                                # caller resumes via /research/reply
    if gap.converged: break
    queries = gap.next_queries
yield convergence
```

### 8.3 State + checkpoints

`research_sessions.state_snapshot` (JSONB) holds `{entries_projection, queries, iteration, last_search_at, ...}`. Written at each iteration boundary. Resume rehydrates from snapshot (`_rehydrate_state`).

### 8.4 Pause/resume

`pause_expires_at = NOW() + 1h`. `POST /research/reply` checks the TTL, re-validates `session.status`, injects `reply` as a gap_query, and resumes the loop in place. Generators: `run_research`, `resume_research`, both wrapped in `_run_with_session_lifecycle` which catches `CancelledError` → finalizes session `status='cancelled'`, `error_message='client_disconnect'`.

### 8.5 Two-tier model strategy

| Step | Model | Why |
|---|---|---|
| Decompose | `model_router` (qwen3:4b) | Fast, cheap |
| Distill (per-page → entries) | `model_general` or `qwen2.5:7b` | Higher quality extraction |
| Summary | 7b | Better synthesis |
| Gap analysis | 4b | Fast classification |
| Contradiction detection | 4b | Fast classification |

The 235b model is **explicitly avoided** in research loops — it's slow, expensive on CPU, and overkill for the per-step decisions.

### 8.6 SearXNG throttle

`research_searxng_delay` (default 1.5s) is enforced between calls in research_agent. **`gt_extractor` does NOT throttle** — review LOW #5 (gt_extractor can starve research-side SearXNG when both are concurrent).

---

## 9. Assist Mode

Source: `app/modules/assist_agent.py`, `app/modules/assist_replan.py`, `app/modules/prompt_assembly.py`. Endpoints in `app/routers/assist.py`.

### 9.1 Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/assist/start` | Promote a job (status=`planning`) into Assist Mode (`assisted_executing`). Creates session row. |
| `GET` | `/assist/{session_id}` | Session + per-step status roll-up |
| `GET` | `/assist/{session_id}/next` | Claim the next pending step + render upstream-last context block |
| `POST` | `/assist/{session_id}/submit` | Record human evidence (`action='submit'` or `'skip'`) |
| `POST` | `/assist/{session_id}/handoff` | Hand a node back to the autonomous executor (SSE) |
| `POST` | `/assist/{session_id}/pause` | Pause an active session |
| `POST` | `/assist/{session_id}/resume` | Resume a paused session |
| `DELETE` | `/assist/{session_id}` | Abandon (cancel job + close session) |
| `POST` | `/assist/{session_id}/friction` | Append a friction note for post-mortem |
| `GET` | `/assist/{session_id}/friction` | List friction notes |

### 9.2 Mirror invariant (load-bearing)

On step commit:
```
BEGIN;
  UPDATE assist_steps SET status='committed', evidence=:evidence WHERE ...;
  UPDATE dag_nodes SET status='done', output_text=:evidence WHERE ...;
  -- optionally bump assist_sessions.last_activity_at
COMMIT;
```

This single-transaction mirror lets `_compile_output`, `_fetch_upstream_outputs`, and downstream RAG-grounding paths consume human output indistinguishably from autonomous output. **No changes to those paths required.**

### 9.3 Re-plan policies

| Policy | Behavior | LLM cost |
|---|---|---|
| `context_only` (default) | No regeneration; downstream upstream-last assembly absorbs divergence implicitly | Zero |
| `selective` | Divergence detector (`qwen2.5:7b`) flags major divergences; affected subgraph reset | Per-divergence |
| `full` | Regenerate all pending nodes | High; discouraged |
| `disabled` | Skip detection entirely | Zero |

### 9.4 Reaper interaction

`cleanup.reap_stale_jobs` skips `assisted_*` job statuses on the normal cadence (15-min loop). A separate idle sweep (`assist_idle_threshold_days`, default 7 days) cancels truly abandoned sessions by checking `assist_sessions.last_activity_at`.

---

## 10. TOON data format

**TOON** = Token-Oriented Object Notation. Used at the LLM ↔ structured-data boundary in scaffold-engine. Achieves ~60% fewer tokens than JSON and +4.2% retrieval accuracy in RAG pipelines.

### 10.1 Identity + workflow

- Extension: `.toon`
- Media type: `text/toon`
- Indentation: spaces only (no tabs — byte/visual consistency)
- Storage workflow: JSON (storage) ↔ TOON (LLM boundary) ↔ JSON (processing)

### 10.2 Core principles

1. **Declare once, reference never.** Field names appear only in the header.
2. **Count everything.** `[N]` length markers prevent truncation and hallucinated rows.
3. **Flatten aggressively.** Key-fold nested paths into single-line declarations.
4. **Short keys.** `src` not `source_url`. Drop redundant prefixes inside scoped objects.
5. **Delimiter-match data.** Commas default; `<|>` for data containing commas; tabs for large uniform tables.
6. **Metadata first.** A `meta:` block before arrays gives the LLM context before it processes rows.
7. **Strict mode on.** Row count == `[N]`, value count == field count, consistent delimiters, no blank lines inside blocks.

### 10.3 Syntax

**Scalar:**
```
key: value
```

**Tabular array (highest efficiency):**
```
header[N]{field1,field2,field3}:
val1,val2,val3
val1,val2,val3
```

**Mixed/nested array (heterogeneous shape):**
```
items[N]:
- scalar_value
- key1: val
  key2: val
```

**Key folding:**
```
data.meta.items[N]{id,name,status}:
1,widget,active
2,gadget,retired
```

**Pipe-delimited override (data contains commas):**
```
contacts[2]<|>{name,address,phone}:
Alice Smith|123 Main St, Apt 4|555-0101
Bob Jones|789 Oak Ave, Suite 12|555-0202
```

### 10.4 Strict-mode contract

| Rule | Purpose |
|---|---|
| Row count == `[N]` | Detects truncation |
| Values per row == field count | Detects corruption |
| Delimiter consistent header↔rows | Prevents parse drift |
| Indent = exact multiples of indent size | Enforces structure |
| No blank lines inside blocks | Eliminates ambiguity |

Violations are structural errors, not warnings.

### 10.5 Validator implementation

A reference validator implementation lives at `docs/toon/toon_validator_reference/` (Python: `cli.py`, `core.py`, `fix_agent.py`, `__main__.py`, `llm_client.py`, `gates/`). Imported when needed; not on the orchestrator's hot path.

### 10.6 Format efficiency ranking

```
TOON (tabular) > TSV/CSV > Markdown > YAML > minified JSON > formatted JSON > XML
```

### 10.7 Minimal complete example

```
meta:
  v: 1
  src: inventory_db
  ts: 2026-03-11

products[4]{id,name,cat,price,stock}:
1,Widget A,hardware,29.99,142
2,Widget B,hardware,49.99,87
3,Service X,software,9.99,null
4,Service Y,software,19.99,null

suppliers[2]<|>{id,name,addr,contact}:
1|Acme Parts|456 Industrial Blvd, Bay 3|acme@example.com
2|Global Supply|789 Trade Rd, Unit 12|global@example.com
```

---

## 11. Module reference

> _This section will be populated by an automated code-surface enumeration (in progress). It contains every public function and class in `app/`, `sdk/`, `cli/`, `pipelines/`, `scripts/` with one-line purpose. Skip to §12 if you only need configuration._

### 11.1 `app/` — orchestrator

#### `app/main.py` — 1168 lines
FastAPI orchestrator: all 23-step workflow endpoints, health checks, lifecycle management, middleware stack.

Functions:
- `async def health() -> dict` — Concurrent dependency health check (PostgreSQL, Milvus, Redis, Ollama)
- `async def cleanup_stale_jobs(db) -> dict` — Find and resolve stale/orphaned jobs
- `async def submit_idea(body, db) -> dict` — Submit new idea → trigger refinement
- `async def ideate_endpoint(body, db) -> dict` — Phase 1: analyze idea, assess feasibility, halt for confirmation
- `async def ideate_confirm_endpoint(body, db) -> dict` — Phase 2: user confirms → research → ingest → compile
- `async def get_dag(job_id, db) -> dict` — Retrieve DAG nodes + job status
- `async def generate_dag_endpoint(body, db) -> dict` — Generate DAG from refined idea brief
- `async def query_rag(body) -> dict` — Query RAG pipeline (embed → search → rerank → return)
- `async def list_dedup_log(limit, offset) -> dict` — List logged near-duplicate rejections
- `async def extract_gt(body) -> dict` — Extract ground truths via SearXNG + LLM distillation
- `async def gt_list_endpoint(page, per_page, include_history, domain) -> dict` — Paginated TOON entries
- `async def gt_search_endpoint(body) -> dict` — Semantic search TOON entries
- `async def gt_detail_endpoint(entry_id) -> dict` — Full content of a specific TOON entry
- `async def gt_stats_endpoint() -> dict` — Collection summary
- `async def prompts_list(job_id, db) -> dict` — All prompts for a job's DAG nodes
- `async def prompts_detail(job_id, node_key, db) -> dict` — Full prompt for a specific node
- `async def prompts_history(job_id, node_key, db) -> dict` — Audit trail of prompt edits
- `async def prompts_update(job_id, node_key, body, db) -> dict` — Update optimized prompt for pending/failed node
- `async def exec_status(job_id, db) -> dict` — Execution state for a job
- `async def exec_retry(body, db) -> dict` — Reset a failed node to pending for retry
- `async def optimize_endpoint(body) -> PromptOptimizeResult` — Optimize prompt (strip, reduce, verify, score)
- `async def execute_next(body) -> ExecutionResult` — Execute the next pending DAG node
- `async def execute_all_endpoint(body) -> StreamingResponse` — Execute all DAG nodes in sequence (SSE)
- `async def research_endpoint(body, request) -> StreamingResponse` — Autonomous research (SSE)
- `async def research_reply_endpoint(body, request) -> StreamingResponse` — Resume paused research session
- `async def research_pdf_endpoint(request, file, extractor, domain) -> StreamingResponse` — PDF ingestion (SSE)
- `async def research_pdf_upload_page(request)` — HTML upload page for PDF ingestion
- `async def research_history() -> dict` — List recent research sessions
- `async def research_history_detail(session_id) -> dict` — Get a single research session by ID
- `async def skip_node_endpoint(body, db) -> ExecutionResult` — Skip a specific DAG node
- `async def create_schedule(body, db) -> ScheduleResponse` — Create a recurring research schedule
- `async def list_schedules(limit, offset, db) -> dict` — List scheduled jobs (paginated)
- `async def delete_schedule(schedule_id, db) -> dict` — Delete a scheduled job
- `async def list_jobs(status, q, limit, offset, db) -> JobListResponse` — Paginated job list with filtering
- `async def delete_job(job_id, db) -> DeleteResponse` — Hard-delete a job (cascades)
- `async def rename_job(job_id, body, db) -> JobSummary` — Rename a job
- `async def list_research_sessions(status, q, limit, offset, db) -> ResearchSessionListResponse`
- `async def delete_research_session(session_id, db) -> DeleteResponse`
- `async def rename_research_session(session_id, body, db) -> ResearchSessionSummary`

Lifespan:
- `async def lifespan(app)` — Startup: verify Ollama/Milvus/Postgres, run migrations, pre-warm reranker, init scheduler. Shutdown: cleanup, close clients, disconnect.

#### `app/config.py` — 288 lines
Environment-driven settings + DAG-validation enums.

Constants:
- `VALID_TASK_TYPES = frozenset({"research", "decision", "action", "validation", "output"})`
- `VALID_STRATEGIES = frozenset({"sequential", "parallel", "hybrid", "conditional"})`
- `VALID_TOOLS = frozenset({"LLM", "CodeGen", "SearXNG", "Milvus"})`
- `VALID_DOMAINS = frozenset({"prompt", "rag", "eng", "eng_design", "llm", "spec", "code", "qa"})` (§17.329 — `eng_design` for circuit/EDA content; `eng` keeps its historical software-engineering meaning)
- `ROLE_FIELDS = frozenset(...)` — `get_model()` allowlist
- `TTL_POLICY: dict[str, int]` — source_type → seconds
- `DEFAULT_TTL_SECONDS = 180 * 86400`

Classes:
- `class Settings(BaseSettings)` — All config from env. Property: `sync_database_url` (APScheduler DSN).

Functions:
- `def get_model(role, overrides=None) -> str` — Override > env var > default; allowlist-protected.

#### `app/database.py` — 29 lines
Async SQLAlchemy engine + session factory.

Module-level: `engine`, `async_session`. Function: `async def get_db() -> AsyncGenerator[AsyncSession]`.

#### `app/auth.py` — 50 lines
`X-API-Key` middleware.

Constants: `_AUTH_EXEMPT_PATHS = frozenset({"/health"})`. Functions: `async def require_api_key(request, key)` — validates header; returns key or raises 401.

#### `app/schemas.py` — 625 lines
Pydantic schemas for all 8 tables + endpoint I/O models. See §6 for full reference. Vendored byte-equal at `sdk/scaffold_client/schemas.py`.

#### `app/logging_config.py` — 136 lines
structlog as the unified formatter for stdlib logging.

Functions:
- `def setup_logging(json_logs, log_level, log_file)` — Configure structlog.
- `def configure_logging_once(...)` — Idempotent variant for tests.
- `def drop_color_message_key(_, __, event_dict)` — Uvicorn deduplication filter.
- `def _resolve_level(log_level) -> int` — Validate level with INFO fallback.

#### `app/model_router.py` — 344 lines
Ollama API routing with retry cascade.

Constants: `CLOUD_MODELS = frozenset(...)`. Re-exports `ModelResponse` from providers.base.

Functions:
- `async def _call_ollama(endpoint, payload, model, timeout) -> ModelResponse`
- `def _get_client() -> httpx.AsyncClient` — Shared persistent Ollama client (audit-flagged: bypasses utils/http_clients).
- `async def close_client() -> None` — No-op (backward compat).
- `def _is_cloud(model) -> bool` — Detect cloud models.
- `def _timeout_for(model) -> int` — Cloud vs local timeout.
- `def _smart_fallback(model, default_fallback) -> str` — Map non-existent models to fallbacks.
- `async def generate(...)`, `async def chat(...)`, `async def embed(...)`, `async def classify(...)` — Public role-keyed dispatch (Sprint E).

#### `app/scheduler.py` — 245 lines
APScheduler integration; `/research` recurrence.

Functions:
- `def get_scheduler() -> AsyncIOScheduler | None` — Running scheduler or None.
- `async def init_scheduler() -> AsyncIOScheduler | None` — Create + rehydrate from DB + start.
- `async def shutdown_scheduler() -> None` — Graceful, bounded.
- `async def _rehydrate() -> None` — Re-add enabled schedules from DB on startup.
- `async def add_schedule(db, id, topic, depth, cron, tz) -> datetime` — Register + return next_run_at. (Audit HIGH: APScheduler-first then DB → orphan ghost on DB failure.)
- `async def delete_schedule(db, id) -> bool`

#### `app/rerankers.py` — 209 lines
CrossEncoder reranker + RRF fallback.

Constants: `_MAX_PAIRS = 20`.

Classes:
- `@dataclass RerankedItem` — `index, score, text, metadata`.
- `@dataclass RerankResult` — `items, backend, latency_ms`.

Functions:
- `def reset_reranker()` — Reset state for retry-load.
- `def _get_cross_encoder()` — Lazy-load with double-checked locking.
- `def _format_query(query, instruction) -> str`
- `def _format_document(document) -> str`
- `def rerank_cross_encoder(query, documents, top_k, max_pairs) -> RerankResult | None`
- `def rerank_rrf(documents, top_k, k=60) -> RerankResult` — Reciprocal Rank Fusion (no model).
- `def rerank(query, documents, top_k) -> RerankResult` — Try CrossEncoder, fall back to RRF.

#### `app/migrations.py` — 158 lines
SQL migration runner; auto-applies at lifespan startup.

Functions:
- `async def run_migrations()` — Scans `db/migrations/*.sql`, applies unseen, tracks in `schema_migrations`. (Audit HIGH: lock-release-before-apply race + BEGIN-inside-asyncpg-txn defect.)
- `def _has_own_transaction(sql) -> bool` — Heuristic for migrations carrying their own `BEGIN/COMMIT`.

### 11.2 `app/modules/` — orchestration

#### `app/modules/execution_agent.py` — 1262 lines
DAG node execution + SSE streaming + tool dispatch + verification + auto-retry.

Functions:
- `async def execute_next_node(job_id, skip_optimize, skip_verify, model_overrides) -> ExecutionResult | dict` — Execute one pending node.
- `async def execute_all_nodes(job_id, model_overrides) -> AsyncGenerator[str]` — Execute all nodes (SSE).
- `async def skip_node(job_id, node_key, db) -> ExecutionResult` — Mark node skipped.
- `async def retry_failed_node(job_id, node_key, db) -> dict` — Reset failed node to pending.

Internal helpers: `_get_job`, `_get_next_node` (atomic claim), `_set_node_status`, `_log_execution`, `_compile_output` (4-strategy fallback), `_build_pipeline_summary`, `_fetch_upstream_outputs`.

#### `app/modules/research_agent.py` — 2189 lines
Autonomous research + URL/GitHub/OpenAPI/PDF direct modes + pause/resume.

Functions:
- `async def run_research(topic, depth, domain, model_overrides) -> AsyncGenerator[dict]` — Main loop (SSE).
- `async def run_research_pdf(pdf_bytes, filename, extractor, domain, model_overrides) -> AsyncGenerator[dict]`
- `async def resume_research(session_id, user_reply, model_overrides) -> AsyncGenerator[dict]`

Re-exports from `research_extractors` and `research_state`: topic decomposition, extraction, PDF text extraction, URL fetching, GitHub/OpenAPI parsing, SearXNG caching, session lifecycle, state snapshots, heartbeat, pause/resume, SSE emission, `_run_with_session_lifecycle` (catches `CancelledError` → `cancelled`, `client_disconnect`).

#### `app/modules/research_extractors.py`
Per-mode extraction implementations: `extract_url`, `extract_github_repo`, `extract_openapi_spec`, `extract_pdf_text`, plus shared `chunk_text` and `distill_to_entries`. PyMilvus / pdfplumber / pypdf / trafilatura calls wrapped via `asyncio.to_thread`.

#### `app/modules/research_state.py`
Session DB lifecycle: `_guard_and_create_session` (race-safe insert; catches IntegrityError from migration 020's UNIQUE partial index → 409), `_save_state_snapshot`, `_rehydrate_state`, `_finalize_session`, `_run_with_session_lifecycle` decorator.

#### `app/modules/dag_generator.py` — 618 lines
DAG creation, validation, persistence.

Functions:
- `async def generate_dag(job_id, db, model, model_overrides) -> dict` — LLM decompose → validate → INSERT nodes with `is_output_node` for leaves.

Helpers: `_validate_task`, `_validate_graph` (Kahn cycle check, schema), `_distill_tasks` (LLM), `_normalize_tasks` (clamp tool/domain to allowlists), `_max_node_keys_truncation`.

#### `app/modules/ideation_workflow.py` — 370 lines
Phase 1 + Phase 2 orchestration.

Constants: `FEASIBILITY_SYSTEM`, `COMPILE_SYSTEM`.

Functions:
- `async def analyze_and_confirm(idea_text, db, model, domain, model_overrides) -> dict` — Phase 1.
- `async def research_and_compile(job_id, db, user_feedback, push_to_github, model_overrides) -> dict` — Phase 2. (Audit HIGH: `await db.close()` mid-Phase-2 → race against subsequent `db.execute()`.)

#### `app/modules/rag_pipeline.py` — 647 lines
Embed → vector + keyword (parallel) → RRF merge → CrossEncoder rerank → confidence filter; ingest with 3-tier dedup + version chain.

Constants: `COLLECTION_NAME = "toon_v2"`, `EMBED_DIM = 512`, `DEFAULT_TOP_K = 10`, `MAX_TOP_K = 100`, `CONFIDENCE_THRESHOLD = 0.8`, `RRF_K = 60`, `KEYWORD_MAX_TERMS = 5`, `_STOPWORDS`.

Classes:
- `@dataclass RagResult` — `content, title, tags, source_url, entry_id, domain, scores (vector/keyword/rrf/rerank/final), version, supersedes_id`.

Functions:
- `async def query_rag(query, top_k, confidence_threshold, skip_rerank, include_history, domain) -> dict` — Full RAG pipeline.
- `async def ingest_entries(entries, domain, model_overrides) -> dict` — Embed → 3-tier dedup → upsert.
- `async def list_entries(domain, limit, offset) -> dict` — Paginated TOON entries.

Helpers: `_normalize_entry` (2-line delegation to `IngestEntry.from_input` from `_rag_entry.py` — see §11.2 below; canonical-shape conversion), `_domain_expr` (Milvus expression), `_build_embedding_text` (title + tags + content).

#### `app/modules/prompt_optimizer.py` — 242 lines
Prompt optimization — filler strip, token count, clarity scoring, verification.

Constants: `FILLER_PATTERNS`, compiled `_FILLER_RE`.

Classes:
- `@dataclass AnalysisResult` — `token_count, filler_count, hedge_count, has_imperative_structure, issues, structured_issues`.
- `@dataclass OptimizationResult` — full result shape (matches `PromptOptimizeResult` Pydantic).

Functions:
- `async def optimize_prompt(prompt, model_optimizer, model_verifier, skip_verify, model_overrides) -> OptimizationResult`
- `def _analyze(text) -> AnalysisResult`
- `def _deterministic_strip(text) -> str`

#### `app/modules/execution_verify.py` — 96 lines
Verifier (fail-closed).

Constants: `VERIFY_SYSTEM`.

Functions:
- `async def _verify_output(task_title, output, model) -> tuple[Literal["pass","fail"], str, float]`

#### `app/modules/execution_compile.py` — 45 lines
Final-output compilation (no LLM calls).

Functions:
- `async def _compile_output(job_id, db) -> str` — 4-strategy fallback (explicit `is_output_node` markers → title heuristic → last CodeGen → concatenation).

#### `app/modules/execution_handler.py` — 73 lines
Execution status queries.

Functions:
- `async def execution_status(job_id, db) -> dict` — Backs `GET /exec/status/{job_id}`.

#### `app/modules/cleanup.py` — 145 lines
Stale-job reaper (15-min loop). Status-aware — skips `assisted_*` on the normal cadence.

Functions:
- `async def reap_stale_jobs(db) -> dict` — Unified Stages 0–6 sweep.
- `async def reap_abandoned_assist(db) -> dict` — Separate assist idle sweep (`assist_idle_threshold_days`).
- `async def cleanup_loop()` — Background loop; runs every `cleanup_interval_seconds`.

#### `app/modules/gt_extractor.py` — 440 lines
SearXNG → distill → TOON formatting → optional GitHub push.

Constants: `TOPIC_MAP`, `TOPIC_KEYWORDS`, `DISTILL_SYSTEM`, `DISTILL_PROMPT`.

Functions:
- `async def extract_ground_truths(topic, queries, push_to_github, target_file, model) -> dict`
- `async def search_searxng(query, max_results) -> list[dict]`
- `def format_toon_rows(entries) -> str` — TOON markdown.
- `async def push_to_github(content, file, branch, commit_msg) -> dict`
- `def _sanitize_toon_entry(entry) -> dict`
- `def detect_topic_id(text, keywords) -> int`

(Audit LOW #5: no SearXNG throttle here, divergent from `research_agent`.)

#### `app/modules/gt_browser.py` — 271 lines
Async-safe GT browsing with supersede filter.

Functions: `gt_list`, `gt_search`, `gt_detail`, `gt_stats`. Domain fan-out when `domain=None` (iterate sorted `VALID_DOMAINS`).

#### `app/modules/idea_refinement.py` — 171 lines
Raw idea → structured brief.

Constants: `ALLOWED_DOMAINS = {"prompt", "rag", "llm", "spec", "eng", "eng_design"}` (§17.330), `REFINE_SYSTEM`, `REFINE_PROMPT`.

Functions:
- `async def refine_idea(idea_text, db, model, domain, model_overrides, target_status) -> dict`
- `def _truncate_title(text_in, max_chars) -> str`

#### `app/modules/prompt_inspector.py` — 116 lines
Prompt analysis + revision.

Functions:
- `async def list_prompts(job_id, db) -> dict` — All node prompts for a job.
- `async def get_prompt(job_id, node_key, db) -> dict`
- `async def get_prompt_history(job_id, node_key, db) -> PromptHistoryResponse`
- `async def update_prompt(job_id, node_key, prompt, db) -> dict` — Writes previous prompt as a `prompt_revisions` row before UPDATE (migration 022).

#### `app/modules/prompt_assembly.py`
Shared upstream-last prompt assembly used by `execution_agent` and `assist_agent`.

Constants: `EXECUTION_SYSTEM_LLM`, `EXECUTION_SYSTEM_CODEGEN`.

Functions:
- `def system_for_tool(tool) -> str`
- `def truncate_output(content, max_chars) -> str` — Preserve head/tail, truncate middle.
- `def build_base_prompt(node, brief) -> str`

#### `app/modules/assist_agent.py`
Assist Mode session lifecycle + step state machine; mirrors human evidence to `dag_nodes.output_text`.

Functions: `start_session`, `get_session`, `next_step`, `submit_step` (mirror invariant), `handoff_step`, `pause_session`, `resume_session`, `abandon_session`, `add_friction_note`, `list_friction_notes`.

#### `app/modules/assist_replan.py`
Divergence detection + selective subgraph reset for Assist Mode. Implements `context_only` / `selective` / `full` / `disabled` policies.

Functions: `detect_divergence` (4b classifier), `reset_subgraph`, `select_replan_policy`. (Audit LOW: unknown policy logs warn + returns None instead of raising.)

#### `app/modules/_rag_entry.py`
Canonical ingest-entry shape — typed `IngestEntry` Pydantic model that centralizes the TOON↔Milvus dual-name conversion (audit item 6).

Class:
- `class IngestEntry(BaseModel)` — fields: `title`, `content`, `domain_tags`, `source_url`, `source_type`, `confidence`. Defaults applied for missing fields. `extra="ignore"` so callers can pass richer dicts.

Constructors:
- `IngestEntry.from_input(entry: dict) -> IngestEntry` — accepts either TOON-shaped or Milvus-shaped dicts; preserves the legacy first-non-empty-alias-wins semantics that Pydantic's stock `AliasChoices` would not (the latter picks the first PRESENT alias regardless of value).
- `IngestEntry.from_milvus(row: dict) -> IngestEntry` — documentation-named alias for from_input; used by Milvus-read paths to make their intent explicit.

Serializers:
- `to_milvus() -> dict` — Milvus storage shape (long-name keys: `canonical_text`, `topic`, `domain_tags`, `source_url`, `source_type`, `confidence_score`).
- `to_canonical_dict() -> dict` — legacy `_normalize_entry`-compatible shape for in-process consumers.

`rag_pipeline._normalize_entry()` is now a 2-line delegation here.

#### `app/modules/recovery.py`
Per-status next-action registry — turns the existing `jobs.status` lifecycle into structured guidance for the OWUI pipeline, CLI, and SDK (audit item 10).

Constants:
- `NEXT_ACTIONS: dict[str, list[dict]]` — registry mapping every `JobStatus` value to its valid next-step descriptors. Each descriptor: `{action, command, endpoint, method, description, node_specific}`. Covers all 14 lifecycle states including `assisted_*` branches.

Functions:
- `next_actions_for(status, job_id, *, failed_node_key=None, blocked_node_key=None, running_node_key=None) -> list[dict]` — resolve the registry into concrete actions; substitutes `{job_id}` / `{node_key}` placeholders. Picks the most informative node_key (failed > blocked > running) for `node_specific` entries; leaves the placeholder literal when no context. Returns `[]` for unknown status (also logs a warning).
- `all_known_statuses() -> tuple[str, ...]` — used by parity tests to assert the registry covers every value of `JobStatus`.

Wired into `execution_handler.execution_status()` so every `/exec/status/{job_id}` response carries a `next_actions` field. Tests at `tests/test_recovery.py` enforce status-coverage parity with `JobStatus`.

### 11.3 `app/middleware/`

#### `app/middleware/request_id.py` — 57 lines
Class: `class RequestIdMiddleware(BaseHTTPMiddleware)` — binds `request_id` contextvar; outermost in stack at runtime. (Audit MED: inbound `X-Request-ID` echoed without sanitization.)

#### `app/middleware/performance.py` — 131 lines
Class: `class PerformanceMiddleware(BaseHTTPMiddleware)` — request timing, `X-Request-Duration-Ms` header, `/health` log-level split.

Function: `async def log_model_call(model, endpoint, request_type, ttft_ms, total_duration_ms, tokens_prompt, tokens_completion, tokens_per_sec, success, error_message, job_id, node_id)` — Persist `performance_logs` rows.

#### `app/middleware/error_logging.py` — 89 lines
Class: `class ErrorLoggingMiddleware(BaseHTTPMiddleware)` — captures unhandled exceptions, classifies, persists to `error_logs`.

Function: `def _classify_error(exc) -> str` — Map exception type to `error_type` enum. (Audit MED: `TypeError` → "validation" misclassification.)

### 11.4 `app/routers/`

#### `app/routers/status.py` — 251 lines
`GET /status` (job state counts + recents) and `GET /logs/{job_id}` (paginated execution logs).

Classes: `StatusCounts`, `JobSummary`, `StatusResponse`, `NodeLog`, `LogsResponse`.

Functions:
- `async def get_status(limit, status_filter, db) -> StatusResponse`
- `async def get_logs(job_id, include_output, include_compiled, limit, offset, db) -> LogsResponse`

#### `app/routers/assist.py`
Assist Mode endpoints — see §9.

Pydantic body classes: `AssistStartInput`, `AssistSubmitInput`, `AssistHandoffInput`, `AssistFrictionInput`.

Endpoint functions: `assist_start`, `assist_get_session`, `assist_next`, `assist_submit`, `assist_handoff` (SSE), `assist_pause`, `assist_resume`, `assist_done`, `assist_abandon`, `assist_friction_post`, `assist_friction_list`.

### 11.5 `app/providers/`

#### `app/providers/base.py` — 304 lines
Provider abstraction (Sprint E).

Classes:
- `class ProviderError(Exception)` — base
- `class ProviderCapabilityError(ProviderError)`
- `class ProviderUnavailableError(ProviderError)`
- `class ProviderTimeoutError(ProviderError)`
- `@dataclass Tool(name, description, input_schema)` — JSON-Schema-backed tool descriptor
- `@dataclass ToolCall(id, name, arguments)` — model-emitted call
- `@dataclass ModelResponse(text, model, success, error, ttft_ms, total_duration_ms, tokens_prompt, tokens_completion, retries, fallback_used, provider, raw, tool_calls)`
- `class LLMProvider(ABC)` — name, supports_chat, supports_streaming, supports_embedding, supports_native_tools

Abstract methods on `LLMProvider`:
- `async def chat_completion(model, messages, temperature, max_tokens, timeout, **opts) -> ModelResponse`
- `async def list_models() -> list[str]`

Optional/default-raises methods:
- `async def generate(model, prompt, system, temperature, max_tokens, timeout, **opts) -> ModelResponse` — default delegates to `chat_completion`
- `async def embed(model, texts, timeout) -> list[list[float]]` — raises `ProviderCapabilityError` by default
- `async def stream_chat(model, messages, ...) -> AsyncIterator[str]` — raises by default
- `async def tool_call(model, messages, tools, ..., tool_choice) -> ModelResponse` — raises by default
- `async def health_check() -> dict[str, Any]`

#### `app/providers/__init__.py`
Provider registry. Functions: `register(name, provider)`, `provider_for_role(role) -> LLMProvider` (capability-gated), `_autoload()`.

#### `app/providers/ollama.py`
`class OllamaProvider(LLMProvider)` — thin adapter delegating to `model_router._dispatch_with_retry` and `list_models`. `supports_native_tools=True` (Sprint I.2).

#### `app/providers/openai.py`
`class OpenAIProvider(LLMProvider)` — raw httpx through shared `get_openai_client()`. Auth header per call. Supports `OPENAI_BASE_URL` override for vLLM/LocalAI/Ollama-OpenAI-mode.

### 11.6 `app/utils/`

#### `app/utils/http_clients.py` — 109 lines
Shared httpx clients with connection pooling.

Functions: `init_clients()` (eager-init at lifespan), `get_ollama_client()`, `get_searxng_client()`, `get_github_client()`, `get_generic_http_client()`, `get_openai_client()`, `async def close_clients()`.

#### `app/utils/embedding_cache.py` — 180 lines
Two-tier embedding cache (in-memory LRU + Redis at `embedv3:{model}:d{dim}:{hash}`).

Class: `class EmbeddingCache` — async `get`, `put`, `stats`.

Functions: `normalize_cache_text(text) -> str`, `_encode_embedding(embedding) -> bytes` (float32), `_decode_embedding(blob) -> list[float]`, `async def get_cache() -> EmbeddingCache` (singleton).

#### `app/utils/llm_parsing.py` — 115 lines
Shared LLM output parsing.

Functions:
- `def strip_think_tags(text) -> str` — Remove `<think>`/`<thinking>` tags (closed or open).
- `def parse_json_object(raw) -> dict | None` — 4-step fallback (plain → repair → bracket extract → repair).
- `def parse_json_array(raw) -> list | None` — Same 4-step.

#### `app/utils/job_utils.py` — 23 lines
Function: `async def fail_job(db, job_id, error)` — mark job failed with truncated `error_summary`.

#### `app/utils/embedding.py` — 42 lines
Public embedding helper.

Constants: `_QUERY_INSTRUCTION` (e.g. "Represent this query for retrieval: ").

Function: `async def embed_query(query) -> list[float] | None` — instruction-prefixed, MRL-truncated to 512d, two-tier cached.

#### `app/utils/milvus_utils.py` — 162 lines
Shared Milvus accessor with auto-creation.

Constants: `COLLECTION_NAME = "toon_v2"`, `DIM = 512`, `PRIMARY_FIELD = "entry_id"`, `VECTOR_FIELD = "dense_vector"`, `_CACHE_TTL_S = 30.0`.

Functions: `def get_collection() -> Collection | None` (thread-safe cached, double-checked locking auto-create), `build_toon_v2_schema() -> Schema`, `build_toon_v2_index_params(client) -> IndexParams` (HNSW_SQ8 + scalar indexes).

#### `app/utils/staleness.py` — 113 lines
Functions: `async def sweep_expired() -> dict` (cursor pagination), `def get_ttl_for_source(source_type) -> int`, `def compute_expires_at(source_type, created_at) -> int`.

#### `app/utils/github_ingest.py` — 212 lines
GitHub repo fetch + rate-limit guard + selective tree walk.

Functions: `async def fetch_repo_contents(owner, repo, branch, max_files) -> list[dict]`, `async def fetch_file_blob(owner, repo, sha) -> bytes`. Uses `gather(return_exceptions=True)` — **audit HIGH #8: swallows `CancelledError`**.

#### `app/utils/openapi_ingest.py` — 302 lines
OpenAPI/Swagger fetch + validate + per-endpoint flatten.

Functions: `async def fetch_openapi_spec(url) -> dict`, `def flatten_per_endpoint(spec, max_endpoints, max_params) -> list[dict]`.

#### `app/utils/topic_detection.py` — 40 lines
Function: `def detect_topic_id(text, keywords_by_topic, default) -> int` — score text against keyword maps.

### 11.7 `sdk/scaffold_client/` — Python SDK

#### `sdk/scaffold_client/__init__.py`
Public exports: `Client`, `AsyncClient`, `__version__`, exception hierarchy (`ScaffoldError`, `AuthenticationError`, `ConnectionError`, `NotFoundError`, `OrchestratorError`, `PermissionError`, `RateLimitError`, `RequestError`, `TimeoutError`).

#### `sdk/scaffold_client/_version.py`
Single line: `__version__ = "1.0.0"`.

#### `sdk/scaffold_client/errors.py`
Exception subclasses; all derive from `ScaffoldError`.

#### `sdk/scaffold_client/_transport.py`
Shared HTTP-error translation. Both `Client` and `AsyncClient` funnel responses through here.

Functions:
- `def best_error_detail(resp) -> str` — Extract FastAPI `{"detail": ...}` body.
- `def translate_request_error(exc, *, url) -> Exception` — httpx network error → `ScaffoldError` subclass.
- `def raise_for_status(resp) -> None` — Map non-2xx status → typed exception (401 → `AuthenticationError`, 403 → `PermissionError`, 404 → `NotFoundError`, 429 → `RateLimitError`, other 4xx → `RequestError`, 5xx → `OrchestratorError`).
- `def parse_body(resp) -> Any` — JSON if possible, else raw text.

#### `sdk/scaffold_client/client.py`
Class: `class Client` — sync httpx wrapper.

`__init__(base_url="http://localhost:8000", api_key=None, *, timeout=30.0)` — pre-injects `X-API-Key`, instantiates resource sub-objects.

Methods: `request(method, path, *, params, json) -> Any`, `health()`, `status()`, `logs(job_id, *, limit, offset)`, `ideate(idea, *, domain, model)`, `confirm(job_id, *, feedback, push_to_github)`, `optimize(prompt, *, ...)`, `execute(job_id, *, ...)`, `skip(job_id, node_key)`, `close()`, `__enter__`, `__exit__`.

Sub-objects (instantiated once per Client; stable identity): `c.jobs`, `c.dag`, `c.prompts`, `c.gt`, `c.rag`, `c.schedule`.

#### `sdk/scaffold_client/async_client.py`
Class: `class AsyncClient` — async mirror of `Client`. Same methods + signatures, awaitable.

Streaming helpers (yield `{"event": str, "data": Any}` dicts):
- `async def aiter_research(topic, *, depth, domain, include_heartbeats=False)`
- `async def aiter_research_reply(session_id, reply, *, include_heartbeats)`
- `async def aiter_research_pdf(pdf, *, extractor, domain, filename, include_heartbeats)` — multipart upload
- `async def aiter_execute_all(job_id, *, skip_optimize, skip_verify, include_heartbeats)`

Internal: `async def _stream(method, path, ...)` — opens httpx stream context, applies `_transport.raise_for_status`, parses SSE via `_sse.parse_sse_lines`.

#### `sdk/scaffold_client/_resources.py`
Sync resource classes (one per group). Each takes `Client` in `__init__`, calls `self._client.request(...)`.

- `class JobsResource`: `list`, `status`, `delete`, `update`, `cleanup`, `retry`
- `class DagResource`: `get`, `create`
- `class PromptsResource`: `list`, `get`, `history`, `update`
- `class GtResource`: `create`, `list`, `search`, `detail`, `stats`
- `class RagResource`: `search`, `dedup`
- `class ScheduleResource`: `list`, `create`, `delete`

Helper: `_drop_none(d) -> dict` — strip None-valued kwargs before serialization.

#### `sdk/scaffold_client/_async_resources.py`
Mirror of `_resources.py` — same classes prefixed `Async*`, methods `async def`. Reuses `_drop_none` from sync module.

#### `sdk/scaffold_client/_sse.py`
SSE frame parser.

Function: `async def parse_sse_lines(lines, *, include_heartbeats=False) -> AsyncIterator[dict]` — handles multi-line `data:`, missing trailing blank line, non-JSON payloads, `: keepalive` heartbeats.

Helper: `_make_event(event_type, data_lines) -> dict`.

#### `sdk/scaffold_client/schemas.py`
Byte-equal vendor of `app/schemas.py`. Parity enforced by `tests/test_sdk_schema_parity.py`. `make sync-schemas` regenerates after edits to source.

### 11.8 `cli/scaffold_cli/` — Terminal CLI

#### `cli/scaffold_cli/__init__.py`
3 lines: `__version__ = "0.1.0"`.

#### `cli/scaffold_cli/main.py`
Click entry point.

Commands: `cli` (root group), `version`, `doctor`, `ideate`, `confirm`, `jobs list`, `jobs status` (note: pre-existing bug — calls non-existent `GET /jobs/{id}`; fix scoped to follow-up).

#### `cli/scaffold_cli/client.py`
Thin shim over `scaffold_client.Client` (Sprint J.1.e).

Class: `class CLIError(RuntimeError)` — already-formatted user-friendly messages.

Class: `class Client` — `(api_url, api_key, *, timeout=30.0)`. Methods: `get(path, *, params)`, `post(path, *, json)`, `get_or_none(path)` (None on 404), `close`, `__enter__`, `__exit__`. Internal: `_dispatch(method, path, *, params, json)` catches typed SDK exceptions and re-raises as `CLIError` with CLI-specific remediation hints (`make doctor`, `~/.scaffold/config.toml`).

#### `cli/scaffold_cli/config.py`
Config resolution: flag > env > `~/.scaffold/config.toml` (or `$XDG_CONFIG_HOME/scaffold/config.toml`) > walked-up `.env` > default `http://localhost:8000`.

Class: `@dataclass Config(api_url, api_key, source)`.

Function: `def resolve_config(*, flag_url, flag_key) -> Config`.

### 11.9 `pipelines/` — Open WebUI pipelines

#### `pipelines/scaffold_router.py` — 1367 lines
Primary pipeline. Triage, synthesis, `/go`/`/confirm` auto-chain, `/research`, `/schedule`, `/model`, `/results`. Implements valve bootstrap (template → live → env fallback → persist).

Key classes/functions: `class Pipeline` (OWUI entry), `class CommandParser` (argparse wrapper), `_normalize_input`, `_is_placeholder`, `_suggest_command`, `_handle_*` per command, `_research_and_stream_raw` + `_stream_sse_to_queue` (SSE consumer with idle-counter calibration — see audit retraction #14).

Audit HIGH #12: auto-chain has no recovery state machine for mid-chain failures.

#### `pipelines/gt_browser.py` — 246 lines
GT browsing (requests-based, paginated hints, per_page valve).

#### `pipelines/execution_handler.py` — 339 lines
Direct execution control (`/exec status|next|submit|skip`).

#### `pipelines/prompt_inspector.py` — 236 lines
Prompt analysis (`/prompts list|get|update|history`).

#### `pipelines/dag_viewer.py` — 176 lines
DAG visualization (Mermaid). `/dagviz {job_id}`.

All 5 pipelines share the valve-bootstrap pattern. Per-pipeline helpers are inlined (see Convention #14).

### 11.10 `scripts/`

#### `scripts/openapi_snapshot.py` — 87 lines
`python scripts/openapi_snapshot.py` (writes JSON to stdout); `--check` mode compares against `docs/openapi.json` and exits non-zero on drift.

Functions: `_dump`, `_generate`, `_check`, `main`.

#### `scripts/reindex.py` — 375 lines
Re-embed every `toon_v2` entry. Per-partition fan-out, `entry_id`-cursor pagination, byte-equal `_build_embedding_text` mirror of `rag_pipeline`.

Function: `async def reindex_partition(collection, domain, new_embedder, new_provider, batch_size, dry_run, now_ms)`.

CLI flags: `--new-embedder`, `--new-provider`, `--domain`, `--batch-size`, `--dry-run`, `--yes`.

#### `scripts/score_retrieval.py` — 112 lines
Retrieval quality scoring against `tests/fixtures/golden_set.json`.

Class: `@dataclass QueryResult(query, expected_ids, retrieved_ids, recall_at_5, recall_at_10, mrr, hit)`.

Functions: `async def score_query(item, top_k) -> QueryResult`, `async def run(golden_path, output_path) -> dict`.

#### `scripts/create_toon_v2.py` — 39 lines
Bootstrap the `toon_v2` collection (drop + create with canonical HNSW_SQ8 schema + partition-key isolation).

#### `scripts/_prune_dev_deps.py` — 34 lines
Uninstall dev-only deps in the runtime image.

Functions: `def extract_names(req_file) -> list[str]`, `def main() -> int`.

#### `scripts/{bootstrap,doctor,init,sync_valves}.sh`
Shell scripts (not Python):
- `bootstrap.sh` (204 lines) — first-time setup wizard (`make bootstrap`)
- `doctor.sh` (196 lines) — health audit (`make doctor`); probes every dep + verifies key sync
- `init.sh` (201 lines) — provider/model wizard (`make init`); per-role provider, OPENAI_API_KEY collection, atomic `.env` update
- `sync_valves.sh` (57 lines) — wipe baked-in `api_key` from `pipelines/*/valves.json`

### 11.11 `app/sim/` — engineering-design pipeline

Hosts the `design_circuit` job_type chain (§17.140-§17.156). Five reasoning stages — **extract → confirm → topology-select → size → report** — backed by three EDA sidecars (`scaffold-ngspice`, `scaffold-verilator`, `scaffold-symbiyosys`; see §2). Every numeric claim ties back through `sim_runs.id`; every module here follows the audit-the-attempt contract — failures are persisted data, not exceptions.

Routed by `app/routers/design.py` + `app/routers/sizing.py`; opt-in only — chat flows never touch this code path.

#### `app/sim/__init__.py` — 0 lines
Package marker.

#### `app/sim/spec_schema.json` — 155 lines (not Python)
**Single source of truth** for the spec envelope — same file the LLM extractor pastes into its system prompt AND the wire-accept validator pre-compiles at import. Draft 2020-12. `constraints[]` of `{id, kind, description, target?, min?, max?, tolerance_pct?, unit, criticality}`; `kind` is a closed dotted enum (`electrical.*` / `timing.*` / `thermal.*` / `signal.*` / `physical.*` / `cost.*`); `additionalProperties: false` everywhere blocks the extractor from sneaking unvalidated fields.

#### `app/sim/spec.py` — 236 lines
Schema loaded once at import time, pre-compiled into a `Draft202012Validator`. Re-exports constraint-kind / criticality / interface enums as Python `frozenset` constants (`test_python_enums_mirror_schema_file` is the parity guard — JSON enum drift breaks the test loudly).

Functions / classes:
- `def validate_spec(spec_json) -> SpecValidationResult` — never raises; returns `{ok, errors[]}` with JSON-pointer paths
- `def spec_sha256(spec_json) -> str` — canonical-JSON SHA256 (sorted keys); order-only diffs hash identically
- `SpecValidationResult`, `SpecValidationError` dataclasses

#### `app/sim/spec_store.py` — 259 lines
DB-only helpers; kept separate from `spec.py` so the validator stays schema-only.

Functions: `get_spec`, `confirm_spec`, `unconfirm_spec`, `is_spec_confirmed` (quiet probe), `require_confirmed_spec` (strict gate — raises `SpecNotConfirmedError` distinct from `SpecNotFoundError`), `list_pending_confirmations`. Dataclass: `SpecRow`.

#### `app/sim/spec_extractor.py` — 280 lines
**First LLM in the engineering-design pipeline.** Embeds `spec_schema.json` in the system prompt + two few-shot examples; one-shot strict envelope (`{"spec": {...}}` or `{"ambiguities": [...]}`). `temperature=0` for spec_sha256 reproducibility.

Function: `async def extract_spec(nl_text, *, db, job_id=None, model_role=None) -> ExtractionResult`. Failure paths kept distinct in `ExtractionResult` (ambiguities vs errors); **never writes to `specs` on any failure path**.

#### `app/sim/topology_select.py` — 420 lines
First reasoning stage that consumes a confirmed spec. Numeric-free RAG query (design.kind + constraint kinds, no values) → `query_rag(domain="eng_design")` (§17.329 — was `"eng"` pre-split) → LLM proposes 2-4 candidates with `entry_id` citations → **hard-reject if any cite ∉ retrieval set** → persist `topology_selections` row.

Function: `async def select_topologies(spec_id, *, db, model_role=None, top_k=8, domain="eng_design") -> TopologySelectionResult` (§17.329 — DEFAULT_DOMAIN flipped from `"eng"` to `"eng_design"` so the stage retrieves only circuit/EDA content). Helpers `_build_rag_query`, `_validate_citations`, `_parse_candidates` are individually unit-tested.

#### `app/sim/ngspice.py` — 196 lines
Wrapper around the `scaffold-ngspice` sidecar (§2). HTTP contract: `POST /run {netlist, timeout_s, seed?}`. Two ngspice 44 quirks baked in: `.meas` cards must be inside a `.control/.endc` wrapper in batch mode, and the measurement parser stops at the resource-stats footer (otherwise it captures `Stack = 0 bytes` as a KPI).

Function: `async def run_ngspice(netlist, *, db, timeout_s=None, seed=None, job_id=None, dag_node_id=None) -> NgspiceResult`. **Never raises** — `NgspiceResult(ok=False)` on transport/timeout/non-zero exit; the `sim_runs` row is written even when the sidecar is unreachable.

#### `app/sim/verilator.py` — 228 lines
Wrapper around the `scaffold-verilator` sidecar (§2). Two-phase pipeline (compile → build → run) with separate `build_timeout_s` + `run_timeout_s`. KPI protocol: `$display("KPI name=value")` → regex-parsed into `sim_runs.measurements`.

Function: `async def run_verilator(sv_source, *, top_module, db, run_timeout_s=None, build_timeout_s=None, seed=None, job_id=None, dag_node_id=None) -> VerilatorResult`. Same audit-the-attempt contract as ngspice.

#### `app/sim/symbiyosys.py` — 244 lines
Wrapper around the `scaffold-symbiyosys` sidecar (§2) for formal verification. SV assertions → SMT → verdict ∈ {`PASS`, `FAIL`, `UNKNOWN`, `TIMEOUT`, `ERROR`}. sby's exit codes are authoritative (0/2/4/8/16); regex over the `DONE` summary is the fallback. Counterexample VCDs returned base64-encoded on `FAIL` (not persisted to `sim_runs` v1).

Function: `async def run_symbiyosys(sv_source, *, top_module, db, mode='bmc', depth=20, engine='smtbmc z3', timeout_s=None, seed=None, job_id=None, dag_node_id=None) -> SymbiYosysResult`. Module-level constants: `VERDICT_PASS`, `VERDICT_FAIL`, `VERDICT_UNKNOWN`, `VERDICT_TIMEOUT`, `VERDICT_ERROR`.

#### `app/sim/device_sizing.py` — 767 lines
First closed-loop stage. LLM proposes params + ngspice netlist → §17.140 wrapper runs it → `_check_constraints` compares measurements to spec → on gap, feeds (params, measurements, gap descriptions, ngspice stderr tail) back to LLM. Bounded by `settings.device_sizing_max_iterations` (default 3).

Function: `async def size_device(topology_selection_id, *, db, candidate_idx=0, max_iterations=None, model_role=None) -> DeviceSizingResult`. The row IS the attempt; `converged BOOL` is the outcome. `_check_constraints` treats a `criticality=required` + measurable-kind constraint with no measurement AS a gap (caught a §17.147 bug where forgetting `.meas` claimed convergence with empty measurements). Analog-only refusal at gate (`design.kind != "analog_circuit"` rejected before any LLM/ngspice call).

Helpers `_is_measurable_kind`, `_check_constraints`, `_call_llm_propose` individually unit-tested. Dataclass: `IterationRecord` (full trajectory on result).

#### `app/sim/digital_sizing.py` — 581 lines
Digital-logic counterpart to `device_sizing.py`. Verilator-in-the-loop instead of ngspice-in-the-loop. Reuses `_check_constraints` and the lookup primitives from `device_sizing` so analog/digital code paths share gap-checking semantics.

Function: `async def size_digital_device(topology_selection_id, *, db, candidate_idx=0, max_iterations=None, model_role=None, top_module="tb") -> DigitalSizingResult`. Dataclass: `DigitalIterationRecord`. **`POST /topology-selections/{id}/size` is polymorphic** on `spec.design.kind`: `analog_circuit` → `size_device`, `digital_logic` → `size_digital_device`, other → 400.

#### `app/sim/report.py` — 719 lines
Terminal stage — **pure projection of the audit tables, no LLM.** Joins `device_sizings ⨝ topology_selections ⨝ specs ⨝ sim_runs[]` (or `digital_sizings ⨝ …`). `render_markdown(doc) -> str` is byte-deterministic (`test_render_markdown_is_deterministic` is the guard). Non-converged sizings ARE renderable, with a `⚠ NOT CONVERGED` banner and per-constraint status table.

Functions:
- `async def build_report(sizing_id, *, db, generated_at=None) -> ReportDocument` — main entry
- `def render_markdown(doc) -> str` — pure deterministic projection
- `_classify_constraint` — status enum source of truth (mirrors `device_sizing._check_constraints`)
- `_fetch_chunk_content` — best-effort Milvus citation fetch (§17.319 closed the `content` → `canonical_text` field-rename bug)

Dataclasses: `ReportDocument`, `ReportConstraint`, `ReportCitation`, `ReportSimRun`. Exception: `ReportNotAvailableError` (router maps to 404).

#### `app/sim/design_pipeline.py` — 592 lines
**The five stages wired into one operator-facing flow.** Hosts the `design_circuit` job_type lifecycle. Three callable surfaces:

- `async def create_design_job(nl_text, *, db, model_role=None) -> DesignCreateResult` — runs §17.144 extract; on success creates `jobs` row (`job_type='design_circuit'`) + links `specs.job_id`; on ambiguity OR extractor error returns inline with no rows persisted (keeps failed extractions out of the jobs lifecycle).
- `async def advance_design_stage(job_id, stage, *, db) -> AsyncIterator[str]` — SSE-streaming per-stage advance (`event: stage_start | stage_done | stage_error | done`). `stage` ∈ `{topology, size, report}`. Size branch dispatches analog/digital based on `spec.design.kind`; `stage_done` carries a `kind` field.
- `async def get_design_state(job_id, *, db) -> DesignState` — aggregated state; joins jobs ⨝ specs ⨝ topology_selections ⨝ device_sizings (UNION digital_sizings).

Dataclasses: `DesignCreateResult`, `DesignState`. Exception: `DesignJobNotFoundError` (distinguishes missing job from wrong-`job_type`).

**Full chain.** `POST /design` → `POST /specs/{id}/confirm` → 3× `POST /design/{id}/advance?stage={topology,size,report}` → `GET /design/{id}` for aggregated state, OR `GET /device-sizings/{id}/report` / `GET /digital-sizings/{id}/report` for the rendered Markdown.

---

## 12. Configuration reference

Source: `app/config.py`. Pydantic Settings — env vars with bounded defaults. Read at import time.

### 12.1 Auth + DB

| Var | Default | Notes |
|---|---|---|
| `SCAFFOLD_API_KEY` | `""` (required at startup unless disabled) | Used as `X-API-Key` |
| `SCAFFOLD_AUTH_DISABLED` | `false` | Bypass auth (dev-only) |
| `DATABASE_URL` | `postgresql+asyncpg://scaffold:scaffold_dev_pw@scaffold-postgres:5432/scaffold_engine` | |

### 12.2 External services

| Var | Default | Notes |
|---|---|---|
| `OLLAMA_BASE_URL` | `http://172.18.0.1:11434` | Bridge gateway |
| `MILVUS_URI` | `http://milvus-standalone:19530` | |
| `MILVUS_NUM_PARTITIONS` | 64 | Per-domain partitioning |
| `SEARXNG_URL` | `http://searxng:8080` | |
| `REDIS_URL` | `redis://scaffold-redis:6379/0` | |

### 12.3 Embeddings + reranker

| Var | Default | Notes |
|---|---|---|
| `EMBEDDING_DIM` | 512 | **Locked** — collection geometry depends on it |
| `MODEL_EMBEDDER_ID` | `qwen3-embedding-8b-mrl512` | Cache key prefix |
| `EMBEDDING_BATCH_SIZE` | 32 | |
| `EMBEDDING_CACHE_MEMORY_SIZE` | 10000 | LRU size |
| `EMBEDDING_CACHE_TTL_S` | 30 days | Redis TTL |
| `SEMANTIC_DEDUP_THRESHOLD` | 0.95 | RAG ingest reject threshold |
| `VERSION_CHAIN_THRESHOLD` | 0.90 | RAG ingest version-chain threshold |
| `RERANK_MAX_CANDIDATES` | 32 | |
| `RERANK_DOC_TRUNCATE` | 2000 | Per-doc char cap before rerank |
| `RERANK_WARN_MS` / `_ERROR_MS` | 30000 / 120000 | Latency thresholds |

### 12.4 Model assignments (8 valve-switchable roles + reranker config-only)

| Role | Var | Default |
|---|---|---|
| Generation | `MODEL_GENERAL` | `qwen3-vl:235b-instruct-cloud` |
| Verifier | `MODEL_VERIFIER` | `qwen2.5:7b` |
| Coder | `MODEL_CODER` | `qwen2.5-coder:7b` |
| Router/triage/decompose | `MODEL_ROUTER` | `qwen3:4b` |
| Embedder (config-only) | `MODEL_EMBEDDER_PIPELINE` | `qwen3-embedding:8b` |
| Reranker (config-only) | `MODEL_RERANKER` | `tomaarsen/Qwen3-Reranker-0.6B-seq-cls` |
| Cloud heavy | `MODEL_CLOUD_HEAVY` | `qwen3-vl:235b-instruct-cloud` |
| Cloud alt | `MODEL_CLOUD_ALT` | `qwen3.5:397b-cloud` |
| Fallback | `MODEL_FALLBACK` | `qwen3.5:latest` |

`get_model(role, overrides=None)` enforces an allowlist (`ROLE_FIELDS`) preventing arbitrary attribute access via role string. Override priority: per-request override > env var > config.py default. Empty-string overrides are rejected (raise `ValueError`); `None` means "fall through."

### 12.4.1 Model resolution — the priority chain

Every model role (general, verifier, coder, router, …) resolves through `app.config.get_model(role, overrides=None)`. The lookup order is:

1. **Per-request override** (`overrides` dict argument) — supplied by the SDK / pipeline / orchestrator endpoint when the caller wants to pin a specific model for one call. Empty-string overrides are rejected with `ValueError` (rather than silently falling through). `None` means "skip this layer."
2. **Environment variable** `MODEL_<ROLE>` — read at orchestrator startup into `Settings`. Survives across requests; persisted in `.env`. Example: `MODEL_GENERAL=qwen2.5:7b` makes the generation role use a different default than the cloud-routed `qwen3-vl:235b-instruct-cloud`.
3. **Hardcoded default** in `app/config.py` — the documented Ollama-based stack. Never accessed if (1) or (2) provides a value.

Allowlist: only fields in `ROLE_FIELDS` can be referenced — prevents arbitrary attribute access via role string. The eight role values are: `model_general`, `model_verifier`, `model_coder`, `model_router`, `model_fallback`, `model_cloud_heavy`, `model_cloud_alt`, `model_embedder_pipeline`. Reranker is config-locked (CrossEncoder singleton) and outside `get_model`.

**To inspect the resolved chain at runtime:** `scaffold config show --filter model` (lists every model role with current value + default; `*` marks fields whose runtime value differs from the default). Or `GET /config` for the full JSON. Sensitive values (anything matching key/secret/token/password keywords or a URL with embedded credentials) are redacted in the public surface.

### 12.5 Provider selection (Sprint E+)

Per-role provider routing. Default `ollama` preserves pre-Sprint-E behavior.

| Var | Default |
|---|---|
| `MODEL_GENERAL_PROVIDER` | `ollama` |
| `MODEL_VERIFIER_PROVIDER` | `ollama` |
| `MODEL_CODER_PROVIDER` | `ollama` |
| `MODEL_ROUTER_PROVIDER` | `ollama` |
| `MODEL_FALLBACK_PROVIDER` | `ollama` |
| `MODEL_CLOUD_HEAVY_PROVIDER` | `ollama` |
| `MODEL_CLOUD_ALT_PROVIDER` | `ollama` |
| `MODEL_EMBEDDER_PIPELINE_PROVIDER` | `ollama` |

The reranker is exempt — runs as a CrossEncoder singleton outside the provider system.

### 12.6 OpenAI-compatible provider (Sprint F)

| Var | Default |
|---|---|
| `OPENAI_API_KEY` | `""` |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` (override-able for vLLM/LocalAI/Ollama OpenAI-mode) |
| `OPENAI_TIMEOUT` | 600 |
| `OPENAI_ORGANIZATION` | `""` |

The provider raises `ProviderUnavailableError` at call time if the key is empty when actually needed — leaving it blank while no role is bound to `openai` is fine.

### 12.7 Timeouts + retries

| Var | Default | Notes |
|---|---|---|
| `CLOUD_TIMEOUT` | 3600 | Per-call timeout for cloud models |
| `LOCAL_TIMEOUT` | 1800 | Per-call timeout for local Ollama |
| `VERIFY_TIMEOUT_SECONDS` | 120 | Verifier per-call cap |
| `MAX_RETRIES` | 3 | Default retry budget |
| `NODE_TIMEOUT_SECONDS` | 600 | Per-DAG-node cap |
| `EXECUTION_GLOBAL_RETRY_CAP` | 20 | Job-wide retry budget |
| `SSE_KEEPALIVE_SECONDS` | 15 | Pipeline-side keepalive |

### 12.8 Research agent

| Var | Default | Notes |
|---|---|---|
| `RESEARCH_MAX_ITERATIONS` | 3 | (depth=deep can override) |
| `RESEARCH_MAX_QUERIES` | 8 | Per iteration |
| `IDEATION_MAX_QUERIES` | 5 | Phase 2 |
| `IDEATION_MAX_DISTILL_RESULTS` | 15 | |
| `RESEARCH_MAX_URLS_PER_ITERATION` | 20 | |
| `RESEARCH_SEARXNG_DELAY` | 1.5s | Throttle |
| `RESEARCH_CHUNK_SIZE` | 1500 | Per-page chunk |
| `RESEARCH_TIMEOUT` | 3600 | End-to-end cap |
| `RESEARCH_MAX_URL_BYTES` | 5 MB | Per-URL fetch cap |
| `RESEARCH_MAX_PDF_BYTES` | 20 MB | PDF mode cap |
| `RESEARCH_FETCH_CONCURRENCY` | 5 | Fetcher pool |
| `RESEARCH_FETCH_TIMEOUT` | 15 | Per-page |
| `RESEARCH_URL_FETCH_TIMEOUT` | 30 | Per-URL ingest mode |
| `RESEARCH_HEARTBEAT_INTERVAL` | 8 | SSE keepalive |
| `RESEARCH_MAX_ENTRY_CHARS` | 8000 | Per-entry distill cap |

### 12.9 GitHub + OpenAPI ingest

| Var | Default | Notes |
|---|---|---|
| `GITHUB_TOKEN` | `""` | Optional; raises rate cap |
| `GITHUB_MAX_FILES` | 50 | Per repo |
| `GITHUB_BLOB_CONCURRENCY` | 8 | Parallel file fetch |
| `GITHUB_TIMEOUT` | 30 | |
| `GITHUB_API_BASE` | `https://api.github.com` | |
| `OPENAPI_MAX_ENDPOINTS` | 200 | Per spec |
| `OPENAPI_MAX_PARAMS_PER_ENDPOINT` | 50 | |
| `OPENAPI_TIMEOUT` | 30 | |

### 12.10 Stale-job reaper

| Var | Default | Notes |
|---|---|---|
| `STALE_THRESHOLD_MINUTES` | 30 | Generic stale window |
| `PLANNING_STALE_MINUTES` | 60 | Planning phase |
| `LONG_PHASE_STALE_MINUTES` | 45 | Long-phase (researching/executing) |
| `AWAITING_CONFIRMATION_STALE_MINUTES` | 10080 (7d, max 30d) | Lenient — user may walk away |
| `ASSIST_IDLE_THRESHOLD_DAYS` | 7 | Abandoned-assist sweep |
| `NODE_ORPHAN_THRESHOLD_MINUTES` | 60 | dag_node stuck in `running` → reset to `pending` |
| `CLEANUP_INTERVAL_SECONDS` | 900 (15min) | Reaper loop period |

Reaper warning at startup: `node_timeout_seconds >= stale_threshold_minutes*60` triggers a `config_timeout_reaper_overlap` warning ("reaper may mark live jobs failed mid-execution") — emitted in `_warn_timeout_vs_reaper` model_validator.

### 12.11 Execution agent tuning

| Var | Default | Notes |
|---|---|---|
| `MAX_UPSTREAM_CHARS` | 8000 | Per-upstream cap for assembly |
| `RAG_COSINE_FLOOR` | 0.3 | Discard below this |
| `VERIFIER_TOP_K` | 5 | RAG context budget for verifier |
| `COMPILE_OUTPUT_GATE_CHARS` | 50000 | Compile output cap |
| `COMPILE_OUTPUT_MIN_CHUNK` | 200 | Min chunk size for upstream truncation |
| `PROMPT_MAX_CHARS` | 16384 | Per-prompt edit cap |

### 12.12 Scheduler

| Var | Default |
|---|---|
| `SCHEDULER_ENABLED` | true |
| `SCHEDULER_TIMEZONE` | UTC |
| `SCHEDULER_JOBSTORE_URL` | "" (defaults to `sync_database_url`) |
| `SCHEDULER_JOB_TIMEOUT` | 3600 |
| `SCHEDULER_MISFIRE_GRACE_TIME` | 300 |
| `SCHEDULER_SHUTDOWN_TIMEOUT` | 30 |

### 12.13 GT pipeline

| Var | Default |
|---|---|
| `GT_GITHUB_OWNER` | `LocketKeyLLC` |
| `GT_GITHUB_REPO` | `smokieRAGs` |
| `GT_GITHUB_BRANCH` | `main` |
| `GT_STATS_SCAN_LIMIT` | 16384 |

### 12.14 Logging

| Var | Default |
|---|---|
| `LOG_LEVEL` | `info` |
| `LOG_JSON_FORMAT` | `true` |
| `LOG_FILE` | `null` (set by compose to `/var/log/scaffold/app.jsonl`) |

### 12.15 Provider validation

`validate_models()` runs at `/ideate`, `/ideate/confirm`, `/dag`, `/execute/all`, `/research`. Missing models → HTTP 422 with the list. Internal entry points (`execute_next_node` from execute_all loop) skip this — keeps the function library-callable without N redundant Ollama `/api/tags` probes per DAG run.

### 12.16 Pipeline valves

`pipelines/scaffold_router.py` (admin-configurable per OWUI Pipelines panel):

**Connection:** `api_key`, `orchestrator_url`, `request_timeout=30`, `stream_timeout=3600`, `triage_timeout=3600`, `keepalive_interval=10`, `ollama_url`, `dag_timeout` *(legacy alias, migrated to `stream_timeout` on init)*

**Triage:** `triage_model=qwen3:4b`

**Assist Mode:** `assist_after_confirm=False` (auto-route `/confirm` into assist), `assist_default_handoff_policy=manual`, `assist_default_replan_policy=context_only`, `assist_max_evidence_chars=200000`

**Model overrides (8 roles):** `model_general`, `model_verifier`, `model_coder`, `model_embedder`, `model_reranker`, `model_router`, `model_fallback`, `model_cloud_alt`

**Valve bootstrap pattern** (`valves.template.json` → live `valves.json` → env fallback → persist): each pipeline's `_bootstrap_valves` re-seeds from a template file when the live file is missing or `{}`. `_apply_env_fallbacks` fills empty string-valued valves from `SCAFFOLD_API_KEY` / `SCAFFOLD_ORCHESTRATOR_URL`, then persists resolved values back to disk. **OWUI Pipelines treats every `.py` under `/app/pipelines/` as a pipeline candidate** — so per-pipeline helpers must be inlined or placed in `pipelines/_vendor/` (the loader scan is non-recursive, §17.212). A sibling `_helpers.py` under `pipelines/` gets auto-discovered and quarantined to `pipelines/failed/`; the `:ro` bind-mount overlay does NOT prevent that rename, since `os.rename` operates on the parent directory entry.

**5-place API key sync** (must stay aligned after rotation):
1. `.env`
2. `pipelines/<each>/valves.json`
3. `~/.bashrc` (host)
4. `scaffold-orchestrator` container env
5. `open-webui-pipelines` container env

`make sync-valves` wipes baked-in `api_key` from valves so they fall through to `$SCAFFOLD_API_KEY`.

---

## 13. Logging events catalog

`logging.getLogger("scaffold.<sub>")` emits to stdout + `/var/log/scaffold/app.jsonl` (RotatingFileHandler 50MB×3). `structlog` is the **formatter only** (wired in `app/logging_config.py`); `request_id` propagated via `structlog.contextvars`.

56 structured events across 13 source files. Convention: `snake_case`, past-tense verb-noun. All events use positional `%s` format unless noted as `extra=dict(...)` style.

### 13.1 Engine lifecycle

| Event | Level | Source | Fields |
|---|---|---|---|
| `engine_started` | INFO | `app/main.py` | `log_level` |
| `engine_stopped` | INFO | `app/main.py` | — |
| `ollama_connected` | INFO | `app/main.py` | `models_available` |
| `ollama_connection_failed` | WARN | `app/main.py` | `url`, `error` |
| `milvus_connected` | INFO | `app/main.py` | `uri` |
| `milvus_connection_failed` | WARN | `app/main.py` | `uri`, `error` |
| `startup_cleanup_begin` | INFO | `app/main.py` | — |
| `startup_cleanup_complete` | INFO | `app/main.py` | `running_to_failed`, `planning_to_cancelled` |
| `startup_cleanup_failed` | ERROR | `app/main.py` | `error` |

### 13.2 Job lifecycle

| Event | Level | Source | Fields |
|---|---|---|---|
| `job_created` | INFO | `idea_refinement.py` | `job` (uuid) |
| `job_refined` | INFO | `idea_refinement.py` | `job` |
| `job_failed` | ERROR | `idea_refinement.py` | `job`, `error` |
| `job_autocompleted` | INFO | `execution_agent.py` | `job` |
| `stale_job_cleaned` | INFO | `app/main.py` | `job_id`, `old_status`, `new_status`, `age_minutes` |

### 13.3 DAG generation

| Event | Level | Source | Fields |
|---|---|---|---|
| `dag_generated` | INFO | `dag_generator.py` | `job`, `node_count` |
| `dag_generation_failed` | ERROR | `dag_generator.py` | `job`, `error` |
| `auto_dag_generation_failed` | ERROR | `execution_agent.py` | `job`, `error` |
| `idempotency_rejected` | WARN | `dag_generator.py` | `job`, `existing_nodes` |
| `dag_truncated` | WARN | `dag_generator.py` | `original_count`, `kept_count`, `dropped_keys` |
| `dag_undercount` | WARN | `dag_generator.py` | `node_count` |

### 13.4 DAG validation

| Event | Level | Source | Fields |
|---|---|---|---|
| `invalid_dependency` | WARN | `dag_generator.py` | `node_key`, `invalid_ref`, `valid_keys` |
| `self_reference_removed` | WARN | `dag_generator.py` | `node_key` |
| `invalid_tool_defaulted` | WARN | `dag_generator.py` | `node_key`, `original_tool`, `defaulted_to=LLM` |
| `invalid_domain_defaulted` | WARN | `dag_generator.py` | `node_key`, `original_domain` |
| `dag_cycle_detected` | ERROR | `dag_generator.py` | `involved_keys` |

### 13.5 Node execution

| Event | Level | Source | Fields |
|---|---|---|---|
| `node_execution_started` | INFO | `execution_agent.py` | `node`, `job`, `model` |
| `node_execution_failed` | ERROR | `execution_agent.py` | `node`, `error` |
| `node_timeout` | WARN | `execution_agent.py` (extra=dict) | `node_key`, `tool`, `elapsed_s`, `timeout_s` |
| `node_verification_failed` | WARN | `execution_agent.py` | `node`, `reason` |
| `verification_complete` | INFO | `execution_agent.py` (extra=dict) | `node_key`, `verified`, `confidence` |
| `node_reset` | INFO | `execution_handler.py` | `node`, `job` |
| `compiled_output_stored` | INFO | `execution_agent.py` | `chars`, `job` |
| `partial_compiled` | INFO | `execution_agent.py` | `job`, `chars` |
| `partial_compile_failed` | WARN | `execution_agent.py` | `job`, `error` |
| `upstream_truncated` | INFO | `execution_agent.py` (extra=dict) | `node_key`, `original_chars`, `truncated_chars`, `upstream_nodes` |

### 13.6 Pipeline completion

| Event | Level | Source | Fields |
|---|---|---|---|
| `pipeline_completed` | INFO | `execution_agent.py` | `job`, `total`, `passed`, `failed`, `duration_ms` |

### 13.7 Tool dispatch

| Event | Level | Source | Notes |
|---|---|---|---|
| `tool_dispatch` | INFO | `execution_agent.py` | Format: `tool_dispatch: {tool} {action} node={node_key}`. Actions: `auto_skip`, `blocked_manual`, `model_coder`, `web_search`, `rag_search`, `skip` |
| `tool_dispatch_unknown` | WARN | `execution_agent.py` | `tool`, `node` |

### 13.8 Retrieval + context injection

| Event | Level | Source | Fields |
|---|---|---|---|
| `retrieval_completed` | INFO | `rag_pipeline.py` | `query`, `domain`, `n_results`, `top_score`, `latency_ms` |
| `search_executed` | INFO | `rag_pipeline.py` | `vector_hits`, `keyword_hits`, `query` |
| `rag_context_injected` | INFO | `execution_agent.py` | `chars`, `node` |
| `searxng_context_injected` | INFO | `execution_agent.py` | `chars`, `node` |
| `milvus_retrieval` | INFO | `execution_agent.py` (extra=dict) | `node_key`, `domain`, `top_k`, `results_returned`, `total_chars_injected`, `reranker_used` |
| `milvus_rerank` | INFO | `execution_agent.py` (extra=dict) | `node_key`, `candidates_in`, `candidates_out`, `top_score` |
| `searxng_search_failed` | WARN | `execution_agent.py` | `error` |
| `milvus_search_failed` | WARN | `execution_agent.py` (extra=dict) | `node_key`, `error` |

### 13.9 Reranker

| Event | Level | Source | Fields |
|---|---|---|---|
| `crossencoder_loading` | INFO | `rerankers.py` | `model` |
| `crossencoder_loaded` | INFO | `rerankers.py` | `elapsed_s` |
| `crossencoder_load_failed` | ERROR | `rerankers.py` | `error` |
| `reranker_completed` | INFO | `rerankers.py` | `docs`, `elapsed_ms`, `top_score` |
| `crossencoder_inference_failed` | WARN | `rerankers.py` | `error` |
| `reranker_fallback_activated` | WARN | `rerankers.py` | — |

### 13.10 HTTP middleware

| Event | Level | Source | Fields |
|---|---|---|---|
| `http_request_completed` | INFO | `middleware/performance.py` | `method`, `path`, `status`, `duration_ms` |
| `http_request_failed` | ERROR | `middleware/error_logging.py` | `exception`, `method`, `path`, `error` |
| `blocked_node_query_failed` | WARN | `execution_agent.py` | `job`, `error` |
| `prompt_updated` | INFO | `prompt_inspector.py` | `node`, `job` |

---

## 14. Testing + CI

### 14.1 Test counts (re-baselined post-§17.807, 2026-08-16 — local `make test` on the python-3.14.6 / pytest-9.1.1 / pymilvus-3.0 / MilvusClient stack)

**Runtime stack (§17.591–593):** the dev + prod images now run **Python 3.14.6** with **pytest 9.1.1**, **pymilvus 3.0.0** (MilvusClient API — the ORM path is gone), **torch 2.13.0+cpu**, pydantic 2.13.4, uvicorn 0.51.0. The EDA sidecars are on python 3.14 too (§17.592).

**Current local baseline (`make test`, full dev-image suite):** **5380 passed, 1 skipped, 26 failed, 99 errors in 27:58** (5506 collected) — measured 2026-08-16 after §17.807 (install-time options: multi-user scoped keys + vLLM compute preset), on the python-3.14.6 + pytest-9.1.1 + pymilvus-3.0 + MilvusClient image. This **supersedes the stale §17.625 4199/0 line below** (2026-07-18), which predated ~1300 tests of §17.626–807 growth during which the *full* `make test` sweep was not re-run — CI gates only a subset (see the CI baseline paragraph below), so the plain-`make test` regressions accumulated unmeasured. **All 26 failures + 99 errors are pre-existing harness/environment artifacts — ZERO are code regressions and none were introduced by §17.807** (verified three ways: §17.807's own tests are green — `test_auth.py` +4 / `test_api_keys.py`, 34 passed; the `test_web_ui` failures are **byte-identical with §17.807 stashed**; and the `migrations_hook_crashed` error reproduces on clean code). Full categorization:

- **20 failures — pipeline tests** (`test_scaffold_router_*`, split 3/2/2/2/6/5 across chat-routing/nudge/continuity/helpers/nl_commands/welcome): the documented **`--noconftest` requirement** (`tests/conftest.py` eager-loads `app`, which shadows the pipeline mocks under a full `pytest tests/` run — see §14 "Pipeline tests require `--noconftest`"). **FIXED (§17.807 follow-up):** `make test` now `--ignore-glob='*/test_scaffold_router_*'` (excludes the lane), and a new **`make test-pipelines`** runs all 43 files with `--noconftest` → **1175 passed in ~19 s**; `make test-all` runs both lanes. Reclaims all 20.
- **99 errors + 2 failures — app-lifespan-under-TestClient async-teardown harness issue** (`test_web_ui` 75 err + 1 fail, `test_web_pages` 15 err + 1 fail, `test_web_node_actions` 7 err, `test_cost_rollup` 2 err): spinning up the app lifespan under Starlette's `TestClient` runs the async migration runner on an `anyio` BlockingPortal whose asyncpg connection lands on a **different event loop** → `migrations_hook_crashed: … attached to a different loop` (202 log lines) and `RuntimeWarning: coroutine 'Connection._cancel' was never awaited` at teardown. Pre-existing test-infra problem (reproduced with §17.807 stashed), independent of feature code.
- **3 failures — tests assumed code-default valve state, but `make test` runs *inside the live orchestrator*** where those valves are ON. `test_cite_aware_summary_17799` ×2 rely on the default of `citation_faithfulness_check_enabled` (compose has `CITATION_FAITHFULNESS_CHECK_ENABLED=true`, §17.798–800). `test_scheduler::test_init_starts_scheduler_and_rehydrates` asserted `add_job.call_count == 4` but got **5** — NOT a `SCHEDULER_ENABLED` issue (as first mis-categorized) but the §17.803 `x_model_role_learning` job, which registers when `model_role_learning_enabled` is ON (compose has `MODEL_ROLE_LEARNING_ENABLED=true` from the §17.806 rollout). **FIXED (§17.807 follow-up) *hermetically*, NOT via a dev-override env pin:** pinning these valves OFF in `docker-compose.dev.yml` would disable citation-faithfulness AND role-learning on *this host's live system* (the dev override IS the live runtime here — `PARALLEL_EXECUTION_ENABLED=false` proves it applies), features the operator deliberately enabled. Instead each test now `monkeypatch.setattr(..., False)` the specific valve it depends on (mirroring the sibling `…_when_enabled` tests) — deterministic regardless of ambient env, zero runtime impact. Reclaims all 3.
- **1 failure — live-eval quality gate**: `integration/test_retrieval_eval::test_citation_faithfulness_gate` (`assert 0.333 >= 0.55` on a live citation-faithfulness score) — a corpus/model-variance environmental gate, same class as the documented §17.595 corpus-seed pattern.

**§17.807 follow-up — 23 of 26 failures reclaimed (post-fix sweep measured 2026-08-16).** The pipeline lane (20) and the ambient-valve tests (3) are fixed as described above. **Measured post-fix — `make test` (core lane, pipeline excluded): 4228 passed, 1 skipped, 3 failed, 99 errors in 19:28; `make test-pipelines` (lane): 1175 passed in 18 s; combined `make test-all`: 5403 passed, 1 skipped, 3 failed, 99 errors.** (This is the new authoritative baseline; the 5380/26/99 figure above is the pre-fix single-`pytest tests/` run that motivated the lane split.) **The remaining 3 failures + 99 errors are two pre-existing causes, neither a code regression:** (a) the app-lifespan-under-`TestClient` async-teardown harness issue — `test_web_ui::TestAuthBypass` + `test_web_pages::test_web_research_detail_renders` (2 fail) and the 99 setup-errors across `test_web_ui`(75)/`test_web_pages`(15)/`test_web_node_actions`(7)/`test_cost_rollup`(2), where the migration runner on a BlockingPortal hits a different event loop; and (b) the 1 live-eval `test_retrieval_eval::test_citation_faithfulness_gate` quality gate (corpus/model variance). Fixing the `TestClient` lifespan teardown is the one remaining item for a fully clean sweep. None block anything. (SDK/CLI unaffected: `make test-sdk` / `make test-cli` are separate suites.)

**§17.808 update — core lane now CLEAN.** The `TestClient` teardown was fixed (see §17.808 in the running log: MCP session-manager once-per-instance re-entry + DB pool cross-loop under NullPool). Re-measured **`make test` (core lane): 4330 passed, 1 skipped, 0 failed, 0 errors in 18:51** — the 99 errors + 2 web failures are gone (the `test_retrieval_eval` live-eval gate also passed this run, though it stays variance-prone). Combined **`make test-all` ≈ 5505 passed, 1 skipped, 0 failed** (core 4330 + pipeline 1175). The 1 skip is the documented live-LLM model-variance self-skip in `test_topology_select_db.py`. This is the new authoritative baseline.

**Prior baseline (`make test`, full dev-image suite):** **4199 passed, 1 skipped, 0 failed in 19:40** — measured 2026-07-18 after §17.625 (assist finalizes a job parked in `awaiting_assist`: add the status to `start_assist_session`'s transition IN-list so the walkthrough reaches `completed`), on the python-3.14.6 + pymilvus-3.0 + MilvusClient image. **+1 regression test** over the §17.624 4198 baseline (`test_full_walkthrough_from_awaiting_assist` integration; the unit awaiting_assist-start test gained a transition-SQL assertion). The 1 skip is the documented **live-LLM model-variance self-skip** in `tests/integration/test_topology_select_db.py`. Prior baseline: **4198 passed, 1 skipped, 0 failed in 19:18** — measured 2026-07-18 after §17.624 (hands-on assist gate: park predominantly-Shell/human DAGs in `awaiting_assist` instead of fabricating `completed`), on the python-3.14.6 + pymilvus-3.0 + MilvusClient image. **+11 regression tests** over the §17.623 4187 baseline (`test_hands_on_assist_gate.py` ×8; assist-start-on-awaiting_assist ×1; umbrella awaiting_assist propagation ×1; recovery per-status parametrize picks up `awaiting_assist` ×1 — plus the recovery-registry / `StatusCounts` / `_rollup_umbrella` / SSE-mock updates the new status forced, all net-zero). The 1 skip is the documented **live-LLM model-variance self-skip** in `tests/integration/test_topology_select_db.py`. Prior baseline: **4187 passed, 1 skipped, 0 failed in 18:55** — measured 2026-07-18 after §17.623 (`/assist` re-opens a `completed`/`cancelled` job for a hands-on redo instead of a dead-end 409), on the python-3.14.6 + pymilvus-3.0 + MilvusClient image. **+1 regression test** over the §17.619–622 4186 baseline (`test_start_session_reopens_completed_job`; `test_start_session_rejects_invalid_status` repurposed to `researching`, net +1). The 1 skip is the documented **live-LLM model-variance self-skip** in `tests/integration/test_topology_select_db.py`. Prior baseline: **4186 passed, 1 skipped, 0 failed in 20:32** — measured 2026-07-18 after §17.619–622 (all four 2026-07-18 audit deferrals resolved + live-verified: #12 compile-synthesis connection release, #32 fetch_cache DBSIZE on dedicated Redis DB, #20 assist handoff_policy auto-handoff, #16 StackOverflow disputed-claim ingest), on the python-3.14.6 + pymilvus-3.0 + MilvusClient image. **+4 regression tests** over the §17.618 4182 baseline (0 failures): +1 compile-synthesis commit-ordering, +4 assist handoff-policy trigger, +1 SO disputed-claim, −2 net from the fetch_cache cardinality-test rewrite (SCAN→DBSIZE mocks). The 2026-07-18 multi-agent audit is now **fully closed** — all 40 confirmed findings fixed, 0 deferred, #41 reviewed-intentional. The 1 skip is the documented **live-LLM model-variance self-skip** in `tests/integration/test_topology_select_db.py`. Prior baseline: **4182 passed, 1 skipped, 0 failed in 20:43** — measured 2026-07-18 after §17.608–618 (the 2026-07-18 multi-agent improvement audit: 34 confirmed fixes + #42 doc-drift across 11 commits — rerank cap, SSE lifecycle, cloud retry/backoff, 11 router quick-wins, SSRF drift, defensive-guard cluster, node prompt-edit, node/execution reliability, RAG/provenance batching, wired half-wired features), on the python-3.14.6 + pymilvus-3.0 + MilvusClient image. **+22 regression tests** over the §17.607 4160 baseline, 0 failures — **this closes the 2026-07-18 multi-agent audit** (§17.618; 4 findings deferred #12/16/20/32, #41 reviewed-intentional). The 1 skip is the documented **live-LLM model-variance self-skip** in `tests/integration/test_topology_select_db.py` (topology-select 409 empty-RAG/citation draw — re-runs green). Prior baseline (`make test`, full dev-image suite): **4160 passed, 1 skipped, 0 failed in 19:52** — measured 2026-07-18 after §17.601–607 (the 16 Low whole-repo-audit fixes: llm_parsing raw-first parse, cancellation shields, /config + /health robustness, cache-key/pagination/TTL, SSE-frame + triage think-strip, classify budget + human-node count + to_milvus key, SDK ConflictError + CLI dead-ref), on the python-3.14.6 + pymilvus-3.0 + MilvusClient image. **+18 regression tests** over the §17.600 4142 baseline, 0 failures — **this closes the full 2026-07-17 whole-repo audit** (3 High §17.594 + 14 Medium §17.596–600 + 16 Low §17.601–607, all fixed with tests). The 1 skip is the documented **live-LLM model-variance self-skip** in `tests/integration/test_topology_select_db.py` (topology-select 409 empty-RAG/citation draw — re-runs green). (SDK/CLI Low fixes verified separately: `make test-sdk` 143 passed, `make test-cli` 165 passed.) Prior baseline (`make test`, full dev-image suite): **4142 passed, 1 skipped, 0 failed in 19:11** — measured 2026-07-18 after §17.596–600 (the 14 Medium whole-repo-audit fixes: auth/config, event-loop-blocking I/O, transaction/cancellation durability, assist/handoff, data provenance), on the python-3.14.6 + pymilvus-3.0 + MilvusClient image. **+17 regression tests** over the §17.595 4125 baseline, 0 failures — no cross-module regression from the 14 fixes across ~15 files. Prior baseline (`make test`, full dev-image suite): **4125 passed, 1 skipped, 0 failed in 19:21** — measured 2026-07-17 after §17.594 (3 High audit-fixes + the `assist_handoff_noop` SSE-inventory registration) and §17.595 (golden-corpus re-seed), on the python-3.14.6 + pymilvus-3.0 + MilvusClient image. **+5 tests** over the prior 4121-test total (the §17.594 regression tests). The 1 skip is the documented **live-LLM model-variance self-skip** in `tests/integration/test_topology_select_db.py` (the topology-select draw returned a 409 empty-RAG/citation result — re-runs green). An intermediate 2026-07-17 run measured **4119/1/6** BEFORE §17.595: all 6 failures were `test_retrieval_golden`×5 + `test_retrieval_eval_gate`, an **environmental corpus-seed gap** (the local `toon_v2` had decayed to `eng`1937+`rag`362 only — the `prompt`/`llm`/`spec` golden domains held zero entries via the documented `compose down` corpus-loss pattern), **NOT a code regression** (§17.594 never touches the retrieval path; a Milvus `restart` confirmed the engine served eng/rag goldens). §17.595 re-seeded the 5 curated golden docs (`scripts/seed_corpus_remainder.py`, now covering all five — corpus `eng`1937/`rag`363/`prompt`2/`llm`1/`spec`1 = 2304) and closed all 6 → the 4125/1/0 green run above. Prior baseline (`make test`, full dev-image suite): **4120 passed, 1 skipped, 0 failed in 26:47** — measured 2026-07-13 on the rebuilt **python-3.14.6 + pymilvus-3.0 + MilvusClient** image after §17.591–593. Same 4121-test suite as the prior baseline (no tests removed); the 1 skip is the documented **live-LLM model-variance self-skip** in `tests/integration/test_topology_select_db.py` (the topology-select model draw returned a 409 hallucinated-citation, so the test skips rather than fails — re-runs green). `/health` green under 3.14 (Postgres/Milvus/Redis/Ollama all up). Prior baseline **4121 passed, 0 failed in ~33:36** — measured 2026-07-06 after the §17.580–585 arc (feasibility/compile native tool-calling + the retry-into-`model_router.tool_call` refactor + the fastapi/starlette/otel security upgrade + regression tests), on the pre-migration python-3.12 / pymilvus-2.5.18 stack. The §17.591 MilvusClient migration re-shaped ~15 test files but netted 0 test-count delta (API port, not new coverage); §17.592–593 are deps/Dockerfile only. This supersedes the stale §17.540 baseline below; the +150 over 3971 is the cumulative §17.541–585 feature/test growth (per-commit deltas in the §-log). Prior baseline **3971 passed, 0 failed, 0 skipped in 26:22** — measured 2026-06-17 after §17.539–540 (+11 over §17.538's 3960: +10 `test_scaffold_router_assist_chat_routing.py` history-based recovery [§17.539], +1 `test_assist_lazy_imports.py` stale-import guard [§17.540]). Fully clean, 0 skips. Prior baseline **3960 passed, 0 failed, 0 skipped in 23:46** — measured 2026-06-17 after §17.538 (+4 over §17.537's 3956, all from the §17.538 durable-chatmap fix: +4 `test_assist_chatmap_status.py` PUT-durable-write + GET-PG-recovery cases; migration 054 adds no net tests). Fully clean, 0 skips. Prior baseline **3956 passed, 0 failed, 0 skipped in 28:20** — measured 2026-06-16 after §17.537 (+19 over §17.533's 3937, all from the §17.537 assist-aware chat routing fix: +15 `test_scaffold_router_assist_chat_routing.py` + 4 `test_assist_chatmap_status.py`; §17.534–536 were CI/migration/compose changes with no `tests/` delta). Fully clean, 0 skips. Prior baseline **3937 passed, 0 failed, 0 skipped in 26:38** — measured 2026-06-15 after §17.533 (+34 over §17.521's 3903, all from the §17.522–533 distill-fix + task-decomposition arc: §17.522 un-skipped 12 silently-skipped Phase-2 tests + added distill-route assertions, §17.523 +4 triage Components, §17.524 +4 quick_research, §17.526 +7 decomposition, §17.528 +2 umbrella results, §17.529 +0 [recovery coverage existing], §17.530 +3 resurrection guards, §17.531 +3 security guards, §17.532 +4 reaper/render, §17.533 +4 umbrella compile — net reconciles after the §17.527 cleanup-test rename and the Phase-2 un-skip). Fully clean, 0 skips. Prior baseline **3903 passed, 0 failed, 0 skipped in 22:11** — measured 2026-06-14 after §17.521 (+3 net over §17.520's 3900: +2 `test_assist_agent` non-UUID guard, +2 `test_scaffold_router_assist_status` non-UUID, −1 from start-path fixture consolidation). Clean re-run after fixing the 5 start-path tests whose non-UUID fixture ids the §17.521 guard correctly rejected. Prior baseline **3900 passing in 20:48** — measured 2026-06-14 after §17.520 (+4 `test_scaffold_router_assist_status.py` over §17.519's 3896). The run reported 3899 passed / 1 failed: the single failure was `test_codegen_golden_live[dataclass-module]`, a **live-LLM model-variance straggler** (unrelated to §17.520's pipeline-side `/assist status` + triage-wording changes) — **passes on retry** (re-ran green in 59.86s). The §17.488/489 `chat_until_nonempty` guards absorb most such draws; the codegen-golden goldens have no equivalent single-draw guard, so a rare bad draw can still surface. Prior baseline **3896 passed, 0 failed, 0 skipped in 23:40** — measured 2026-06-14 after §17.519 (+5: `compute_deliverable_kind` ×4 + `/logs` deliverable_kind surfacing ×1; the `test_status_logs` `_make_row` default + migration 052 add no net tests). Prior baseline **3891 passed, 0 failed, 0 skipped in 21:25** — measured 2026-06-14 after §17.517–518 (+2 over §17.516's 3889: `test_execution_agent_prompt_build.py::TestGroundingDomainFanout` for §17.517; §17.518 is a flake-fix, no net new tests). §17.518 eliminated the banner-test live-synthesis timeout that flaked the two prior full runs at 3889/2. Prior baseline **3889 passed, 0 failed, 0 skipped in 22:53** — measured 2026-06-14 after §17.514–516 (+9 over §17.513's 3880: §17.514 +2 tuple regressions in `test_execution_codegen_verify.py`, §17.515 +2 domain-enum guards in `test_idea_refinement.py`, §17.516 +5 assist-completed banner in `test_compile_plan_only_banner.py`). These were the pre-deployment dogfood fixes (autonomous CodeGen crash, eng_design misroute, assist-completion summary). Prior baseline **3880 passed, 0 failed, 0 skipped in 22:31** — measured 2026-06-14 after §17.506–513 (+18 over §17.504's 3862: §17.506 +7 `test_compile_plan_only_banner.py`, §17.507 +3 `TestLongNameCoercion`, §17.508/509/512 +8 `test_scaffold_router_audit_fixes.py`+`test_research_summary_antibleed.py`; §17.510/511/513 no net new beyond those). §17.513 made the §17.506 banner integration tests deterministic (disable synthesis) — two prior full runs flaked 3879/1 on them before the fix. Prior baseline **3862** measured 2026-06-14 after §17.504 (+16 `test_scaffold_router_assist_nudge.py`). Prior baseline **3846** measured 2026-06-13 after §17.501–503 (+6 over §17.500's 3840: +3 `test_research_domain_detection.py` for §17.501, +3 `test_research_searxng_engines.py` for §17.503; §17.502 + §17.504 are pipeline-side — §17.502 has no `tests/` delta, §17.504 adds the nudge tests). Prior baseline **3840** measured 2026-06-13 after §17.500 (+5 deep-research tests over §17.499's 3835; §17.499 +10 verbosity; §17.498 model_coder swap no-test-delta; §17.497/495–496's 3825 base). Fully clean (0 skips). Lineage from §17.494 **3810** → §17.495/496 model A/B harness (+13 `test_model_ab.py`, scripts-only) → §17.497 codegen exec-gate fix (+2 `test_codegen_exec_smoke.py`) = **3825**. Earlier per-§ lineage: §17.487 **3764** → §17.488 **3766** → §17.489 **3769** → §17.490 **3779** → §17.491 **3787** → §17.492 **3795** → §17.493 **3807** → §17.494 **3810**. Per-§ measured lineage: §17.488 **3766** → §17.489 **3769** → §17.490 **3779** (+10 auto-learn) → §17.491 **3787** (+8 sandbox-verify) → §17.492 **3795** (+8 destructive-gate) → §17.493 **3807** (+12 streaming) → §17.494 **3810** (+3 sim-stage empty-draw redraw tests). Both chronic live-LLM stragglers stay green via the §17.488/§17.489 `chat_until_nonempty` guards. Wall-clock dominated by live cloud-model latency on the integration tests. (Live integration tests remain subject to model variance — a sustained empty-response spell could still surface, but the empty-guard now absorbs single bad draws across the whole sim pipeline.)

**CI baseline (`test.yml` · Unit Tests, `-k "not integration"` minus the 3 service-dependent modules):** **3975 passed, 14 skipped, 94 deselected (integration) — coverage 79.51% (gate 77%)** — measured 2026-07-07 on a fresh PR (#105), on the pre-migration python-3.12 stack. **Post-§17.591–593 the stack moved to python 3.14 / pymilvus 3.0 / MilvusClient**; the Unit Tests job re-ran green on #107 (pymilvus 3.0) and #74 (python 3.14) — each **built the real new-stack image and passed the full non-integration suite** — but those runs' exact pass/coverage numbers weren't recaptured here (log retention), so the precise count above predates the stack change and will refresh on the next measured PR. GitHub Actions **runs again** (the earlier billing block is cleared): pip-audit, Tier-1 Smoke, and Unit Tests all gate PRs and pass. Since §17.588/589 this job **pulls** the prebuilt `scaffold-orchestrator-ci` image (built on main) instead of building it per-PR, and `--ignore`s `test_execution_agent_{sse,concurrency}` + `test_sse_streaming` (they need live Redis/Milvus/Ollama this no-services job lacks; they run in `make test` + the nightly full-stack). **Tier-1 Smoke (`ci.yml`, `-m smoke`):** **2044 passed, 2 skipped, 1396 deselected** (same #105 run).

- **SDK (`make test-sdk`)**: **142 passed** in 2.2s (unchanged since §17.421).
- **CLI (`make test-cli`)**: **165 passed** in 1.2s (unchanged since §17.427→§17.462).

The CI figure is the rolling snapshot refreshed each run; the local figures are refreshed by a `make test` sweep. Per-commit deltas live in the §-log below. The static-parity subset (schemas / sse-events / next-actions vendor byte-equality + the SSE-inventory and SDK-schema scans) now also runs at push time via the §17.393 `ci-tier-0` pre-push hook (`make hooks-install` to activate per clone).

### 14.2 Markers

| Marker | Meaning | Time |
|---|---|---|
| `smoke` | Fast unit / extraction pipeline | <2 min |
| `validate` | Integration — needs API + reranker + verifier | <15 min |

### 14.3 CI tiers

The real CI surface is **three workflows / four jobs** — the old 3-tier `make` model here was stale: `make ci-local` and `make ci-eval` **never existed** (the names predate §17.247's `ci-tier-2` and §17.358's retirement of `make eval`), and "24 tests / <30s" was off by ~70× (refreshed §17.398).

| Workflow · job | What it runs | Trigger | Runner | Status |
|---|---|---|---|---|
| `ci.yml` · **smoke** | §17.175 OpenAPI gate → `make ci-tier-0` (§17.393/§17.395 static-parity: 4 vendor byte-equal gates + SSE-inventory + SDK-schema scans) → `make ci-smoke` (**2044 `-m smoke` passed, 2 skipped**, #105) | every push & `pull_request:[main]` | `ubuntu-latest` | green, ~12 min — a PR gate |
| `ci.yml` · **pip-audit** | `pip-audit --strict -r` on all 3 req files (§17.585 — torch's unfixable PYSEC-2026-139/CVE-2025-3000 `--ignore-vuln`'d with rationale) | every push & `pull_request:[main]` | `ubuntu-latest` | green, ~1 min |
| `ci.yml` · **integration** | `make ci-tier-2` — `/health` + `make doctor` + golden-retrieval sidecar + bench gates | push to `main`, **gated** `vars.RUN_TIER2_INTEGRATION=='true'` | self-hosted | **skipped** by default (§17.397 — host runner is bound to a different repo) |
| `test.yml` · **unit-tests** | §17.588/589 — **pulls** the prebuilt `scaffold-orchestrator-ci` image (built on main by `build-ci-image.yml`), mounts the PR's source over it, runs `pytest tests/ -k "not integration"` minus the 3 service-dependent modules + coverage gate. Dep-change PRs fall back to a local build. | push & `pull_request:[main]` | `ubuntu-latest` + postgres svc | **green: 3975 passed, 14 skipped, 94 deselected, coverage 79.51% (gate 77) in ~24 min** pytest / ~31 min job (#105). cap 50 min (ceiling for the rare build fallback). The heavy torch+reranker build moved off the PR path to `build-ci-image.yml` on main — see #99/#102. |
| `retrieval-quality.yml` · **score** | `pytest test_score_retrieval.py test_rag_pipeline_smoke.py` (recall@k / MRR math + 3-query fusion smoke) | PR touching `rag_pipeline.py` / `rerankers.py` / `golden_set.json` | `ubuntu-latest` | non-blocking (`continue-on-error`) |

**Cloud-safe (`ubuntu-latest`):** `smoke` needs no live services (`ci-tier-0` + `ci-smoke` are static / no-stack); `test.yml` provisions only a Postgres service (no Milvus/Ollama).

**Self-hosted only:** tier 2 (`make ci-tier-2`) needs the full live compose stack — Milvus standalone wants 8+ GB (free runners cap at 7) and Ollama CPU inference exceeds cloud timeouts. Gated off by default (§17.397).

### 14.4 Pipeline-test caveat

Pipeline tests require `--noconftest` because `tests/conftest.py` eager-loads `app`. Container test path is `/code/tests/`. Don't run pytest from the host against the orchestrator's modules — env diverges (no Milvus, no Postgres).

### 14.5 Make targets

| Target | Effect |
|---|---|
| `make test` | Full orchestrator suite (in-container) |
| `make test-sdk` | SDK suite (`/code/sdk/tests/`) |
| `make test-cli` | CLI suite (`/code/cli/tests/`) |
| `make ci` | CI-safe tests, dev image (no live deps) + bench gates |
| `make ci-smoke` | Cloud-safe smoke (`-m smoke`, host pytest) — `ci.yml` tier 1 |
| `make ci-tier-0` | §17.393 static-parity gate (no services, ~2s) — pre-push hook + PR gate |
| `make ci-tier-2` | §17.247 full-stack integration (needs the live compose stack) |
| `make hooks-install` | §17.393 — activate `.githooks` pre-push hook (run once per clone) |
| `make agent` | Smoke-marked execution-agent tests |
| `make bench` | Performance benchmark suite |
| `make health` / `status` / `clean` | Hit `/health`, `/status`, `/jobs/cleanup` via curl |
| `make migrate` | Force-run migrations inside container |
| `make logs` / `logs-follow` | Container logs |
| `make dev-up` | Bring up dev compose overlay |
| `make build` / `restart` | Rebuild orchestrator / restart only |
| `make bootstrap` | First-time setup wizard |
| `make doctor` | Health audit (every dep + key sync) |
| `make init` | Provider/model wizard |
| `make sync-valves` | Wipe baked-in `api_key` from `pipelines/*/valves.json` |
| `make sync-schemas` | Refresh `sdk/scaffold_client/schemas.py` from `app/schemas.py` |
| `make reindex` | Re-embed the toon_v2 corpus |
| `make openapi-snapshot` | Regenerate `docs/openapi.json` from the live FastAPI app |
| `make openapi-check` | Verify snapshot matches the live spec (CI gate) |
| `make help` | Show all targets |

---

## 15. Conventions and invariants

> Violating these produces silent breakage. Read before any non-trivial change.

1. **Migrations.** `db/init.sql` is the post-migration-025 baseline. Never edit retroactively. Schema changes go in `db/migrations/NNN_*.sql` only. Runner (`app/migrations.py`) auto-applies at lifespan startup. Opt out with `SCAFFOLD_RUN_MIGRATIONS_ON_STARTUP=false`.

2. **Logger identity.** Stdlib `logging` is the runtime logger (`logger = logging.getLogger("scaffold.<sub>")`). `structlog` is the **formatter only**, wired in `app/logging_config.py`. Don't import `structlog.get_logger` in module code — that diverges from the unified output stack. (Audit found 5 violations as of 2026-05-05; staged for cleanup.)

3. **Middleware order.** Declared in `app/main.py` as `ErrorLogging → Performance → RequestId`. Starlette runs middleware in **reverse-add order**, so the runtime stack from outermost is `RequestId → Performance → ErrorLogging`. `request_id` must bind first so every downstream log line carries it.

4. **Async-first.** Every I/O call is async. Blocking libs (PyMilvus, CrossEncoder, pypdf, pdfplumber, trafilatura) get wrapped in `asyncio.to_thread` or `loop.run_in_executor`. Don't introduce sync I/O in a request path — it deadlocks the event loop under load.

5. **Pinned everything.** All pip deps in `requirements*.txt`, all Docker images by SHA256 digest in compose. New deps need versions, new images need digests.

6. **Embedder + reranker are config-only.** `MODEL_EMBEDDER_PIPELINE` and `MODEL_RERANKER` cannot be swapped per-request — embedding dim is locked at 512 and the reranker is a CrossEncoder singleton. The other 7 roles are valve-switchable.

7. **HTTP client pool reuse.** Shared httpx clients live in `app.utils.http_clients` (eager init at lifespan). Don't construct ad-hoc `httpx.AsyncClient` inside request paths. (Audit found `model_router.py` and `_check_ollama` violate this; staged.)

8. **Tests in dev image only.** `make test` runs in the **dev** image. Pipeline tests require `--noconftest` (`tests/conftest.py` eager-loads `app`). Container test path is `/code/tests/`.

9. **API key sync (5 places).** `.env`, `pipelines/<each>/valves.json`, `~/.bashrc`, `scaffold-orchestrator` container env, `open-webui-pipelines` container env. After rotation, verify all five.

10. **OpenAPI stability.** `docs/openapi.json` is the v1.0.0 contract. `make openapi-check` enforces no silent drift. Breaking changes require: `app/main.py:FastAPI(version=...)` major bump + CHANGELOG entry + new snapshot.

11. **Vendored SDK schemas.** `sdk/scaffold_client/schemas.py` is byte-equal to `app/schemas.py`. `tests/test_sdk_schema_parity.py` enforces equality. After editing `app/schemas.py`, `make sync-schemas` regenerates.

12. **No down migrations.** All migrations are forward-only DDL. Production rollback requires manual SQL — deliberate posture.

13. **`apscheduler_jobs.next_run_time` type.** DOUBLE PRECISION — APScheduler's interpretation depends on the float layout. Don't alter.

14. **OWUI pipeline file rule.** OWUI Pipelines treats every `.py` under `/app/pipelines/` as a pipeline candidate. Per-pipeline helpers must be **inlined OR placed in `pipelines/_vendor/`** (§17.212 — the loader scan is non-recursive, so a subdirectory is invisible to it). A sibling `_helpers.py` under `pipelines/` gets auto-discovered and quarantined to `pipelines/failed/`; the `:ro` per-file bind mount does NOT prevent the rename — `os.rename` operates on the parent directory entry, which is RW.

15. **Pipeline auto-chain on `/confirm`.** Lives in `pipelines/scaffold_router.py`, **not** in orchestrator endpoints. Curl-only paths bypass it.

16. **Non-root runtime posture (X.28).** Production containers run as non-root with `read_only: true` rootfs (orchestrator), `cap_drop: ALL`, and `no-new-privileges`. Orchestrator UID/GID is pinned to `10001` (`scaffold`); postgres `999:999`, redis `999:1000`, searxng `977:977`, pipelines `1000:1000` (host UID — required so `valves.json` writes from the OWUI valve UI land as the host user). `milvus` and `open-webui` keep image-default root because `cap_drop: ALL` breaks their entrypoints; they get `no-new-privileges` only. Pre-X.28 named volumes need a one-shot `bash scripts/chown_named_volumes.sh` (chowns `scaffold-engine_{hf-cache,scaffold-logs}` to `10001:10001` via a throwaway alpine sidecar) before the first non-root deploy, otherwise the orchestrator crash-loops with `PermissionError: '/var/log/scaffold/app.jsonl'`. The dev override (`docker-compose.dev.yml`) flips `user: 1000:1000` + `read_only: false` + `LOG_FILE: ""` (skips the `RotatingFileHandler` against the prod-owned logs volume), and the dev image stage carries an extra UID-1000 `useradd` so `pwd.getpwuid(1000)` (called by huggingface_hub during reranker load) doesn't raise `KeyError`.

---

## 16. Known issues

### 2026-07-17 whole-repo correctness audit — ✅ FULLY RESOLVED (fixed 2026-07-18)

A second full audit run as a **multi-agent workflow**: 66 agents — one correctness reviewer per subsystem (bootstrap, model_router, execution, research, rag, dag, assist, scheduler, ingest, routers, gt/prompt, migrations, caches, sim, observability, web, pipelines, cli/sdk) plus 6 cross-cutting invariant sweeps (stale pymilvus-ORM, multi-statement migrations, cancellation safety, sync-in-async, logger divergence, Milvus schema parity) — with **every finding adversarially re-verified** against the code (defaults to false-positive when unproven). **35 findings confirmed, 6 candidates refuted → 33 unique root causes, all fixed** with regression tests + dated §-entries. `make test` grew **4121 → 4160 (+39 tests)**, 0 failures; SDK 143 / CLI 165 green. Every fix pushed to `origin/main`, each push gated green by `make ci-tier-0`.

**3 High** (§17.594; §17.595 re-seeded the golden corpus to restore the suite):
- ✅ Assist `single`-mode handoff ran the ENTIRE remaining DAG (called unscoped `execute_all_nodes`) — now claims exactly the one node via `execute_next_node(preclaimed_node=…)`.
- ✅ `push_to_github=True` crashed GT extraction — the `bool` param shadowed the module-level `push_to_github` coroutine; call the `_push_to_github` alias.
- ✅ Blocking `pypdf` parse on the event loop in `fetch_arxiv_full` — routed through `research_extractors._bounded_extract` (thread + timeout).

**14 Medium** (§17.596–600):
- ✅ §17.596 auth/config — non-ASCII `X-API-Key` → 401 (was `TypeError`→500); `_is_cloud` matches `:cloud`; `validate_models` filters to ollama-provider roles.
- ✅ §17.597 event-loop I/O — SSRF-guard `getaddrinfo` and BM25 `describe_collection` probe moved off-loop (+ per-client BM25 memo).
- ✅ §17.598 transaction durability — `persist_job_artifacts` isolated in its own committed session; `alerts.emit` rolls back on INSERT failure.
- ✅ §17.599 assist/handoff — session finalized when a handoff completes the job; recovery links render the real `session_id` (not `job_id`).
- ✅ §17.600 provenance — snapshot serializes `source` (not `source_url`); `get_design_state` unions digital sizings; SO/HN URLs encoded; TOON append renumbers ids; `insert_node` re-opens a terminal job.

**16 Low** (§17.601–607):
- ✅ §17.601 `llm_parsing` — try `json.loads(raw)` first (think-tag/fence strip corrupted valid JSON string values).
- ✅ §17.602 cancellation shields — `research_and_compile` cancel-write and the scheduled model-A/B result-write.
- ✅ §17.603 `/config` resolves `default_factory` defaults; `/health` guards pg/ollama/milvus `BaseException`.
- ✅ §17.604 rag-cache key includes `domain_hint`; staleness sweep drops the unordered-`max(id)` cursor; HF card TTL keyed on ref immutability.
- ✅ §17.605 SSE fragment collapses newlines; triage strips `<think>` (shared helper).
- ✅ §17.606 `classify` gets a generous budget + empty-guard; skipped-human node marked `verified`; `to_milvus` emits `title`.
- ✅ §17.607 SDK gains `ConflictError` (409); CLI `_stream_research` uses the real `_stream` (dead `_aiter_sse` ref removed).
- ✅ §17.608 rerank cap mismatch fixed: `max_pairs` plumbed through so `RERANK_MAX_CANDIDATES > 20` no longer silently disables reranking (2026-07-18 audit #1).
- ✅ §17.609 web SSE lifecycle: terminal `close` frame + `sse-close` stops the reconnect-storm; heartbeats forwarded; stall counter uses true per-cycle time (2026-07-18 audit #2/#39/#9).
- ✅ §17.610 cloud role-routed calls get retry/backoff via `_retry_provider_call` (+529 retryable, regex classifier); native-tool telemetry recorded; Anthropic mid-stream errors propagate (2026-07-18 audit #3/#29/#38).
- ✅ §17.611 API/router quick-win cluster: /config int-redaction, /logs ordering, gt_list count, recent_jobs_costs window, artifact list projection, partial-results count, dead const, double-parse, dup query, node-action UUID guard, /health probe timeouts (2026-07-18 audit #4/5/8/10/17/18/21/22/23/37/40).
- ✅ §17.612 SSRF drift closed: topic-mode fetch + verify-recheck now route through / re-check the §17.93 hardened fetch guard (byte cap + post-redirect host check) (2026-07-18 audit #6).
- ✅ §17.613 sibling defensive-guard cluster: rm-gate anchoring, gt drift-filter, case-insensitive CodeGen/Shell, assist_done JSON guards, gap-analysis-failure retry, model_ab rowcount audit, deduped provenance warning (2026-07-18 audit #7/15/24/25/28/30/34).
- ✅ §17.614 node prompt edits honored: prompt_template (not optimized_prompt) is the editable+invalidating field (2026-07-18 audit #11).
- ✅ §17.615 node/execution cluster: topology-aware truncation, sizing convergence evidence guard, size stage_error on non-persist, async confirm off the threadpool, decompose TOCTOU advisory lock (2026-07-18 audit #14/26/27/35/36); #12 DB-session-across-LLM was deferred here, now RESOLVED in §17.619.
- ✅ §17.616 RAG/provenance batching: one batched exact-hash dedup query + multi-row provenance INSERT (2026-07-18 audit #31/33); #32 fetch_cache SCAN deferred (needs shared-Redis infra change).
- ✅ §17.617 wired half-wired features: JobSummary parent_job_id/component_index populated + status.py class rename, assist divergence_count surfaced (2026-07-18 audit #19/13); #16 SO disputed-claim + #20 handoff_policy deferred (unverifiable feature work).
- ✅ §17.618 CLOSEOUT of the 2026-07-18 multi-agent audit: 34 confirmed + #42 doc-drift resolved (§17.608–617), 4 deferred (#12/16/20/32), #41 reviewed-intentional.
- ✅ §17.619 resolved deferred #12: single `db.commit()` releases the pooled connection before compile-synthesis's LLM call (no session-across-LLM); live-LLM verified.
- ✅ §17.620 resolved deferred #32: fetch_cache counts via O(1) `DBSIZE` on a dedicated Redis DB (db1); live Redis verified (caught a `from_url(db=)` gotcha).
- ✅ §17.621 resolved deferred #20: assist `handoff_policy` auto values delegate to the autonomous executor on skip via a background handoff; live wiring verified.
- ✅ §17.622 resolved deferred #16: StackOverflow disputed-claim ingest fetches non-accepted answers; live StackExchange verified (disputed_claim now produced). **All four 2026-07-18 audit deferrals now closed; only #41 stands as reviewed-intentional.**
- ✅ §17.623 `/assist` on a `completed`/`cancelled` job now **re-opens** it for a hands-on redo (resets DAG nodes + assist steps to pending, archives the prior autonomous `compiled_output`, `reopened=true` banner) instead of the confusing "already completed" 409; fixes the user-reported "told me it was completed when it wasn't" on an autonomously-run home-lab component. Live-verified end-to-end.
- ✅ §17.624 **root-cause fix** for the same symptom: a new **hands-on assist gate** (`hands_on_assist_gate_enabled`, default on) parks a predominantly-Shell/human DAG in the new `awaiting_assist` status **before** executing — nodes stay pending, deliverable is the plan + run-`/assist` banner — instead of fabricating runbook "done" output and rolling up to a misleading `completed`. Migration 055; umbrella roll-up + assist-start + status rendering taught the new status. Live-verified: Proxmox component (5 Shell/8) parked with zero fabrication.
- ✅ §17.634 **OWUI background/task calls now bypass routing** (caught in a live OWUI browser test of §17.633). OWUI fires auto title/tag/follow-up generation through the same pipe per user message; §17.633's continuity path calls `assist_start` (a side effect), so those background calls **spuriously started assist sessions** on unrelated jobs. Fix: a guard at the top of `pipe()` detects `body.metadata.task` and short-circuits to a raw `_direct_completion` (no triage/assist/continuity/command routing) — OWUI still gets its title/tags, zero side effects. Live-verified (assist_start invocations = 0 on a title-gen call). +8 tests. Only a real browser test surfaced this — container tests call pipe() once per message and miss OWUI's fan-out.
- ✅ §17.633 **Cross-chat assist continuity — natural language reconnects to in-progress work from a NEW chat** (user, 3rd report: "still feels as though natural language isn't prevalent… i used a new chat"). Root cause: OWUI sends no chat_id AND a new chat has no session marker, so neither session-discovery path fired — every natural message fell to the triage planner even with a live Proxmox session (7 pending steps) available. Fix: `_reconnect_in_progress` (topic-match or resume-phrasing → resume via idempotent `assist_start`, which re-emits the marker so the new chat tracks it; ambiguous → pick-list; new idea → planner) + a first-turn in-progress banner (`assist_continuity_enabled`, default on). **Live-verified in a fresh chat**: "continue proxmox"/"what's next on proxmox" reconnect (step shown, no planner blocks); "where were we" → pick-list; a new idea → banner + planner. +26 unit tests.
- ✅ §17.632 **A/B'd the coder + general roles; swapped model_general → deepseek-v4-pro** (follow-on to §17.631). **Coder: no swap** — codegen A/B was a clean 24/24 tie across kimi/deepseek/glm-5.1/glm-5.2/minimax/gpt-oss; the ~0.7s speed spread is within noise and doesn't justify overriding kimi's proven cli-entrypoint faithfulness (the §17.572→575 lesson). **General: swapped** qwen3.5:397b→deepseek-v4-pro:cloud — qwen3.5 was 3.4× slower (19.2s vs 5.6s) at equal reliability (5/5 non-empty) and equal/better synthesis quality; gpt-oss:120b was faster still but emits U+2011 non-breaking hyphens (rejected). 3-site env sync (compose/config/valve); live-verified (clean content, no fallback). A maintain-or-better latency win on ideation/spec/assist-guide/compile.
- ✅ §17.631 **model_research_extract was pointed at a RETIRED model — re-A/B'd the live Ollama Cloud catalog** (found while confirming model-switching + quality). `qwen3-coder-next:cloud` was retired by Ollama Cloud 2026-07-15 (HTTP 410); the role had silently fallen back to kimi (~3/10 on the distill goldens — regressed to ~30% reliability). Confirmed the switch mechanism itself works (role→model resolution + live routing, no unwanted fallback), then pulled + A/B'd the current cloud line-up (deepseek-v4-pro/glm-5.1/glm-5.2/minimax-m3/gpt-oss/nemotron/kimi-k2.6). **glm-5.1:cloud** won a repeat=15 tie-breaker (30/30 reliability, 5.9s — fastest of the perfect-reliability models; glm-5.2 was *less* reliable at 28/30). Fixed `model_research_extract → glm-5.1:cloud` (single-site config); distill reliability ~30% → 100%. Live-verified; 147 config/model tests green. Verifier A/B reconfirmed kimi optimal (30/30 @ 1.7s).
- ✅ §17.630 **NL command router, Phase 3 — destructive deletes, always confirmed** (completes the §17.628 arc). Adds `jobs_delete`, `schedule_delete`, `research_delete` to the top-level NL router. Every delete resolves + echoes the named target in a stark "⚠️ Permanently delete this {job/schedule/research session}?" card and removes nothing without an affirmative follow-up — reusing the §17.629 `_NL_CONFIRM` machinery, firing the underlying handler with its own confirm token. Classifier 16→19 intents + `target_ref` slot; two new resolvers (`_resolve_schedule_ref`/`_resolve_research_ref`) on a generic `_resolve_named_ref`. +18 unit tests (108 total); live smoke 4/4 deletes correct, builds/reads unaffected. **Closes the engine-wide NL router**: reads, writes, and deletes all driveable by plain sentence — high-confidence intercept, triage default, confirms on everything expensive or destructive.
- ✅ §17.629 **NL command router, Phase 2 — mutating/expensive intents** (follow-on to §17.628). Adds writes to the top-level NL router: `research_topic`, `schedule_add`, `model_set`, `model_reset`, `optimize`, `jobs_rename`. The two that commit real cost (research 20–60 min, schedule recurring) render a **confirm card** with a hidden `<!--NL_CONFIRM-->` action marker and fire only on an affirmative "go"/"yes" follow-up; the cheap/reversible ones run directly. Classifier grew 10→16 intents + 8 slots and now distinguishes a single engine write ("set the *coder model* to kimi") from a multi-step build ("set up proxmox on my box" → `none`, planner). Also fixed a §17.628 bug where the `results` ambiguity reused the assist pick-list (a "1" reply would have started a session, not shown results) → now a plain marker-less disambiguation. +41 unit tests (90 total); live smoke 7/7 writes + 3/3 build-not-command correct; cron derived from "every monday at 9am"→`0 9 * * 1`. Phase 3 (destructive-with-confirm delete) deferred.
- ✅ §17.628 **Engine-wide natural-language command router, Phase 1** (follow-on to §17.626/§17.627; user: "optimize, improve and implement the workflow of all components. With the user using natural language to achieve it."). Extends NL from *inside an assist session* to the **top level**: a plain sentence with no active session now drives read-only components directly instead of always going to triage. New `command_guide.classify_command` (a `route_command` tool-call on kimi, fail-soft → `none`) behind `POST /route`; the pipeline fast-paths obvious phrases ("what's running", "list my jobs") with no LLM and otherwise classifies, **intercepting only on high confidence + a satisfied slot** (ambiguous/idea input → triage, never hijacked). Resolved intents translate to canonical slash strings run through the existing `_handle_command` (zero duplicated logic); `results <name>` reuses the §17.627 job-match + pick-list machinery. Phase-1 surface: status/results/rag_query/jobs_list/jobs_find/model_list/available/probe/help. +49 unit tests, live smoke 6/6 reads + 4/4 non-commands correct. Phases 2 (mutating/expensive) + 3 (destructive-with-confirm) deferred.
- ✅ §17.627 **Natural-language assist, drastically expanded to the whole engine** (follow-on to §17.626). Plain chat in a session now reaches every relevant component: **handoff** ("you do this one" / "you do the rest") hands a step (or the remainder) to the **autonomous executor** — which brings RAG grounding, the sim tools (ngspice/verilator/symbiyosys), the codegen sandbox, and the verifier; **ask** ("is ZFS safe on non-ECC?") runs a **RAG + web research** answer (Milvus corpus + SearXNG + cited synthesis); **status** / **explain_plan** ("where am I" / "show me the plan") render live progress + the full DAG; **set_env** / **set_verbosity** capture the operator's machine + detail level from plain sentences. The intent classifier grew from 7 → 13 intents; obvious phrasings are still fast-pathed with no LLM. Natural **start** got a stateful pick-list follow-up — after an ambiguous "which job?", a bare "1" / "the proxmox one" / "second" starts it (hidden ordered-id marker in history). Live-verified against the real Proxmox session: handoff/ask/status/explain_plan/set_env all classified correctly.
- ✅ §17.626 **Assist Mode is now understandable + conversational** (user-reported: "not giving simple copy-paste instructions… should operate with standard conversation and not require /assist"). Three fixes: (1) the walkthrough stopped **echoing its own section descriptions as headings** (`## Goal — one or two sentences: what this step produces…`) — the guide prompts now separate the literal heading from its meta-guidance with a "never copy these" rule; (2) `render_step` **leads with the plain-language title + walkthrough**, demoting engine jargon (node key/tool/domain/deps/raw prompt) to a muted subtitle + collapsed blocks, and moving the submit call-to-action to a natural "just tell me what happened" footer; (3) **full natural-language flow** — plain chat in an active session is classified into an intent (advance/skip/submit/fix/finalize/pause/question) and routed, obvious verbs matched deterministically (no LLM), and a natural sentence with no active session ("set up proxmox on the dual xeon box") maps to the matching job and **starts it** (ambiguous → pick-list; new idea → planner, never hijacked). Slash commands remain as aliases. Live-verified against the real Proxmox session: clean headings, and submit/question/fix all classified correctly.

**Process note:** mid-audit, running two full `make test` suites concurrently briefly stressed Milvus and produced spurious retrieval-golden failures — a diagnostic detour, not a code regression (the corpus gap was pre-existing; §17.595 fixed the real cause). One-command corpus-restore after a wipe: `docker exec scaffold-orchestrator python scripts/seed_corpus_remainder.py` (§17.595, seeds all 5 curated golden docs).

---

> Captured by the 2026-05-05 architecture audit (`review/*.md`, since absorbed here). 135 distinct findings across ~20k LOC. Each finding cites `file:line` for independent verification.
>
> **Re-verified against live code on 2026-05-07** (post-Sprint-J.1, commits `e6f318d` / `a409ea3` and following). Of the original 18 HIGH items: **15 are fully fixed**, 3 were retracted within the audit itself. **All 10 items in the original priority queue are fixed in code.** All 8 cross-cutting patterns A–H are now resolved. Each item below is marked with its verified status:
>
> - ✅ **FIXED** — code at the cited line shows the corrected pattern; verified by grep / source read on 2026-05-07
> - ⚠️ **PARTIAL** — addressed for the highest-blast-radius case but a related variant remains
> - 🟦 **RETRACTED** — the audit itself withdrew the finding after deeper analysis
> - 🟥 **OPEN** — still present in code as cited

### 16.1 HIGH severity (18 findings)

#### Tier 1 — production hot paths

1. ✅ **FIXED** — `app/migrations.py` (advisory lock scope). The apply loop is now inside the same `async with db.begin():` block as the advisory lock; per-file migrations run inside SAVEPOINTs (`db.begin_nested()`). Concurrent runners are correctly blocked. (Original audit cited L173-189.)

2. ✅ **FIXED** — `app/migrations.py` (BEGIN-in-asyncpg-txn defect). `_strip_outer_transaction(sql)` strips outer `BEGIN;…COMMIT;` from migration files before they execute inside the SAVEPOINT, so asyncpg never sees `BEGIN` inside an active transaction. (Original audit cited L138-147.)

3. ✅ **FIXED** — `app/scheduler.py:add_schedule` + `app/scheduler.py:delete_schedule` (state-ordering bug in both directions). Both functions now use **symmetric register-with-rollback**: `add_schedule` registers in APScheduler first, then writes `next_run_at` in the caller's session, with the in-memory job unregistered if anything raises after registration. `delete_schedule` reads the row up-front, unregisters APScheduler, deletes the DB row, and **re-registers from the captured row data** if the DB delete raises. (Original audit cited `app/scheduler.py:143-165` + `app/main.py:865-874`.)

4. ✅ **FIXED** — `app/modules/ideation_workflow.py` (mid-Phase-2 `db.close`). The offending `await db.close()` has been removed; `grep db.close` returns zero matches in the file. Session lifecycle restructured so Phase-2 I/O runs inside the same session that claimed the job. (Original audit cited L252.)

5. 🟦 **RETRACTED** — `app/modules/execution_agent.py:642` (failed nodes lose `optimized_prompt`). Audit's cluster D verification confirmed both timeout (L629) and general-exception (L642) paths pass `optimized_prompt=exec_prompt` to `_set_node_status`, which `COALESCE`s the value. Adjacent gap (was flagged for future work): prompt-build / RAG-injection unwrapped, so an exception there left the node `'running'` until the orphan reaper. **✅ CLOSED by Sprint W.4** — the whole assembly path (`_build_prompt` → RAG/SearXNG/Milvus injection → upstream stitching → optimize) at `app/modules/execution_agent.py:1017` is now wrapped in a `try/except build_exc` that calls `_set_node_status(..., "failed", verification_reason=…)` and returns a `failed` result, so an exception there marks the node `failed` (with retry-feedback reason populated) instead of stranding it `running`. Verified 2026-06-18.

#### Tier 2 — auditability / data-integrity gaps

6. ✅ **FIXED** — `app/modules/rag_pipeline.py` (version-chain entries skip `dedup_log`). Both branches now write to `dedup_log`: rejected duplicates with `action_taken='rejected'` and superseded entries with `action_taken='versioned'`. Invariant #9 satisfied. (Original audit cited L819-831; current implementation at L850-887.)

7. ✅ **FIXED** — `assist_steps.status='applied'` (dead enum value). Migration 024 dropped `'applied'` from the CHECK constraint.

8. ✅ **FIXED** — `app/utils/github_ingest.py` (`CancelledError` swallowed). The handler now explicitly checks `isinstance(item, asyncio.CancelledError)` and re-raises **before** the broader Exception branch, with a comment explaining `CancelledError` is a `BaseException` (not `Exception`) since Py3.8. Cancellation now propagates correctly. (Original audit cited L209.)

#### Tier 3 — invariant violations

9. ✅ **FIXED** — `app/modules/ideation_workflow.py` (logger identity). Module no longer imports `structlog`; uses stdlib `logging` only. (Original audit cited L46.)

10. ✅ **FIXED** — `app/model_router.py` (ad-hoc httpx). `_get_client()` now delegates to `app.utils.http_clients.get_ollama_client()` — explicitly documented as "delegates to the shared pool". (Original audit cited L36-38.)

11. ✅ **FIXED** — `app/main.py` lifespan (sync PyMilvus calls). Both `connect` and `disconnect` are now wrapped in `loop.run_in_executor(None, lambda: …)` with an explanatory comment. Event loop no longer blocks during the initial Milvus handshake. (Original audit cited L93-97, L172.)

#### Tier 4 — UX / OWUI integration

12. ✅ **FIXED** — auto-chain recovery is now a structured state-aware surface (audit item 10). New `app/modules/recovery.py` exposes a `NEXT_ACTIONS` registry mapping every `JobStatus` value to its set of valid next-step actions (`{action, command, endpoint, method, description, node_specific}`). `execution_handler.execution_status()` resolves the registry per-job — substituting concrete `job_id` and the most-relevant `node_key` (failed > blocked > running) — and returns the result as a `next_actions` field on `/exec/status/{job_id}`. The OWUI `_handle_results` and any SDK/CLI consumer renders the structured guidance instead of hardcoded recovery hints. (Original audit cited `pipelines/scaffold_router.py:893-938`.)

13. ✅ **FIXED** — `pipelines/{execution_handler,dag_viewer,gt_browser,prompt_inspector}.py` print-only API-key drift warnings. All 4 pipelines now have `_drift_hint()` methods that surface a markdown block on user-visible 401 errors (`execution_handler.py:208`, `dag_viewer.py:204`, `gt_browser.py:158`, `prompt_inspector.py:155`).

14. 🟦 **RETRACTED** — `pipelines/scaffold_router.py:1460` (120s SSE timeout aborts long streams). Audit cluster J verification: the 120s is a read-poll interval, not a stream-abort timeout. `requests.iter_lines()` raises `ReadTimeout` after 120s of no data; the handler catches it, increments idle counter, emits a heartbeat, and continues. Stream only declares stalled after `idle_seconds >= max_idle = max(300, 5*keep)`. Real adjacent issue: idle counter calibration (cycle is 120s wall, counter increments by `keep`=10s, so effective stall threshold is ~12× the apparent one). Counter calibration, not a HIGH.

15. 🟦 **RETRACTED** — `pipelines/*` bare `requests.get/post` (no module-level Session). Audit demoted to LOW: anti-pattern but not a measured perf issue at human-paced (~1 cmd/min) traffic. Refactor blocked by 36+ test patch sites.

#### Tier 5 — orchestrator bugs surfaced incidentally

16. ✅ **FIXED** — `app/main.py:/research/pdf` (`UploadFile.filename` None crash). Endpoint now has explicit `if not file.filename or not file.filename.lower().endswith(".pdf")` guard. (Original audit cited L740; current implementation at ~L791.)

17. ✅ **FIXED** — `GET /dag/{job_id}` 500 on bad UUID. Endpoint now wraps `UUID(job_id)` in try/except and raises `HTTPException(status_code=400, detail="Invalid job_id format")`. (Original audit cited L441-459.)

18. ✅ **FIXED** — `POST /execute` dict-error not converted to HTTPException. Endpoint now explicitly converts dict-error responses to `HTTPException`, with comment citing parity with `/ideas`, `/dag`, `/rag`. (Original audit cited L666-678; current implementation at ~L720.)

### 16.2 Cross-cutting patterns

#### Pattern A — Logger identity ✅ FIXED
All cited modules now use stdlib `logging.getLogger("scaffold.<sub>")`:
- `ideation_workflow.py` — no structlog import.
- `execution_handler.py:11` — `scaffold.execution_handler`.
- `prompt_optimizer.py:16` — `scaffold.prompt_optimizer`.
- `prompt_inspector.py:13` — `scaffold.prompt_inspector`.
- `prompt_assembly.py:32` — `scaffold.prompt_assembly`.

#### Pattern B — HTTP-client pool reuse ✅ FIXED
- `app/model_router.py` — delegates to `get_ollama_client()`.
- `app/main.py:_check_ollama` — also delegates to `get_ollama_client()` (verified 2026-05-07).
- 🟦 OWUI pipelines — retracted (LOW; bare `requests` was demoted in audit cluster K). Module-level `_HTTP_SESSION = requests.Session()` is now in place at `scaffold_router.py:36`, addressing the connection-reuse anti-pattern as well.

#### Pattern C — Postgres ↔ APScheduler ordering ✅ FIXED
Symmetric register-with-rollback in both directions — see HIGH #3.

#### Pattern D — Dead schema enum members ✅ FIXED
Migration 025 dropped `model_failure` and `structural` from `error_logs.error_type`. Migration 024 dropped `'applied'` from `assist_steps.status`. CHECK constraints now match what code actually writes.

#### Pattern E — Pydantic ↔ DB column name drift ✅ FIXED
The Pydantic fields are now named `metadata` directly (matching the DB column), so no alias is required. Verified at `schemas.py:91` (`JobBase.metadata`) and `schemas.py:244` (`ArtifactBase.metadata`).

#### Pattern F — Placeholder/UUID validation ✅ FIXED
- `/dag/{job_id}` GET — UUID validation, 400 on parse failure.
- `/research/pdf` filename — None-guard.
- Pipeline-side: `/confirm` checks `feedback` placeholder at `scaffold_router.py:944-957`; `/research/reply` checks `session_id` placeholder at `scaffold_router.py:879-885`.

#### Pattern G — Cancellation safety ✅ FIXED
- Research path — `_run_with_session_lifecycle` catches `CancelledError`.
- GitHub ingest — explicit `isinstance(item, asyncio.CancelledError): raise` before the broader Exception branch.
- Pipelines SSE — counter calibration was retracted; current `read_timeout = max(30, keep)` makes `idle_seconds += keep` count real wall-clock time.

#### Pattern H — Field-name dual-aliases in ingest ✅ FIXED
The TOON↔Milvus conversion is now centralized in the typed `IngestEntry` Pydantic model at `app/modules/_rag_entry.py`. Both short-name (TOON: `topic`/`content`/`tags`/`source`) and long-name (Milvus: `canonical_text`/`domain_tags`/`source_url`) inputs flow through `IngestEntry.from_input(...)`, which preserves the legacy first-non-empty-wins semantics. `rag_pipeline._normalize_entry()` is now a 2-line delegation. Round-trip is enforced via `from_milvus()` / `to_milvus()` constructors and a parity test. The conversion layer is preserved (TOON callers and Milvus rows still both work) but now exists in exactly one typed location instead of scattered `or`-chains. Tests at `tests/test_rag_entry.py` (17 tests).

### 16.3 Schema-side findings

- ✅ `db/init.sql` baseline currency — now post-025 (top-of-file comment confirms).
- 🟦 `db/init.sql:166-167` duplicate indexes also in migration 006 — `CREATE INDEX IF NOT EXISTS` makes this idempotent; documented as intentional.
- ✅ `db/migrations/020_research_sessions_single_running.sql` atomicity — fixed via `app/main.py::_pre_migration_sweep()`. The lifespan now runs an idempotent UPDATE that cancels any `'running'` rows older than 30 min before `run_migrations()` executes. Doubles as crash-recovery for any DB whose orchestrator died mid-execution. No-op on fresh DBs (table-existence check via `information_schema.tables` short-circuits before the UPDATE). Verified live on restart: cleared 1 stuck row from the running DB. Tests at `tests/test_pre_migration_sweep.py`.
- 🟦 `db/migrations/011_scheduled_jobs.sql:14` dead `NULL` in IN list — cosmetic; CHECK still admits NULLs via Postgres semantics. Migration 018 later wrote the correct form.
- 🟦 `app/schemas.py:JobStatus` Literal + `JOB_STATUSES` tuple drift risk — accepted (`JOB_STATUSES = get_args(JobStatus)` at `schemas.py:34` derives the tuple directly from the Literal, eliminating the manual-sync risk the audit flagged).

### 16.4 Remaining open items (post-verification, 2026-05-07)

The original priority queue had 10 items. **All 10 are now fully resolved** in code (commits below). The summary table:

| # | Item | Status |
|---|---|---|
| 1 | CLI `jobs status <id>` 404 bug | ✅ Fixed (`cli/scaffold_cli/main.py::jobs_status` now routes through `/exec/status/{id}`) |
| 2 | Pydantic `meta`↔`metadata` drift | ✅ Fixed (fields renamed to `metadata`) |
| 3 | Logger identity sweep | ✅ Fixed (all 4 modules use `scaffold.<sub>`) |
| 4 | `/health _check_ollama` shared client | ✅ Fixed (delegates to `get_ollama_client()`) |
| 5 | Pipeline placeholder checks | ✅ Fixed (both `/confirm` feedback and `/research/reply` session_id) |
| 6 | RAG dual-alias acceptance | ✅ Fixed — `IngestEntry` Pydantic model centralizes TOON↔Milvus conversion |
| 7 | Migration 020 atomicity | ✅ Fixed — `_pre_migration_sweep()` in lifespan; idempotent on every startup |
| 8 | Idle-counter calibration | ✅ Fixed (`read_timeout = max(30, keep)`) |
| 9 | Drift-hint surface to 4 pipelines | ✅ Fixed (`_drift_hint()` ported to all four) |
| 10 | Auto-chain recovery state machine | ✅ Fixed — `app/modules/recovery.py::NEXT_ACTIONS` registry resolved per-job by `execution_handler`; surfaced as `next_actions` on `/exec/status` |

**Summary:** of 18 original HIGH-severity findings, 15 are fully fixed in code, and 3 were retracted within the audit itself. All 8 cross-cutting patterns A–H are resolved. All 10 items in the original priority queue are fixed. The audit's coverage of the codebase as it stands today is **closed**.

### 16.5 Items NOT covered by the audit

- **Tests phase skipped** — no coverage matrix for `execution_agent`'s retry loop, `ideation_workflow`'s session-lifecycle, or `scheduler`'s misfire handling. The 14 pre-existing test failures in §14.1 are mock-side drift, not coverage gaps. → Partially closed in §17.55 (X.19 retry-loop matrix); live-Postgres concurrency tests still open.
- **Performance benchmarking** — likely PERF issues identified but not measured. → Closed in §17.57 (X.21 component benches) + §17.78 (I4 CI gates).
- **Observability completeness** — log-line fan-out, metric coverage, alerting hooks not audited beyond foundation middleware. → Closed in §17.56 (X.20 rollups) + §17.61 (X.26 Prometheus + push thresholds + OTel scaffolding).
- **Deployment surface** — Dockerfile, compose, `.env.example` not audited. → **Closed in §17.91** (formal closure entry: §17.62/64/65/66/67/68/70/77/78 contributors mapped + Dockerfile digest-pinned inline) and **§17.93** (SSRF guard + loopback-only port bindings).

### 16.6 Verification record (2026-05-07, post-`0c4cc12`)

End-to-end live verification against the running orchestrator after items 6 / 7 / 10 landed. All five layers green.

#### Layer 1 — system baselines

| Check | Result |
|---|---|
| 7 containers up | ✅ |
| `GET /health` overall | `healthy` (postgresql / ollama / milvus / redis all `up`) |
| OpenAPI snapshot | `docs/openapi.json` matches the live spec |
| Schema parity test | 2/2 |
| SDK suite | 88/88 |
| CLI suite | 38/38 |

#### Layer 2 — Item 7 (`_pre_migration_sweep`)

- First boot today (16:36 UTC): `startup_sweep_complete: stale_running_cleared=1` — cleared a genuine stuck `running` row from the live DB.
- Second boot (16:48 UTC): `startup_sweep_complete: stale_running_cleared=0` — confirmed idempotent.
- Unit tests: 4/4 pass.

#### Layer 3 — Item 10 (next-action registry)

Live registry probe across the statuses present in the DB at verification time:

| Job status | next_actions returned |
|---|---|
| `awaiting_confirmation` | `[confirm, delete]` ✅ |
| `failed` | `[retry_node, skip_node, delete]` ✅ |

Concrete-substitution check on a `failed` job (`c8da8c9f-…`): the registry filled in the actual `job_id` and selected `node_key=T2` via the helper's blocked-node fallback (no `dag_node` had `status='failed'`, so the heuristic walked failed → blocked → running and picked the first `pending` node with `deps_met=False`).

- Recovery tests: 25/25.
- `execution_handler.execution_status` next_actions tests: 4/4.
- Existing module tests still pass: 5/5.

#### Layer 4 — Item 6 (`IngestEntry`)

`_normalize_entry` delegation verified via live import inside the orchestrator container:

| Input shape | Result |
|---|---|
| TOON short keys (`topic`/`content`/`tags`/`source`) | Correct canonical dict |
| Milvus long keys (`canonical_text`/`domain_tags`/`source_url`) | Correct canonical dict |
| `{"content": "", "canonical_text": "falls through"}` | `content` resolves to `"falls through"` ✅ (preserves legacy `or`-chain semantics that Pydantic's stock `AliasChoices` would silently break) |

- IngestEntry tests: 17/17.
- rag_pipeline tests: 25/25.
- research_agent_ingestion tests: 4/4.

#### Layer 5 — SDK end-to-end

```
sync probe | job_id=481010cd... status=awaiting_confirmation
  next_actions field present: True
    - confirm      | /confirm 481010cd-9542-4b27-9af3-7c80f468af89
    - delete       | (no command)
async probe | health=healthy total_jobs=116
```

Both `Client.jobs.status()` (sync) and `AsyncClient.health()/status()` (async) deliver the new `next_actions` field through to a v1.0.0 SDK consumer with concrete commands.

#### Closure

All 18 original HIGH-severity findings: 15 fixed in code + 3 retracted within the audit. All 10 priority-queue items: fixed in code. All 8 cross-cutting patterns A–H: resolved. Audit coverage of the codebase as it stands today is **closed**.

### 16.7 Post-audit live findings

Discovered during live verification of later work, outside the 2026-05-05 audit scope.

1. ✅ **FIXED (§17.544, 2026-06-18)** — `app/model_router.py:273` (embedding calls got a non-embedding fallback). `_dispatch_with_retry` does `fallback = fallback or _smart_fallback(model, settings.model_fallback)`, which **silently discards the embed callers' explicit `fallback=None`** — both `app/providers/ollama.py:139` and `app/model_router.py:693` pass it deliberately to mean "embeddings have no fallback." The injected `settings.model_fallback` (`qwen3.5:latest`) is a chat model that returns `HTTP 501 "this model does not support embeddings"` on `/api/embed`. **Effect:** when a `/api/embed` call to `nomic-embed-text` fails — e.g. `HTTP 400 "input length exceeds context length"` on an over-long chunk — the router burns a guaranteed-doomed fallback round-trip (501, ~14s on the live smoke) before failing, adding latency + error-log noise. Observed live during the §17.543 research-ingest smoke (request_id `c456316c`, 2026-06-18). **Fixed in §17.544:** `_dispatch_with_retry` now guards the injection by endpoint (`if endpoint != "/api/embed"`), so `/api/embed` honors the callers' `fallback=None` and never attempts the chat-model fallback. The embedder is config-only (invariant), so there is no valid drop-in embedding fallback to inject anyway. **✅ Secondary FIXED (§17.545):** the originating `HTTP 400 input-length` (input exceeded the 2048-token embedder context) no longer drops the entry — both embed payloads now send `truncate=true`, so over-length input head-truncates to the embedder context instead of failing. (Full sub-chunking of over-long docs for complete tail coverage remains a possible future ingest enhancement, not an open bug.)

2. ✅ **FIXED (§17.547, 2026-06-18)** — 100% native tool-call miss on `qwen3.5:397b-cloud` (`role="model_verifier"`). Measured 16/16 misses on the production research-extraction path: the thinking cloud model returns prose/thinking and never populates `message.tool_calls`, but `OllamaProvider.supports_native_tools=True` is provider-wide so every model is forced down the native path → `read_tool_args` returns `None` → silent fallback to non-LLM chunking for every distilled URL. Same miss affected the other `model_verifier` native-tool paths (`gt_extractor`, `execution_verify`, research decompose/gap). **Fixed in §17.547:** a per-model gate (`_model_lacks_native_tools`, `settings.tool_call_coax_models=["qwen3.5"]`) routes such models through the JSON-coaxing fallback, plus a `tool_call_coax_min_tokens=4096` floor so the thinking model's reasoning doesn't starve the JSON (the call site's `max_tokens=1024` was marginal). Live: production path went from 0/16 → 3/3 runs producing entries.

---

## 17. Sprint history + roadmap

Per-sprint development history (the `§17.x` entries) is maintained in an
operator-internal log and is **not part of the public distribution**.
Release-level history lives in [CHANGELOG.md](./CHANGELOG.md); commit messages
carry the `§X.Y` references for traceability into that log.

## 18. Performance benchmarks

CPU-only on the project's reference T480 (8-core / 16GB). Cloud-routed models (`235b-instruct-cloud`) hit Ollama's cloud inference; latency is dominated by network round-trip.

| Operation | Duration | Notes |
|---|---|---|
| Triage turn (qwen3:4b) | 30–300s+ | Scales with conversation length |
| Idea synthesis (qwen3:4b) | 30–120s | Scales with conversation length |
| `/ideate` (Phase 1) | 100–547s | Refinement + feasibility LLM calls |
| `/ideate/confirm` (Phase 2) | 512–1450s | Research loop + distill + embed + ingest |
| `/dag` | 416–504s | Close to timeout threshold |
| `/execute` (single node) | ~893s | RAG retrieval + reranker + gen + verify |
| `/research` shallow | 18–27 min | Dominated by 7b extraction |
| `/research <url>` | 3–8 min | Single-page, no gap analysis |
| `/research github:...` | 1–5 min | Fetch + ingest; no LLM distill |
| `/research/pdf` 1-page | ~6 min | Cold-start dominated |
| `/health` | ~43ms | Postgres + Ollama + Milvus + Redis (warm) |

### Retrieval quality baseline

| KB size | Date | Coverage | Recall@5 | Recall@10 | MRR | Harness |
|---|---|---|---|---|---|---|
| 501 | 2026-04-18 | 95.0% | 0.95 | — | 0.86 | `scripts/score_retrieval.py` (in-process) |
| **1093** | **2026-05-07 (W.8)** | **95.0%** | **0.933** | **0.933** | **0.860** | HTTP `/rag` (W.8 ad-hoc, equivalent semantics) |

Quality held flat under 2x corpus growth. MRR identical; Recall@5 -1.7pt on a single multi-doc query (g016 — got 2 of 3 expected docs in top-5). One MISS (g011) consistent with prior runs. See §17.26 for the W.8 sprint details + the ground_truth.json staleness finding it surfaced.

`scripts/score_retrieval.py` computes `recall@5`, `recall@10`, `mrr`, `coverage` against `tests/fixtures/golden_set.json`. CI workflow `retrieval-quality.yml` runs unit tests on PRs touching retrieval code; live scoring is local/manual (GitHub runners lack Milvus + Ollama).

---

## 19. Glossary

Every project-specific term you'll see in chat, in CLI output, in the SDK, in container logs, or in the source code. Cross-referenced where helpful.

### Workflow concepts

**Job** — a top-level project, one row in the `jobs` table, identified by a UUID. Carries a title, status, refined brief, optional compiled output, and audit timestamps. A job's lifecycle is a 14-state state machine; see §3 and the `JobStatus` table in §6.1.

**DAG** — directed acyclic graph. The execution plan for a job, generated in `planning` phase. Each node has a tool (LLM / CodeGen / SearXNG / Milvus), a domain, and a `depends_on` array.

**DAG node** — single execution step in a job's DAG. One row in `dag_nodes`. Has its own status (`pending → running → done | failed | skipped`), an `assigned_model`, and `output_text` once executed.

**Phase 1** — idea refinement + feasibility assessment. Driven by `app/modules/ideation_workflow.refine_and_assess`. Runs synchronously when you `POST /ideate`; halts the job at `awaiting_confirmation`.

**Phase 2** — research + ingest + compile. Driven by `app/modules/ideation_workflow.research_and_compile`. Runs when you `POST /ideate/confirm`; advances the job through `researching → planning`.

**Awaiting confirmation** — job status meaning "Phase 1 produced a brief; the system is waiting for the human to approve it." This is the only deliberate halt point in the lifecycle.

**Triage** — the chat-side conversational phase before `/go`. Lightweight model (qwen3:4b by default) asks scoping questions until the goal, scope, and constraints are clear.

**Synthesis** — when `/go` fires, the chat transcript is condensed into a single canonical brief. Done by the same model as triage.

**Compile / compiled output** — the final deliverable assembled from leaf DAG nodes. `_compile_output()` in `execution_agent` uses a 4-strategy fallback: explicit `is_output_node=TRUE` markers → title-heuristic → last CodeGen → concatenation.

**Output node / `is_output_node`** — a column on `dag_nodes` (added in migration 017) that the DAG generator flags `TRUE` for leaves. `_compile_output` prefers explicit markers (Strategy 0) before falling back to heuristics.

### Knowledge base

**Knowledge base** — informal name for the Milvus `toon_v2` collection that the system uses for RAG retrieval. Populated by `/research` and ingest paths.

**TOON** — Token-Oriented Object Notation. The data format used at the LLM ↔ structured-data boundary. ~60% fewer tokens than JSON; +4.2% RAG retrieval accuracy. See §10.

**Domain** — high-level topic partition. One of `prompt`, `rag`, `eng`, `llm`, `spec`. Used as Milvus's partition key for tenant isolation.

**Partition key** — Milvus feature for isolating subsets of a collection. Each domain gets its own partition; queries can target one or fan out across all five.

**Embedding** — the 512-dim float vector produced by the embedder model. Locked at 512 dimensions at the schema level; switching embedders requires reindexing the corpus (see USER_GUIDE "Embedder portability").

**Cosine similarity** — distance metric used by Milvus for vector search. Range −1..+1; higher is more similar.

**HNSW_SQ8** — the index Milvus uses on the dense_vector field. Hierarchical Navigable Small World graph + 8-bit scalar quantization. Refines candidates with float16 for accuracy.

**RRF (Reciprocal Rank Fusion)** — the algorithm that merges vector-search and keyword-search results. Formula: `score = 1 / (k + rank)`, default `k=60`. See §7.1.

**Reranker** — a CrossEncoder model (`Qwen3-Reranker-0.6B-seq-cls`) that scores query-document pairs more accurately than the initial retrieval. Runs in a thread executor (PyMilvus blocks; CrossEncoder blocks).

**3-tier ingest** — RAG ingest's dedup policy. Cosine > 0.95 → reject. 0.90–0.95 → version chain (new entry supersedes the matched one). < 0.90 → new entry.

**Version chain / supersede** — when a new entry is "almost" a duplicate of an existing one (cosine 0.90–0.95), the new entry is inserted with `supersedes_id = matched.entry_id`. Retrieval filters superseded entries by default; `include_history=True` opts into the full chain.

**Dedup log** — audit table (`dedup_log`) recording every rejection AND every version-chain supersede. Each row carries the cosine score and the matched existing entry_id.

**Source type / TTL** — knowledge base entries carry a `source_type` (`real_time`, `news`, `community`, `tech_docs`, `curated`, `official_docs`, `ai_generated`); the staleness sweeper uses `config.TTL_POLICY` to expire entries past their TTL.

### Model roles + providers

**Role** — abstract job for a model. Eight roles: `general`, `verifier`, `coder`, `router`, `embedder_pipeline`, `reranker`, `cloud_heavy`, `cloud_alt`, `fallback`. Each maps to a model name via `MODEL_<ROLE>` env vars.

**Provider** — backend that serves a role. Default `ollama` (local). Other registered providers: `openai` (covers OpenAI + any compatible endpoint via `OPENAI_BASE_URL`).

**`get_model(role, overrides=None)`** — the central function for resolving "what model to use." Override > env > default; allowlist-protected.

**Verifier** — the model that reads a node's output and decides whether it satisfies the node's success criteria. Failures trigger retries up to `max_retries` (default 3). After 3 failures the node moves to `blocked`.

### Assist Mode

**Assist Mode** — human-driven walk through a job's DAG. The system acts as co-pilot: shows you each step's prompt + upstream context; you supply the output as evidence. See §9 + USER_GUIDE scenario D.

**Assist session** — one row in `assist_sessions` per active assist walk. UNIQUE per job (only one active session at a time per job).

**Assist step** — one row in `assist_steps` per `(session, node_key)` pair. Has its own state machine: `pending → presented → awaiting_input → received → committed`.

**Mirror invariant** — on assist step commit, the human's evidence is mirrored to `dag_nodes.output_text` in the same DB transaction. Lets the existing `_compile_output`, `_fetch_upstream_outputs`, and RAG-grounding paths consume human output indistinguishably from autonomous output.

**Re-plan policy** — per-session setting controlling whether the system regenerates downstream nodes when an assist step diverges from expectations. `context_only` (default), `selective`, `full`, `disabled`.

**Friction note** — free-text annotation on an assist step (`/assist friction <session_id> <node_key> <note>`) for post-mortem review.

**Handoff** — switching one or more assist steps back to autonomous execution mid-walk. `/assist handoff <session_id> <node_key> single|all`.

### Streaming + lifecycle

**SSE (Server-Sent Events)** — the protocol the orchestrator uses for streaming endpoints (`/research`, `/execute/all`, `/research/reply`, `/research/pdf`). Plain HTTP with `text/event-stream` content type. The SDK's `aiter_*` methods parse the wire format and yield typed event dicts.

**Keepalive** — SSE comment lines (`: keepalive\n\n`) emitted every ~2s when the underlying generator is idle. Forces the socket to be probed so client disconnects propagate as `CancelledError` within ~1s.

**Lifespan** — FastAPI's startup + shutdown hook (`@asynccontextmanager`). The orchestrator's lifespan verifies dependencies, runs migrations, pre-warms the reranker, starts the scheduler, and starts the cleanup task.

**Migration runner** — `app/migrations.py`. Auto-applies SQL files in `db/migrations/` at lifespan startup. Holds an outer transaction with a Postgres advisory lock; each migration runs in a SAVEPOINT.

**Reaper / cleanup** — `app/modules/cleanup.py::reap_stale_jobs`. Runs every 15 min (`cleanup_interval_seconds`). Cancels jobs stuck in long-phase statuses past their thresholds. Skips `assisted_*` statuses on the normal cadence (separate idle sweep handles those).

**Orphan node** — a `dag_nodes` row stuck in `running` past `node_orphan_threshold_minutes` (default 60). Reset to `pending` for automatic re-execution.

**Idempotency guard** — atomic UPDATE pattern that prevents double-execution under concurrent calls. Used by `research_and_compile` (Phase 2 claim), `generate_dag` (DAG count check), and migration 020's UNIQUE partial index.

### Infrastructure

**Ollama** — local LLM runtime, runs on the host (not in a container). Reached from the orchestrator via the bridge gateway `172.18.0.1:11434`. Lazy-loads models on demand from `~/.ollama/models`.

**Milvus** — vector database. Standalone deployment with embedded ETCD. Listens on `:19530`. Hosts the `toon_v2` collection.

**SearXNG** — privacy-respecting metasearch engine. Powers `/research`'s search step. Listens on `:8888`.

**Open WebUI (OWUI)** — the chat frontend. Listens on `:3000`. Talks to `open-webui-pipelines` (`:9099`) which hosts the slash-command logic.

**Pipeline (in the OWUI sense)** — a Python module under `pipelines/` that OWUI loads at runtime to handle chat interactions. Five pipelines ship with scaffold-engine: `scaffold_router` (primary), `execution_handler`, `dag_viewer`, `gt_browser`, `prompt_inspector`.

**Valve (in the OWUI sense)** — a runtime-configurable parameter on a pipeline. Stored in `valves.json` per pipeline; editable via the OWUI admin panel without a restart. Examples: `api_key`, `orchestrator_url`, `stream_timeout`.

**Bridge gateway** — Docker's `ai-network` bridge; `172.18.0.1` is the host's address from inside any container on the network. Used by the orchestrator and pipelines to reach host Ollama. `host.docker.internal` is NOT available on Pop!_OS native Docker.

**Bind mount** — Docker volume that maps a host directory into a container. The dev compose mounts `./app:/code/app:ro`, `./tests:/code/tests:ro`, etc., so source edits show up live in the running container. The prod compose mounts none of these — image is hermetic (X.27, §17.62). One Docker gotcha: if a referenced host bind path does not exist, Docker silently `mkdir`s it as `root:root` *before* applying any `:ro` flag, so a stray host-source mount can leave unwritable phantom dirs on the host filesystem.

### Configuration

**`.env`** — the single source of runtime configuration. Containers inherit it via `env_file`. Pipelines read it through env-fallback when `SCAFFOLD_VALVES_ENV_OVERRIDE=true`.

**Valve bootstrap** — pattern used by all 5 pipelines: template → live `valves.json` → env fallback → persist. Implemented inline per-pipeline (NOT extracted to a shared module — OWUI auto-discovers any `.py` under `/app/pipelines/` as a candidate pipeline).

**API key sync (5 places)** — `.env`, `pipelines/<each>/valves.json`, `~/.bashrc`, `scaffold-orchestrator` container env, `open-webui-pipelines` container env. Must stay aligned after rotation. `make doctor` checks this; `make sync-valves` wipes baked-in `api_key` from `valves.json` so they fall through to `$SCAFFOLD_API_KEY`.

**OpenAPI snapshot** — `docs/openapi.json`. The v1.0.0 contract anchor. `make openapi-check` enforces no silent drift between the live spec and this file.

### CLI / SDK

**`scaffold-engine-client`** — the Python SDK. Pip-installable. Exports `Client`, `AsyncClient`, the typed exception hierarchy (`ScaffoldError` + 8 subclasses), and `schemas` (vendored byte-equal copy of `app/schemas.py`).

**`scaffold-engine-cli`** — the terminal CLI (binary name: `scaffold`). Click-based. As of Sprint J.1.e, is a thin shim over the SDK that translates SDK exceptions into CLI-friendly error strings.

**`next_actions`** — structured "what to do next" field added to `/exec/status` responses. From `app/modules/recovery.py::NEXT_ACTIONS`. Surfaced to chat (`/results`), CLI (`scaffold jobs status`), and SDK (`client.jobs.status()`).

---

*End of OVERVIEW. Day-to-day operator commands live in `USER_GUIDE.md`. First-touch onboarding in `README.md`.*
