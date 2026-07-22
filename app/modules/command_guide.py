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

# Read-only intents (§17.628, Phase 1).
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
)
COMMAND_INTENTS = _READ_INTENTS + _WRITE_INTENTS + ("none",)

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
                    "status = overview of what jobs are running ('what's running', 'any jobs going'). "
                    "results = the OUTCOME/compiled output of a job, often named ('how did the proxmox "
                    "job turn out'); put the job name in job_ref. "
                    "rag_query = search the knowledge base / ingested notes ('search my notes for X', "
                    "'what do we know about X'); put the search text in query. "
                    "jobs_list = see recent jobs ('list my jobs'). "
                    "jobs_find = FIND a job by topic ('find my kubernetes job'); put the text in query. "
                    "model_list = which model each role uses now. "
                    "model_available = which models are installed. "
                    "model_probe = are the models reachable. "
                    "help = what can I do / the commands. "
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
                    "For intent=rag_query or jobs_find: the search text as a clean "
                    "standalone query (strip filler). Omit otherwise."
                ),
            },
            "job_ref": {
                "type": "string",
                "description": (
                    "For intent=results or jobs_rename: how the operator referred to "
                    "the job — a name/topic fragment or an id. Omit if none given."
                ),
            },
            "topic": {
                "type": "string",
                "description": (
                    "For intent=research_topic or schedule_add: the research topic as "
                    "a clean standalone phrase. Omit otherwise."
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
                    "For intent=jobs_rename: the new title for the job. Omit otherwise."
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
    "Two families of action exist: READS (status, results, searching the "
    "knowledge base, listing jobs/models, help) and WRITES (run web research on "
    "a topic, schedule recurring research, set/reset a model role, optimize a "
    "prompt, rename a job).\n\n"
    "Critical distinction — a WRITE action here is a SINGLE engine operation. A "
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
        "model_name": "", "prompt": "", "new_name": "",
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
    }
