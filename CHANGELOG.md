# Changelog

Notable changes to scaffold-engine. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [SemVer](https://semver.org/).

Day-to-day development is tracked at sprint granularity in the commit log (`fix(§X.Y)` / `feat(§X.Y)` references). This file records the release-level view.

## [1.5.0] — 2026-08-29

UI consolidation lands its second act: the job hub gives every job one URL, the retired `/web` console's grace-period redirects are removed, and fresh installs on underpowered hardware get an honest warning instead of a mystery failure (§17.857–§17.859, PRs #297–#299).

### Changed

- **The job hub — one job, one URL.** `#/job/:id` replaces the six peer views a job's life used to span (approval gate, plan editor, DAG canvas, execution theater, output viewer, trace browser — each with its own copy-pasted job picker). A persistent header strip (title · status · Compare) sits over six tabs: Overview · Plan · Run · Output · Traces · Costs. Overview *is* the approval gate while the job awaits confirmation; Run embeds the assist walkthrough for assist-mode jobs and the autonomous theater otherwise, and warns before navigating away from a streaming run. The old `#/theater`, `#/output`, `#/plan`, `#/dag`, `#/approvals/:id`, and `#/traces/:id` routes are removed (hard switch); the sidebar's Operate group shrinks to Dashboard / Jobs / Approvals / Compare. Per-job costs get a first-class surface (totals, by-model and by-kind breakdowns, budget status) (§17.859).

### Added

- **Slow-box warning in the setup wizard.** Fresh installs on memory-tight boxes (15–16 GB) can't finish a DAG step on local models inside the 600s default `NODE_TIMEOUT_SECONDS` — jobs would burn every retry and fail with nothing saying why. `/models/probe` now flags local tags whose warm 8-token test generation exceeds `SLOW_BOX_PROBE_WARN_MS` (default 5s; a slow first probe gets one warm re-probe so one-time model load doesn't false-flag), and the wizard's health step probes the applied `model_general` and surfaces an advisory warning with the two ways out: the Ollama Cloud preset, or raising `NODE_TIMEOUT_SECONDS`. Per-role Probe buttons show the same "slow for this box" tag (§17.858).

### Removed

- **The `/web` console's redirect stubs and support scaffolding.** v1.4.0 retired the server-rendered htmx console to permanent redirects as a one-release grace period; this release deletes the remainder: the `/web/*` redirect routes, the htmx templates and vendored assets, the root `/static` mount (it served only `/web` assets — the SPA's assets live under `/ui/static/`), the CSRF middleware that existed for the console's auth-exempt forms, and the `/web/` + `/static/` auth exemptions. Old `/web` bookmarks now 404 — use the `/ui` SPA. No API surface changes (the routes were never in the OpenAPI contract) (§17.857).

### Fixed

- **Milvus corpus-loss on `docker compose down` — root-caused and fixed.** The `toon_v2` knowledge corpus no longer comes back empty after a `down`/`up` cycle. The embedded etcd — which holds every collection's metadata and segment references — was configured with a *relative* data dir (`default.etcd`), so it wrote to the container's **ephemeral overlay layer** instead of the `milvus-data-v2` volume; a new container from `down`/`up` therefore started with no segment references and orphaned the segment files still sitting on the volume (`docker restart`, reusing the same container, masked this). `milvus-config/user.yaml` now pins the etcd data dir onto the volume, mounted read-only in compose. Applying it to an existing deployment needs a one-time etcd migration (copy the live ephemeral etcd onto the volume before the first recreate) — a backup-gated runbook is in `internal/milvus-etcd-migration-runbook.md`. Live-verified: a full `docker compose down`/`up` now preserves all entities. The long-standing `restart`-not-`down` operational constraint is **retired** — both are safe (§17.855).

## [1.4.0] — 2026-08-26

UI consolidation and security hardening: the native `/ui` SPA becomes the front door (server-rendered `/web` retired to redirects), a first-run experience takes a fresh install from `make bootstrap` to a completed job with zero file edits, and the 2026-08-24 six-subsystem audit's criticals are closed (§17.812–§17.841, PRs #212–#273). Highlights:

- **Security** — SSRF guards on `/research openapi:` (spec fetch + remote `$ref` resolution now route through the public-host guard with post-redirect re-checks); `/web` auth-gated and then retired; empty-key auth symmetry on the OpenAI surface; CI now runs the real migration chain so schema-dependent enforcement (RBAC) is tested against real tables; pip-audit ignores carry dated expiries (§17.812, §17.829).
- **Silent-degradation fixes** — twelve audit criticals/majors: the reranker fails visible and recovers without a restart; SearXNG failures warn instead of caching empties; dedup failures are loud with reconcilable markers; Milvus read-your-writes consistency on version-chain walks; scheduled research reports failed/skipped truthfully; startup ordering (clients before orphan-resume); cancellation no longer swallowed in teardown windows; migration failure alerts + refuses to serve (escape hatch: `SCAFFOLD_FAIL_ON_MIGRATION_ERROR=false`); profile apply is atomic and always revertible; ETA math agrees across SSE and `/exec/status`; embedding-cache keys track the real embedder knob; BM25 fallback is annotated (`keyword_backend`) (§17.813).
- **Native operator UI at full parity** — login with identity (`GET /auth/whoami`, server-derived edit attribution), models/RAG/settings/schedules/traces/costs/artifacts/alerts views, assist step verbs, skip/retry node verbs, unified approve→run chain across surfaces, live progress + ETAs everywhere; grouped navigation, violet identity, theme + density toggles, dashboard command center; a zero-runtime-dep JS test lane (`make test-ui`) in CI (§17.815–§17.818, §17.840).
- **First-run experience** — `make bootstrap` → one-click browser pairing link (`/ui/?key=…`) → optional admin account (password sign-in) → "Connect your models" wizard with per-role test probes → green health board. Model role code defaults are local-safe (no cloud calls on a fresh install); Open WebUI is an optional compose profile; `.env.example` is complete and CI-gated (§17.819, §17.821, §17.823–§17.824, §17.840).
- **Operations** — `make backup` / `make restore` cover Postgres + the Milvus collection (drill-verified full recovery, including the documented `compose down` Milvus trap); optional webhook sink for system alerts (Slack/Discord/ntfy-compatible); doctor is profile-aware (§17.821–§17.822, §17.835).
- **Assist routing integrity** — deterministic veto post-filter over the unified decision (error-paste forces fix, help-questions reroute to research, advance/finalize retire the in-flight step — one-turn completion); slash-command turns get full derive treatment; `/track` honors caller context (§17.814, §17.839).
- **Model management API** — `GET/PUT/DELETE /models/roles` (live effective config with source), `POST /models/probe` (generate-based liveness), OWUI `/model set` routed through the orchestrator (§17.815).
- **Research & retrieval quality** — real `research_fetch` progress events with honest per-URL failure reasons; citation numbering guaranteed 1:1 with the Sources block; rerank-cap and provenance warnings surfaced; the §17.729 relevance gate now defends the primary research path (default on after a clean golden A/B); RAG cache invalidates on ingest; staleness sweep reports `clean` honestly (§17.831–§17.837, §17.841).
- **CI & test hardening** — pipelines/CLI/SDK lanes in CI; the SSE/exec-concurrency modules un-ignored (env-bisected, not service-bound); SSRF tests in the fast PR gate; `integration` marker hygiene + skip-count watch; Trivy surfaced as annotations (§17.826–§17.830).

**Contract:** API version moves 1.2.0 → 1.4.0. Additive: `/auth/whoami`, `/auth/account/*` + `/auth/login`, `/models/roles` + `/models/probe`, `/research/start` + `/research/sessions/{id}`, `/meta/domains`, `/trace/{job_id}`, alerts endpoints, and the `progress` / `research_fetch` SSE events. Behavioral: every `/web/*` route now returns a permanent redirect to its `/ui` SPA equivalent (the HTML console is retired; removal of the redirect stubs is scheduled for the next release); model-role code defaults changed from cloud to local tags (env pins override, existing `.env` installs unaffected).

## [1.3.0] — 2026-08-24

Multi-user access control, a fast-execution profile, and uniform progress/ETA streaming, layered on the v1.2.0 stack (§17.809–§17.811). Highlights:

- **Multi-user & RBAC** — per-user job ownership with a two-tier role model (`admin` | `user`), extending the §17.807 scoped keys from "named keys, equal access" into real isolation. Every job (and research session, schedule, assist session, design job, artifact) is owned by its creator: a `user` sees and manages only their own work — cross-user access returns **404, not 403**, so it can't even confirm another user's job exists — while `admin` and the master key see everything. Enforced across the whole JSON API and gated by `MULTI_USER_ENABLED`, so single-user installs are byte-for-byte unchanged. Keys carry an owner tag (several keys can map to one user) and a role, minted with `make key-add … OWNER=… ROLE=user|admin`. The `/v1`, `/mcp`, and server-rendered `/web` surfaces stay master-admin-only by design (their internal loopback re-authenticates as the master key); per-user access is the JSON API and the `/ui` SPA (§17.810, migration 068).
- **Quick-mode profile** — a GPU/cloud-fast preset targeting a sub-5-minute build for small DAGs: `/model profile quick` (global) and `/go --quick` (per-job) apply a fast model map plus execution-side levers — skip the CPU cross-encoder reranker and the per-node prompt-optimize pass, and cap the node count. Honest scope: the floor is the sum of cloud node latencies, so large branchy builds still won't fit (§17.809).
- **Progress ETAs & streaming summaries** — a uniform `progress` SSE event with elapsed/percent/ETA across every long-running component (DAG execution, research, RAG ingest, decomposition, simulation, assist), backed by a shared `ProgressTracker`/`EmitThrottle`. DAG progress is computed on read from `dag_nodes` timestamps (no metadata write, so the parallel frontier can't race it). Deterministic per-phase summaries default on; optional LLM narration defaults off (§17.811).
- **Platform** — retired-cloud-model guard (probe liveness on `/api/generate`, not the stale tag list, after a coder model 410'd mid-run) and a golden-task mapping fix for the `model_triage` role (§17.809b, §17.791×§17.805).

The API contract is unchanged from v1.2.0 — the RBAC layer is additive (dependency-level enforcement plus two nullable columns) and the `progress` event is additive to the SSE stream, with no endpoint or request/response schema changes.

## [1.2.0] — 2026-08-16

80 commits since v1.1.0 (§17.714–§17.808). Highlights:

- **Operator SPA** — a standalone single-page operator UI at `/ui`: DAG canvas, execution theater, dashboard, research explorer, and assistant chat, plus a human-in-the-loop layer with an approval gate, plan editor, output viewer, job comparison, and a Ctrl-K command palette (§17.778–§17.779).
- **Execution robustness** — automatic crash-resume (a job resumes at the failed node on the next boot), per-node token streaming (`node_token` SSE deltas), and hard per-job cost/token budget enforcement (§17.774, §17.776, §17.777).
- **Retrieval & research quality** — RAGAS metrics (context precision/recall + faithfulness) layered on the retrieval goldens and CI-gated; a citation-faithfulness gate that scores per-citation attribution on the live research summary; domain-aware scheduled research that can pin an ingest partition; and bounded research fetch (concurrency + per-depth URL caps) (§17.794, §17.798–§17.802).
- **Role→model learning** — periodic golden re-A/B produces stage-swap proposals surfaced as confirmation cards (nothing auto-swaps); all eight switchable roles are now learnable; `make init` gained install-time options (single- vs multi-user scoped keys, CPU-local vs GPU/cloud vLLM preset) (§17.803–§17.807).
- **MCP integration** — two-sided Model Context Protocol support: the engine exposes its tools as an MCP server (HTTP `/mcp/` + stdio), and DAG nodes can call external MCP tools via a server registry. Both default off (§17.772).
- **Structured outputs** — provider-aware, grammar-constrained decoding (`response_schema`) that applies the constraint only where the backend enforces it (§17.773).
- **Assist Mode** — unified turn routing (one decision step replaces the phrase-gate stack); the assistant can add a guided step mid-session; a per-step "living recap" is surfaced to the operator (📍 panel + 👉 do-this-next callout); problem-solving discipline that honors constraints instead of thrashing; prefer-the-easiest-tool guidance; screen-state grounding before keystrokes; and cross-component fact sharing across umbrella components (§17.727–§17.771).
- **Platform** — split test lanes (`make test` core / `make test-pipelines` / `make test-all`) and TestClient lifespan-teardown fixes that cleared the remaining web-test errors (§17.808).

## [1.1.0] — 2026-08-05

790 commits since v1.0.0. Highlights:

- **Task decomposition** — a large idea now splits into an umbrella project with component jobs, each running the full research → plan → execute pipeline, with live roll-up of progress (`/decompose`, OVERVIEW §17.523+).
- **Assist Mode matured into the flagship surface** — cross-chat continuity, decision nodes (suggest-don't-decide), plan-affecting notes that trigger surface-and-ask re-planning, mid-session pivots, and a unified session memory with supersession so the assistant follows corrections instead of stale facts (§17.633–§17.714).
- **Execution-context awareness** — the assistant tracks which `user@host` the operator is actually on (deterministic sensor + per-message refresh) and threads it into every step (§17.701–§17.716).
- **Research reliability** — search-engine rotation fixes with 0-results fallback, research-derived operator options that become explicit DAG decision nodes, and timeline-aware brief synthesis where later corrections supersede earlier statements (§17.662+, §17.694, §17.712).
- **DAG quality passes** — deterministic post-generation passes connect isolated nodes, converge terminal leaves, enforce a single deliverable, and repair (rather than fail) cyclic generated graphs (§17.668–670, §17.696).
- **Formal verification** — SymbiYosys-backed formal property checking for hardware-design workflows (§17.414–417).
- **Natural-language routing** — safe read commands routable by plain language alongside slash commands (§17.655+).
- **Platform** — PyMilvus 3 / MilvusClient migration, Python 3.14 across the stack, dependency refresh (redis 8, fastapi 0.140, opentelemetry 1.44).
- **Hardening** — two full multi-agent audits closed out (2026-06 architectural review; 2026-07-18 audit); cloud CI made trustworthy again.
- **Governance** — LICENSE (BUSL-1.1), CONTRIBUTING, SECURITY, this changelog.

## [1.0.0] — 2026-05-07

First stable release.

- Full ideate → refine → feasibility-halt → research → DAG generation → verified execution → compile pipeline.
- API contract pinned at v1.0.0 (`docs/openapi.json`).
- Self-hosted stack: Ollama (inference), Milvus (vector search), Postgres (state), Redis, SearXNG (web search), Open WebUI + pipelines (chat surface).
- Native web UI (`/web/jobs`) with live SSE execution streaming.
- Prometheus `/metrics` with LLM, HTTP RED, alert, job, and concurrency metrics.
- Three simulation sidecars for hardware-design workflows (ngspice, Verilator, SymbiYosys).
- SDK, CLI, and Open WebUI slash-command surfaces.

## [0.2.0] — 2026-04-14

Early pre-release: core orchestrator, initial DAG execution, and RAG pipeline.

[Unreleased]: https://github.com/LocketKeyLLC/scaffold-engine/compare/v1.3.0...HEAD
[1.3.0]: https://github.com/LocketKeyLLC/scaffold-engine/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/LocketKeyLLC/scaffold-engine/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/LocketKeyLLC/scaffold-engine/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/LocketKeyLLC/scaffold-engine/compare/v0.2.0...v1.0.0
[0.2.0]: https://github.com/LocketKeyLLC/scaffold-engine/releases/tag/v0.2.0
