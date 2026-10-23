"""§17.1766 — a plan for code that extends the engine is complete: every module is tested where it is written, no
module takes an engine module's name, and one step wires the parts into the engine.

Live (job 2772acae, 28 steps): the plan wrote nineteen modules and nothing that joins them. No step registered the
daily run, mounted the dashboard, consumed the order stream or called ingest → propose → approve → execute in order
(a read-only review found no call sites at all); the one test step (T22) came last, depended on fourteen modules,
and failed three times on bugs in all of them at once; and T12 was planned as `kill_switch.py`, the engine's own VPN
kill switch. Each is a property of the plan, checkable before a step runs, so it is completed deterministically here
(the step texts gain what they lack, a missing step is added) rather than asked of the planner model again.
"""
from __future__ import annotations

import re

from app.modules.engine_source import module_index

_MODULE_RE = re.compile(r"`?\b([a-z_][a-z0-9_]*)\.py\b`?")
_TEST_RE = re.compile(r"^(?:test_\w+|\w+_test|conftest)$")
_WIRING_RE = re.compile(r"\bapp/main\.py\b|\binclude_router\b|\bget_scheduler\b|\bon startup\b|\blifespan\b|"
                        r"\bwire[sd]?\b.{0,40}\b(?:engine|lab|modules?|together)\b|\bentry (?:point|function)\b", re.I)
#: the step that ASKS (not the ones that act on, record or show an approval)
_ASKS_APPROVAL_RE = re.compile(r"approve-before-run|\b(?:request|ask(?:s|ing)?\s+for|seek|get)\w*\s+(?:\w+\s+){0,3}"
                               r"(?:operator\s+)?approv|\bapproval (?:step|gate|request|prompt)\b", re.I)
_STOP = {"implement", "build", "create", "add", "write", "the", "a", "an", "lab", "engine", "scaffold", "research"}


def _text(t: dict) -> str:
    return " ".join(str(t.get(k) or "") for k in ("name", "notes", "description"))


def _module_of(t: dict) -> str | None:
    """The module a CodeGen task writes: the first non-test `x.py` its text names."""
    if str(t.get("tool") or "").lower() != "codegen":
        return None
    for m in _MODULE_RE.finditer(_text(t)):
        if not _TEST_RE.match(m.group(1)):
            return m.group(1)
    return None


def prefix_for(brief: dict | None) -> str:
    """A short word from the brief's title for the job's own module names (`paper` for "Paper-trading …")."""
    title = str((brief or {}).get("title") or "")
    for w in re.findall(r"[A-Za-z]{4,}", title):
        if w.lower() not in _STOP:
            return w.lower()
    return "job"


def complete_engine_plan(tasks: list[dict], brief: dict | None) -> list[str]:
    """Complete ``tasks`` in place; return what was changed, one line each (the plan's validator warnings)."""
    changes: list[str] = []
    engine_leaves = {m.rsplit(".", 1)[-1] for m in module_index() if m.startswith("app.modules.")}
    pre = prefix_for(brief)

    # 1. a module that would take an engine module's name is renamed, in every task that names it
    for t in tasks:
        mod = _module_of(t)
        if mod and mod in engine_leaves:
            new = f"{pre}_{mod}"
            pat = re.compile(rf"\b{re.escape(mod)}\.py\b|\bapp\.modules\.{re.escape(mod)}\b")
            for u in tasks:
                for k in ("name", "notes", "description"):
                    if u.get(k):
                        u[k] = pat.sub(lambda m: m.group(0).replace(mod, new), str(u[k]))
            changes.append(f"engine_module_renamed: {t.get('id')} `{mod}.py` → `{new}.py` "
                           f"(app.modules.{mod} is the engine's own)")

    # 2. every module is tested in the step that writes it
    for t in tasks:
        mod = _module_of(t)
        if not mod or re.search(rf"\btest_{re.escape(mod)}\.py\b", _text(t)):
            continue
        t["notes"] = (str(t.get("notes") or "").rstrip() + " " if t.get("notes") else "") + (
            f"In this same step also write `tests/test_{mod}.py`: pytest tests of what `{mod}.py` must do (its rules, "
            "its edge cases, its failure paths), with `async_session`, HTTP clients and model calls mocked -- they "
            "run before the step passes, with no network, database or secrets.")
        changes.append(f"module_tests_added: {t.get('id')} writes tests/test_{mod}.py")

    # 3. approval is asked now and answered later: a step specified to return the answer cannot be written
    #    (live T14: `request_approval(proposal) -> bool`; every draft either blocked or returned before any answer)
    for t in tasks:
        if _module_of(t) and _ASKS_APPROVAL_RE.search(_text(t)) and "operator_ask" not in _text(t):
            t["notes"] = str(t.get("notes") or "").rstrip() + (
                " The operator's answer arrives later, through `app.modules.operator_ask`: this step opens the "
                "decisions (`open_decision_job`) and acts in an `@answer_handler` when each answer is recorded; no "
                "function can return the answer when it asks, so nothing here returns or waits for one.")
            changes.append(f"approval_made_asynchronous: {t.get('id')} asks through operator_ask")

    # 4. one step joins the parts into the engine
    modules = [t for t in tasks if _module_of(t)]
    if len(modules) >= 2 and not any(_WIRING_RE.search(_text(t)) for t in tasks):
        ids = {str(t.get("id")) for t in tasks}
        new_id, n = "WIRE", 2
        while new_id in ids:
            new_id, n = f"WIRE{n}", n + 1
        names = ", ".join(f"`{_module_of(t)}.py`" for t in modules)
        tasks.append({
            "id": new_id,
            "name": f"Wire the {pre} modules into the engine",
            "type": "action",
            "tool": "CodeGen",
            "depends_on": [str(t["id"]) for t in modules],
            "inputs": [], "outputs": [],
            "notes": (
                f"Python module `{pre}_wiring.py`: the one place the engine starts this work. It holds the entry "
                f"function that runs the flow end to end in order, calling the modules this job wrote ({names}) "
                "by their real names and signatures; registers any recurring run with `get_scheduler()`; mounts any "
                "new APIRouter; starts any stream consumer; and exposes `def install(app) -> None` doing all of it, "
                "so the engine needs ONE line where it starts (state that line exactly). Nothing a step built may be "
                f"left uncalled. In this same step also write `tests/test_{pre}_wiring.py` exercising the entry "
                "function with every module mocked at its boundary (database, HTTP, models)."),
        })
        changes.append(f"wiring_step_added: {new_id} joins {len(modules)} modules (none of the plan's steps "
                       "registered, mounted or called them)")
    return changes
