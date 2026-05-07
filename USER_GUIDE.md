# Scaffold Engine — User Guide

This guide is organized around **what you're trying to do**, not "every command in alphabetical order." Find the scenario that matches what's in your head, follow it end-to-end, and you'll see the relevant commands in context.

For setup, install, and your literal first run, see [README.md](./README.md). For architecture, internals, and reference docs, see [OVERVIEW.md](./OVERVIEW.md). The glossary at the bottom of OVERVIEW covers every project term.

> **Heads up about Open WebUI.** Most chat-driven scenarios below assume you're typing into Open WebUI at `http://localhost:3000`, with the `scaffold_router` model selected. The `scaffold` CLI and the Python SDK can do everything OWUI can — equivalents are listed at the end of each scenario.

---

## Table of contents

1. [How a project flows through the system](#how-a-project-flows-through-the-system)
2. [Job statuses you'll see](#job-statuses-youll-see)
3. **Scenarios:**
   - [A. I have an idea and want it built](#scenario-a--i-have-an-idea-and-want-it-built)
   - [B. I want the system to research a topic](#scenario-b--i-want-the-system-to-research-a-topic)
   - [C. I want to ingest a specific source (URL, GitHub repo, OpenAPI spec, PDF)](#scenario-c--i-want-to-ingest-a-specific-source)
   - [D. I want to walk through a project myself, with the system as co-pilot](#scenario-d--i-want-to-walk-through-a-project-myself-with-the-system-as-co-pilot)
   - [E. I want a research job to run on a schedule](#scenario-e--i-want-a-research-job-to-run-on-a-schedule)
4. [Programmatic access — Python SDK + CLI](#programmatic-access--python-sdk--cli)
5. [Configuration — `.env`, model overrides, pipeline valves](#configuration--env-model-overrides-pipeline-valves)
6. [Manage saved work — jobs, research sessions, schedules](#manage-saved-work)
7. [Embedder portability — when (and how) to reindex](#embedder-portability--when-and-how-to-reindex)
8. [Troubleshooting](#troubleshooting)
9. [Where everything lives](#where-everything-lives)

---

## How a project flows through the system

```
You describe an idea  ─▶  Triage assistant asks scoping questions
                         (just chat normally; no slash commands needed)
                                       │
                                       ▼
You type /go             System refines the idea + checks feasibility
                                  (Phase 1; ~2–9 minutes)
                                       │
                                       ▼
                          ⏸  HALT at confirmation gate
                          Read the plan; type /confirm <job_id>
                                       │
                                       ▼
                          System auto-runs Phase 2 + DAG + execution:
                          research → ingest → plan → execute → compile
                                  (~10–60 minutes total)
                                       │
                                       ▼
                          Final compiled output appears in chat
```

**One pause point.** The confirmation gate is the only deliberate stop. Everything before and after auto-chains.

**`/idea <text>` is a fast path.** If you already know exactly what you want and don't need triage chat, type `/idea Your full description here` instead of just chatting + `/go`.

---

## Job statuses you'll see

Every project (we call them "jobs" internally) moves through this state machine. The chat surfaces the current status in `/status` and `/results <job_id>` responses; the Python SDK exposes it as the `status` field on every job dict.

| Status | What it means | Typical duration | What you can do |
|---|---|---|---|
| `pending` | Just created; refinement hasn't started yet | <5 sec | Wait |
| `refining` | LLM is producing the structured brief | 1–3 min | Wait |
| `awaiting_confirmation` | Brief is done; system is waiting on your `/confirm` | unbounded | `/confirm <job_id>` to proceed, or `/jobs delete <job_id>` to abandon |
| `researching` | Phase 2 — searching, fetching, distilling, ingesting | 10–25 min | Wait. Watch progress with `/results <job_id>` |
| `planning` | Generating the DAG | 7–9 min | Wait |
| `executing`, `running` | Running DAG nodes in dependency order | 10–60+ min | Wait. `/skip <job_id> <node_key>` if a node is stuck |
| `completed` | Final output is in the job record | terminal | `/results <job_id>` to read it |
| `failed`, `blocked` | A node failed verification 3 times, or some other unrecoverable error | terminal | `/results <job_id>` shows next-step retry/skip commands |
| `cancelled` | Job was abandoned (manually or by the reaper) | terminal | `/jobs delete <job_id>` to remove |
| `assisted_executing`, `assisted_running`, `assisted_paused` | Job is being walked through by you in Assist Mode (scenario D) | varies | See scenario D for the assist-mode commands |

The `next_actions` machinery (delivered in audit item 10) means **every status carries its own list of valid next-step commands** — both `/results <job_id>` and the SDK's `client.jobs.status(id)` return them with placeholders pre-filled.

---

## Scenario A — I have an idea and want it built

This is the most common path. You describe what you want; the system builds it.

### A.1 The path you'll take

1. Open `http://localhost:3000` in a browser. Make sure the model selector at the top shows `scaffold_router` (or any name containing "scaffold").
2. Type your idea as a normal chat message. For your first run, keep it small and self-contained — for example:
   > Build a Python script that watches a folder and gzips any file older than 7 days.
3. The triage assistant replies with scoping questions. Answer them in plain English. The triage maintains a 4-section view (scope so far / options / gaps / its recommendation), so you can always tell where the conversation stands.
4. Once scope is locked, type `/go`. The system runs Phase 1 (idea refinement + feasibility) and **halts at the confirmation gate**. You'll see a refined brief and a job ID like `a4f2c891-1234-...`.
5. Read the brief. If it matches what you want, type `/confirm a4f2c891-1234-...`. Optional: append free-text feedback to tweak the plan before continuing — `/confirm a4f2c891-1234-... please use bash instead of python`.
6. The system auto-chains research → DAG → execute. Progress streams to the chat.
7. When the job hits status `completed`, the final output appears in the chat. Re-fetch any time with `/results <job_id>`.

### A.2 Skipping the triage chat

If you already know exactly what you want, use `/idea` instead:

> `/idea Build a Python script that watches a folder and gzips any file older than 7 days.`

This jumps straight to Phase 1 — no triage conversation. Same `/confirm` flow afterward.

### A.3 What can go wrong, and what to do

| Symptom | What's happening | Fix |
|---|---|---|
| `/confirm` command does nothing | The job isn't in `awaiting_confirmation` | Run `/results <job_id>` — it'll tell you the actual state and the right next command |
| A node fails after three retries | Job moves to `blocked`. Common cause: an LLM verifier rejected the output 3× | `/results <job_id>` shows a `/exec retry <job_id> <node_key>` line ready to copy. Or `/skip <job_id> <node_key>` to abandon that node and let downstream proceed |
| Whole job is `failed` and nothing is retryable | An upstream phase (research / planning) hit an unrecoverable error | `/results <job_id>` shows the error summary; the suggested next step is usually `/jobs delete <job_id>` and re-running with a tighter prompt |
| Phase 2 (research) takes a very long time | This is normal — 10–25 min on CPU is the baseline. Use `/results` to confirm work is happening | If it's stalled (no progress in container logs for 30+ min), the reaper will eventually mark it `cancelled`; you'll need to re-run |

### A.4 Equivalents in the CLI and SDK

```bash
# CLI:
scaffold ideate "Build a Python script that watches a folder and gzips files older than 7 days"
# (prints job_id; copy it)
scaffold confirm <job_id>
scaffold jobs status <job_id>     # progress + next_actions list
```

```python
# SDK:
from scaffold_client import Client

with Client("http://localhost:8000", api_key="...") as c:
    job = c.ideate("Build a Python script that watches a folder and gzips files older than 7 days")
    job_id = job["job_id"]
    c.confirm(job_id)
    while True:
        s = c.jobs.status(job_id)
        if s["job_status"] == "completed":
            print(s["compiled_output"])
            break
```

For SSE-streamed execution with live progress events, use `AsyncClient.aiter_execute_all(job_id)` — the SDK README has a full streaming example.

---

## Scenario B — I want the system to research a topic

Sometimes you don't want a project — you just want the knowledge base to learn about something so future projects can ground in that content. Use `/research`.

### B.1 The path

In chat:

> `/research kubernetes pod lifecycle --depth medium`

The system decomposes the topic into queries, runs them through SearXNG, fetches each result page (trafilatura extraction), distills facts via LLM, and ingests them into the Milvus knowledge base.

**Depth flag:**
- `--depth shallow` — 1 iteration. Fastest (~20 min on CPU). Good for narrow topics.
- `--depth medium` — 2 iterations. Default. Most balanced.
- `--depth deep` — 4 iterations. Most thorough; can take 60–90 min.

### B.2 Watching progress

The chat receives SSE events as research happens:

```
🔍 Decomposing "kubernetes pod lifecycle" → 6 queries
🔎 Searching: pod lifecycle phases (12 results)
📄 Fetching 8 of 12 pages…
✨ Extracted 14 facts
💾 Ingested 11 (3 deduplicated)
🔁 Iteration 2 of 2 — gap analysis: needs more on "init containers"
…
✅ Research complete — 27 entries added to the knowledge base
```

If the agent gets stuck on ambiguity (it can't tell if you mean Python's `requests` or the JS `requests` library, say), it pauses and asks:

```
⏸ Research paused — clarification needed
Question: Did you mean kubernetes "pods" or generic Linux processes?
Reply with: /research/reply <session_id> <your answer>
```

Reply with what you meant; the agent resumes from that exact iteration.

### B.3 Querying what was learned

After research finishes, you can interrogate the knowledge base directly:

> `/rag pod lifecycle phases`

Returns the top matches with scores. RAG retrieval also runs automatically during DAG execution — every code-gen or analysis node pulls relevant entries as upstream context.

### B.4 What can go wrong

| Symptom | Fix |
|---|---|
| `Research already in progress` | An orphaned session — the reaper clears it within 30 min, or `/research/list` to find it and `/research/delete` it |
| Agent never converges (keeps gap-analyzing) | Cap with `--depth shallow` next time; or kill the session via `/research/delete <session_id>` |
| SearXNG returns no results | Either `searxng` container is down (`docker ps`), or the query happens to not match anything indexed. Reword |

---

## Scenario C — I want to ingest a specific source

Skip the search step entirely when you already know the source. Four direct-ingest modes:

### C.1 A single web page

> `/research https://en.wikipedia.org/wiki/Transformer_(machine_learning_model)`

Just fetches that one page, runs trafilatura extraction, distills, ingests. ~3–8 min. No search, no decomposition.

### C.2 A GitHub repository

> `/research github:anthropics/anthropic-sdk-python`

Fetches the README, top-level Markdown files, anything in `docs/**/*.md`, and Python module docstrings. Caps at 50 files (configurable via `GITHUB_MAX_FILES` in `.env`). Faster than topic mode (~1–5 min) since there's no LLM distillation step — the docs are already structured.

If you need authenticated access (private repos or higher rate limits), set `GITHUB_TOKEN` in `.env` and `make restart`.

### C.3 An OpenAPI/Swagger specification

> `/research openapi:https://petstore3.swagger.io/api/v3/openapi.json`

Fetches the spec, validates it, flattens to one knowledge base entry per endpoint (with summary, parameters, response schema). Caps at 200 endpoints.

This is particularly useful for grounding code-generation tasks in a target API — every entry has the endpoint URL, method, and full schema, so the LLM gets typed context.

### C.4 A PDF document

PDFs are uploaded via the orchestrator's HTTP endpoint, not chat (the chat path doesn't support file uploads cleanly).

```bash
curl -F file=@spec.pdf -H "X-API-Key: $SCAFFOLD_API_KEY" http://localhost:8000/research/pdf
```

Or open `http://localhost:8000/research/pdf` in a browser — the orchestrator serves a drag-and-drop form there. Either way, the response streams SSE events through to the caller.

The extractor defaults to `auto`: tries `pypdf` first, falls back to `pdfplumber` if pypdf returns empty (scanned PDFs typically need plumber). Pin one explicitly with `?extractor=pypdf` or `?extractor=plumber` in the URL.

### C.5 Routine maintenance — wipe knowledge base entries

Knowledge base entries don't auto-expire by default; they have TTLs by source type (see OVERVIEW §12.3 for the policy). The staleness sweeper (`app/utils/staleness.py`) runs as part of the cleanup loop and removes entries past their TTL. To force-remove a session and its entries:

```
/research/delete <session_id>
```

then to permanently delete:

```
/research/delete <session_id> confirm
```

(within 5 minutes of the preview).

---

## Scenario D — I want to walk through a project myself, with the system as co-pilot

Sometimes you don't want autonomous execution — you want to drive each step yourself with the engine as a smart co-pilot. **Assist Mode** runs the same DAG as autonomous execute, but each node waits on you to provide the output (your text, command output, file diff, screenshot — anything captured as evidence). Subsequent nodes pick up your output as their upstream context, exactly as autonomous nodes would.

### D.1 Promoting a job into Assist Mode

After `/confirm` produces a DAG (job is in `planning` or `executing`), promote it:

> `/assist <job_id>`

The job moves to `assisted_executing`. The system replies with an `assist_session_id` and renders the first step (the prompt the LLM would have used + any upstream context).

### D.2 Working through steps

| Command | What it does |
|---|---|
| `/assist next <session_id>` | Fetches the next pending step (whose deps are met) |
| `/assist submit <session_id> <node_key>` followed by your evidence in a triple-backtick fence | Records your evidence as the node's output and unblocks downstream |
| `/assist skip <session_id> <node_key>` | Marks the node skipped without evidence — downstream still proceeds |
| `/assist handoff <session_id> <node_key> single` | Hands one node back to autonomous execution; control returns on the next step |
| `/assist handoff <session_id> <node_key> all` | Hands the entire remaining DAG to autonomous execution |
| `/assist pause <session_id>` | Pauses the session; resumable across days (sessions persist) |
| `/assist resume <session_id>` | Picks up where you left off |
| `/assist friction <session_id> <node_key> <note>` | Logs a free-text note (e.g. "the docs were misleading") for post-mortem review |
| `/assist done <session_id>` | Once every step is terminal, shows the compiled output |

### D.3 The mirror-to-DAG invariant

When you commit a step, your evidence is **mirrored to `dag_nodes.output_text` in the same database transaction** as the assist-step row. Downstream paths (`_compile_output`, `_fetch_upstream_outputs`, the RAG-grounding code) consume that output exactly as if it had come from autonomous execution. **No part of the system needs to know the difference** — you can mix autonomous and human steps freely via `/assist handoff`.

### D.4 Re-plan policies

If you supply evidence that diverges substantially from what the LLM expected, downstream nodes might no longer fit. Three policies decide what happens:

| Policy | Behavior | LLM cost |
|---|---|---|
| `context_only` (default) | No regeneration; the upstream-last assembly absorbs divergence implicitly | Zero |
| `selective` | A small classifier model checks for divergence; if it flags major drift, the affected subgraph resets so you can re-walk it | Per divergence |
| `full` | All pending nodes regenerate — discouraged | High |
| `disabled` | No detection at all | Zero |

Default is `context_only` — works well for most uses. Set per-session at `/assist start` time, or via the `assist_default_replan_policy` valve on `scaffold_router`.

### D.5 Auto-routing `/confirm` into Assist Mode

If you usually work in Assist Mode rather than autonomous, flip the `assist_after_confirm` valve on `scaffold_router` (Open WebUI admin → pipelines → scaffold_router → valves). After that, every `/confirm` lands directly in Assist Mode without a separate `/assist <job_id>` step.

### D.6 Driving an entire job from the terminal — the U.8.B verb set

After U.8.B every job-control endpoint has a CLI verb so you never have to switch to chat for execution control:

```bash
scaffold ideate "build a markdown linter"        # Phase 1
scaffold confirm <job_id>                        # Phase 2 (curl-equivalent — no auto-chain)
scaffold status                                   # counts + recent + next_actions
scaffold whatnow                                  # actionable jobs only
scaffold dag <job_id>                             # node table
scaffold dag <job_id> --mermaid                   # paste-into-docs diagram
scaffold logs <job_id>                            # per-node state + output preview
scaffold logs <job_id> --include-output           # full output_text per node
scaffold exec retry <job_id> <node_key>           # reset a failed node
scaffold skip <job_id> <node_key>                 # mark skipped, unblock downstream
scaffold jobs cleanup                             # sweep stale jobs (admin)
scaffold rag "your query"                         # KB query (bare form still works)
scaffold rag dedup                                # near-duplicate rejection log
scaffold research reply <sid> "yes, proceed"      # resume a paused research session
scaffold research pdf ./design-spec.pdf           # ingest a local PDF
```

`scaffold confirm` is intentionally curl-equivalent (Phase 2 only). The OWUI auto-chain (`/confirm` → `/dag` → `/execute/all`) lives in `pipelines/scaffold_router.py`; a CLI `--chain` flag is a future track.

### D.7 Driving Assist Mode from the terminal or SDK

Every chat verb above has a CLI and SDK equivalent (added in U.8.A). The session is stateless on every surface — paste the `<session_id>` in each call.

```bash
# CLI walkthrough (parallels /assist in chat)
scaffold assist start <job_id>
scaffold assist next  <session_id>
scaffold assist submit <session_id> <node_key> --output "ran ok"
scaffold assist submit <session_id> <node_key> --file diff.patch --evidence-kind file_diff
scaffold assist skip   <session_id> <node_key>
scaffold assist handoff <session_id> <node_key> --mode single        # streams SSE
scaffold assist pause   <session_id>
scaffold assist resume  <session_id>
scaffold assist abandon <session_id> --yes
scaffold assist friction add  <session_id> <node_key> "took 3 tries"
scaffold assist friction list <session_id>
scaffold assist status <session_id>
```

```python
# SDK — sync
from scaffold_client import Client

with Client("http://localhost:8000", api_key="...") as c:
    sess = c.assist.start(job_id, replan_policy="disabled")
    step = c.assist.next(sess["session_id"])
    c.assist.submit(sess["session_id"], step["node_key"],
                    output="ran make test, all green",
                    evidence_kind="command_output")

# SDK — async, with streaming handoff
from scaffold_client import AsyncClient

async with AsyncClient("http://localhost:8000", api_key="...") as ac:
    async for evt in ac.aiter_assist_handoff(session_id, "T2", mode="all_remaining"):
        print(evt["event"], evt["data"])
```

The streaming `/handoff` endpoint is async-only (mirrors `aiter_research` and `aiter_execute_all`). The CLI `assist handoff` runs an asyncio loop internally and prints events as they arrive — Ctrl-C is safe; the orchestrator finalizes any in-flight node.

---

## Scenario E — I want a research job to run on a schedule

Recurring research is run by APScheduler in-process. Schedules persist across restarts (rehydrated from the `scheduled_jobs` table on lifespan startup).

### E.1 Adding a schedule

In chat:

> `/schedule add "0 9 * * 1" latest llm research --depth=medium`

Cron format: `<minute> <hour> <day-of-month> <month> <day-of-week>`. The example fires every Monday at 09:00.

Time zones default to UTC. Override per-schedule:

> `/schedule add "0 9 * * 1" latest llm research --tz=America/New_York`

The `tz` field accepts any IANA zone name.

### E.2 Listing and deleting

> `/schedule list`

Shows every saved schedule with its next-run timestamp, last run status, last run's job ID, and run count.

> `/schedule delete <schedule_id>`

Removes the schedule. Already-completed `research_sessions` from past runs are preserved.

### E.3 What happens when a scheduled run fires

APScheduler invokes the same `run_research` generator that handles interactive `/research`. The session is timeout-bounded by `scheduler_job_timeout` (default 3600s); after that the run is finalized as `last_status='timeout'`. The misfire grace window (`scheduler_misfire_grace_time`, default 300s) covers brief downtime — if the orchestrator is offline when a fire is due but starts within the grace window, the job runs.

### E.4 What can go wrong

| Symptom | Fix |
|---|---|
| Schedule didn't fire | Check `scheduler_enabled` is `true` in `.env`; check `make doctor` reports the scheduler running |
| Two scheduled runs racing for the same topic | The UNIQUE partial index on `research_sessions.status='running'` prevents this — the second run hits a 409 and finalizes as `last_status='failed'` with a clear error |
| `last_status` stuck on `running` after a clean restart | The pre-migration startup sweep (audit item 7, in lifespan) cancels stale `running` rows on every boot. Re-running `make restart` cleans them |

---

## Programmatic access — Python SDK + CLI

Everything you can do in chat, you can do programmatically. Two surfaces:

### Python SDK

Pip-installable typed client at `sdk/`. Pinned to API v1.0.0.

```bash
pip install -e ./sdk        # development install
# or, once published:
pip install scaffold-engine-client
```

**Sync example:**

```python
from scaffold_client import Client

with Client("http://localhost:8000", api_key="...") as c:
    health = c.health()
    job = c.ideate("Build a markdown linter")
    c.confirm(job["job_id"])

    # Pretty-formatted recovery actions are on every status response:
    s = c.jobs.status(job["job_id"])
    for action in s["next_actions"]:
        print(f"  {action['action']:12} {action.get('command') or action['endpoint']}")
```

**Async + streaming example:**

```python
import asyncio
from scaffold_client import AsyncClient

async def main():
    async with AsyncClient("http://localhost:8000", api_key="...") as c:
        async for event in c.aiter_research("kubernetes operators"):
            if event["event"] == "convergence":
                break
            print(event["event"], event["data"])

asyncio.run(main())
```

Breaking out of the `async for` loop closes the SSE stream cleanly; the orchestrator's keepalive watchdog finalizes the session as `cancelled` within ~2s. No explicit cancel call needed.

**Errors.** Every transport failure raises a subclass of `ScaffoldError`. Catch the base class once for "any failure," or branch on `AuthenticationError` / `NotFoundError` / `OrchestratorError` etc. for specific UX.

### `scaffold` CLI

Wraps the SDK with a `click`-based command-line surface. Same authentication; same operations. As of Sprint U.7 the CLI reaches full parity with the OWUI surface.

```bash
# Basics
scaffold version                            # CLI version + config source
scaffold doctor                             # /health probe
scaffold whatnow                            # every job needing attention

# Idea → run
scaffold ideate "build a markdown linter"   # Phase 1
scaffold confirm <job_id>                   # auto-chains research/plan/exec
scaffold project new "..."                  # idea + friendly nickname
scaffold project resume <nickname-or-uuid>  # dispatch the next valid action

# Jobs
scaffold jobs list [--status running]
scaffold jobs status <job_id>
scaffold jobs find "linter"                 # title substring search
scaffold jobs rename <job_id> "new title"
scaffold jobs delete <job_id> [--yes]
scaffold skip <job_id> <node_key>           # unblock a stuck DAG node

# Research + knowledge base
scaffold research topic "kubernetes pods" --depth medium
scaffold research url https://example.com/page
scaffold research github anthropics/anthropic-sdk-python
scaffold research openapi https://petstore3.swagger.io/api/v3/openapi.json
scaffold research list / find / rename / delete
scaffold rag "milvus index"                 # query the KB

# Schedules
scaffold schedule list
scaffold schedule add "0 9 * * 1" "topic" --depth medium --tz America/New_York
scaffold schedule delete <id> [--yes]

# Misc
scaffold optimize "Please could you maybe write a function that..."
scaffold model list                          # current per-role assignments
scaffold model available                     # what Ollama has loaded
scaffold config show [--filter model] [--non-defaults]
scaffold explain <status>                    # local plain-English status lookup
```

Streaming endpoints (`research topic/url/github/openapi`) print one event per line and exit on convergence; Ctrl-C cleanly cancels the orchestrator session.

Config resolution priority: `--api-key` flag > `SCAFFOLD_API_KEY` env > `~/.scaffold/config.toml` > walked-up `.env` > default.

---

## Configuration — `.env`, model overrides, pipeline valves

Everything the system reads at runtime — secrets, API keys, per-role provider routing, timeouts — comes from **`.env`** in the repo root. Containers inherit it via docker-compose; pipelines read it through env-fallback when `SCAFFOLD_VALVES_ENV_OVERRIDE=true` (the bootstrap default).

### Common configuration tasks

| Goal | Command |
|---|---|
| Pick which provider serves which role (Ollama / OpenAI / OpenAI-compatible) | `make init` (interactive wizard) |
| Generate / rotate API keys + secrets from scratch | `make bootstrap --force` |
| Wipe stale baked-in `api_key` values from `pipelines/*/valves.json` | `make sync-valves` |
| Verify everything matches | `make doctor` |
| Apply a `.env` change to the running stack | `make restart` |

### Model role assignments

Eight valve-switchable roles. Default model in parentheses; override via `MODEL_<ROLE>` in `.env` or via `/model set <role> <model>` in chat:

- **General** (`qwen3-vl:235b-instruct-cloud`) — main generation, used by ideation/research
- **Verifier** (`qwen2.5:7b`) — validates LLM outputs
- **Coder** (`qwen2.5-coder:7b`) — CodeGen tool nodes
- **Router** (`qwen3:4b`) — DAG planning, gap analysis (cheap + fast)
- **Fallback** (`qwen3.5:latest`) — cascade fallback
- **Cloud heavy** (`qwen3-vl:235b-instruct-cloud`) — heavy alternative
- **Cloud alt** (`qwen3.5:397b-cloud`) — heaviest model
- **Embedder** (`qwen3-embedding:8b`) — config-locked, dimension-locked at 512d (see "Embedder portability" below)
- **Reranker** (`tomaarsen/Qwen3-Reranker-0.6B-seq-cls`) — config-locked CrossEncoder singleton

`/model set <role> <model>` works for the first seven. The embedder and reranker need a config change + restart (and the embedder also needs a corpus reindex — see below).

### Per-role provider routing (Sprint E+)

Each role has a corresponding `MODEL_<ROLE>_PROVIDER` setting. Default is `ollama` everywhere; flip to `openai` to route that role through `OPENAI_API_KEY` + `OPENAI_BASE_URL`. The reranker is exempt — it's outside the provider system.

`OPENAI_BASE_URL` defaults to `https://api.openai.com/v1` but can point at vLLM, LocalAI, Ollama-OpenAI-mode, or any compatible endpoint. One provider implementation, many backends.

When a role-routed call fails, the error is enriched with `[role=<role> provider=<provider>]` and a remediation hint (rotate key, raise timeout, switch provider) — so the message you see in `make doctor`, the orchestrator logs, or an OWUI failure response tells you exactly what to fix.

### Pipeline valves (Open WebUI admin)

Each pipeline (`scaffold_router`, `execution_handler`, `dag_viewer`, `gt_browser`, `prompt_inspector`) has its own `valves.json`. The OWUI admin UI lets you edit them at runtime. Notable valves on `scaffold_router`:

| Valve | Default | What it controls |
|---|---|---|
| `api_key` | `(env fallback)` | API key sent to the orchestrator. Empty + env var falls through to `SCAFFOLD_API_KEY`. |
| `orchestrator_url` | `http://scaffold-orchestrator:8000` | Where to send chat-driven calls |
| `request_timeout` | 30 | Non-streaming request timeout (seconds) |
| `stream_timeout` | 3600 | SSE streaming timeout (seconds) |
| `assist_after_confirm` | False | If True, `/confirm` routes into Assist Mode automatically |
| `assist_default_handoff_policy` | `manual` | Default for new assist sessions: `manual` or `all` |
| `assist_default_replan_policy` | `context_only` | Default for new assist sessions |

---

## Manage saved work

### `/jobs <sub>` — list, find, rename, delete projects

Same pattern across every "manage saved work" command:

| Subcommand | What it does |
|---|---|
| `/jobs` or `/jobs list` | List 25 most recent (paginate with `--limit` and `--offset`) |
| `/jobs <status>` | Filter by status — e.g. `/jobs failed`, `/jobs running` |
| `/jobs find <text>` | Search by job title |
| `/jobs rename <id> <new title>` | Rename |
| `/jobs delete <id>` | Preview deletion (safe; no actual delete yet) |
| `/jobs delete <id> confirm` | Permanent (within 5 min of preview) |
| `/jobs help` | Show subcommand reference in chat |

Valid status filters: `pending`, `refining`, `awaiting_confirmation`, `researching`, `planning`, `executing`, `running`, `completed`, `failed`, `cancelled`, `blocked`, `assisted_executing`, `assisted_running`, `assisted_paused`.

### `/research/<sub>` — manage research sessions

Same five subcommands as `/jobs`, but each is its own command rather than positional:

- `/research/list`, `/research/find <text>`, `/research/rename <id> <new>`, `/research/delete <id>`, `/research/delete <id> confirm`, `/research/help`.

> Deleting a research session removes only the session metadata. Knowledge base entries already ingested from it are preserved.

### `/schedule <sub>` — manage scheduled research jobs

See scenario E above.

---

## Embedder portability — when (and how) to reindex

The Milvus collection (`toon_v2`) is built around a **512-dim vector geometry**. That dimension is locked at the schema level — it's part of the collection's HNSW_SQ8 index, the orchestrator's `embedding_dim` setting, and the `truncate_and_normalize` helper that every ingest path runs through. You should **not** change it.

What you CAN change is which embedder produces the 512 numbers. `MODEL_EMBEDDER_PIPELINE` (and `MODEL_EMBEDDER_PIPELINE_PROVIDER`) names the model. Any embedder whose native output is ≥ 512 dimensions works — the orchestrator slices to the first 512 and L2-normalizes.

**The catch:** different embedders produce different vector geometries. If you swap `MODEL_EMBEDDER_PIPELINE` from `qwen3-embedding:8b` to `text-embedding-3-small` and don't reindex, every cosine similarity in your existing corpus is now meaningless — old vectors and new query vectors live in different vector spaces. Searches will silently return garbage.

**To switch embedders safely:**

```bash
# 1. See how much data would be re-embedded.
make reindex REINDEX_ARGS="--dry-run --new-embedder text-embedding-3-small --new-provider openai"

# 2. Quiesce ingest (optional but recommended): stop any /research / ingest jobs.

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
| `--new-embedder <model>` | current `MODEL_EMBEDDER_PIPELINE` | Embedder tag to re-embed with |
| `--new-provider ollama|openai` | current setting | Provider for the embedder role |
| `--domain eng|llm|rag|spec|prompt` | all five | Restrict to one Milvus partition (incremental migration) |
| `--batch-size <n>` | 32 | Embeddings per provider call |
| `--dry-run` | off | Count entries; make no changes |
| `--yes` | off | Skip the destructive-operation prompt |

The script preserves every entry field except `dense_vector`, `model_id`, and `updated_at`. Concurrent ingests during reindex are safe — each entry is upserted by primary key.

**The reranker is also locked.** `MODEL_RERANKER` is a CrossEncoder singleton outside the provider system. Changing it requires a process restart but does **not** require reindexing — reranking happens at query time, not ingest time.

---

## Troubleshooting

| Symptom | Most likely cause | Try this |
|---|---|---|
| `401 Unauthorized` on every command | Pipeline lost its API key | `docker restart open-webui-pipelines`. Confirm `SCAFFOLD_API_KEY` is set in `.env` and `make doctor` reports key sync OK |
| `/help` doesn't render | Open WebUI is caching a stale model | Open a brand-new chat (top-right + sign) |
| File upload not picked up | OWUI session state | Hard-refresh the browser (Ctrl/Cmd-Shift-R) and start a new chat |
| Job stuck in `running` for hours | Either an in-flight LLM call or an orphaned node | `/results <job_id>` to check; the reaper auto-recovers orphans after 60 min (`NODE_ORPHAN_THRESHOLD_MINUTES`) |
| `/research` returns "Research already in progress" | Orphaned session at `status='running'` | Reaper clears it within 30 min, or `/research/list` to find it and delete manually |
| Triage feels stuck on the same question | The triage prompt forces gap-tracking | Answer the gap directly; the triage will mark it `✓ covered` once you do |
| `make doctor` says key sync is drifting | `.env`, `valves.json`, `~/.bashrc`, container env, OWUI pipelines container env disagree | `make sync-valves` wipes baked-in `api_key` from `valves.json`; set the env, `make restart`, re-run `make doctor` |
| Container restart causes loss of in-flight work | The lifespan startup sweep cancels stuck `running` rows older than 30 min on every boot. Anything in flight at the time of restart will be cancelled cleanly | If you need to preserve in-flight research, `/research/pause <session_id>` before restarting |

For deeper debugging, the orchestrator logs are at:

```bash
docker logs --tail 200 -f scaffold-orchestrator
```

Logs are JSON-formatted; pipe to `jq` for readability:

```bash
docker logs --tail 200 -f scaffold-orchestrator | jq -r '"\(.timestamp) [\(.level)] \(.event)"'
```

---

## Where everything lives

| What | Where |
|---|---|
| Chat UI | <http://localhost:3000> |
| PDF upload form | <http://localhost:8000/research/pdf> |
| Health check (no auth) | <http://localhost:8000/health> |
| OpenAPI contract | `~/scaffold-engine/docs/openapi.json` |
| Pipeline code | `~/scaffold-engine/pipelines/scaffold_router.py` |
| Orchestrator code | `~/scaffold-engine/app/` |
| Python SDK | `~/scaffold-engine/sdk/` |
| Terminal CLI | `~/scaffold-engine/cli/` |
| Comprehensive technical reference | `~/scaffold-engine/OVERVIEW.md` (incl. glossary) |

---

*This guide covers user-facing scenarios and commands. For architecture, every module's public function, the database schema, configuration reference, the TOON data format, the logging event catalog, known issues, and sprint history, see [OVERVIEW.md](./OVERVIEW.md).*
