# Changelog

Notable changes to scaffold-engine. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [SemVer](https://semver.org/).

Day-to-day development is tracked at sprint granularity in [OVERVIEW.md](./OVERVIEW.md) (§ sprint history) and in the commit log (`fix(§X.Y)` / `feat(§X.Y)` references). This file records the release-level view.

## [Unreleased]

- Ongoing §17.x sprint work since v1.0.0: execution-context and session-memory fixes, per-message plan derivation, observability/alerting expansion, retrieval-quality tooling, and closure of the 2026-07-18 audit work queue (see `docs/AUDIT_2026-07-18.md` / OVERVIEW §16).

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

[Unreleased]: https://github.com/LocketKeyLLC/scaffold-engine/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/LocketKeyLLC/scaffold-engine/compare/v0.2.0...v1.0.0
[0.2.0]: https://github.com/LocketKeyLLC/scaffold-engine/releases/tag/v0.2.0
