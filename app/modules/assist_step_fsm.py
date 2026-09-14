"""§17.1074 — the assist step's state machine, declared once.

The mirror invariant between `assist_steps.status` and `dag_nodes.status`
(§17.286) has been re-fixed four times (§17.878, 880, 911, 1054): each time a
write site moved one table and not the other, or moved the pointer without
a claim. Twenty-seven SQL sites write `assist_steps.status`; nothing said
which (from → to) moves were legal or which node status each step status
must mirror. This module does, with `transitions` as the declaration, and
exposes three things the write sites and the tests use:

- ``legal(src, dst)`` — is this a transition the machine allows?
- ``mirror_ok(step_status, node_status)`` — do the two tables agree?
- ``check(site, src, dst, node_status)`` — the runtime guard (logs
  ``step_fsm_violation``; raises only when ``assist_step_fsm_strict``).

It does NOT execute writes — the trial is the oracle, not the executor.
``mermaid()`` renders the machine for docs.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("scaffold")

STEP_STATES = ["pending", "presented", "awaiting_input", "committed", "skipped", "handed_off", "escalated"]
NODE_STATES = ["pending", "running", "done", "skipped", "failed"]
TERMINAL = frozenset({"committed", "skipped", "handed_off", "escalated"})

TRANSITIONS = [
    {"trigger": "claim",          "source": "pending",                                        "dest": "presented"},
    {"trigger": "goto",           "source": ["pending", "presented"],                         "dest": "presented"},
    {"trigger": "ask_input",      "source": "presented",                                      "dest": "awaiting_input"},
    {"trigger": "input_received", "source": "awaiting_input",                                 "dest": "presented"},
    {"trigger": "commit",         "source": ["presented", "awaiting_input"],                  "dest": "committed"},
    {"trigger": "skip",           "source": ["pending", "presented", "awaiting_input"],       "dest": "skipped"},
    {"trigger": "handoff",        "source": ["pending", "presented", "awaiting_input"],       "dest": "handed_off"},
    {"trigger": "escalate",       "source": ["presented", "awaiting_input"],                  "dest": "escalated"},
    # a reopen requires a pre-image (§17.1056) and is the ONLY way out of a terminal state
    {"trigger": "reopen",         "source": list(TERMINAL),                                   "dest": "pending"},
    # restore = undo a reopen from its pre-image (§17.1056)
    {"trigger": "restore",        "source": ["pending", "presented"],                         "dest": "committed"},
    # §17.878 pointer heal / stale-guidance bust: presented → pending is a deliberate un-claim
    {"trigger": "unclaim",        "source": ["presented", "awaiting_input"],                  "dest": "pending"},
]

# which node statuses each step status may sit next to (the mirror invariant)
MIRROR: dict[str, frozenset[str]] = {
    "pending":        frozenset({"pending"}),
    "presented":      frozenset({"pending"}),
    "awaiting_input": frozenset({"pending"}),
    "committed":      frozenset({"done"}),
    "skipped":        frozenset({"skipped"}),
    "handed_off":     frozenset({"pending", "running", "done", "failed"}),
    "escalated":      frozenset({"pending", "failed"}),
}

_machine = None


def _get_machine():
    global _machine
    if _machine is None:
        from transitions import Machine

        class _Step:
            pass

        _machine = Machine(model=_Step(), states=STEP_STATES, transitions=TRANSITIONS,
                           initial="pending", auto_transitions=False, ignore_invalid_triggers=True)
    return _machine


_LEGAL: dict[tuple[str, str], set[str]] | None = None


def legal(src: str, dst: str, trigger: str | None = None) -> bool:
    """Is src → dst allowed — by ANY trigger, or by the named one? Naming the
    trigger is what keeps the oracle sharp: `pending → committed` is legal
    for `restore` (undo a reopen from its pre-image) and illegal for
    `commit` (that is the §17.878 commit-without-claim shape)."""
    global _LEGAL
    if _LEGAL is None:
        pairs: dict[tuple[str, str], set[str]] = {}
        for t in TRANSITIONS:
            srcs = t["source"] if isinstance(t["source"], list) else [t["source"]]
            for s in srcs:
                pairs.setdefault((s, t["dest"]), set()).add(t["trigger"])
        _LEGAL = pairs
    if src == dst:
        return True
    triggers = _LEGAL.get((src, dst))
    if not triggers:
        return False
    return trigger is None or trigger in triggers


def mirror_ok(step_status: str | None, node_status: str | None) -> bool:
    if step_status is None or node_status is None:
        return True  # nothing to compare (fresh row / missing side)
    return node_status in MIRROR.get(step_status, frozenset())


def check(site: str, *, src: str | None, dst: str, node_status: str | None = None, node_key: str = "",
          trigger: str | None = None) -> bool:
    """The runtime guard. Returns True when the move is legal AND the mirror
    holds; otherwise logs `step_fsm_violation` (and raises under the strict
    valve). Sites call it with what they are ABOUT to write."""
    from app.config import settings
    ok_move = legal(src, dst, trigger) if src is not None else True
    ok_mirror = mirror_ok(dst, node_status)
    if ok_move and ok_mirror:
        return True
    msg = (f"step_fsm_violation site={site} node_key={node_key} step={src!r}->{dst!r} trigger={trigger!r} "
           f"node={node_status!r} legal_move={ok_move} mirror_ok={ok_mirror}")
    if getattr(settings, "assist_step_fsm_strict", False):
        raise RuntimeError(msg)
    logger.warning(msg)
    return False


def mermaid() -> str:
    lines = ["stateDiagram-v2"]
    for t in TRANSITIONS:
        srcs = t["source"] if isinstance(t["source"], list) else [t["source"]]
        for s in sorted(srcs):
            lines.append(f"    {s} --> {t['dest']}: {t['trigger']}")
    return "\n".join(lines)
