"""§17.1085 — the gate REGISTRY: every fail-safe in the assist loop, named,
with the surfaces it must cover and a runner that cannot fail silently.

Why this exists. On 2026-09-17 a port-forwarding step went wrong four ways
with fourteen gates in the code. Reading how: each gate had been built
from ONE incident and keyed to that incident's shape; they were attached
to generation paths one call site at a time (two prompt renderers, five
verifier call sites — every new gate reached some and missed others,
§17.751/§17.914/§17.1083 all found this the same way); they validated
the ANSWER but never the RECORDS the answer was built from; and every one
of them ran inside `except Exception: pass`, so a gate that crashed on a
new shape simply vanished from the run. Fail-soft is right for the turn
and wrong for the gate: the operator must never lose a turn to a gate
bug, and the operator must always be able to see that a gate is broken.

This module makes three things true by construction:
- REGISTRY: `GATES` lists every gate — answer gates, record invariants,
  prompt guards — with the surfaces it must be applied on. A test walks the
  code and fails when a surface lacks one.
- FAIL-LOUD: `run_gate` wraps a gate call; an exception is logged as
  `assist_gate_crashed gate=<name>` with the traceback, counted, and
  surfaced on `/health` as `assist_gates` (degraded when any crashed since
  start). The turn still completes; the crash is never invisible.
- REPLAY: `tests/fixtures/assist_replay/*.json` hold real drafts and the
  gate outcomes they must produce; CI runs them (deterministic gates), the
  nightly goldens run the model-in-the-loop ones.
"""
from __future__ import annotations

import logging
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger("scaffold")

# Surfaces — where an answer is produced or a record is written.
GUIDE, FIX, RESEARCH, STREAM, EXECUTOR = "guide", "fix", "research", "stream", "executor"
STATE_WRITE, FACT_WRITE, PROMPT = "state_write", "fact_write", "prompt"


@dataclass(frozen=True)
class Gate:
    name: str
    kind: str                       # answer | record | prompt
    module: str                     # where it lives
    symbol: str                     # the function/marker a surface must call
    surfaces: tuple[str, ...]
    since: str                      # the §17.x that introduced it
    what: str                       # one line, plain words


GATES: tuple[Gate, ...] = (
    # ── answer gates (inside verify_answer, so one call covers them all) ──
    Gate("unsourced_values", "answer", "assist_evidence", "verify_answer", (GUIDE, FIX, RESEARCH, STREAM, EXECUTOR), "§17.1027",
         "every version / IP / URL / non-standard port in an answer must trace to the prompt it was given"),
    Gate("citation_backing", "answer", "assist_evidence", "verify_answer", (GUIDE, FIX, RESEARCH, STREAM), "§17.1027",
         "every [n] must be backed by source n"),
    Gate("addresses_question", "answer", "assist_evidence", "verify_answer", (GUIDE, FIX, RESEARCH, STREAM), "§17.1031",
         "the answer talks about what was asked"),
    Gate("command_shape", "answer", "assist_evidence", "verify_answer", (GUIDE, FIX, RESEARCH, STREAM), "§17.1033",
         "commands are copy-pasteable and of the right shape"),
    Gate("ingress_target", "answer", "assist_inventory", "topology=", (GUIDE, FIX, RESEARCH, STREAM), "§17.1083b",
         "a port forward / reservation targets the map's entry point, never a management port"),
    Gate("problem_class_query", "retrieval", "assist_evidence", "class_query", (RESEARCH,), "§17.1086",
         "a second web query with the operator's specifics removed and the vendor kept — the threads by everyone who hit this before"),
    # ── fix-path integrity gates ──
    Gate("no_repeat_fix", "answer", "assist_guide", "_gate(", (FIX,), "§17.906", "a fix does not repeat a command already tried on the step"),
    Gate("banned_values", "answer", "assist_guide", "find_banned_values", (FIX,), "§17.893", "a ruled-out value never comes back"),
    Gate("resource_kind", "answer", "assist_guide", "find_resource_kind_violations", (FIX,), "§17.898", "qm for VMs, pct for containers"),
    Gate("shell_unsafe", "answer", "assist_guide", "find_shell_unsafe_commands", (FIX, RESEARCH), "§17.906", "no metacharacter inside an unquoted --flag=value"),
    Gate("look_before_change", "answer", "assist_guide", "guess_before_look", (FIX,), "§17.907", "after N failed fixes the next thing is a command that PRINTS state"),
    Gate("contradicted_facts", "answer", "assist_guide", "find_contradicted_facts", (FIX,), "§17.908", "a fix does not call a confirmed value fake"),
    Gate("redundant_discovery", "answer", "assist_state", "find_redundant_discovery", (FIX,), "§17.914", "never re-ask for state already on file"),
    # ── record invariants (what the answers are built FROM) ──
    Gate("state_invariants", "record", "assist_state", "reconcile_system_state", (STATE_WRITE,), "§17.1084",
         "a MAC belongs to one machine, a disk to its id, a record to its kind's form; list names authoritative"),
    Gate("fact_reconcile", "record", "assist_inventory", "reconcile_fact", (FACT_WRITE,), "§17.1084",
         "a new fact is read against the records before it is stored; MAC fragments name their machine"),
    Gate("contract_conflicts", "record", "assist_contracts", "find_contract_conflicts", (PROMPT,), "§17.968",
         "two artefacts the session wrote that disagree with each other are named"),
    # ── prompt guards (what every generation sees) ──
    Gate("system_map", "prompt", "assist_inventory", "render_system_map", (PROMPT,), "§17.1083",
         "one line per machine + the ingress path + conflicts, at the head of the state tier on BOTH renderers"),
    Gate("confirmed_state", "prompt", "assist_state", "render_system_state", (PROMPT,), "§17.914",
         "the operator's own config output, marked as configuration only"),
    Gate("missing_tools", "prompt", "assist_render", "missing_tools", (PROMPT,), "§17.913",
         "tools the shell has proven it lacks are never prescribed"),
)

BY_NAME: dict[str, Gate] = {g.name: g for g in GATES}

# Where each surface's code lives — the coverage test walks these.
SURFACE_CODE: dict[str, tuple[str, str, str]] = {
    # surface: (module file, start marker, end marker) — the region that must contain every gate's symbol
    GUIDE: ("assist_guide.py", "_vreport_guide = await verify_answer(", "_sourced_meta_guide"),
    FIX: ("assist_guide.py", "def _gate(draft: str)", "_sourced_meta_fix = list("),
    RESEARCH: ("assist_research_lib.py", "need = derive_need(question, operator_notes=operator_notes,", "grounding = _vreport"),
    # the research surface's post-answer gates live in assist_agent.run_step_research
    "research_post": ("assist_agent.py", "res = await assist_guide.research_one(", "return {"),
    STREAM: ("assist_guide.py", "_vreport_stream = await verify_answer(", "_vreport_stream.get"),
}
# surfaces whose code spans more than one region
SURFACE_UNION: dict[str, tuple[str, ...]] = {RESEARCH: (RESEARCH, "research_post")}
RENDERERS: tuple[tuple[str, str], ...] = (
    ("assist_render.py", "def render_environment_block"),
    ("assist_render.py", "def render_session_memory"),
)


# ---------------------------------------------------------------------------
# Fail-loud runner
# ---------------------------------------------------------------------------

@dataclass
class _Crash:
    gate: str
    count: int = 0
    last_error: str = ""
    last_at: float = 0.0
    last_trace: str = ""


_CRASHES: dict[str, _Crash] = {}
_RUNS: dict[str, int] = {}
_STARTED_AT = time.time()


def run_gate(name: str, fn: Callable[..., Any], *args: Any, default: Any = None, **kwargs: Any) -> Any:
    """Call a gate. The gate's own exception never breaks the turn — but it is
    logged with its traceback under ONE event name, counted, and shown on
    /health. `default` is what the caller proceeds with (usually "no hits")."""
    _RUNS[name] = _RUNS.get(name, 0) + 1
    try:
        return fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 — the whole point: contain AND report
        c = _CRASHES.setdefault(name, _Crash(gate=name))
        c.count += 1
        c.last_error = f"{type(exc).__name__}: {exc}"[:300]
        c.last_at = time.time()
        c.last_trace = traceback.format_exc()[-1500:]
        logger.error("assist_gate_crashed gate=%s count=%d err=%s", name, c.count, c.last_error, exc_info=True)
        return default


async def run_gate_async(name: str, fn: Callable[..., Any], *args: Any, default: Any = None, **kwargs: Any) -> Any:
    _RUNS[name] = _RUNS.get(name, 0) + 1
    try:
        return await fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001
        c = _CRASHES.setdefault(name, _Crash(gate=name))
        c.count += 1
        c.last_error = f"{type(exc).__name__}: {exc}"[:300]
        c.last_at = time.time()
        c.last_trace = traceback.format_exc()[-1500:]
        logger.error("assist_gate_crashed gate=%s count=%d err=%s", name, c.count, c.last_error, exc_info=True)
        return default


def gate_health() -> dict[str, Any]:
    """The /health block: registered gates, runs and crashes since start."""
    crashed = {n: {"count": c.count, "last_error": c.last_error, "last_at": c.last_at} for n, c in _CRASHES.items()}
    return {
        "status": "degraded" if crashed else "up",
        "registered": len(GATES),
        "runs": dict(_RUNS),
        "crashed": crashed,
        "since": _STARTED_AT,
    }


def reset_for_tests() -> None:
    _CRASHES.clear()
    _RUNS.clear()
