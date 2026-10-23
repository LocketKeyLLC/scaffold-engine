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
                               r"(?:operator\s+)?approv|\bapproval (?:step|gate|request|prompt|flow)\b|\b(?:request|ask_for|get)_approval\b"
                               r"|\bfor (?:the )?(?:operator(?:'s)? )?approval\b",
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
_LLM_WORK_RE = re.compile(r"^\s*(?:document|write\s+(?:the\s+)?(?:docs|readme|documentation|lab documentation)|design|"
                          r"decide|research|review|summari[sz]e|choose|plan|outline|draft\s+the\s+(?:docs|readme))\b", re.I)
_RUNTIME_VERB_RE = re.compile(r"^\s*(?:generate|produce|compute|calculate|score|rank|propose|run|send|place|fetch|"
                              r"ingest|record|store|reconcile|track|monitor|execute|classify|extract)\b", re.I)
_CHECK_STEP_RE = re.compile(r"^\s*(?:verify|validate|check|ensure|confirm|assert|prove)\b\s*", re.I)
_MIGRATION_STEP_RE = re.compile(r"\bmigration\b", re.I)
_RUN_TESTS_RE = re.compile(r"\b(?:run|execute|runs|executes)\b.{0,30}\b(?:tests?|test suite|pytest)\b|\bpytest\b", re.I)
_STORE_SECRET_RE = re.compile(r"\b(?:store|save|add|put|integrate|set)\b.{0,60}\b(?:keys?|secrets?|credentials?|tokens?)\b",
                              re.I)
_SCHEMA_WORDS_RE = re.compile(r"\b(?:migration|ddl|schema)\b", re.I)
_APPLY_DDL_RE = re.compile(r"\b(?:run|runs|apply|applies|execute|executes)\b.{0,30}\b(?:ddl|schema|migrations?|create table)\b",
                           re.I)
_DDL_RE = re.compile(r"\bDDL\b|\bCREATE\s+TABLE\b|\bALTER\s+TABLE\b|\bcreate\s+(?:the\s+)?(?:\w+\s+){0,3}tables?\b", re.I)
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

    # 0a. §17.1768/1769/1771 — the engine's own tables are an Alembic revision, never DDL run by hand or by app code.
    #     Live (job 918b6274's re-plans): T3 [Shell] "Runs CREATE TABLE statements in the existing Postgres database";
    #     T3 [CodeGen] "Executable SQL migration implementing T2's schema"; and T2 [CodeGen] "Emit SQL DDL for tables …"
    #     with T3 [CodeGen] "Run the DDL against the existing Postgres" -- tables made at runtime from a module, which
    #     the engine's conventions forbid. A step that WRITES the schema becomes the revision; a step that only APPLIES
    #     it is dropped (the engine applies its revisions when it starts); a lone applier becomes the revision.
    from app.modules.engine_source import _latest_migration
    head = _latest_migration()
    num = int(head[:4]) + 1 if head[:4].isdigit() else 0
    schema = [t for t in tasks if str(t.get("tool") or "").lower() in ("shell", "codegen")
              and (_DDL_RE.search(_text(t)) or _MIGRATION_STEP_RE.search(_text(t)))
              and "alembic/versions/" not in _text(t) and not _CHECK_STEP_RE.match(str(t.get("name") or ""))]
    appliers = [t for t in schema if _APPLY_DDL_RE.search(_text(t))]
    writers = [t for t in schema if t not in appliers]
    if appliers and not writers:
        writers = [appliers.pop(0)]
    for t in appliers:
        _drop(tasks, t)
        changes.append(f"ddl_apply_dropped: {t.get('id')} (the engine applies its Alembic revisions when it starts)")
    prev = head
    for t in writers:
        what = _SCHEMA_WORDS_RE.sub(" ", str(t.get("name") or "tables"))
        rev = f"{num:04d}_{pre}_{_slug(what)}"
        was = str(t.get("tool") or "")
        t["tool"] = "CodeGen"
        t["notes"] = (f"An Alembic revision `alembic/versions/{rev}.py` (revision = \"{rev}\", down_revision = "
                      f"\"{prev}\") creating these tables; the engine applies it when it starts -- no SQL is run by "
                      "hand or by app code. " + str(t.get("notes") or "").strip())
        changes.append(f"ddl_made_a_migration: {t.get('id')} is the Alembic revision {rev} (it was a {was} step)")
        prev, num = rev, num + 1

    # 0d. §17.1769 — a Shell step that runs this job's tests is the sandbox's work already (§17.1765 runs a test step's
    #     tests before it passes); live 918b6274 T29 [Shell] "Run integration tests: Execute test suite" would have run
    #     on the Proxmox host. It is dropped; what stood on it stands on what it stood on.
    for t in [t for t in tasks if str(t.get("tool") or "").lower() == "shell" and _RUN_TESTS_RE.search(_text(t))]:
        _drop(tasks, t)
        changes.append(f"test_run_dropped: {t.get('id')} (the engine-test sandbox runs the tests when their step passes)")

    # 0e. §17.1769 — a key only the operator holds is theirs to give, through the engine's secrets, once the code that
    #     reads it exists; live 918b6274 T4 [Shell] "Store paper-trading key in existing secrets store", first in the
    #     plan, would have blocked every step behind the runner. It becomes a question at the end.
    secret_steps = [t for t in tasks if str(t.get("tool") or "").lower() == "shell" and _STORE_SECRET_RE.search(_text(t))
                    and not _CHECK_STEP_RE.match(str(t.get("name") or ""))]
    for t in secret_steps:
        for u in tasks:
            if t["id"] in (u.get("depends_on") or []):
                u["depends_on"] = [d for d in u["depends_on"] if d != t["id"]] + [
                    d for d in (t.get("depends_on") or []) if d not in u["depends_on"]]
        t["tool"], t["type"] = "LLM", "decision"
        subject = re.sub(r"\b(?:integrate|store|save|add|set|put|the|paper-trading|credentials?|keys?|secrets?|in|into|"
                         r"existing|store)\b", " ", _VERB_RE.sub("", str(t.get("name") or "")), flags=re.I)
        subject = " ".join(subject.split()) or "this lab"
        t["name"] = f"Are the {subject} keys stored in the engine's secrets?"
        t["notes"] = ("The operator stores the keys only they hold on the engine's Machines page → Secrets, under the "
                      "UPPER_SNAKE names this job's code reads (`values_for(db, [...])` in its client step); the "
                      "engine never asks for a value in a step and never logs one. Answer when they are stored. "
                      + str(t.get("notes") or "").strip())
        changes.append(f"secret_step_made_the_operators: {t.get('id')} asks the operator, after the code exists")

    # 0f. §17.1770 — what is left as Shell in a plan for engine code is code the job must write. Live (918b6274's
    #     fourth plan): "Run daily proposal batch", "Execute approved orders", "Acquire point-in-time data", "Run
    #     backtest validation" and three "Verify …" checkpoints were Shell steps -- operating the lab on the Proxmox
    #     host before its code was wired, and checking it by hand. Building the engine is writing its code: a check
    #     becomes a test module the sandbox runs (§17.1765); anything else becomes the module that does it, which
    #     WIRE calls.
    for t in tasks:
        if str(t.get("tool") or "").lower() != "shell":
            continue
        name = str(t.get("name") or "")
        t["tool"] = "CodeGen"
        if _CHECK_STEP_RE.match(name) or str(t.get("type") or "") == "validation":
            t["type"] = "action"
            test = f"test_{pre}_{_slug(_CHECK_STEP_RE.sub('', name))}"
            t["notes"] = (f"Python test module `tests/{test}.py`: pytest tests that prove it -- " + name.rstrip(".") + "."
                          " Mock `async_session`, HTTP clients and model calls; the tests run before the step passes. "
                          + str(t.get("notes") or "").strip())
            changes.append(f"check_made_a_test: {t.get('id')} writes tests/{test}.py (it was a Shell check)")
        else:
            changes.append(f"shell_made_code: {t.get('id')} writes the code that does it (it was a Shell step)")

    # 0g. §17.1772 — an LLM step that DOES the lab's work at build time is the code that does it every day (live
    #     918b6274's sixth plan: T14 [LLM] "Generate trade proposals" between the signal store and the risk checks --
    #     a model making proposals once, now, instead of the module that makes them each morning). Reading, deciding,
    #     designing, reviewing and documenting stay LLM steps.
    for t in tasks:
        if (str(t.get("tool") or "").lower() == "llm" and str(t.get("type") or "action") == "action"
                and not _LLM_WORK_RE.match(str(t.get("name") or "")) and _RUNTIME_VERB_RE.match(str(t.get("name") or ""))):
            t["tool"] = "CodeGen"
            changes.append(f"llm_work_made_code: {t.get('id')} writes the code that does it (it was an LLM step doing "
                           "the lab's work once, at build time)")

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
    # the operator's credentials come after the code that reads them, wired (0e)
    wires = [str(t["id"]) for t in tasks if str(t.get("id", "")).startswith("WIRE")]
    for t in secret_steps:
        want = wires or [str(m["id"]) for m in modules]
        t["depends_on"] = list(dict.fromkeys([*(t.get("depends_on") or []), *want]))
    return changes


def _drop(tasks: list[dict], gone: dict) -> None:
    """Remove ``gone``; each task that depended on it depends on what it depended on."""
    tasks.remove(gone)
    for u in tasks:
        if gone["id"] in (u.get("depends_on") or []):
            u["depends_on"] = list(dict.fromkeys(
                [d for d in u["depends_on"] if d != gone["id"]] + list(gone.get("depends_on") or [])))
