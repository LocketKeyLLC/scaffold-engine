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
#: §17.1331 — a step that BUILDS, and the separate things it asks to be built. A cut draw was the
#: symptom; a draft can also come back small and do a fraction of the work (live, ADD100's third
#: draft was 498 characters that wrote only package.json and would have claimed the whole step).
#: The signal is in the STEP: it asks for more than one deliverable in one block.
_BUILD_RE = re.compile(r"\b(?:re)?(?:build|write|create|implement|develop|rework|add)\b|\bset ?up\b", re.I)
_DELIVERABLES = {
    "a backend": r"\bback[- ]?end\b",
    "a frontend": r"\bfront[- ]?end\b",
    "a user interface": r"\b(?:UI|user interface|web interface)\b",
    "an API": r"\bAPI\b|\bendpoints?\b",
    "a service unit": r"\bsystemd service\b|\bservice unit\b",
    "a page": r"\bpage\b|\bdashboard\b",
}


#: §17.1331b — what joins two deliverables that are both to be built.
_COORDINATED_RE = re.compile(r"[\s,]*(?:and|&|\+|/|,|as well as|plus|then)?[\s,]*(?:the\s+|a\s+|an\s+|its\s+|new\s+)*", re.I)


def deliverables_in(node: Optional[dict]) -> list[str]:
    """The separate things a BUILD step asks to be built, by the step's own words.

    Measured over the live plan's 150 steps: fires on ADD100 alone ("Rebuild the
    control panel…": a backend, a frontend, a page) and on nothing else. "Stop the
    control-panel backend node process" names two but builds neither, so the build
    verb is required; T33 "Build control panel backend" and T34 "Build control panel
    frontend" name ONE each and are correctly sized — they failed for another reason
    (§17.1208, recorded done with 553 and 154 bytes), which splitting would not fix.
    """
    text = " ".join(str((node or {}).get(k) or "") for k in ("title", "description", "prompt_template"))
    if not _BUILD_RE.search(text):
        return []
    spans: list[tuple[int, int, str]] = []
    for name, rx in _DELIVERABLES.items():
        for m in re.finditer(rx, text, re.I):
            spans.append((m.start(), m.end(), name))
    spans.sort()
    # §17.1331b — the deliverables must be COORDINATED, two things to build: "the backend and the
    # frontend". "A service unit FOR the backend" (ADD114) names two and builds one, so a pair
    # joined by "for"/"of"/"in" is one deliverable qualified by another, and does not count.
    named: set[str] = set()
    for (_, e1, n1), (s2, _, n2) in zip(spans, spans[1:], strict=False):
        if n1 == n2 or s2 - e1 > 40:
            continue
        if _COORDINATED_RE.fullmatch(text[e1:s2]):
            named.update({n1, n2})
    return sorted(named)

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
                        "check": {"type": "string", "description": "ONE read-only command whose output shows this step's goal WORKS -- call it and read a real value back; never a grep/ls/cat that only shows the code exists"},
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
    "has ONE read-only command that shows its own goal WORKS (not a later step's) -- for a route or a page, call it "
    "and check the answer carries the real data; a grep for the code's name only shows the code exists, which is not "
    "done; together they do everything the "
    "original step asked for and nothing more; order them so each can run when the one before it is done. Do not "
    "invent addresses, ports, unit names or credentials — use what the step and the facts give you, or say the value "
    "is the operator's to supply."
)


def too_large(frame: Optional[dict], node: Optional[dict] = None) -> str:
    """§17.1330/§17.1331 — the reason this step cannot be carried as one block, or ``""``."""
    for r in (frame or {}).get("refused") or []:
        why = str(r.get("why") or "")
        if _TOO_LARGE_RE.search(why):
            return " ".join(why.split())[:300]
    parts = deliverables_in(node)
    if len(parts) >= 2:
        return (f"the step asks for {len(parts)} separate deliverables in one block ({', '.join(parts)}); "
                f"the engine carries one block of commands per step")
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


#: §17.1408 — the engine's own count correction, so the counter never reads it back as a claim
_ENGINE_NOTE_RE = re.compile(r"ENGINE MEASURED: the plan holds \d+ capability steps \([^)]*\); this step says [^.]*\. Cover all \w+\.")
#: a check that only shows code or a unit EXISTS: grep/ls/test/cat/stat, or is-active/is-enabled alone
_PRESENCE_RE = re.compile(
    r"^\s*(?:(?:pct\s+exec|qm\s+guest\s+exec)\s+\d+\s+--\s+(?:(?:ba)?sh\s+-c\s+['\"])?)?"
    r"(?:grep\b|ls\b|test\b|\[\s|cat\b|stat\b|head\b|wc\b|systemctl\s+is-(?:active|enabled)\b)")
#: a step that builds something that DOES something: a route, an API, a page, a capability
_CAPABILITY_BUILD_RE = re.compile(r"\b(?:route|endpoint|api\b|capabilit|proxy|page|frontend|backend|authentication|reverse proxy)", re.I)


def presence_only(check: str) -> bool:
    """§17.1408 — does this check only show that something EXISTS (not that it works)?"""
    c = str(check or "")
    if re.search(r"\bcurl\b|\bwget\b|\bhttp\b", c):
        return False                         # it calls something
    return bool(_PRESENCE_RE.match(c))


def builds_a_capability(text: str) -> bool:
    return bool(_CAPABILITY_BUILD_RE.search(str(text or "")))


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
        if check and presence_only(check) and builds_a_capability(title + " " + body):
            # §17.1408 — `grep -n palworld server.js` shows the code is THERE, not that
            # it works. Live, ADD122 was recorded done twice on that check while its GET
            # answered `{"settings":{}}` and its PUT could not write. The operator:
            # behaviour checks. The step says so, and the presence check is named for
            # what it is, so neither the drafter nor the judge mistakes it for done.
            descr += (f" Done when it WORKS: exercise it and read a real value back (call the route and check "
                      f"the answer carries the real data; for a write, write a value and read it back). "
                      f"`{check}` only shows the code is there, which is not done.")
        elif check:
            descr += f" Done when `{check}` shows it."
        descr += f"\n\n{SPLIT_MARK.format(key=parent_key)} — {i + 1} of {len(steps)}]"
        out.append({"node_key": key, "title": title, "description": descr, "tool": "LLM",
                    "depends_on": list(parent_deps) if i == 0 else [keys[i - 1]]})
    return out


# ── §17.1334: the numbering and any stated count survive a child coming or going ──
#: `[Engine split of ADD100 — 5 of 8]`, as `children_from` writes it
_STAMP_RE = re.compile(r"\[Engine split of (?P<parent>[A-Za-z0-9_.:-]+)\s*[—–-]\s*(?P<n>\d+) of (?P<total>\d+)\]")
#: a child that says how many of something there are: "renders exactly four sections"
_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
          "seven": 7, "eight": 8, "nine": 9, "ten": 10}
_NUM_WORD = {v: k for k, v in _WORDS.items()}
_COUNT_RE = re.compile(
    r"\b(?:exactly|all|the)\s+(?P<num>one|two|three|four|five|six|seven|eight|nine|ten|\d+)\s+"
    r"(?P<kind>sections?|capabilit(?:y|ies)|tiles?|parts?|items?)\b", re.I)
#: §17.1334b — what a capability child calls itself. The word alone is not the
#: signal: live, ADD121 builds "an editable config-driven capability registry"
#: and ADD126 renders "the three capabilities from the registry" — neither IS a
#: capability. The shape is `implement the <X> capability`.
_CAPABILITY_RE = re.compile(r"\bimplement\b[^.]*?\bcapabilit(?:y|ies)\b", re.I)


def split_children(plan: Optional[list[dict]], parent_key: str) -> list[dict]:
    """This parent's children, in plan order, with their stamp parsed."""
    mark = SPLIT_MARK.format(key=parent_key)
    out = []
    for n in plan or []:
        body = str(n.get("description") or "")
        if mark not in body:
            continue
        m = _STAMP_RE.search(body)
        out.append({**n, "_n": int(m.group("n")) if m else None,
                    "_total": int(m.group("total")) if m else None})
    return out


def parent_of(node: Optional[dict]) -> str:
    """The step this one is a child of, from its own stamp, or ''."""
    m = _STAMP_RE.search(str((node or {}).get("description") or ""))
    return m.group("parent") if m else ""


def stamp_edits(plan: Optional[list[dict]], parent_key: str) -> list[dict]:
    """The children whose `n of N` no longer matches the plan.

    Live (§17.1334): ADD100 was split twice, and after the second run the plan
    held `ADD121 — 1 of 7` beside `ADD122 — 2 of 8`. Two different totals for
    one split, and a child numbered 5 twice. The numbers are the engine's own
    record of what it decided; a stale one misreports the work.
    """
    kids = split_children(plan, parent_key)
    total = len(kids)
    out: list[dict] = []
    for i, k in enumerate(kids):
        want_n, want_total = i + 1, total
        if k["_n"] == want_n and k["_total"] == want_total:
            continue
        body = str(k.get("description") or "")
        new = _STAMP_RE.sub(f"[Engine split of {parent_key} — {want_n} of {want_total}]", body, count=1)
        if new == body:
            continue
        out.append({"node_key": str(k.get("node_key")), "description": new,
                    "says": f"{k['_n']} of {k['_total']}", "should_be": f"{want_n} of {want_total}"})
    return out


def count_edits(plan: Optional[list[dict]], parent_key: str) -> list[dict]:
    """The children that state a count of their siblings which is no longer true.

    Live (§17.1334): ADD126 was titled "the single-page frontend rendering the
    three capabilities" and said it "renders exactly three sections", while
    ADD100's own text asks for FOUR capabilities and four capability children
    stood in the plan. A step that builds the surface for its siblings must not
    carry a smaller number than the siblings it has: that is how a capability
    the operator asked for goes missing with nothing refused.

    Nothing is rewritten in place (§17.1334b). ADD121's text lists the three by
    NAME ("palworld-settings, media-request, scaffold-engine") and is a DONE
    step; swapping its number would leave a list of three behind a word saying
    four, and would falsify a record of what ran. A PENDING child gets the
    correction APPENDED, naming every sibling it must cover. A finished child
    gets a note on the frame and no write at all.
    """
    kids = split_children(plan, parent_key)
    caps = [k for k in kids if _CAPABILITY_RE.search(str(k.get("title") or ""))]
    real = len(caps)
    if real < 2:
        return []                      # nothing to count
    names = ", ".join(f"{k.get('node_key')} {_short_title(k)}" for k in caps)
    out: list[dict] = []
    for k in kids:
        if _CAPABILITY_RE.search(str(k.get("title") or "")):
            continue                   # a capability step counts no siblings
        said = raw = kind = ""
        for field in ("title", "description"):
            # §17.1408 — the engine's own correction says "this step says three
            # capabilities", and the counter read that back as the step's claim:
            # every pass appended another copy (live, ADD126 carried TWELVE).
            m = _COUNT_RE.search(_ENGINE_NOTE_RE.sub("", str(k.get(field) or "")))
            if not m:
                continue
            raw, kind = m.group("num"), m.group("kind")
            said = int(raw) if raw.isdigit() else _WORDS.get(raw.lower(), 0)
            if said and said != real:
                break
            said = ""
        if not said:
            continue
        word = _NUM_WORD.get(real, str(real))
        hit = {"node_key": str(k.get("node_key")), "siblings": real,
               "says": f"{raw} {kind}", "should_be": f"{word} {kind}"}
        correction = (f"ENGINE MEASURED: the plan holds {real} capability steps ({names}); this step says "
                      f"{raw} {kind}. Cover all {word}.")
        if str(k.get("status") or "pending") != "pending":
            hit["note"] = correction          # a finished step's record is not rewritten
        elif correction in str(k.get("description") or ""):
            continue                          # §17.1408 — said once is said
        else:
            hit["description"] = str(k.get("description") or "").rstrip() + "\n\n" + correction
        out.append(hit)
    return out


def _short_title(node: dict) -> str:
    """`implement the media request capability (…)` → `media request`."""
    t = str(node.get("title") or "")
    m = re.search(r"\bimplement\s+(?:the\s+)?(.+?)\s+capabilit", t, re.I)
    return m.group(1) if m else t.split(":")[-1].strip()[:40]


async def reconcile_split(job_id: str, plan: Optional[list[dict]], parent_key: str) -> list[str]:
    """Apply both corrections through the node editor; return what was said on the frame."""
    if not parent_key:
        return []
    from app.modules import node_editor
    from app.database import get_db
    said: list[str] = []
    jobs = [("renumbered", e) for e in stamp_edits(plan, parent_key)] + \
           [("recounted", e) for e in count_edits(plan, parent_key)]
    if not jobs:
        return []
    async for db in get_db():
        for what, e in jobs:
            try:
                fields = {f: e[f] for f in ("title", "description") if f in e}
                res = await node_editor.edit_node(
                    job_id, e["node_key"], fields,
                    edited_by=f"engine:split of {parent_key} ({what})", db=db, cascade=False)
                if res.get("error"):
                    logger.warning("split_reconcile_failed node=%s err=%s", e["node_key"], res["error"])
                    continue
                said.append(f"{e['node_key']} said `{e['says']}`; the plan holds {e['should_be']}" if what == "renumbered"
                            else f"{e['node_key']} said `{e['says']}` while {e['siblings']} capability steps stand "
                                 f"in the plan; corrected to `{e['should_be']}`")
            except Exception as exc:
                logger.warning("split_reconcile_error node=%s err=%r", e["node_key"], exc)
        break
    return said


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
