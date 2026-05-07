# Scaffold Engine — User Guide

A beginner-friendly reference for using Scaffold Engine through Open WebUI.

> Scaffold Engine takes an idea, refines it, researches it, plans a workflow, and executes that workflow — all from a chat interface. This guide covers every command you can type into the chat box.

---

## Getting started

**First-time install (fresh clone):**
1. `make bootstrap` — generates `.env`, creates the docker network + volumes, builds and starts every container. Takes 5–10 min on first run.
2. `make init` *(optional)* — interactive wizard to pick a provider per role (default Ollama) and collect API keys for cloud providers. Skip if you only use local Ollama.
3. `make doctor` — verifies every dependency. Re-run anytime something looks off.
4. Open the Open WebUI chat at **http://localhost:3000** and create your admin account.

**Day-to-day:**
1. Open **http://localhost:3000**.
2. Make sure the model selector shows **scaffold_router** (or a name containing "scaffold").
3. Start a new chat and type a message. That's it — you're in.

If a command fails with `401 Unauthorized`, the pipeline lost its API key. With `SCAFFOLD_VALVES_ENV_OVERRIDE=true` in `.env` (the bootstrap default) just `docker compose up -d` to refresh; otherwise `docker restart open-webui-pipelines` and check `make doctor` for drift.

---

## Configuration: `.env` is the single source of truth

Everything Scaffold Engine reads at runtime — secrets, API keys, per-role provider routing, timeouts — comes from **`.env`** in the repo root. Containers inherit it via docker-compose; pipelines read it through env-fallback when `SCAFFOLD_VALVES_ENV_OVERRIDE=true` (the bootstrap default).

**To change configuration:**

| Goal | Command |
|---|---|
| Pick which provider serves which role (Ollama / OpenAI / OpenAI-compatible) | `make init` |
| Generate / rotate API keys + secrets from scratch | `make bootstrap --force` |
| Wipe stale baked-in `api_key` values in `pipelines/*/valves.json` | `make sync-valves` |
| Verify everything matches | `make doctor` |
| Apply a `.env` change to the running stack | `make restart` |

**Per-role provider routing.** Each model role (`MODEL_GENERAL`, `MODEL_VERIFIER`, …) has a corresponding `MODEL_<ROLE>_PROVIDER` setting. Default is `ollama`; set to `openai` to route that role through `OPENAI_API_KEY` + `OPENAI_BASE_URL`. The reranker is config-locked and stays out of the provider system.

**OpenAI-compatible endpoints.** `OPENAI_BASE_URL` defaults to `https://api.openai.com/v1` but can point at vLLM, LocalAI, Ollama-OpenAI-mode, or any compatible server — one provider implementation, many backends.

**Friendly errors.** When a role-routed call fails, the error is enriched with `[role=<role> provider=<provider>]` and a remediation hint (rotate key, raise timeout, switch provider, etc.) — so the message you see in `make doctor`, the orchestrator logs, or an OWUI failure response tells you exactly what to fix.

---

## Embedder portability — when (and how) to reindex

The Milvus collection (`toon_v2`) is built around a **512-dim vector geometry**. That dimension is locked at the schema level — it's part of the collection's HNSW_SQ8 index, the orchestrator's `embedding_dim` setting, and the `truncate_and_normalize` helper that every ingest path runs through. You do **not** want to change it.

What you CAN change is which embedder produces the 512 numbers. `MODEL_EMBEDDER_PIPELINE` (and its provider, `MODEL_EMBEDDER_PIPELINE_PROVIDER`) names the model. Any embedder whose native output is ≥ 512 dimensions works — the orchestrator slices to the first 512 and L2-normalizes.

**The catch:** different embedders produce different geometries. If you swap `MODEL_EMBEDDER_PIPELINE` from `qwen3-embedding:8b` to `text-embedding-3-small` and don't reindex, every cosine similarity in your existing corpus is now **meaningless** — the old vectors and new query vectors live in different vector spaces. Searches will silently return garbage.

**To switch embedders safely:**

```bash
# 1. See how much data would be re-embedded.
make reindex REINDEX_ARGS="--dry-run --new-embedder text-embedding-3-small --new-provider openai"

# 2. Quiesce ingest (optional but recommended): stop any /research / /ingest jobs.

# 3. Run the live reindex.
make reindex REINDEX_ARGS="--new-embedder text-embedding-3-small --new-provider openai"

# 4. Update .env:
#    MODEL_EMBEDDER_PIPELINE=text-embedding-3-small
#    MODEL_EMBEDDER_PIPELINE_PROVIDER=openai

# 5. Pick up the new env in the live stack.
make restart
make doctor
```

**Flags `scripts/reindex.py` accepts** (pass via `REINDEX_ARGS`):

| Flag | Default | Purpose |
|---|---|---|
| `--new-embedder <model>` | current `MODEL_EMBEDDER_PIPELINE` | The embedder tag to re-embed with |
| `--new-provider ollama|openai` | current setting | Provider for the embedder role |
| `--domain eng|llm|rag|spec|prompt` | all five | Restrict to one Milvus partition (incremental migration) |
| `--batch-size <n>` | 32 | Embeddings per provider call |
| `--dry-run` | off | Count entries; make no changes |
| `--yes` | off | Skip the destructive-operation prompt |

The script preserves every entry field except `dense_vector`, `model_id`, and `updated_at`. Concurrent ingests during reindex are safe — each entry is upserted by primary key, so whichever write lands last wins, and reindex revisits any entry it sees on a later page.

**The reranker is also locked.** `MODEL_RERANKER` is a CrossEncoder singleton outside the provider system. Changing it requires a process restart but does **not** require reindexing — reranking happens at query time, not ingest time.

---

## How a typical session flows
You describe an idea
→ Triage assistant asks scoping questions
→ You type /go
→ System refines the idea + checks feasibility (Phase 1)
→ Halts at confirmation gate
→ You type /confirm <job_id>
→ System auto-runs research → DAG planning → execution (Phase 2+)
→ Final compiled output appears in chat

One pause point (the confirmation gate). Everything else auto-chains.

**Job statuses you'll see:**
`pending → refining → awaiting_confirmation → researching → planning → executing → running → completed` (or `failed` / `cancelled` / `blocked`)

---

## Commands at a glance

| When you want to... | Use |
|---|---|
| Start scoping an idea | Just type a message |
| Submit an idea fast (skip the chat) | `/idea <text>` |
| Lock the scope and refine | `/go` |
| Approve and execute | `/confirm <job_id>` |
| Walk through it yourself with help | `/assist <job_id>` |
| See what's running | `/status` |
| Read a finished job | `/results <job_id>` |
| Search what the system knows | `/rag <query>` |
| Add web content to the knowledge base | `/research <topic>` |
| Get help | `/help` |

Full reference below.

---

## 🗣 Scope & kickoff

### *(plain message)*
Whatever you type that doesn't start with `/` goes to the **triage assistant** — a lightweight model that asks scoping questions until your goal, scope, and constraints are clear. It maintains a 4-section view (scope so far / options / gaps / its pick) so you always know where you stand.

**Example:**
> I want to build a CLI tool that converts screenshots into a searchable PDF.

### `/go` or `/run`
Locks the scoped idea, runs **Phase 1** (idea refinement + feasibility check), and halts at a confirmation gate. You'll see a `job_id` in the response.

**Example:**
> `/go`

### `/idea <text>`
Submits an idea straight to Phase 1 with no triage chat. Use when you already know exactly what you want.

**Example:**
> `/idea Build a Python script that lists files in a directory sorted by size descending`

### `/confirm <job_id> [feedback]`
Approves a refined idea. The system then auto-chains: research → DAG planning → node execution → final output. Optional feedback applies tweaks before continuing.

**Examples:**
> `/confirm a4f2c891-...`
>
> `/confirm a4f2c891-... please use bash instead of python`

---

## ⚙ Workflow control

### `/execute <job_id>`
Manually runs all pending DAG nodes for a job. Use this if:
- You cancelled mid-execution and want to resume
- The auto-chain stalled
- You're re-running after a fix

### `/skip <job_id> <node_key>`
Skips a specific node so downstream nodes can proceed. Useful when one node is blocking on something you don't actually need.

**Example:**
> `/skip a4f2c891-... T3`

### `/results <job_id>`
Shows a job's output, in-flight progress, or failure details. For failed/blocked jobs it includes per-node failure info and pre-filled recovery commands (`/exec retry`, `/skip`).

**Example:**
> `/results a4f2c891-...`

### `/status`
Lists active jobs grouped by state, with recent activity. Quick overview of what's running, what's stuck, and what's done.

---

## 🤝 Assistant Mode (manual implementation)

Sometimes you don't want the engine to *run* the plan — you want to do it yourself, with the engine acting as a co-pilot. Assistant Mode walks you through the DAG one node at a time, shows you the prompt + upstream context for each step, and captures whatever you do (command output, a file diff, free text) as that node's output. Subsequent nodes pick up your output as their upstream context — exactly like the autonomous run.

### `/assist <job_id>`
Promotes a job into Assist Mode and renders the first step. The job moves from `planning`/`executing` to `assisted_executing`.

**Example:**
> `/assist a4f2c891-...`

### `/assist next <session_id>`
Fetches the next pending step (with deps satisfied) and shows you the prompt + upstream outputs.

### `` /assist submit <session_id> <node_key> ``
Submits your work as the node's output. **Multi-line evidence in a triple-backtick fence:**

````
/assist submit <session_id> <node_key>
```
$ ls -la
total 4
drwxr-xr-x  2 me me 4096 Jan 1 00:00 .
```
````

### `/assist skip <session_id> <node_key>`
Skips a node — downstream nodes still proceed.

### `/assist handoff <session_id> <node_key> [single|all]`
Hands a node back to the autonomous executor. `single` returns control on the next step; `all` lets autonomous take the rest of the DAG.

### `/assist pause <session_id>` / `/assist resume <session_id>`
Pauses / resumes; sessions persist across days.

### `/assist done <session_id>`
Shows the compiled output once all steps are terminal.

### `/assist friction <session_id> <node_key> <note>`
Logs a note for later post-mortem ("docs were wrong", "took 3 attempts").

> **Tip:** to make `/confirm` route into Assist Mode automatically, the admin can flip the `assist_after_confirm` valve on `scaffold_router`.

---

## 📚 Knowledge base

The system has a built-in vector database (Milvus) that stores knowledge it has researched. You feed it content via `/research`, and the workflow uses that content as grounding when executing tasks.

### `/rag <query>`
Searches the knowledge base directly and returns the top matches with relevance scores. Use this to check what the system already knows before researching.

**Example:**
> `/rag how does HNSW indexing work`

### `/research <topic>`
Autonomous web research — the system decomposes the topic, runs SearXNG searches, fetches pages, distills facts, and ingests them into the knowledge base. Returns a summary + Next steps when done.

**Optional flag:** `--depth shallow|medium|deep` (default: medium)
- `shallow` = 1 iteration (fastest, ~20–30 min on CPU)
- `medium` = 2 iterations
- `deep` = 4 iterations (most thorough)

**Example:**
> `/research kubernetes pod lifecycle --depth shallow`

### `/research <url>`
Ingests a single web page directly. No search, no decomposition — just fetch, distill, ingest. Faster than topic mode (~3–8 min).

**Example:**
> `/research https://en.wikipedia.org/wiki/Transformer_(machine_learning_model)`

### `/research github:<owner>/<repo>`
Ingests a GitHub repo's README, top-level Markdown files, `docs/**/*.md`, and module docstrings. Caps at 50 files.

**Example:**
> `/research github:anthropics/anthropic-sdk-python`

### `/research openapi:<url>`
Ingests an OpenAPI/Swagger spec. Each endpoint becomes one knowledge base entry. Caps at 200 endpoints.

**Example:**
> `/research openapi:https://petstore3.swagger.io/api/v3/openapi.json`

### `/research/reply <session_id> <msg>`
The research agent occasionally pauses and asks a clarifying question (when it can't disambiguate the topic). This command resumes the session by answering it.

**Example:**
> `/research/reply 7c2f1a98-... I meant the Python web framework, not the JavaScript one`

### `/research/pdf` *(not a chat command)*
PDFs are uploaded via the browser. Open **http://localhost:8000/research/pdf** for a drag-and-drop form, or use curl:
curl -F file=@spec.pdf http://localhost:8000/research/pdf

---

## 🗂 Manage saved work

Every command in this section uses the same pattern:
- `/<thing>` or `/<thing> list` → list
- `/<thing> find <text>` → search by name
- `/<thing> rename <id> <new name>` → rename
- `/<thing> delete <id>` → preview deletion (safe)
- `/<thing> delete <id> confirm` → permanent delete (within 5 min of preview)

### `/jobs <sub>`
List, filter, find, rename, or delete jobs.

**Examples:**
> `/jobs` — list 25 most recent
>
> `/jobs failed` — filter by status
>
> `/jobs find sorting` — search titles
>
> `/jobs delete a4f2c891` — preview
>
> `/jobs delete a4f2c891 confirm` — permanent
>
> `/jobs help` — show subcommand reference

Valid status filters: `pending`, `refining`, `awaiting_confirmation`, `researching`, `planning`, `executing`, `running`, `completed`, `failed`, `cancelled`, `blocked`.

### `/research/<sub>` (slash form)
Manage research sessions. Same five subcommands, but each is its own command.

**Examples:**
> `/research/list`
>
> `/research/find kubernetes`
>
> `/research/rename 7c2f1a98 new topic name`
>
> `/research/delete 7c2f1a98`
>
> `/research/delete 7c2f1a98 confirm`
>
> `/research/help`

> **Note:** Deleting a research session removes only the session metadata. Knowledge base entries already ingested from it are preserved.

### `/schedule <sub>`
Manage recurring research jobs (cron-style).

**Examples:**
> `/schedule list`
>
> `/schedule add "0 9 * * 1" latest llm research --depth=medium` *(every Monday at 9am)*
>
> `/schedule delete <schedule_id>`
>
> `/schedule help`

Cron format: `<minute> <hour> <day-of-month> <month> <day-of-week>`. Time zone defaults to UTC; pass `--tz=America/New_York` to override.

---

## 🔧 Configuration & utilities

### `/model <sub>`
View and switch which model handles each role (generation, verification, code, etc.).

**Subcommands:**
- `/model list` — show current assignments per role
- `/model available` — show all installed Ollama models
- `/model set <role> <model>` — assign a model to a role
- `/model reset` — restore defaults
- `/model probe <role>` — test the assigned model (round-trip a prompt)
- `/model help`

**Roles:** `general`, `verifier`, `coder`, `router`, `fallback`, `cloud_alt`. Embedder and reranker are config-locked (dimension-locked).

**Example:**
> `/model set coder qwen2.5-coder:7b`

### `/optimize <prompt>`
Runs a prompt through the optimizer to tighten and improve it. Useful before pasting into a node prompt or external system.

**Example:**
> `/optimize Write a function that sorts a list`

### `/help`
Shows the full command list grouped by category.

---

## Python SDK (programmatic access)

The orchestrator ships a typed Python client at [`sdk/`](./sdk/) for scripts and integrations. It pins to API **v1.0.0** and wraps every non-streaming endpoint with typed methods, plus async SSE helpers for `/research` and `/execute/all`. Full reference: [sdk/README.md](./sdk/README.md).

### Install (dev — orchestrator repo checkout)

```bash
pip install -e ./sdk
```

Once published: `pip install scaffold-engine-client`.

### Sync example

```python
from scaffold_client import Client

with Client("http://localhost:8000", api_key="...") as c:
    job = c.ideate("Build a markdown linter")
    print(job["job_id"], job["feasibility"]["feasible"])

    for row in c.jobs.list(limit=5)["jobs"]:
        print(row["status"], row["title"])
```

### Async + streaming example

```python
import asyncio
from scaffold_client import AsyncClient

async def main():
    async with AsyncClient("http://localhost:8000", api_key="...") as c:
        async for event in c.aiter_research("kubernetes operators"):
            if event["event"] == "convergence":
                break

asyncio.run(main())
```

Breaking out of the `async for` closes the stream cleanly; the orchestrator's keepalive watchdog finalizes the session as `cancelled` within ~2s.

### Errors

Every transport failure raises a subclass of `ScaffoldError` — catch the base class once, or branch on `AuthenticationError` / `NotFoundError` / `OrchestratorError` / etc. for specific UX. See [sdk/README.md](./sdk/README.md#errors) for the full table.

---

## Troubleshooting

| Symptom | Try this |
|---|---|
| `401 Unauthorized` on every command | `docker restart open-webui-pipelines` |
| `/help` doesn't render | Open a brand-new chat (Open WebUI sometimes caches) |
| File upload not picked up | Hard-refresh the browser, then start a new chat |
| Job stuck in `running` for hours | `/results <job_id>` to check; the reaper auto-recovers orphans after 60 min |
| `/research` returns "Research already in progress" | Likely an orphaned session; reaper clears it within 30 min |
| Triage feels stuck on the same question | Answer the gap directly — the prompt forces it to mark gaps `✓ covered` once you do |

For deeper debugging, the orchestrator logs are at:
docker logs --tail 200 -f scaffold-orchestrator

---

## Where things live (in case you need to peek)

| What | Where |
|---|---|
| Chat UI | http://localhost:3000 |
| PDF upload form | http://localhost:8000/research/pdf |
| Health check | http://localhost:8000/health |
| Pipeline code | `~/scaffold-engine/pipelines/scaffold_router.py` |
| Orchestrator code | `~/scaffold-engine/app/` |
| Python SDK | `~/scaffold-engine/sdk/` |
| OpenAPI snapshot (contract) | `~/scaffold-engine/docs/openapi.json` |
| Project overview (deep technical reference) | `~/scaffold-engine/scaffold-engine-overview.md` |

---

*This guide covers user-facing commands only. For architecture, model stack, internal APIs, and the full audit history, see `scaffold-engine-overview.md`.*
