"""Engine-wide natural-language command router (§17.628).

The §17.626/§17.627 arc made plain chat drive the engine *inside an active
assist session* (see :mod:`app.modules.assist_guide`). This module is the
sibling classifier for the **top level**: when a chat has NO active assist
session, a plain sentence should still be able to drive the rest of the engine
("what's running", "how did the proxmox job go", "search my notes for ZFS on
non-ECC", "list the models") instead of requiring a memorized slash command.

Design is a deliberate 1:1 mirror of ``assist_guide.classify_turn``:

    * one :class:`model_router.Tool` (``route_command``),
    * a single cheap ``model_router.tool_call`` on the classifier role
      (``model_verifier`` — kimi, reliable native tool-calls, already loaded),
    * fail-soft: on any model/parse error, or on a message that is not clearly
      a command, return ``intent='none'`` so the pipeline falls through to
      triage (the idea-building conversation) untouched.

Phase 1 (this commit) covers only the **read-only** surface — nothing here
mutates state, so a misfire is at worst a wrong read, never a wrong write.
Mutating / expensive / destructive intents land in later phases behind
confirms; the tool's ``intent`` enum grows then.

The ``confidence`` field is load-bearing: the pipeline intercepts a message
only on ``confidence='high'`` (plus a required-slot check). Everything the
model is unsure about degrades to triage. This is the "high-confidence
intercept, triage default" contract — the router must never hijack a genuine
idea-building turn.
"""
from __future__ import annotations

import logging

from app import model_router
from app.config import settings

logger = logging.getLogger("scaffold.command_guide")

# Read-only intent surface for Phase 1. `none` is the safe default: the message
# is conversational / an idea / anything not unambiguously one of these reads.
COMMAND_INTENTS = (
    "status",          # active jobs overview           → /status
    "results",         # compiled output of a job       → /results [job_ref]
    "rag_query",       # query the Milvus knowledge base → /rag <query>
    "jobs_list",       # list recent jobs               → /jobs list
    "jobs_find",       # search jobs by title           → /jobs find <query>
    "model_list",      # currently-routed models        → /model list
    "model_available", # models present in `ollama list`→ /model available
    "model_probe",     # live per-role availability ping → /model probe
    "help",            # the command surface            → /help
    "none",            # not a command — fall to triage
)

_CONFIDENCE = ("high", "medium", "low")

_ROUTE_TOOL = model_router.Tool(
    name="route_command",
    description=(
        "Decide whether the operator's message is a request to run one of the "
        "engine's READ-ONLY actions, and if so which one. This is the top-level "
        "router for a self-hosted LLM orchestration engine. The operator is NOT "
        "in a step-by-step session; they are talking to the engine at large. "
        "Choose exactly one intent. Default to 'none' unless the message is "
        "clearly a request for one of these specific actions."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "intent": {
                "type": "string",
                "enum": list(COMMAND_INTENTS),
                "description": (
                    "status = they want an overview of what jobs are running / "
                    "active ('what's running', 'what have I got going', 'any jobs going'). "
                    "results = they want the OUTCOME / compiled output of a job, often "
                    "naming it ('how did the proxmox job turn out', 'show me the result of the CLI job', "
                    "'what did the postgres research find'). Put the job name in job_ref. "
                    "rag_query = they want to search the knowledge base / their ingested notes / "
                    "the corpus ('search my notes for X', 'what do we know about X', "
                    "'look up X in the knowledge base'). Put the search text in query. "
                    "jobs_list = they want to see their recent jobs ('list my jobs', 'show my jobs', "
                    "'what jobs do I have'). "
                    "jobs_find = they want to FIND a job by topic ('find my job about kubernetes', "
                    "'do I have a job for the home lab'). Put the search text in query. "
                    "model_list = which model is routed to each role right now ('what models are set', "
                    "'which model does the coder use'). "
                    "model_available = which models are installed / pullable ('what models do I have', "
                    "'what's in ollama'). "
                    "model_probe = are the models actually reachable ('are the models up', 'ping the models'). "
                    "help = they want to know what they can do / the commands ('what can you do', 'help', "
                    "'how do I use this'). "
                    "none = ANYTHING else — describing something to build, refining an idea, a general "
                    "question, chit-chat, or a request to DO/CREATE/RESEARCH/RUN something (those are not "
                    "read-only). When unsure, choose none."
                ),
            },
            "confidence": {
                "type": "string",
                "enum": list(_CONFIDENCE),
                "description": (
                    "high = the message is unambiguously this read-only action. "
                    "medium/low = plausibly, but it could be conversation or an idea. "
                    "Only mark high when you would be comfortable running the action "
                    "immediately with no confirmation."
                ),
            },
            "query": {
                "type": "string",
                "description": (
                    "For intent=rag_query or jobs_find: the search text, as a clean "
                    "standalone query (strip filler like 'search my notes for'). "
                    "Omit otherwise."
                ),
            },
            "job_ref": {
                "type": "string",
                "description": (
                    "For intent=results: how the operator referred to the job — a "
                    "name/topic fragment ('proxmox', 'the CLI tool') or an id if they "
                    "gave one. Omit if they referred to no specific job."
                ),
            },
        },
        "required": ["intent", "confidence"],
    },
)

_ROUTE_SYSTEM = (
    "You are the top-level router for a self-hosted LLM orchestration engine. "
    "The operator typed a plain message (no slash command). Decide if it is a "
    "request to run one of the engine's READ-ONLY actions (see the tool), and "
    "how confident you are.\n\n"
    "Critical safety rule: this router only handles READS. If the message asks "
    "to BUILD, CREATE, PLAN, RESEARCH, RUN, SET, CHANGE, DELETE, or SCHEDULE "
    "anything — or describes a thing they want made, or is a general question / "
    "conversation — the intent is 'none' (it belongs to the planner, not here). "
    "When genuinely unsure between a command and conversation, choose 'none' "
    "with low confidence. It is far better to send a borderline message to the "
    "planner than to hijack an idea-building turn.\n"
    "Call route_command exactly once."
)


async def classify_command(*, message: str, role: str | None = None) -> dict:
    """Classify a top-level plain-language message into a read-only engine
    intent.

    Returns ``{"intent": <one of COMMAND_INTENTS>, "confidence": <high|medium|
    low>, "query": str, "job_ref": str}``. Fail-soft: on any model/parse error,
    or an unusable/absent tool call, returns ``intent='none'`` (confidence
    ``low``) so the caller degrades to triage rather than misfiring a command.
    """
    role = role or settings.assist_classify_model_role
    fallback = {"intent": "none", "confidence": "low", "query": "", "job_ref": ""}
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
    return {
        "intent": intent,
        "confidence": confidence,
        "query": (args.get("query") or "").strip(),
        "job_ref": (args.get("job_ref") or "").strip(),
    }
