# Scaffold Engine

A self-hosted DAG orchestration engine for multi-step LLM workflows. You give it an idea; it researches the topic, plans an execution graph, runs each step with verification, and hands back a compiled output. Everything runs locally on your hardware (Ollama for inference, Milvus for vector search, Postgres for state, SearXNG for web search) — no cloud calls unless you opt in.

This README is a **complete from-zero walkthrough**: every command, what it does, what you'll see, and what to do if it goes wrong. If you finish reading this end-to-end you should be able to clone the repo on a fresh machine and have your first compiled output ~45 minutes later.

For details beyond setup-and-first-run, read:

- **[USER_GUIDE.md](./USER_GUIDE.md)** — every command, organized by what you're trying to do (start a project, do research, run a manual walkthrough, schedule something recurring, …).
- **[OVERVIEW.md](./OVERVIEW.md)** — comprehensive technical reference. Architecture, every module, every public function, the full database schema, configuration, the TOON data format, the logging catalog, known issues, sprint history, and a glossary.

---

## What scaffold-engine actually does

You type an idea. The system:

1. **Refines** the idea into a structured brief (problem statement, success criteria, constraints).
2. **Assesses feasibility** and **halts**. You read the plan and approve it. This is the only deliberate pause point.
3. **Researches** the topic — runs SearXNG searches, fetches pages, distills facts into a knowledge base.
4. **Generates a DAG** of execution nodes (research / decision / action / validation / output). Each node has a tool assigned (LLM, code generation, web search, RAG retrieval).
5. **Executes** each node in dependency order. Output of upstream nodes becomes context for downstream ones. A verifier checks each result; failed nodes auto-retry up to three times.
6. **Compiles** the final output from the leaf nodes and stores it.

A typical run on CPU-only hardware takes 30–60 minutes for a non-trivial topic. Most of that time is the LLM thinking; you can watch progress stream in real time.

---

## Before you start — what you need

Before running the install steps below, make sure you have:

| Requirement | Why | How to verify |
|---|---|---|
| Docker + Docker Compose | The orchestrator, database, vector store, search engine, and chat UI all run as containers. | `docker --version` and `docker compose version` should both work. |
| Ollama installed on your **host** (not in a container) | Runs the local LLMs. The orchestrator reaches it through the docker bridge gateway. | `ollama list` should respond. Install from <https://ollama.ai>. |
| ~30 GB free disk | Models alone are ~25 GB; the vector store and Postgres add a few more. | `df -h .` |
| ~16 GB RAM | Milvus needs ~8 GB to load comfortably; Ollama loads models on demand. | `free -h` (Linux) / Activity Monitor (mac) |
| `git` and a UTF-8 terminal | For cloning and running commands. | `git --version` |

If you're on Pop!_OS / Ubuntu and Docker is fresh, also run `sudo usermod -aG docker $USER` and log out + back in so you can run `docker` without `sudo`.

---

## First-time install

These steps go from a fresh `git clone` to a running stack ready to take ideas. Run them in order.

### 1. Clone the repo

```bash
git clone https://github.com/LocketKeyLLC/scaffold-engine.git
cd scaffold-engine
```

You should see `app/`, `sdk/`, `cli/`, `pipelines/`, `db/`, `docker-compose.yml`, and a few other directories.

### 2. Create your `.env`

```bash
cp .env.example .env
```

Now open `.env` in your editor and set the four **required** values at the top — the file walks you through each one. The most important is `SCAFFOLD_API_KEY`, which gates every authenticated request to the orchestrator. Generate one with `openssl rand -hex 32` if you don't have a preferred secret-generation flow.

> **What can go wrong:** if you skip this step and start the stack, `docker compose` will refuse to bring up the orchestrator (the compose file has `${SCAFFOLD_API_KEY:?...}` as a hard requirement). The error message tells you which variable is missing.

### 3. Pull the local models

```bash
ollama pull qwen3:4b qwen2.5:7b qwen2.5-coder:7b nomic-embed-text qwen3.5:latest
```

This downloads the five default local models. Each is 1–8 GB except `nomic-embed-text` (~270 MB); on a typical home connection plan for ~10 minutes total. Ollama caches them in `~/.ollama/models`.

> **Embedder note:** the embedder is `nomic-embed-text` (137M params, 768-dim native, MRL-truncated to 512 to match Milvus). Earlier docs and pre-§17.81 deploys referenced `qwen3-embedding:8b`; that model wedges deterministically on Ollama 0.17.5 + this host's `--ollama-engine` path (OVERVIEW §17.81/82) and is no longer used. If you pulled it under prior instructions you can safely `ollama rm qwen3-embedding:8b`.

> **What can go wrong:**
> - "model not found" → Ollama isn't running. Start it: `ollama serve` (foreground) or check the systemd unit on Linux.
> - Slow download → Ollama shows download progress; if it stalls, Ctrl-C and retry. Resume is automatic.
> - You don't have to use all five. The system will tell you at request time which roles are missing models — see `make doctor` below.

### 4. Bring up the stack

```bash
docker compose up -d
```

This starts seven containers: orchestrator, Postgres, Milvus, Redis, SearXNG, Open WebUI, and the OWUI pipelines. First time takes ~3 minutes (image downloads + initial DB migration). Subsequent starts are ~15 seconds.

> **What you'll see:** `docker compose` prints a green checkmark per container as each becomes healthy. If any go red, check `docker compose logs <name>` for that container.

### 5. Verify everything is healthy

```bash
make doctor
```

This runs an end-to-end audit. Expected output is a short list of subsystem checks, all `OK`. The script also confirms your `.env` API key matches what the running containers are using and warns if they've drifted.

```bash
curl -H "X-API-Key: $SCAFFOLD_API_KEY" http://localhost:8000/health
```

Returns a JSON object with `status: "healthy"` and per-dependency latency numbers. Postgres, Milvus, Redis, and Ollama should all show `up`.

> **What can go wrong:**
> - `Cannot reach orchestrator` → run `docker ps` to confirm all containers are up. Check `docker logs scaffold-orchestrator` for startup errors.
> - `ollama: down` → host Ollama isn't running, or the bridge gateway isn't reaching it. Confirm `ollama list` works on the host. If yes, check that `OLLAMA_BASE_URL` in `.env` points at the bridge gateway (default `http://172.18.0.1:11434` for Pop!_OS / native Docker).

### 6. Open the chat UI

```bash
open http://localhost:3000
```

(Or just paste the URL into your browser.) Open WebUI loads. Create the local admin account (it's not federated; the credentials only exist on your machine). At the top of the chat window, the model selector should show **scaffold_router** (or any name containing "scaffold") — that's the OWUI pipeline that talks to the orchestrator.

If `scaffold_router` doesn't appear in the model dropdown, the OWUI pipelines container hasn't picked up the pipeline files yet. Run `docker restart open-webui-pipelines` and refresh the page.

---

## Your first project — end to end

Now type your first idea into the chat. For your first run, pick something small that the system can complete in 20–30 minutes:

> Build a Python script that lists files in a directory sorted by size, with human-readable file sizes.

Press Enter. The system replies with refined-brief output and a job ID. Copy the job ID (a UUID like `481010cd-9542-4b27-9af3-7c80f468af89`).

Then approve and execute:

```
/confirm 481010cd-9542-4b27-9af3-7c80f468af89
```

The system runs research → DAG generation → node execution, streaming progress events to your chat. When it's done you'll see the final compiled script.

Check progress at any time with:

```
/results 481010cd-9542-4b27-9af3-7c80f468af89
```

That command knows the job's current status and shows what to do next — including pre-filled commands if a node failed and needs a retry.

> **What can go wrong on your first run:**
> - Phase 2 (research) can take 10–25 minutes on CPU. The chat shows a visible "⏳ Phase 2 — researching + ingesting… (Xm YYs elapsed)" marker every ~2 minutes (§17.173). For sub-step detail, tail the orchestrator (`docker logs -f scaffold-orchestrator`) — it logs each SearXNG query, distillation batch, and Milvus ingest as it happens.
> - If the system says `awaiting_confirmation` and won't move forward, you skipped step 6 (the `/confirm` command).
> - If a DAG node fails after three auto-retries, it goes to `blocked`. Run `/results <job_id>` for a copy-pasteable retry or skip command.

---

## Day-to-day operations

Once the stack is running, these are the commands you'll use most often:

| What you want | Command | What it does |
|---|---|---|
| See live system health | `make health` | Hits `/health`, prints subsystem latencies. |
| Tail logs in real time | `make logs-follow` | `docker logs -f scaffold-orchestrator`, scroll-back included. |
| List active jobs | `make status` | Hits `/status`, prints a counts table + recent jobs. |
| Reap stale jobs | `make clean` | Triggers the cleanup endpoint; safe to run anytime. |
| Apply DB migrations | `make migrate` | The lifespan auto-applies migrations on startup; this is for force-runs. |
| Re-run health audit | `make doctor` | Full pre-flight, with explanations. |
| Show all targets | `make help` | Self-documenting Makefile; every target has a one-line description. |

Open WebUI is for chat-driven workflows. The Python SDK and `scaffold` CLI exist for programmatic access — see [USER_GUIDE.md](./USER_GUIDE.md) for examples of both.

---

## Updating

Pull the latest code and rebuild:

```bash
git pull
docker compose up -d --build
```

Migrations run automatically at startup. If a migration fails (rare), the orchestrator container will report it in its logs and refuse to start; review the error and check OVERVIEW.md §15 for migration policy.

For embedder model changes (rarely needed), see USER_GUIDE.md "Embedder portability" — switching embedders requires re-embedding the corpus, and the docs walk through that with `make reindex`.

---

## Tearing it down

```bash
docker compose down
```

Stops all containers but preserves volumes (Postgres data, Milvus collections, model caches).

```bash
docker compose down -v
```

Stops containers AND deletes volumes. **This loses every job, knowledge base entry, and schedule** — use with care.

```bash
git clean -fdx
```

Removes everything not tracked by git (your `.env`, build caches, etc.). Pair with the `-v` step above for a true clean slate.

---

## Project layout

```
scaffold-engine/
├── app/             FastAPI orchestrator. Endpoints, modules, schemas.
├── sdk/             Python client (scaffold-engine-client). Sync + async.
├── cli/             Terminal client (scaffold-engine-cli). Wraps the SDK.
├── pipelines/       5 Open WebUI pipelines. Slash-command surface.
├── db/              init.sql + forward-only migrations under db/migrations/.
├── docker/          Dockerfiles for the three simulation sidecars.
├── scripts/         bootstrap, doctor, init, sync-valves, reindex, …
├── tests/           pytest suite covering orchestrator + SDK + CLI.
├── docker-compose.yml      production runtime (no tests, no Makefile in image)
├── docker-compose.dev.yml  dev override (mounts tests, Makefile, docs)
├── Dockerfile              multi-stage: builder → runtime → dev
└── docs/openapi.json       v1.1.0 API contract (machine-readable)
```

---

## Optional surfaces

The default `docker compose up -d` brings up everything below — there's no opt-in step beyond a healthy stack. Listed here so you know what you have:

- **Prometheus `/metrics`** (no auth). Set `METRICS_ENABLED=true` in `.env` (default on). The orchestrator emits counters/gauges for job execution latency, node retry counts, RAG cache hit rates, error counts by type. Sample scrape: `curl http://localhost:8000/metrics`. See USER_GUIDE for the alert-rule starter pack.
- **Simulation sidecars.** Three FastAPI services at `127.0.0.1:8001-8003` for hardware-design tasks: `scaffold-ngspice` (analog SPICE), `scaffold-verilator` (digital SystemVerilog), `scaffold-symbiyosys` (formal verification). The orchestrator invokes them via the `ai-network` bridge; they isolate untrusted simulator input from the orchestrator's process tree. Each has its own `/health`; the orchestrator's `/health` aggregates them. If you don't run hardware-design workflows you can comment out the three `scaffold-*` services in `docker-compose.yml` without losing other functionality.
- **Native web UI.** `http://localhost:8000/web/jobs` is a server-rendered HTML browser for jobs, ideate/confirm forms, and live SSE-streamed execution progress. Auth-bypassed on the loopback bind, since `localhost:8000` is the operator's box.

---

## Status

Active solo development. v1.0.0 tagged 2026-05-07; API contract pinned at v1.1.0 (`docs/openapi.json`). The audit-flagged work queue (10 items) is closed in code; see OVERVIEW §16. For the current test-suite counts and any known failures, see OVERVIEW §14.1 — that section is updated each sprint; the README intentionally does not duplicate the number.

## License

See `LICENSE`.
