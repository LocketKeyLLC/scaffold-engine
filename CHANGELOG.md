# Changelog

Notable changes to scaffold-engine. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [SemVer](https://semver.org/).

Day-to-day development is tracked at sprint granularity in [OVERVIEW.md](./OVERVIEW.md) (§ sprint history) and in the commit log (`fix(§X.Y)` / `feat(§X.Y)` references). This file records the release-level view.

## [Unreleased]

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
- **Hardening** — two full multi-agent audits closed out (2026-06 architectural review; 2026-07-18 audit, ledger in `docs/AUDIT_2026-07-18.md`); cloud CI made trustworthy again (§17.718).
- **Governance** — LICENSE (BUSL-1.1), CONTRIBUTING, SECURITY, this changelog.

## [1.0.0] — 2026-05-07

First stable release.

- Full ideate → refine → feasibility-halt → research → DAG generation → verified execution → compile pipeline.
- API contract pinned at v1.1.0 (`docs/openapi.json`).
- Self-hosted stack: Ollama (inference), Milvus (vector search), Postgres (state), Redis, SearXNG (web search), Open WebUI + pipelines (chat surface).
- Native web UI (`/web/jobs`) with live SSE execution streaming.
- Prometheus `/metrics` with LLM, HTTP RED, alert, job, and concurrency metrics.
- Three simulation sidecars for hardware-design workflows (ngspice, Verilator, SymbiYosys).
- SDK, CLI, and Open WebUI slash-command surfaces.

## [0.2.0] — 2026-04-14

Early pre-release: core orchestrator, initial DAG execution, and RAG pipeline.

[Unreleased]: https://github.com/LocketKeyLLC/scaffold-engine/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/LocketKeyLLC/scaffold-engine/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/LocketKeyLLC/scaffold-engine/compare/v0.2.0...v1.0.0
[0.2.0]: https://github.com/LocketKeyLLC/scaffold-engine/releases/tag/v0.2.0
