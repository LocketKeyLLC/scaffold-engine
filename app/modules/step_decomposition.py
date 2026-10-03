"""§17.1330 — splitting a step the engine cannot carry as one block.

The executor's unit of work is ONE approved block of commands. Every mechanism
the engine has for work that is bigger than that operates on JOBS: `decomposition`
spawns component jobs under an umbrella. Nothing splits a STEP.

So a planner-emitted step that is really a project is a dead end. Live, ADD100
"Rebuild the control panel to do what was chosen in ADD99" asks for three
capabilities (edit the Palworld server's settings, request a film or show, reach
the engine itself) across a backend, a frontend and a service. Its draws came
back 12,614 characters and cut mid-line; §17.1312 refused them, correctly, and
the frame offered `myself | skip`. The operator's own decision sat behind a step
the engine would refuse forever. And it had happened before, quietly: T33 "Build
control panel backend" and T34 "Build control panel frontend" are CodeGen nodes
recorded **done** with 553 and 154 bytes of output.

Refusing is right. Stopping there is not: a step the engine cannot carry is a
plan defect the engine can fix, with the primitives it already has (`insert_node`,
`edit_node`, `reorder_nodes`, the pause's restart). It splits the step into
ordered children it CAN carry, chains them, makes the parent wait for the last
one, and asks about the first. The parent is not rewritten: once the children are
done its own checks are read off the machine (§17.1302) and it is recorded done
having run nothing.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Optional

from app.providers.base import Tool

logger = logging.getLogger("scaffold")

#: §17.1330 — stamped on every child, so a split is visible in the plan and never repeated.
SPLIT_MARK = "[Engine split of {key}"
MIN_CHILDREN, MAX_CHILDREN = 2, 8

#: the refusals that mean "this does not fit in one block", not "this block is wrong".
_TOO_LARGE_RE = re.compile(r"content cut|too large for one draw", re.I)

SPLIT_TOOL = Tool(
    name="record_step_split",
    description="The ordered steps this one step has to become, each one block of work.",
    input_schema={
        "type": "object",
        "properties": {
            "steps": {
                "type": "array",
                "minItems": MIN_CHILDREN,
                "maxItems": MAX_CHILDREN,
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "one line, names the machine and the one thing this step does"},
                        "description": {"type": "string", "description": "what to do, concretely, in this one step only"},
                        "check": {"type": "string", "description": "ONE read-only command whose output shows this step's goal is met"},
                    },
                    "required": ["title", "description", "check"],
                },
            },
        },
        "required": ["steps"],
    },
)

SPLIT_SYSTEM = (
    "You split ONE step of a plan into the ordered steps it has to become. The engine that runs them carries "
    "exactly one approved block of shell commands per step, on one machine, with a time budget — so a step that "
    "writes a whole application does not fit, and a step that writes one file, installs one package set, or wires "
    "one service does. Rules: between 2 and 8 steps; each one does ONE thing and says which machine it is on; each "
    "has ONE read-only command that shows its own goal is met (not a later step's); together they do everything the "
    "original step asked for and nothing more; order them so each can run when the one before it is done. Do not "
    "invent addresses, ports, unit names or credentials — use what the step and the facts give you, or say the value "
    "is the operator's to supply."
)


def too_large(frame: Optional[dict]) -> str:
    """§17.1330 — the reason this step cannot be carried as one block, or ``""``."""
    for r in (frame or {}).get("refused") or []:
        why = str(r.get("why") or "")
        if _TOO_LARGE_RE.search(why):
            return " ".join(why.split())[:300]
    return ""


def already_split(plan: Optional[list[dict]], node_key: str) -> list[str]:
    """The children a previous split made for this step, in plan order."""
    mark = SPLIT_MARK.format(key=node_key)
    return [str(n.get("node_key")) for n in plan or [] if mark in str(n.get("description") or "")]


def _next_keys(plan: Optional[list[dict]], count: int) -> list[str]:
    nums = [int(m.group(1)) for n in plan or []
            for m in [re.fullmatch(r"ADD(\d+)", str(n.get("node_key") or ""))] if m]
    start = (max(nums) + 1) if nums else 1
    return [f"ADD{start + i}" for i in range(count)]


def children_from(steps: list[dict], *, parent_key: str, parent_deps: list[str], keys: list[str],
                  machine: str = "") -> list[dict]:
    """The `insert_node` specs for a split: chained, stamped, each with its check."""
    out: list[dict] = []
    for i, (key, st) in enumerate(zip(keys, steps, strict=False)):
        title = " ".join(str(st.get("title") or "").split())[:200]
        body = " ".join(str(st.get("description") or "").split())
        check = " ".join(str(st.get("check") or "").split())
        if not title or not body:
            continue
        descr = body
        if machine and machine.lower() not in (title + " " + body).lower():
            descr += f" On {machine}."
        if check:
            descr += f" Done when `{check}` shows it."
        descr += f"\n\n{SPLIT_MARK.format(key=parent_key)} — {i + 1} of {len(steps)}]"
        out.append({"node_key": key, "title": title, "description": descr, "tool": "LLM",
                    "depends_on": list(parent_deps) if i == 0 else [keys[i - 1]]})
    return out


async def propose_split(node: dict, brief, environment: Optional[dict], upstream: str = "",
                        reason: str = "") -> list[dict]:
    """Ask for the ordered steps. Fail-soft to ``[]`` — a step that cannot be split
    stays refused, which is today's behaviour and survivable."""
    from app import model_router
    from app.utils.tool_call_args import read_tool_args
    b = brief if isinstance(brief, str) else json.dumps(brief or {}, default=str)
    env = environment or {}
    facts = [str(f.get("text") if isinstance(f, dict) else f) for f in (env.get("facts") or [])][:30]
    subs = env.get("substitutions") or {}
    prompt = (
        f"THE STEP TO SPLIT — {node.get('node_key')}: {node.get('title') or ''}\n\n"
        f"{' '.join(str(node.get('description') or '').split())[:4000]}\n\n"
        f"WHY IT DOES NOT FIT AS ONE STEP: {reason or 'it is larger than one approved block of commands'}\n\n"
        f"WHAT EARLIER STEPS ESTABLISHED (reproduce what they decided; do not re-decide it):\n{str(upstream or '')[:6000]}\n\n"
        f"KNOWN FACTS:\n" + "\n".join(f"- {f[:200]}" for f in facts) + "\n\n"
        + ("PINNED VALUES:\n" + "\n".join(f"- {k} = {v}" for k, v in list(subs.items())[:30]) + "\n\n" if subs else "")
        + f"THE JOB:\n{b[:2000]}\n\nSplit it."
    )
    try:
        resp = await model_router.tool_call(
            messages=[{"role": "user", "content": SPLIT_SYSTEM + "\n\n" + prompt}],
            tools=[SPLIT_TOOL], role="model_general", temperature=0.1,
            tool_choice="auto", max_tokens=3000)
        steps = (read_tool_args(resp) or {}).get("steps") or []
    except Exception as exc:
        logger.warning("step_split_draw_failed node=%s err=%r", node.get("node_key"), exc)
        return []
    out = [s for s in steps if isinstance(s, dict) and str(s.get("title") or "").strip()
           and str(s.get("description") or "").strip()]
    if len(out) < MIN_CHILDREN:
        logger.warning("step_split_unusable node=%s steps=%d", node.get("node_key"), len(out))
        return []
    return out[:MAX_CHILDREN]


async def split_step(job_id: str, node: dict, plan: Optional[list[dict]], brief, environment: Optional[dict],
                     upstream: str = "", reason: str = "", machine: str = "") -> list[str]:
    """Split the step in place: insert the children, chain them, make this step wait
    for the last, order them before it. Returns the lines for the frame; ``[]`` when
    nothing was changed (already split, unusable proposal, or a write that failed)."""
    key = str(node.get("node_key") or "")
    if not key:
        return []
    done_before = already_split(plan, key)
    if done_before:
        logger.info("step_split_already node=%s children=%s", key, done_before)
        return []
    steps = await propose_split(node, brief, environment, upstream, reason)
    if not steps:
        return []
    keys = _next_keys(plan, len(steps))
    specs = children_from(steps, parent_key=key, parent_deps=[str(d) for d in (node.get("depends_on") or [])],
                          keys=keys, machine=machine)
    if len(specs) < MIN_CHILDREN:
        return []

    from app.database import async_session
    from app.modules import node_editor
    made: list[str] = []
    for spec in specs:
        try:
            async with async_session() as db:
                res = await node_editor.insert_node(job_id, spec, db=db, edited_by=f"engine:split of {key} — {reason[:100]}")
            if res.get("status") == "ok":
                made.append(spec["node_key"])
            else:
                logger.warning("step_split_insert_refused node=%s child=%s res=%r", key, spec["node_key"], res)
        except Exception as exc:
            logger.warning("step_split_insert_failed node=%s child=%s err=%r", key, spec["node_key"], exc)
    if len(made) < MIN_CHILDREN:
        return []
    try:
        async with async_session() as db:
            deps = [str(d) for d in (node.get("depends_on") or [])]
            await node_editor.edit_node(job_id, key, {"depends_on": deps + [made[-1]]}, db=db, cascade=False,
                                        edited_by=f"engine:split — {key} waits for {made[-1]}")
        node["depends_on"] = [str(d) for d in (node.get("depends_on") or [])] + [made[-1]]
    except Exception as exc:
        logger.warning("step_split_parent_edit_failed node=%s err=%r", key, exc)
    try:                                        # the children run BEFORE the step they came from
        async with async_session() as db:
            listing = await node_editor.list_nodes(job_id, db)
        order = [str(n.get("node_key")) for n in (listing or {}).get("nodes") or []]
        order = [k for k in order if k not in made]
        if key in order:
            at = order.index(key)
            order = order[:at] + made + order[at:]
            async with async_session() as db:
                await node_editor.reorder_nodes(job_id, order, db=db, edited_by=f"engine:split of {key}")
    except Exception as exc:
        logger.warning("step_split_reorder_failed node=%s err=%r", key, exc)
    logger.warning("step_split job=%s node=%s children=%s reason=%r", job_id, key, made, reason[:120])
    return ([f"split {key} into {len(made)} steps it can carry: " + ", ".join(made)]
            + [f"{key} now waits for {made[-1]}"])
