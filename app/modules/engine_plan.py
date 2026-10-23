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
                               r"(?:operator\s+)?approv|\bapproval (?:step|gate|request|prompt|flow)\b|\b(?:request|ask_for|get)_approval\b",
                               re.I)
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


_VERB_RE = re.compile(r"^\s*(?:implement|configure|ingest|store|acquire|build|determine|create|add|write|define|"
                      r"design|make|set\s*up|compute|fetch|track|generate|record|run|load)\s+", re.I)
_DDL_RE = re.compile(r"\bCREATE\s+TABLE\b|\bALTER\s+TABLE\b|\bcreate\s+(?:the\s+)?(?:\w+\s+){0,3}tables?\b", re.I)
_TESTS_STEP_RE = re.compile(r"\b(?:write|add|implement|build)\b.{0,40}\btests?\b|\btest suite\b", re.I)
_TESTS_WORD_RE = re.compile(r"\b(?:tests?|suite)\b", re.I)
_MIGRATION_RE = re.compile(r"\b(?:alembic|migration|(?:postgres|database|db|audit)\s+schema|create\s+tables?)\b", re.I)
_ARTICLES = {"a", "an", "the", "of", "for", "and", "to", "with", "via", "from", "into", "on", "in"}


def _slug(title: str) -> str:
    """`Implement audit trail writes` → `audit_trail_writes` (at most four words)."""
    words = [w for w in re.findall(r"[a-z0-9]+", _VERB_RE.sub("", title).lower()) if w not in _ARTICLES]
    return "_".join(words[:4]) or "step"


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

    # 0a. §17.1768 — the engine's own tables are an Alembic revision, never DDL a shell step runs by hand (live
    #     918b6274 T3 [Shell] "Create audit trail tables: Runs CREATE TABLE statements in the existing Postgres
    #     database" -- on whichever machine the runner is, outside the engine's history)
    from app.modules.engine_source import _latest_migration
    head = _latest_migration()
    nxt = f"{int(head[:4]) + 1:04d}" if head[:4].isdigit() else "NNNN"
    for t in tasks:
        if str(t.get("tool") or "").lower() == "shell" and _DDL_RE.search(_text(t)):
            rev = f"{nxt}_{pre}_{_slug(str(t.get('name') or 'tables'))}"
            t["tool"] = "CodeGen"
            t["notes"] = (f"An Alembic revision `alembic/versions/{rev}.py` (revision = \"{rev}\", down_revision = the "
                          "engine's newest revision) creating these tables; the engine applies it when it starts -- "
                          "no SQL is run by hand. " + str(t.get("notes") or "").strip())
            changes.append(f"ddl_made_a_migration: {t.get('id')} is the Alembic revision {rev} (it was a Shell step "
                           "running CREATE TABLE)")

    # 0b. §17.1768 — a step that writes tests writes a test module, not a module with tests of its own
    for t in tasks:
        if (str(t.get("tool") or "").lower() == "codegen" and _TESTS_STEP_RE.search(str(t.get("name") or ""))
                and not _MODULE_RE.search(_text(t))):
            name = f"test_{pre}_{_slug(_TESTS_WORD_RE.sub('', str(t.get('name') or '')))}"
            t["notes"] = (f"Python test module `tests/{name}.py` (pytest; mock `async_session`, HTTP clients and model "
                          "calls; it runs before the step passes)." + (" " + str(t["notes"]).strip() if t.get("notes") else ""))
            changes.append(f"test_module_named: {t.get('id')} writes tests/{name}.py")

    # 0. a code step that names no module is given one, from its title (live, job 918b6274 2026-10-23: the planner
    #    wrote "Exports append_signal(), …" for every step and named no file -- nothing below could see a module, so
    #    the plan went uncompleted and each step would have guessed its own file name)
    taken = {m for t in tasks if (m := _module_of(t))}
    for t in tasks:
        if str(t.get("tool") or "").lower() != "codegen" or _module_of(t) or _MODULE_RE.search(_text(t)):
            continue
        if _MIGRATION_RE.search(_text(t)):
            continue                                       # a schema step writes an Alembic revision, not a module
        base = f"{pre}_{_slug(str(t.get('name') or t.get('id') or 'step'))}"
        name, n = base, 2
        while name in taken or name in engine_leaves:
            name, n = f"{base}_{n}", n + 1
        taken.add(name)
        t["notes"] = f"Python module `{name}.py`." + (" " + str(t["notes"]).strip() if t.get("notes") else "")
        changes.append(f"module_named: {t.get('id')} writes `{name}.py` (its text named no file)")

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
        # the step's own name says it asks, or it exports the asking function; a step that only mentions the
        # approval flow it is "consumed by" (live 918b6274 T6, T24) does not ask
        asks = (_ASKS_APPROVAL_RE.search(str(t.get("name") or ""))
                or re.search(r"\b(?:request|ask_for|get)_approval\b", _text(t)))
        if _module_of(t) and asks and "operator_ask" not in _text(t):
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
