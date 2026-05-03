# Scaffold Engine — User Guide

A beginner-friendly reference for using Scaffold Engine through Open WebUI.

> Scaffold Engine takes an idea, refines it, researches it, plans a workflow, and executes that workflow — all from a chat interface. This guide covers every command you can type into the chat box.

---

## Getting started

1. Open the Open WebUI chat at **http://localhost:3000**.
2. Make sure the model selector shows **scaffold_router** (or a name containing "scaffold").
3. Start a new chat and type a message. That's it — you're in.

If a command fails with `401 Unauthorized`, the pipeline lost its API key. Restart the pipelines container: `docker restart open-webui-pipelines`.

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
| Project overview (deep technical reference) | `~/scaffold-engine/scaffold-engine-overview.md` |

---

*This guide covers user-facing commands only. For architecture, model stack, internal APIs, and the full audit history, see `scaffold-engine-overview.md`.*
