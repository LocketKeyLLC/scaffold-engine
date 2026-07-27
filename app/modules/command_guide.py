"""Engine-wide natural-language command router (§17.628 / §17.629).

The §17.626/§17.627 arc made plain chat drive the engine *inside an active
assist session* (see :mod:`app.modules.assist_guide`). This module is the
sibling classifier for the **top level**: when a chat has NO active assist
session, a plain sentence should still be able to drive the rest of the engine
("what's running", "research the latest on postgres tuning", "set the coder
model to kimi") instead of requiring a memorized slash command.

Design is a deliberate 1:1 mirror of ``assist_guide.classify_turn``:

    * one :class:`model_router.Tool` (``route_command``),
    * a single cheap ``model_router.tool_call`` on the classifier role
      (``model_verifier`` — kimi, reliable native tool-calls, already loaded),
    * fail-soft: on any model/parse error, or on a message that is not clearly
      a command, return ``intent='none'`` so the pipeline falls through to
      triage (the idea-building conversation) untouched.

Phased rollout:
    * §17.628 (Phase 1) — **read-only** intents (status / results / rag_query /
      jobs_list / jobs_find / model_list|available|probe / help). A misfire is
      at worst a wrong read.
    * §17.629 (Phase 2) — **mutating / expensive** intents (research_topic,
      schedule_add, model_set, model_reset, optimize, jobs_rename). The two
      that commit real cost (research, schedule) are gated behind an explicit
      confirm card in the pipeline; the rest are cheap/reversible and run
      directly. Destructive intents (delete) stay for Phase 3.

The ``confidence`` field is load-bearing: the pipeline intercepts a message
only on ``confidence='high'`` (plus a required-slot check). Everything the
model is unsure about degrades to triage. This is the "high-confidence
intercept, triage default" contract — the router must never hijack a genuine
idea-building turn (a request to BUILD a multi-step deliverable belongs to the
planner, not here).
"""
from __future__ import annotations

import logging

from app import model_router
from app.config import settings

logger = logging.getLogger("scaffold.command_guide")

# Read-only intents (§17.628, Phase 1; extended §17.655, Phase 4).
_READ_INTENTS = (
    "status",          # active jobs overview           → /status
    "results",         # compiled output of a job       → /results [job_ref]
    "rag_query",       # query the Milvus knowledge base → /rag <query>
    "jobs_list",       # list recent jobs               → /jobs list
    "jobs_find",       # search jobs by title           → /jobs find <query>
    "model_list",      # currently-routed models        → /model list
    "model_available", # models present in `ollama list`→ /model available
    "model_probe",     # live per-role availability ping → /model probe
    "help",            # the command surface            → /help
    # §17.655 (Phase 4) — the remaining safe reads. A misfire is at worst a
    # wrong read, never a write; all degrade to triage below 'high' confidence.
    "schedule_list",   # recurring research schedules    → /schedule list
    "research_list",   # recent research sessions        → /research/list
    "research_find",   # search research by topic        → /research/find <query>
    "logs",            # per-node execution log for a job→ /logs [job_ref]
    "cost",            # token/cost rollup for a job      → /cost [job_ref]
    "health",          # subsystem health check          → /health
    "config",          # engine/pipeline configuration   → /config
    "work_here",       # what am I working on now         → /here
    "work_next",       # my single next actionable step   → /next
    # §17.658 (Phase 7) — knowledge-base (ground-truth) + prompt inspection,
    # surfaced from the main chat (their own OWUI pipelines aside).
    "gt_list",         # list ground-truth KB entries     → GET /gt/list
    "gt_search",       # semantic search the GT KB        → POST /gt/search
    "gt_stats",        # GT collection summary            → GET /gt/stats
    "prompts_view",    # a job's per-node prompts         → GET /prompts/<id>
)
# Mutating / expensive intents (§17.629, Phase 2). research_topic + schedule_add
# commit real cost and are confirmed in the pipeline before firing.
_WRITE_INTENTS = (
    "research_topic",  # autonomous web research on a topic → /research <topic>
    "schedule_add",    # recurring research schedule        → /schedule add ...
    "model_set",       # assign a model to a role           → /model set <r> <m>
    "model_reset",     # reset ALL model roles to defaults  → /model reset
    "optimize",        # optimize a prompt                  → /optimize <prompt>
    "jobs_rename",     # rename a job                       → /jobs rename <id> <n>
    # §17.656 (Phase 5) — research INGEST variants + session rename. The three
    # ingest verbs fetch an external source and write it to the knowledge base,
    # so they are confirmed in the pipeline (like research_topic); rename is
    # cheap/reversible and runs directly (like jobs_rename).
    "research_url",    # ingest a single web page          → /research <url>
    "research_github", # ingest a repo's docs              → /research github:<repo>
    "research_openapi",# ingest an OpenAPI spec            → /research openapi:<url>
    "research_rename", # rename a research session         → /research/rename <id> <n>
    # §17.658 (Phase 7) — ground-truth extraction (SearXNG + LLM; confirmed).
    "gt_extract",      # extract ground truths on a topic  → POST /gt
)
# Workflow-control intents (§17.657, Phase 6). State-altering job/DAG control;
# each is confirmed in the pipeline (confirm/execute kick expensive multi-step
# runs; cancel/skip/retry/cleanup flip state). Kept distinct from DELETES —
# these are reversible operational verbs, not data removal.
_WORKFLOW_INTENTS = (
    "confirm_job",     # approve → auto-chain build     → /confirm <id>
    "execute_job",     # run all pending DAG nodes      → /execute <id>
    "retry_node",      # retry a failed/blocked node    → /exec retry <id> <node>
    "skip_node",       # skip a DAG node                → /skip <id> <node>
    "cancel_job",      # cancel a job (reversible)      → /cancel <id>
    "cleanup",         # reap stale jobs                → /cleanup
)
# Destructive intents (§17.630, Phase 3). ALWAYS confirmed in the pipeline —
# the named target is resolved and echoed, and nothing deletes without an
# explicit affirmative follow-up.
_DESTRUCTIVE_INTENTS = (
    "jobs_delete",     # delete a job                  → /jobs delete <id> confirm
    "schedule_delete", # delete a research schedule    → /schedule delete <id>
    "research_delete", # delete a research session     → /research/delete <id> confirm
)
COMMAND_INTENTS = (
    _READ_INTENTS + _WRITE_INTENTS + _WORKFLOW_INTENTS + _DESTRUCTIVE_INTENTS
    + ("none",)
)

_CONFIDENCE = ("high", "medium", "low")
_DEPTHS = ("shallow", "medium", "deep")

_ROUTE_TOOL = model_router.Tool(
    name="route_command",
    description=(
        "Decide whether the operator's message is a request to run one of the "
        "engine's actions, and if so which one. This is the top-level router "
        "for a self-hosted LLM orchestration engine. The operator is NOT in a "
        "step-by-step session; they are talking to the engine at large. Choose "
        "exactly one intent. Default to 'none' unless the message is clearly a "
        "request for one of these specific actions — in particular, a request "
        "to BUILD / CREATE / SET UP a multi-step software or infrastructure "
        "deliverable is 'none' (it belongs to the planner, not a single command)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "intent": {
                "type": "string",
                "enum": list(COMMAND_INTENTS),
                "description": (
                    # --- reads (Phase 1) ---
                    "status = overview of what JOBS are running ('what's running', 'any jobs going') — "
                    "a job census, NOT a backend-service health check (that's health) and NOT your own "
                    "current task (that's work_here). "
                    "results = the final OUTCOME/compiled answer of a job, often named ('how did the "
                    "proxmox job turn out', 'show the result of X') — NOT its step-by-step logs (that's "
                    "logs) and NOT its cost (that's cost); put the job name in job_ref. "
                    "rag_query = search the knowledge base / ingested notes ('search my notes for X', "
                    "'what do we know about X'); put the search text in query. "
                    "jobs_list = see recent build/deliverable jobs ('list my jobs') — NOT web-research "
                    "runs (that's research_list). "
                    "jobs_find = FIND a build job by topic ('find my kubernetes job') — NOT a research "
                    "session (that's research_find); put the text in query. "
                    "model_list = which MODEL each role uses now — the model roles ONLY, NOT the full "
                    "engine settings (that's config). "
                    "model_available = which models are installed. "
                    "model_probe = are the models reachable. "
                    "help = what can I do / the commands. "
                    # --- more reads (Phase 4, §17.655) ---
                    "schedule_list = show recurring research SCHEDULES / cron jobs ('what's "
                    "scheduled', 'list my schedules', 'show recurring research'). "
                    "research_list = list past web-RESEARCH SESSIONS ('show my research', 'recent "
                    "research runs', 'past research sessions') — these are /research web-lookup runs, "
                    "distinct from build jobs; do NOT use jobs_list for these. "
                    "research_find = FIND a past web-RESEARCH session by topic ('find my research on "
                    "zfs', 'search my research for proxmox') — a research run, NOT a build job "
                    "(jobs_find); put the topic in query. "
                    "logs = a job's per-node execution LOG/trace — which DAG steps ran, their status, "
                    "why one failed ('show the logs for the proxmox job', 'what happened on that run', "
                    "'why did it fail') — the execution trace, NOT the final answer (results) or the "
                    "cost; put any job name in job_ref, or omit it for the active job. "
                    "cost = a job's token/COST/spend rollup — money and tokens used ('how much did the "
                    "proxmox job cost', 'what did that run cost', 'token spend for X') — NOT the output "
                    "(results); put any job name in job_ref, or omit for the active job. "
                    "health = are the backend SERVICES up — an infrastructure health check of "
                    "Postgres/Ollama/Milvus/Redis ('health check', 'is everything up', 'are the "
                    "services ok', 'is the engine healthy') — NOT a job census (status). "
                    "config = show ALL engine/pipeline CONFIGURATION / settings ('show my config', "
                    "'what are my settings') — every setting, NOT just the model roles (model_list). "
                    "work_here = what am I personally WORKING ON right now / where I left off ('what "
                    "am I working on', 'where was I', 'my active work') — my current task, NOT the "
                    "system-wide job census (status). "
                    "work_next = my single NEXT actionable step in my current work ('what should I do "
                    "next', 'what now', 'what's next'). "
                    # --- knowledge-base + prompt inspection (Phase 7, §17.658) ---
                    "gt_list = list GROUND-TRUTH knowledge-base entries ('list ground truths', 'show the "
                    "GT entries', 'what ground truths do I have') — the curated ground-truth KB, distinct "
                    "from rag_query's research notes. "
                    "gt_search = semantically SEARCH the GROUND-TRUTH KB ('search ground truths for X', "
                    "'find the GT about X'); put the text in query. Prefer rag_query when the user says "
                    "'my notes' rather than 'ground truths'. "
                    "gt_stats = GROUND-TRUTH KB summary/counts ('ground truth stats', 'how many ground "
                    "truths', 'GT breakdown'). "
                    "prompts_view = show a job's per-node PROMPTS ('show the prompts for the proxmox job', "
                    "'what prompts is job abc using', 'inspect the prompts'); put the job in job_ref. "
                    # --- writes (Phase 2) ---
                    "research_topic = run autonomous WEB RESEARCH on a topic and ingest findings "
                    "('research the latest on postgres tuning', 'look up what's new in Proxmox 8', "
                    "'investigate ZFS vs btrfs'); put the topic in topic and any 'shallow/medium/deep' in depth. "
                    "This is a research request, NOT a request to build software. "
                    "schedule_add = set up a RECURRING research run ('every monday research kubernetes news', "
                    "'schedule a weekly report on AI papers'); put the topic in topic, a cron expression in "
                    "cron, any depth in depth, any timezone in tz. "
                    "model_set = assign/switch a model for a role ('set the coder model to kimi', 'use "
                    "qwen3:8b for general'); put the role in model_role and the model name in model_name. "
                    "model_reset = reset ALL model roles back to defaults ('reset the models', 'put the "
                    "models back to defaults'). "
                    "optimize = improve/refine a PROMPT the user gives ('optimize this prompt: ...', 'make "
                    "this prompt better: ...'); put the prompt text in prompt. "
                    "jobs_rename = rename an existing job ('rename job abc to Home Lab Setup'); put the job "
                    "reference in job_ref and the new title in new_name. "
                    # --- research ingest variants + rename (Phase 5, §17.656) ---
                    "research_url = ingest ONE specific web PAGE verbatim into the knowledge base — the "
                    "user gives an explicit http(s) URL ('ingest this page https://x/article', 'read and "
                    "save https://x/post', 'add https://x to my notes'); put the bare URL in url. NOT "
                    "open-ended topic research (research_topic). "
                    "research_github = ingest a GitHub REPO's docs (README + docstrings) — the user names "
                    "a repo ('read the docs at github.com/owner/repo', 'ingest the owner/repo repo'); put "
                    "just 'owner/repo' in repo (strip any github.com/ prefix). "
                    "research_openapi = ingest an OpenAPI/Swagger SPEC (one entry per endpoint) — the user "
                    "points at a spec ('ingest the openapi spec at https://api.x/openapi.json'); put the "
                    "spec URL in url. "
                    "research_rename = rename a past RESEARCH session ('rename the zfs research to ZFS "
                    "Tuning Notes'); put the session reference in job_ref and the new topic in new_name. "
                    "gt_extract = EXTRACT ground truths on a topic via web search + LLM ('extract ground "
                    "truths about kubernetes networking', 'build ground truths on ZFS'); put the topic in "
                    "topic. Distinct from research_topic (research notes) — this curates the GT KB. "
                    # --- destructive (Phase 3) ---
                    "jobs_delete = DELETE/remove a job ('delete the kubernetes job', 'remove that old CLI "
                    "job'); put the job name/id in target_ref. "
                    "schedule_delete = DELETE/remove a recurring research SCHEDULE ('delete the weekly "
                    "kubernetes schedule', 'stop the monday research'); put the schedule topic/id in target_ref. "
                    "research_delete = DELETE/remove a past RESEARCH SESSION ('delete the proxmox research', "
                    "'remove that research on zfs'); put the session topic/id in target_ref. "
                    "(All deletes are confirmed with the operator before anything is removed.) "
                    # --- workflow control (Phase 6, §17.657) ---
                    "confirm_job = APPROVE a job that is waiting for confirmation and kick off the full "
                    "build ('approve the proxmox job', 'go ahead and build the kubernetes one', 'confirm "
                    "and run job abc'); put the job in job_ref. This starts a long (10-40 min) run. "
                    "execute_job = RUN/execute a job's already-planned DAG nodes ('execute the homelab "
                    "job', 'run all the steps for abc'); put the job in job_ref. "
                    "retry_node = RE-RUN a failed/blocked DAG node ('retry node T3 on the kube job', "
                    "'rerun the failed step', 'retry the kube job'); put the job in job_ref and, IF the "
                    "operator named a node key (e.g. 'T3'), put it in node_key — otherwise omit node_key "
                    "and the failed node is found automatically. "
                    "skip_node = SKIP a failed/blocked DAG node ('skip node T3 on the proxmox job', 'skip "
                    "the failing step'); put the job in job_ref and, if a node key was named, node_key — "
                    "otherwise omit it and the failed node is found automatically. "
                    "cancel_job = CANCEL/stop a job ('cancel the proxmox job', 'stop that run') — flips it "
                    "to cancelled (reversible via resume), NOT a permanent delete (jobs_delete); put the "
                    "job in job_ref. "
                    "cleanup = reap/clean up STALE or abandoned jobs ('clean up stale jobs', 'run the "
                    "reaper', 'tidy up old stuck jobs') — no specific target. "
                    # --- the safe default ---
                    "none = ANYTHING else — describing/building a multi-step deliverable ('build a CLI that "
                    "…', 'set up proxmox on my box', 'make me an app'), a general question, or chit-chat. "
                    "When unsure, choose none."
                ),
            },
            "confidence": {
                "type": "string",
                "enum": list(_CONFIDENCE),
                "description": (
                    "high = the message is unambiguously this action. medium/low = "
                    "plausibly, but it could be conversation or a build request. Only "
                    "mark high when you would run the action (reads) or offer it for "
                    "confirmation (writes) without further clarification."
                ),
            },
            "query": {
                "type": "string",
                "description": (
                    "For intent=rag_query, jobs_find, research_find, or gt_search: the "
                    "search text as a clean standalone query (strip filler). Omit otherwise."
                ),
            },
            "job_ref": {
                "type": "string",
                "description": (
                    "For intent=results, logs, cost, jobs_rename, research_rename, or "
                    "prompts_view: how the operator referred to the job/session — a "
                    "name/topic fragment or an id. Omit if none given (logs/cost then fall "
                    "back to the active job)."
                ),
            },
            "url": {
                "type": "string",
                "description": (
                    "For intent=research_url: the bare web page URL to ingest. For "
                    "research_openapi: the OpenAPI/Swagger spec URL. Include the "
                    "http(s):// scheme; omit otherwise."
                ),
            },
            "repo": {
                "type": "string",
                "description": (
                    "For intent=research_github: the repository as 'owner/repo' (strip "
                    "any 'https://github.com/' prefix and trailing path). Omit otherwise."
                ),
            },
            "node_key": {
                "type": "string",
                "description": (
                    "For intent=retry_node or skip_node: the DAG node key the operator "
                    "named (e.g. 'T3', 'T2'), verbatim. Omit if they did not name one — "
                    "the pipeline then auto-resolves the job's failed/blocked node."
                ),
            },
            "topic": {
                "type": "string",
                "description": (
                    "For intent=research_topic, schedule_add, or gt_extract: the topic "
                    "as a clean standalone phrase. Omit otherwise."
                ),
            },
            "depth": {
                "type": "string",
                "enum": list(_DEPTHS),
                "description": (
                    "For research_topic/schedule_add: research depth if the operator "
                    "stated one ('quick/shallow', 'deep/thorough'). Omit if unstated."
                ),
            },
            "cron": {
                "type": "string",
                "description": (
                    "For intent=schedule_add: a 5-field cron expression capturing the "
                    "cadence they described ('every monday at 9am' → '0 9 * * 1'). Omit "
                    "if you cannot confidently derive one."
                ),
            },
            "tz": {
                "type": "string",
                "description": (
                    "For intent=schedule_add: an IANA timezone if the operator named "
                    "one ('America/New_York'). Omit otherwise."
                ),
            },
            "model_role": {
                "type": "string",
                "description": (
                    "For intent=model_set: the role to reassign — one of general, "
                    "verifier, coder, router, fallback, cloud_alt. Omit otherwise."
                ),
            },
            "model_name": {
                "type": "string",
                "description": (
                    "For intent=model_set: the model tag to assign (e.g. 'kimi-k2.7-"
                    "code:cloud', 'qwen3:8b'), exactly as the operator said it. Omit "
                    "otherwise."
                ),
            },
            "prompt": {
                "type": "string",
                "description": (
                    "For intent=optimize: the prompt text to optimize, verbatim. Omit "
                    "otherwise."
                ),
            },
            "new_name": {
                "type": "string",
                "description": (
                    "For intent=jobs_rename: the new title for the job. For "
                    "research_rename: the new topic for the research session. Omit "
                    "otherwise."
                ),
            },
            "target_ref": {
                "type": "string",
                "description": (
                    "For intent=jobs_delete / schedule_delete / research_delete: how "
                    "the operator referred to the thing to delete — a name/topic "
                    "fragment or an id. Omit otherwise."
                ),
            },
        },
        "required": ["intent", "confidence"],
    },
)

_ROUTE_SYSTEM = (
    "You are the top-level router for a self-hosted LLM orchestration engine. "
    "The operator typed a plain message (no slash command). Decide if it is a "
    "request to run one of the engine's actions (see the tool), and how "
    "confident you are.\n\n"
    "Families of action exist: READS (status, results, searching the "
    "knowledge base, listing jobs/models/schedules/research sessions, a job's "
    "logs or cost, a health check, your config, listing/searching the "
    "ground-truth KB, viewing a job's prompts, and 'what am I working on / "
    "what's next', help), WRITES (run web research on a "
    "topic, ingest a specific page / GitHub repo / OpenAPI spec into the "
    "knowledge base, extract ground truths on a topic, schedule recurring "
    "research, set/reset a model role, "
    "optimize a prompt, rename a job or research session), WORKFLOW CONTROL "
    "(approve/confirm a job, execute its DAG, retry or skip a node, cancel a "
    "job, clean up stale jobs — each confirmed before it runs), and DELETES "
    "(remove a job, a research schedule, or a "
    "past research session — always confirmed before anything is removed).\n\n"
    "Critical distinction — a command here is a SINGLE engine operation. A "
    "request to BUILD, CREATE, or SET UP a multi-step software/infrastructure "
    "deliverable ('build a CLI that converts screenshots to PDF', 'set up "
    "proxmox on my dual-xeon box', 'make me a dashboard') is NOT a command — it "
    "is 'none' (the planner handles those). 'set up proxmox' ≠ 'set the coder "
    "model'; 'build X' ≠ 'research X'. When genuinely unsure between a command "
    "and a build/conversation, choose 'none' with low confidence — it is far "
    "better to send a borderline message to the planner than to hijack it.\n"
    "Call route_command exactly once."
)


async def classify_command(*, message: str, role: str | None = None) -> dict:
    """Classify a top-level plain-language message into an engine intent.

    Returns ``{"intent": <one of COMMAND_INTENTS>, "confidence": <high|medium|
    low>, "query", "job_ref", "topic", "depth", "cron", "tz", "model_role",
    "model_name", "prompt", "new_name"}`` (all slots strings, empty when
    unused). Fail-soft: on any model/parse error, or an unusable/absent tool
    call, returns ``intent='none'`` (confidence ``low``) so the caller degrades
    to triage rather than misfiring a command.
    """
    role = role or settings.assist_classify_model_role
    fallback = {
        "intent": "none", "confidence": "low", "query": "", "job_ref": "",
        "topic": "", "depth": "", "cron": "", "tz": "", "model_role": "",
        "model_name": "", "prompt": "", "new_name": "", "target_ref": "",
        "url": "", "repo": "", "node_key": "",
    }
    text = (message or "").strip()
    if not text:
        return fallback
    user = (
        "Operator's message:\n"
        f"{text[:2000]}\n\n"
        "Call route_command."
    )
    try:
        resp = await model_router.tool_call(
            [
                {"role": "system", "content": _ROUTE_SYSTEM},
                {"role": "user", "content": user},
            ],
            [_ROUTE_TOOL],
            role=role,
            temperature=0.0,
            max_tokens=512,
            tool_choice="auto",
        )
    except Exception as exc:  # network / provider error — never block the turn
        logger.warning("command_classify_failed: %s", exc)
        return fallback
    if not resp.success or not resp.tool_calls:
        return fallback
    args = resp.tool_calls[0].arguments or {}
    intent = args.get("intent")
    if intent not in COMMAND_INTENTS:
        return fallback
    confidence = args.get("confidence")
    if confidence not in _CONFIDENCE:
        confidence = "low"
    depth = args.get("depth")
    if depth not in _DEPTHS:
        depth = ""

    def _s(key: str) -> str:
        return (args.get(key) or "").strip()

    return {
        "intent": intent,
        "confidence": confidence,
        "query": _s("query"),
        "job_ref": _s("job_ref"),
        "topic": _s("topic"),
        "depth": depth,
        "cron": _s("cron"),
        "tz": _s("tz"),
        "model_role": _s("model_role"),
        "model_name": _s("model_name"),
        "prompt": _s("prompt"),
        "new_name": _s("new_name"),
        "target_ref": _s("target_ref"),
        "url": _s("url"),
        "repo": _s("repo"),
        "node_key": _s("node_key"),
    }
