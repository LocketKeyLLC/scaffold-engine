# Scaffold Engine

A self-hosted DAG orchestration engine for multi-step LLM workflows. Submit an idea, get a structured execution plan that is researched, ingested into a vector store, decomposed into a dependency graph, and executed node-by-node, all on local hardware.

Runs entirely on CPU-only inference (Ollama + Milvus + Postgres + Redis + SearXNG) with optional opt-in cloud models for heavy lifting.

## What it does

The pipeline: User idea -> Triage (qwen3:4b conversational refinement) -> Refine (structured brief) -> Feasibility (confidence + recommended research queries) -> HALT at awaiting_confirmation (user reviews) -> /confirm -> Research (SearXNG + LLM distill -> Milvus ingest) -> DAG (generated execution graph, Kahn cycle check) -> Execute (dependency order, SSE streamed, verifier-gated, auto-retry) -> Compile (final output assembled from leaf nodes).

## Quick start

Prerequisites: Docker + Docker Compose, Ollama installed on host, ~30GB disk for models and the vector store.

Steps:
1. git clone https://github.com/LocketKeyLLC/scaffold-engine.git
2. cd scaffold-engine
3. cp .env.example .env (then fill in the 4 REQUIRED values at the top)
4. ollama pull qwen3:4b qwen2.5:7b qwen2.5-coder:7b qwen3-embedding:8b qwen3.5:latest
5. docker compose up -d
6. curl -H "X-API-Key: $SCAFFOLD_API_KEY" http://localhost:8000/health
7. Open http://localhost:3000 for the Open WebUI front-end.

A complete pipeline run on CPU takes 30 to 60 minutes for a non-trivial topic. See the Performance section in the overview for details.

## Common operations

- Health check: make health
- Tail logs: make logs-follow
- Run full test suite: make test
- List active jobs: make status
- Reap stale jobs: make clean
- Apply DB migrations: make migrate
- Show all targets: make help

## Project layout

- app/ - orchestrator (FastAPI): main.py, modules/, utils/, middleware/, routers/
- pipelines/ - Open WebUI pipelines (chat-side commands)
- db/ - schema + migrations 002-022
- tests/ - ~745 tests covering orchestrator + pipelines
- docs/ - audit fix lists, TOON spec, CI notes
- scripts/ - retrieval scoring, toon collection setup

## Where to learn more

The single source of truth for everything else (architecture, full API reference, model stack, RAG pipeline internals, design decisions, performance benchmarks, every fix to date) lives in scaffold-engine-overview.md.

## Status

Active solo development. Test suite is green: 745 passing, 5 skipped pending KB content, 0 failing. Both audit fix lists are at zero open items as of April 2026.

## License

See LICENSE.
