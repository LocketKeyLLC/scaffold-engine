# Scaffold Engine — Comprehensive Overview

The single source of truth for the project. Architecture, runtime, every module, every public function, the full database schema, all configuration, the data formats, the logging catalog, the known issues, and the sprint history.

This file replaces the prior scattered docs (`docs/ARCHITECTURE.md`, `docs/CHANGELOG.md`, `docs/CI.md`, `docs/logging-events.md`, `docs/audit/*`, `docs/toon/TOON_*.md`, the `review/*.md` audit notes, and the per-package READMEs). For day-to-day operator commands, see `USER_GUIDE.md`. For first-touch onboarding, see `README.md`.

> **Pinned to API v1.0.0** (`docs/openapi.json`). `make openapi-check` enforces no silent contract drift.

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
| Ollama | 11434 | (host, not containerized) | local install, CPU-only |

Compose: `docker-compose.yml` is the prod runtime (no tests, no Makefile in image). `docker-compose.dev.yml` is the dev override (mounts `tests/`, `Makefile`, `docs/` for snapshot regen, adds dev pip deps). `make dev-up` brings up the dev overlay; `make test` requires the dev image.

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
3. `_normalize_tasks`: clamps `tool` to `VALID_TOOLS` (LLM/CodeGen/SearXNG/Milvus), `domain` to `VALID_DOMAINS` (prompt/rag/eng/llm/spec), defaults invalids with a warning event.
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
| `input_text` | TEXT | original idea text |
| `refined_brief` | JSONB | from idea_refinement |
| `compiled_output` | TEXT | from execution_agent._compile_output |
| `error_summary` | TEXT | populated on failure paths |
| `metadata` | JSONB DEFAULT `{}` | (Pydantic field is `meta` — alias drift, see Known Issues) |
| `created_at`, `updated_at`, `completed_at` | TIMESTAMPTZ | `updated_at` auto-trigger |

`status` lifecycle: `pending → refining → awaiting_confirmation → researching → planning → executing → running → completed | failed | cancelled | blocked` plus `assisted_executing | assisted_running | assisted_paused`.

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
| `domain` | VARCHAR(10) | one of `VALID_DOMAINS` (prompt/rag/eng/llm/spec) |
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
- `VALID_DOMAINS = frozenset({"prompt", "rag", "eng", "llm", "spec"})`
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

Helpers: `_normalize_entry` (canonical field names; handles dual aliases — see Pattern H), `_domain_expr` (Milvus expression), `_build_embedding_text` (title + tags + content). (Audit HIGH: versioned entries skip `dedup_log`.)

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

Constants: `ALLOWED_DOMAINS = {"prompt", "rag", "llm", "spec", "eng"}`, `REFINE_SYSTEM`, `REFINE_PROMPT`.

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

**Valve bootstrap pattern** (`valves.template.json` → live `valves.json` → env fallback → persist): each pipeline's `_bootstrap_valves` re-seeds from a template file when the live file is missing or `{}`. `_apply_env_fallbacks` fills empty string-valued valves from `SCAFFOLD_API_KEY` / `SCAFFOLD_ORCHESTRATOR_URL`, then persists resolved values back to disk. **OWUI Pipelines treats every `.py` under `/app/pipelines/` as a pipeline candidate** — so per-pipeline helpers must be inlined, not extracted to a shared module (a sibling `_helpers.py` gets auto-discovered and quarantined).

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

### 14.1 Test counts (post-Sprint J.1)

- **Orchestrator (`make test`)**: 899 passed, 14 pre-existing failed, 5 skipped
- **SDK (`make test-sdk`)**: 88 passed (20 skeleton + 37 sync typed + 17 async typed + 7 SSE parser + 7 streaming integration)
- **CLI (`make test-cli`)**: 38 passed

The 14 orchestrator failures span: `test_cleanup.py` (6 — `reap_stale_jobs` mock side_effect drift after 4→5+ statement expansion), `test_execution_handler.py::test_status_connection_error_rendered`, `test_retrieval_golden.py` (1 TDD case), `test_scaffold_router_commands.py` (4 — status/research commands), `test_scaffold_router_helpers.py::test_contains_key_commands`, `test_schedule_command.py::test_unknown_sub_returns_help`. Pre-existing on clean main; not regressions.

### 14.2 Markers

| Marker | Meaning | Time |
|---|---|---|
| `smoke` | Fast unit / extraction pipeline | <2 min |
| `validate` | Integration — needs API + reranker + verifier | <15 min |

### 14.3 CI tiers

| Tier | Target | Trigger | Where | Tests | Time |
|---|---|---|---|---|---|
| 1 | `make ci-smoke` | Every push & PR | GitHub cloud runners | 24 unit (extraction pipeline) | <30s |
| 2 | `make ci-local` | Manual / main merge | local self-hosted | 24 unit + 4 integration + 7 golden | ~5 min |
| 3 | `make ci-eval` | Manual | local | 40-query ground truth | ~8 min (cached <1s) |

**Cloud-safe (Tier 1):** `tests/test_verify_extraction.py` — 24 pure-Python tests, no Docker, no Milvus, no Postgres, no Ollama. Installs from `requirements-ci.txt`. Runs on GitHub free `ubuntu-latest` (7 GB RAM, 2 vCPU).

**Local-only (Tier 2+):** Milvus standalone needs 8+ GB RAM (free runners cap at 7); Ollama CPU inference too slow for cloud CI timeouts. The integration job is conditioned on a self-hosted runner being available.

### 14.4 Pipeline-test caveat

Pipeline tests require `--noconftest` because `tests/conftest.py` eager-loads `app`. Container test path is `/code/tests/`. Don't run pytest from the host against the orchestrator's modules — env diverges (no Milvus, no Postgres).

### 14.5 Make targets

| Target | Effect |
|---|---|
| `make test` | Full orchestrator suite (in-container) |
| `make test-sdk` | SDK suite (`/code/sdk/tests/`) |
| `make test-cli` | CLI suite (`/code/cli/tests/`) |
| `make ci` | CI-safe tests (no live deps) |
| `make ci-smoke` | Tier 1 (cloud-safe) |
| `make ci-local` | Tier 2 (needs local stack) |
| `make ci-eval` | Tier 3 (40-query ground-truth) |
| `make agent` | Smoke-marked execution agent tests |
| `make eval` | Retrieval eval against ground truth |
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

14. **OWUI pipeline file rule.** OWUI Pipelines treats every `.py` under `/app/pipelines/` as a pipeline candidate. Per-pipeline helpers must be **inlined** — a sibling `_helpers.py` gets auto-discovered and quarantined to `pipelines/failed/`.

15. **Pipeline auto-chain on `/confirm`.** Lives in `pipelines/scaffold_router.py`, **not** in orchestrator endpoints. Curl-only paths bypass it.

---

## 16. Known issues

> Captured by the 2026-05-05 architecture audit (`review/*.md`, since absorbed here). 135 distinct findings across ~20k LOC. Each finding cites `file:line` for independent verification.
>
> **Re-verified against live code on 2026-05-07** (post-Sprint-J.1 / commit `ba8168f`). Of the original 18 HIGH items: **13 are fully fixed**, 2 are partial, 3 were retracted within the audit itself. Each item below is marked with its verified status:
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

5. 🟦 **RETRACTED** — `app/modules/execution_agent.py:642` (failed nodes lose `optimized_prompt`). Audit's cluster D verification confirmed both timeout (L629) and general-exception (L642) paths pass `optimized_prompt=exec_prompt` to `_set_node_status`, which `COALESCE`s the value. Adjacent real gap remains: prompt-build / RAG-injection at L530-L595 is unwrapped, so an exception there leaves the node `'running'` until the 60-min orphan reaper resets it. Flagged for future work.

#### Tier 2 — auditability / data-integrity gaps

6. ✅ **FIXED** — `app/modules/rag_pipeline.py` (version-chain entries skip `dedup_log`). Both branches now write to `dedup_log`: rejected duplicates with `action_taken='rejected'` and superseded entries with `action_taken='versioned'`. Invariant #9 satisfied. (Original audit cited L819-831; current implementation at L850-887.)

7. ✅ **FIXED** — `assist_steps.status='applied'` (dead enum value). Migration 024 dropped `'applied'` from the CHECK constraint.

8. ✅ **FIXED** — `app/utils/github_ingest.py` (`CancelledError` swallowed). The handler now explicitly checks `isinstance(item, asyncio.CancelledError)` and re-raises **before** the broader Exception branch, with a comment explaining `CancelledError` is a `BaseException` (not `Exception`) since Py3.8. Cancellation now propagates correctly. (Original audit cited L209.)

#### Tier 3 — invariant violations

9. ✅ **FIXED** — `app/modules/ideation_workflow.py` (logger identity). Module no longer imports `structlog`; uses stdlib `logging` only. (Original audit cited L46.)

10. ✅ **FIXED** — `app/model_router.py` (ad-hoc httpx). `_get_client()` now delegates to `app.utils.http_clients.get_ollama_client()` — explicitly documented as "delegates to the shared pool". (Original audit cited L36-38.)

11. ✅ **FIXED** — `app/main.py` lifespan (sync PyMilvus calls). Both `connect` and `disconnect` are now wrapped in `loop.run_in_executor(None, lambda: …)` with an explanatory comment. Event loop no longer blocks during the initial Milvus handshake. (Original audit cited L93-97, L172.)

#### Tier 4 — UX / OWUI integration

12. ⚠️ **PARTIAL** — `pipelines/scaffold_router.py` auto-chain recovery. There is no full recovery state machine on the `/ideate/confirm → /dag → /execute/all` chain, but `/results <job_id>` now surfaces `compile_status="partial"` plus a per-failed-node table with inline `/exec retry` and `/skip` recovery commands (L1850 region). Users can resume from a known partial-failure state without manual command knowledge. The structured state machine remains future work. (Original audit cited L893-938.)

13. ⚠️ **PARTIAL** — `pipelines/{execution_handler,dag_viewer,gt_browser,prompt_inspector}.py` print-only API-key drift warnings. `scaffold_router` (the most user-facing pipeline) surfaces a `_drift_hint()` markdown block on user-visible 401 errors. The other 4 pipelines still warn via stdout `print()` only. UX gap, not a security boundary — drift IS captured in container logs.

14. 🟦 **RETRACTED** — `pipelines/scaffold_router.py:1460` (120s SSE timeout aborts long streams). Audit cluster J verification: the 120s is a read-poll interval, not a stream-abort timeout. `requests.iter_lines()` raises `ReadTimeout` after 120s of no data; the handler catches it, increments idle counter, emits a heartbeat, and continues. Stream only declares stalled after `idle_seconds >= max_idle = max(300, 5*keep)`. Real adjacent issue: idle counter calibration (cycle is 120s wall, counter increments by `keep`=10s, so effective stall threshold is ~12× the apparent one). Counter calibration, not a HIGH.

15. 🟦 **RETRACTED** — `pipelines/*` bare `requests.get/post` (no module-level Session). Audit demoted to LOW: anti-pattern but not a measured perf issue at human-paced (~1 cmd/min) traffic. Refactor blocked by 36+ test patch sites.

#### Tier 5 — orchestrator bugs surfaced incidentally

16. ✅ **FIXED** — `app/main.py:/research/pdf` (`UploadFile.filename` None crash). Endpoint now has explicit `if not file.filename or not file.filename.lower().endswith(".pdf")` guard. (Original audit cited L740; current implementation at ~L791.)

17. ✅ **FIXED** — `GET /dag/{job_id}` 500 on bad UUID. Endpoint now wraps `UUID(job_id)` in try/except and raises `HTTPException(status_code=400, detail="Invalid job_id format")`. (Original audit cited L441-459.)

18. ✅ **FIXED** — `POST /execute` dict-error not converted to HTTPException. Endpoint now explicitly converts dict-error responses to `HTTPException`, with comment citing parity with `/ideas`, `/dag`, `/rag`. (Original audit cited L666-678; current implementation at ~L720.)

### 16.2 Cross-cutting patterns

#### Pattern A — Logger identity (largely fixed)
Original violation list and current state:
- ✅ `ideation_workflow.py:46` — fixed (no longer imports structlog).
- 🟥 `execution_handler.py:78`, `prompt_optimizer.py:16`, `prompt_inspector.py:11` — `__name__`-style loggers; still resolve to `app.modules.*` rather than `scaffold.*`. Cosmetic violation; logs are still emitted, just not under the conventional namespace.
- 🟥 `prompt_assembly.py:32` — `logging.getLogger("scaffold")` with no submodule suffix.

The HIGH-tier instance is fixed; the LOW-tier sweep (rename to `scaffold.<sub>`) remains future cleanup.

#### Pattern B — HTTP-client pool reuse (largely fixed)
- ✅ `app/model_router.py` — fixed (delegates to shared pool).
- 🟥 `app/main.py:_check_ollama` — still constructs a fresh `httpx.AsyncClient(timeout=5)` per `/health` probe. ~43 ms per `/health` call doesn't show measured impact, but the invariant is violated.
- 🟦 OWUI pipelines — retracted (LOW; bare `requests` is intentional given test mock surface).

#### Pattern C — Postgres ↔ APScheduler ordering
✅ **FIXED** in both directions — see HIGH #3.

#### Pattern D — Dead schema enum members
✅ **FIXED** — Migration 025 dropped `model_failure` and `structural` from `error_logs.error_type`. Migration 024 dropped `'applied'` from `assist_steps.status`. CHECK constraints now match what code actually writes.

#### Pattern E — Pydantic ↔ DB column name drift
🟥 **OPEN** — `JobBase.meta` (`schemas.py:88`) vs. `jobs.metadata` (`init.sql:23`); same drift in `ArtifactBase.meta` vs. `artifacts.metadata`. No alias, fields silently dropped on round-trip. Fix: either add `Field(alias="metadata")` + `populate_by_name=True`, or drop the `meta` field.

#### Pattern F — Placeholder/UUID validation
- ✅ `/dag/{job_id}` GET — fixed (UUID validation, see HIGH #17).
- ✅ `/research/pdf` filename — fixed (None-guard, see HIGH #16).
- 🟥 Pipeline-side: `/confirm` doesn't placeholder-check `feedback`; `/research/reply` doesn't placeholder-check `session_id` (`scaffold_router.py:826` and `:888` per audit).

#### Pattern G — Cancellation safety
- ✅ Research path — `_run_with_session_lifecycle` correctly catches `CancelledError` and finalizes session as `cancelled`.
- ✅ GitHub ingest — fixed (see HIGH #8).
- 🟦 Pipelines SSE — counter calibration only (see retracted HIGH #14).

#### Pattern H — Field-name dual-aliases in ingest
🟥 **OPEN** — RAG ingest still accepts `source`/`source_url`, `content`/`canonical_text`, `title`/`topic`, `tags`/`domain_tags`. Maintenance debt; not a correctness issue.

### 16.3 Schema-side findings

- ✅ **`db/init.sql` baseline currency** — was post-008 at audit time; now post-025 (top-of-file comment confirms). Self-corrected.
- 🟦 **`db/init.sql:166-167` duplicate indexes also in migration 006** — `CREATE INDEX IF NOT EXISTS` makes this idempotent. Documented in init.sql comments; intentional.
- 🟥 **`db/migrations/020_research_sessions_single_running.sql`** — not fully atomic under concurrent traffic. Fresh DBs OK (runner holds the lock); established DBs already past 020 OK. Edge case for new deploys hitting heavy concurrent ingest mid-bootstrap.
- 🟦 **`db/migrations/011_scheduled_jobs.sql:14`** `CHECK (last_status IN (…, NULL))` — dead `NULL` in IN list. Cosmetic; constraint still admits NULLs via Postgres CHECK semantics. Migration 018 later wrote the correct `IS NULL OR x IN (…)` form.
- 🟥 **`app/schemas.py:JobStatus` Literal + `JOB_STATUSES` tuple drift risk** — two sources of truth manually kept in lockstep. Comment acknowledges. Adding a new status requires three edits (Literal, tuple, DB CHECK).

### 16.4 Remaining fix queue (post-verification)

Items still actually open, in rough priority order:

1. **Pre-existing CLI bug** — `scaffold jobs status <id>` calls non-existent `GET /jobs/{id}` (silently 404s). One-line fix: route `cli/scaffold_cli/main.py::jobs_status` through `client.jobs.status()` (which wraps the real `GET /exec/status/{id}`).
2. **Pydantic ↔ DB alias drift** (Pattern E) — `meta` ↔ `metadata` in JobBase/ArtifactBase. Add `Field(alias=…)` + `populate_by_name=True`, or drop the field.
3. **Logger identity sweep** (Pattern A residue) — rename `__name__`-style loggers in `execution_handler`, `prompt_optimizer`, `prompt_inspector`, `prompt_assembly` to `scaffold.<sub>` form.
4. **`/health _check_ollama` shared client** — replace ad-hoc `httpx.AsyncClient(timeout=5)` with a call to `get_ollama_client()` (or a dedicated short-timeout health client in `http_clients.py`).
5. **Pipeline placeholder checks** — add `_is_placeholder()` calls on `/confirm` feedback and `/research/reply` session_id (audit Pattern F).
6. **Drop dual-alias acceptance** (Pattern H) — pick canonical names; add deprecation comment, then remove fallback paths.
7. **Migration 020 atomicity** — combine the stale-row UPDATE + UNIQUE INDEX into a single locking transaction (or reorder so the UPDATE precedes any concurrent ingest window).
8. **Idle-counter calibration** in `scaffold_router._stream_sse_to_queue` — increment by actual elapsed wall time, not the heartbeat interval (audit retracted HIGH #14 noted this).
9. **Print-only API-key drift in 4 minor pipelines** (HIGH #13 partial) — port the `_drift_hint()` markdown surface from `scaffold_router` to `execution_handler`/`dag_viewer`/`gt_browser`/`prompt_inspector`.
10. **Auto-chain recovery state machine** (HIGH #12 partial) — formalize the `/results`-based recovery surface into a structured state machine.

### 16.5 Items NOT covered by the audit

- **Tests phase skipped** — no coverage matrix for `execution_agent`'s retry loop, `ideation_workflow`'s session-lifecycle, or `scheduler`'s misfire handling. The 14 pre-existing test failures in §14.1 are mock-side drift, not coverage gaps.
- **Performance benchmarking** — likely PERF issues identified but not measured.
- **Observability completeness** — log-line fan-out, metric coverage, alerting hooks not audited beyond foundation middleware.
- **Deployment surface** — Dockerfile, compose, `.env.example` not audited.

---

## 17. Sprint history + roadmap

### 17.1 Roadmap state

12-item roadmap. Items 1–10 done. Items 11 + 12 remain.

| # | Item | Status |
|---|---|---|
| 1–6 | Pre-Sprint-E foundation, hardening rounds, RAG pipeline, research agent, OWUI pipelines, assist mode | done (pre-2026-05-06) |
| 7 | Embedder portability (`scripts/reindex.py` + `make reindex`) | done 2026-05-06 (`63ccd42`) |
| 8 | (Sprint H) Terminal CLI | done 2026-05-06 (`1f5f999`) |
| 9 | (Sprint I) Streaming + native tool-calling | done 2026-05-06 (I.1 `f768553` + I.2 `3e5f3d6`) |
| 10 | Python SDK + stable OpenAPI (Sprint J.1, 6 commits) | done 2026-05-07, tagged `v1.0.0` |
| 11 | Native single-page web UI | pending |
| 12 | Cost + latency telemetry | pending |

### 17.2 Sprint E — Provider abstraction (2026-05-06)

`app/providers/` shipped: `base.py` (LLMProvider ABC + capability flags), `__init__.py` (registry + `provider_for_role` + capability gate), `ollama.py` (thin adapter delegating to `model_router._dispatch_with_retry`/`list_models`).

`MODEL_*_PROVIDER` settings + `model_router.generate/chat/embed/classify` accept a `role=` kwarg. When set, dispatch goes through `provider_for_role`. Capability gate raises `ProviderCapabilityError` if a chat role binds to a non-chat provider (reranker exempt).

10 call sites migrated to `role=`: `rag_pipeline ×3`, `utils/embedding`, `gt_extractor`, `dag_generator`, `idea_refinement`, `ideation_workflow ×3`. Pattern 3 — helper-internal sites in `research_agent ×7`, `execution_agent ×1`, `execution_verify ×1`, `assist_replan ×1`, `prompt_optimizer ×2` — deferred (helpers take `model: str` from upstream caller).

### 17.3 Sprint F — OpenAI provider (2026-05-06, `f051975`)

`OpenAIProvider`: raw httpx through shared `get_openai_client()`, no openai SDK dep. `OPENAI_BASE_URL` override-able for vLLM/LocalAI/Ollama-OpenAI-mode. Auth header built per-call. Streaming + native tool calls advertised but concrete impls deferred to Sprint I. `scripts/doctor.sh` gained an OpenAI section.

### 17.4 Sprint G — UX foundation (2026-05-06, `b395d75`)

- **G.1:** `model_router._format_provider_error(resp, role)` enriches failed responses with `[role=X provider=Y] <error> — <hint>`. Provider-specific hints (openai → rotate `OPENAI_API_KEY`; ollama → check `OLLAMA_BASE_URL`).
- **G.2:** `make init` (`scripts/init.sh`) — interactive provider/model wizard. Idempotent.
- **G.3:** `make sync-valves` (`scripts/sync_valves.sh`) — wipes baked-in `api_key` from `pipelines/*/valves.json`. With `SCAFFOLD_VALVES_ENV_OVERRIDE`, `.env` becomes the single source of truth.

### 17.5 Sprint H — Terminal CLI (2026-05-06, `1f5f999`)

Pip-installable `scaffold-engine-cli` at `cli/`. `scaffold version | doctor | ideate | confirm | jobs list | jobs status`. Click-based; sync httpx with friendly error translation. Config resolution: flag > env > `~/.scaffold/config.toml` (or `$XDG_CONFIG_HOME/scaffold/config.toml`) > walked-up `.env` > default `http://localhost:8000`. SSE-streamed endpoints (`/research`, `/execute/all`) deferred to Sprint I.

### 17.6 Sprint I — Streaming + tool calling (2026-05-06)

- **I.1 (`f768553`):** Concrete `stream_chat` for both providers — Ollama (line-delimited JSON, `done=true` terminator) + OpenAI (SSE `data: {...}` frames, `[DONE]` terminator). Both yield plain `str` deltas. Streaming intentionally skips retry+fallback (mid-stream failure → re-issue from caller). 12 streaming tests.
- **I.2 (`3e5f3d6`):** Native tool-calling abstraction. `Tool` (name, description, input_schema=JSON Schema) and `ToolCall` (id, name, arguments=dict) dataclasses; `ModelResponse.tool_calls` field; `LLMProvider.tool_call()` ABC. OpenAIProvider translates to `{type: function, function: {...}}` wire shape; OllamaProvider uses identical wire shape (Ollama 0.3+ copied OpenAI's structure) but expects `arguments` as a dict and synthesizes `tool_<index>` for the missing tool-call id.

### 17.7 Item 7 — Embedder portability (2026-05-06, `63ccd42`)

`scripts/reindex.py` + `make reindex REINDEX_ARGS="..."`. Per-partition fan-out across `VALID_DOMAINS`, `entry_id`-cursor pagination, embedding-text format locked to mirror `rag_pipeline._build_embedding_text` (test asserts byte-equality). Upserts preserve every non-vector field. Mounted `./scripts:/code/scripts:ro` so newer scripts visible without image rebuild.

### 17.8 Sprint J.1 — Python SDK + stable OpenAPI (2026-05-07, 6 commits, tagged `v1.0.0`)

| # | Commit | Subject |
|---|---|---|
| a | `334dba8` | OpenAPI snapshot + drift check, FastAPI v1.0.0 |
| b | `be833f5` | scaffold-engine-client skeleton + schema parity |
| c | `ff56583` | Sync Client typed methods + resource sub-objects |
| d | `87de74b` | AsyncClient mirror + SSE streaming helpers |
| e | `5e31d02` | CLI switches to scaffold-engine-client |
| f | `b1695da` | Full SDK README + USER_GUIDE + v1.0.0 anchor |

- **J.1.a** — Locked the public HTTP API contract before the SDK starts depending on it. FastAPI bumped 0.1.0 → 1.0.0; `docs/openapi.json` (44 paths, sorted-keys JSON, ~100 KB); `make openapi-snapshot` / `openapi-check`.
- **J.1.b** — `sdk/` package mirroring `cli/` layout. Both `Client` (sync httpx) and `AsyncClient` (async httpx) skeletons + ScaffoldError hierarchy + `_transport.py` shared error mapping. Vendored `app/schemas.py` byte-equal at `sdk/scaffold_client/schemas.py`. Parity test in `tests/`.
- **J.1.c** — Typed methods. Top-level workflow (`ideate`/`confirm`/`optimize`/`execute`/`skip`/`health`/`status`/`logs`) on `Client`; resource sub-objects (`client.jobs`, `client.dag`, `client.prompts`, `client.gt`, `client.rag`, `client.schedule`) with stable identity per Client. None-valued kwargs drop out before serialization.
- **J.1.d** — `AsyncClient` parity for non-streaming endpoints + four SSE helpers (`aiter_research`, `aiter_research_reply`, `aiter_research_pdf`, `aiter_execute_all`). SSE parser at `sdk/scaffold_client/_sse.py` yields `{"event": str, "data": Any}` dicts.
- **J.1.e** — `cli/scaffold_cli/client.py` is now a thin shim over `scaffold_client.Client`. Catches SDK typed exceptions and re-raises as the existing `CLIError` with CLI-specific remediation hints. CLI suite passes 38/38 unchanged. `cli/pyproject.toml` drops `httpx`, adds `scaffold-engine-client>=1.0,<2.0`. `docker-compose.yml` extends `PYTHONPATH` to `/code:/code/sdk`.
- **J.1.f** — Full `sdk/README.md`, "Python SDK" section in `USER_GUIDE.md`, CHANGELOG wrap-up. Local annotated tag `v1.0.0` at `b1695da`, pushed to `origin`.

### 17.9 Open follow-ups (not yet sprinted)

- `research_agent` / `execution_agent` migration to native `tool_call()` (away from JSON-prompt coaxing)
- Pattern 3 helper-internal call-site migration deferred from Sprint E.7
- Pre-existing CLI bug: `scaffold jobs status <id>` calls non-existent `GET /jobs/{id}` (silently 404s)

---

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

### Retrieval quality baseline (2026-04-18, KB=501 entries)

- Coverage: 95%
- Mean Recall@5: 0.95
- Mean MRR: 0.86

`scripts/score_retrieval.py` computes `recall@5`, `recall@10`, `mrr`, `coverage` against `tests/fixtures/golden_set.json`. CI workflow `retrieval-quality.yml` runs unit tests on PRs touching retrieval code; live scoring is local/manual (GitHub runners lack Milvus + Ollama).

---

*End of OVERVIEW. Day-to-day operator commands live in `USER_GUIDE.md`. First-touch onboarding in `README.md`.*
