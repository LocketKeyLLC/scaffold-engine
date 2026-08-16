# Changelog

Notable changes to scaffold-engine. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [SemVer](https://semver.org/).

Day-to-day development is tracked at sprint granularity in the commit log (`fix(§X.Y)` / `feat(§X.Y)` references). This file records the release-level view.

## [Unreleased]

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

[Unreleased]: https://github.com/LocketKeyLLC/scaffold-engine/compare/v1.2.0...HEAD
[1.2.0]: https://github.com/LocketKeyLLC/scaffold-engine/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/LocketKeyLLC/scaffold-engine/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/LocketKeyLLC/scaffold-engine/compare/v0.2.0...v1.0.0
[0.2.0]: https://github.com/LocketKeyLLC/scaffold-engine/releases/tag/v0.2.0
