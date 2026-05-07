# Scaffold Engine

A self-hosted DAG orchestration engine for multi-step LLM workflows. Submit an idea, get a structured execution plan that is researched, ingested into a vector store, decomposed into a dependency graph, and executed node-by-node — all on local hardware.

Runs entirely on CPU-only inference (Ollama + Milvus + Postgres + Redis + SearXNG). Cloud models opt-in for heavy roles. Pinned to **API v1.0.0**.

## What it does

The pipeline:

```
User idea
  → Triage (qwen3:4b conversational refinement)
  → Refine (structured brief)
  → Feasibility (confidence + recommended research queries)
  → HALT at awaiting_confirmation (user reviews)
  → /confirm
  → Research (SearXNG + LLM distill → Milvus ingest)
  → DAG (generated execution graph, Kahn cycle check)
  → Execute (dependency order, SSE streamed, verifier-gated, auto-retry)
  → Compile (final output assembled from leaf nodes)
```

## Quick start

Prerequisites: Docker + Docker Compose, Ollama installed on host, ~30 GB disk for models + the vector store.

```bash
git clone https://github.com/LocketKeyLLC/scaffold-engine.git
cd scaffold-engine

# 1. Fill in the 4 REQUIRED values at the top of .env
cp .env.example .env

# 2. Pull the local model stack (CPU-only)
ollama pull qwen3:4b qwen2.5:7b qwen2.5-coder:7b qwen3-embedding:8b qwen3.5:latest

# 3. Bring the stack up
docker compose up -d

# 4. Sanity check
curl -H "X-API-Key: $SCAFFOLD_API_KEY" http://localhost:8000/health

# 5. Open the chat UI
open http://localhost:3000
```

A complete pipeline run on CPU takes 30–60 minutes for a non-trivial topic.

## Common operations

```bash
make health         # /health round-trip
make logs-follow    # tail orchestrator logs
make test           # run the full test suite (in dev container)
make status         # /status round-trip
make clean          # reap stale jobs
make migrate        # apply DB migrations
make doctor         # full health audit (probes every dep + key sync)
make help           # show all targets
```

## Project layout

```
scaffold-engine/
├── app/                # orchestrator (FastAPI)
├── sdk/                # Python client (scaffold-engine-client)
├── cli/                # terminal client (scaffold-engine-cli)
├── pipelines/          # 5 Open WebUI pipelines (chat-side commands)
├── db/                 # init.sql + migrations 002–025
├── scripts/            # reindex.py, score_retrieval.py, doctor.sh, …
├── tests/              # ~900 tests covering orchestrator + SDK + CLI
└── docs/openapi.json   # v1.0.0 contract snapshot (sole survivor under docs/)
```

## Where to learn more

- **[USER_GUIDE.md](./USER_GUIDE.md)** — operator guide. Every chat command, programmatic SDK access, troubleshooting. The right read if you're driving the system.
- **[OVERVIEW.md](./OVERVIEW.md)** — comprehensive technical reference. Architecture, every module, every public function, the full database schema, configuration, the TOON data format, the logging catalog, known issues, and sprint history. The right read if you're modifying the system.
- **[docs/openapi.json](./docs/openapi.json)** — the v1.0.0 HTTP contract.

## Status

Active solo development. v1.0.0 tagged 2026-05-07. Roadmap items 1–10 of 12 done; items 11 (single-page web UI) and 12 (cost + latency telemetry) remain. Test baseline: orchestrator 899/14 pre-existing/5 skipped; SDK 88; CLI 38.

## License

See `LICENSE`.
