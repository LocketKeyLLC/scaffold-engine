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

import logging
import re

from app.modules.engine_source import module_index

logger = logging.getLogger("scaffold")

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
_BLOCKS_RE = re.compile(r"[^.]*\b(?:blocks?|waits?|polls?)\b(?:\s+\w+){0,3}\s+(?:until|for)\b[^.]*\b(?:approv|reject|answer|decision)\w*[^.]*\.?",
                        re.I)
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


#: §17.1799 — "entry points for A, B, C" in a step's text
_ENTRY_POINTS_RE = re.compile(r"\bentry points? for ([^.;:`]+)", re.I)


def _stem(item: str) -> str:
    """The word that names an entry point (`dashboard` of "operator dashboard"); its first 6 letters match a step."""
    words = [w for w in re.findall(r"[a-z]+", item.lower()) if len(w) > 3 and w not in ("daily", "batch", "operator")]
    return words[-1][:6] if words else ""


#: §17.1797 — the per-module test sentence written before §17.1783 (no database; `async_session` mocked)
_STALE_TESTS_RE = re.compile(r"In this same step also write `tests/test_(\w+)\.py`:[^`]*(?:`[^`]*`[^`]*)*?with no network, "
                             r"database or secrets\.")


def _tests_note(mod: str) -> str:
    return (f"In this same step also write `tests/test_{mod}.py`: pytest tests of what `{mod}.py` must do (its "
            "rules, its edge cases, its failure paths). They run before the step passes, with no network or secrets; "
            "`async_session` is a scratch database with this job's tables, so SQL is tested for real (each test starts "
            "with them empty and inserts the rows it needs; never DELETE from them yourself -- an append-only table "
            "refuses it); HTTP clients and model calls are mocked.")


def _carried_prefix(carried: list[str]) -> str | None:
    """§17.1814 — the word the earlier job's modules start with (`paper` for `paper_order_executor`, …)."""
    heads = [m.split("_", 1)[0] for m in carried if "_" in m]
    return max(sorted(set(heads)), key=heads.count) if heads else None


_EDITS_RE = r"\b(?:edit|fix|change|update|modify|correct|repair|extend)\w*\s+(?:the\s+|existing\s+|in\s+)*`?"


def _carried_own(t: dict, carried: list[str]) -> str | None:
    """§17.1814 — the earlier job's module this step changes: its title names it ("Fix SPY benchmark tracker"), or
    its text says to edit it ("Edit paper_order_executor in place"). A module it only USES is not its own."""
    from app.modules.engine_source import _names_module
    mod = _module_of(t)
    if mod in carried:                                # §17.1818 — the module it is given is the earlier job's
        return mod
    title = str(t.get("name") or "").lower()
    for leaf in carried:
        if _names_module(leaf, title, set(re.findall(r"[a-z0-9]+", title))):
            return leaf
    for leaf in carried:
        if re.search(_EDITS_RE + re.escape(leaf) + r"\b", _text(t), re.I):
            return leaf
    return None


#: §17.1818 — a step that reads the earlier job's code and reports on it
_REVIEW_STEP_RE = re.compile(r"^\s*(?:audit|review|inspect|examine|analy[sz]e|assess|inventory|read)\b", re.I)
_BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*)$")


def review_findings(output: str, carried: list[str]) -> dict[str, list[str]]:
    """§17.1818 — what a review step found in each of the earlier job's modules: every bullet whose subject (the first
    module it names) is one of them and that says something about it; a bare
    list of paths -- "the twelve untested modules" -- is an inventory, not a finding (five words or more beside the
    code; a bare path has none)."""
    found: dict[str, list[str]] = {}
    for line in (output or "").splitlines():
        m = _BULLET_RE.match(line)
        if not m:
            continue
        body = m.group(1).strip()
        hits = sorted(((mm.start(), leaf) for leaf in carried
                       for mm in [re.search(rf"\b{re.escape(leaf)}\b", body)] if mm), key=lambda x: x[0])
        if not hits:
            continue
        prose = re.sub(r"`[^`]*`", " ", body)
        if len(re.findall(r"[A-Za-z]{2,}", prose)) < 5:
            continue
        found.setdefault(hits[0][1], []).append(body)
    return found


def add_review_fixes(tasks: list[dict], reviews: dict[str, str], carried: list[str]) -> list[str]:
    """§17.1818 — each module of the earlier job a review step found defects in, and no step fixes, gets a fix step
    (that module, edited in place, with its tests); the code steps not yet run wait for the fixes. Live (follow-up
    5b4880c0): T1 "Audit existing paper modules" found `paper_trade_proposer` returning proposals with no `signal_id`
    and inserting rejected ones with NULL `signal_id` and quantity 0 -- both refused by the schema -- and nothing in the
    plan fixed it; T6, the daily batch built on it, failed nine attempts on those rows."""
    changes: list[str] = []
    if not carried or not reviews:
        return changes
    owned = {own for t in tasks if (own := _carried_own(t, carried))}
    existing = {t["id"] for t in tasks}
    fixes: list[str] = []
    for key, output in reviews.items():
        n = 0
        for leaf, lines in review_findings(output, carried).items():
            if leaf in owned:
                continue
            n += 1
            fid = f"{key}F{n}"
            while fid in existing:
                n += 1
                fid = f"{key}F{n}"
            existing.add(fid)
            owned.add(leaf)
            tasks.append({"id": fid, "name": f"Fix {leaf} (review {key} findings)", "tool": "CodeGen",
                          "type": "action", "status": "pending", "depends_on": [key],
                          "notes": (f"Python module `{leaf}.py` -- the earlier job's module: edit it in place (the whole "
                                    f"file, its public names kept). Fix what review step {key} found in it:\n"
                                    + "".join(f"- {ln}\n" for ln in lines) + _tests_note(leaf))})
            fixes.append(fid)
            changes.append(f"review_fix_added: {fid} fixes `{leaf}.py` ({len(lines)} finding(s) of {key})")
    if fixes:
        for t in tasks:
            if (t["id"] not in fixes and str(t.get("tool") or "").lower() == "codegen"
                    and t.get("status", "pending") in ("pending", "failed")):
                t["depends_on"] = list(dict.fromkeys([*(t.get("depends_on") or []), *fixes]))
    return changes


def complete_engine_plan(tasks: list[dict], brief: dict | None, carried: list[str] | None = None) -> list[str]:
    """Complete ``tasks`` in place; return what was changed, one line each (the plan's validator warnings).
    ``carried`` (§17.1814): the modules a job this one builds on finished -- a step that fixes one writes it in
    place, and the job's new modules take their prefix."""
    changes: list[str] = []
    engine_leaves = {m.rsplit(".", 1)[-1] for m in module_index() if m.startswith("app.modules.")}
    carried = list(carried or [])
    pre = _carried_prefix(carried) or prefix_for(brief)

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
        own = _carried_own(t, carried)
        if own:                                            # §17.1814 — the earlier job's module, fixed in place
            t["notes"] = (f"Python module `{own}.py` -- the earlier job's module: edit it in place (the whole file, "
                          "its public names kept)." + (" " + str(t["notes"]).strip() if t.get("notes") else ""))
            changes.append(f"carried_module_named: {t.get('id')} edits `{own}.py` in place (the earlier job wrote it)")
            continue
        base = f"{pre}_{_slug(str(t.get('name') or t.get('id') or 'step'))}"
        name, n = base, 2
        while name in taken or name in engine_leaves or name in carried:
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

    # 1b. §17.1799 — a step that wires many modules into several entry points is one step per entry point. Live (job
    #     f00ea97f T25 "Expose as a lab module with entry points for daily batch proposals, approval, execution,
    #     backtesting, dashboard"): 16 attempts over four rounds, each wiring a different subset of ~20 modules and
    #     mostly dropping its tests. Each entry point gets its own module and tests; the step itself re-exports them.
    #     An entry point whose own step was skipped is left out (T24, the dashboard, skipped by the operator).
    skipped = [str(t.get("name") or "").lower() for t in tasks if t.get("status") == "skipped"]
    for t in list(tasks):
        mod = _module_of(t)
        m = _ENTRY_POINTS_RE.search(str(t.get("notes") or "")) if mod else None
        items = [x.strip() for x in re.split(r",\s*(?:and\s+)?|\s+and\s+", m.group(1))] if m else []
        items = [x for x in items if x and len(x.split()) <= 4 and not x.lower().startswith(("with ", "in "))]
        if len(items) < 3:
            continue
        keep = [x for x in items if not any(_stem(x) and _stem(x) in n for n in skipped)]
        left_out = [x for x in items if x not in keep]
        subs: list[dict] = []
        for i, item in enumerate(keep):
            sub_mod = f"{pre}_entry_{_slug(item)}"
            # `E<n>`, never a letter: `<key>D` is §17.1793's data step
            subs.append({"id": f"{t.get('id')}E{i + 1}", "name": f"Entry point: {item}", "tool": "CodeGen",
                         "type": t.get("type") or "action", "depends_on": list(t.get("depends_on") or []),
                         "notes": (f"Python module `{sub_mod}.py`: the lab's `{item}` entry point -- one async "
                                   f"function that runs this one path end to end through the job's own modules (see "
                                   f"\"This job's modules\"), with every input it needs read from those modules or "
                                   f"the job's tables, never a placeholder. Only this path; the other entry points are "
                                   f"their own steps. " + _tests_note(sub_mod))})
        at = tasks.index(t)
        tasks[at:at] = subs
        t["notes"] = (f"Python module `{mod}.py`: the lab's single import point -- re-export the entry function of "
                      + ", ".join(f"`{x['notes'].split('`')[1][:-3]}`" for x in subs)
                      + " and nothing else (no logic of its own). " + _tests_note(mod)
                      + (f" Not exposed: {', '.join(left_out)} (its step was skipped)." if left_out else ""))
        t["depends_on"] = list(t.get("depends_on") or []) + [x["id"] for x in subs]
        changes.append(f"entry_points_split: {t.get('id')} → " + ", ".join(x["id"] for x in subs)
                       + (f" (left out: {', '.join(left_out)})" if left_out else ""))

    # 2. every module is tested in the step that writes it
    for t in tasks:
        mod = _module_of(t)
        if not mod:
            continue
        # §17.1797 — a step planned before the scratch database (§17.1783) still says to mock `async_session` and
        # that tests run "with no network, database or secrets"; live (f00ea97f T25) the verifier refused a real SQL
        # test on that sentence while the engine block said the opposite. The old sentence is replaced.
        notes = str(t.get("notes") or "")
        if _STALE_TESTS_RE.search(notes):
            t["notes"] = _STALE_TESTS_RE.sub(lambda m: _tests_note(m.group(1)), notes)
            changes.append(f"module_tests_reworded: {t.get('id')} tests use the scratch database")
            continue
        if re.search(rf"\btest_{re.escape(mod)}\.py\b", _text(t)):
            continue
        t["notes"] = (notes.rstrip() + " " if notes else "") + _tests_note(mod)
        changes.append(f"module_tests_added: {t.get('id')} writes tests/test_{mod}.py")

    # 3. approval is asked now and answered later: a step specified to return the answer cannot be written
    #    (live T14: `request_approval(proposal) -> bool`; every draft either blocked or returned before any answer)
    for t in tasks:
        # the step's own name says it asks, or it exports the asking function; a step that only mentions the
        # approval flow it is "consumed by" (live 918b6274 T6, T24) does not ask
        asks = (_ASKS_APPROVAL_RE.search(str(t.get("name") or ""))
                or re.search(r"\b(?:request|ask_for|get)_approval\b", _text(t)))
        if _module_of(t) and asks and "operator_ask" not in _text(t):
            # §17.1773 — and the sentence promising the opposite goes (live 918b6274 T8: "Blocks until operator
            # approves or rejects" beside this very note)
            t["notes"] = _BLOCKS_RE.sub(" ", str(t.get("notes") or "")).strip()
            t["notes"] = str(t.get("notes") or "").rstrip() + (
                " The operator's answer arrives later, through `app.modules.operator_ask`: this step opens the "
                "decisions (`open_decision_job`) and acts in an `@answer_handler` when each answer is recorded; no "
                "function can return the answer when it asks, so nothing here returns or waits for one.")
            changes.append(f"approval_made_asynchronous: {t.get('id')} asks through operator_ask")

    # 3b. §17.1773 — a step that requests approval and acts on it in one pass cannot be written either (live 918b6274
    #     T26 "Runs before market open: … request approval, execute approved orders"): it opens the decisions and
    #     returns; what the operator approves is acted on in the answer handler.
    for t in tasks:
        txt = _text(t)
        if (_module_of(t) and "operator_ask" not in txt
                and re.search(r"\brequests?\s+(?:operator\s+)?approval\b|\bexecutes?\s+(?:the\s+)?approved\b", txt, re.I)):
            t["notes"] = _BLOCKS_RE.sub(" ", str(t.get("notes") or "")).strip() + (
                " Approval is asynchronous (`app.modules.operator_ask`): this step opens the decisions and returns; "
                "an approved order is acted on in the `@answer_handler` when the operator's answer is recorded, "
                "never after a wait in the same call.")
            changes.append(f"approval_flow_split: {t.get('id')} acts on approvals in the answer handler")

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
    # 5. §17.1774 — the job's tables exist before any of its code runs against them. Live (job 918b6274's eighth
    #     plan): T5 "Implement risk controls" had no edge to T3 (the revision), ran first, read `paper_positions` /
    #     `paper_orders` and was refused three times (§17.1759) for tables nobody had written yet. Every other code step
    #     stands on the job's revisions (never one the revision itself stands on).
    #    Last, so the steps the passes above made code (and WIRE) stand on it too.
    revisions = [str(t["id"]) for t in tasks if str(t.get("notes") or "").startswith("An Alembic revision `alembic/versions/")]
    if revisions:
        for t in tasks:
            if (str(t.get("tool") or "").lower() != "codegen" or str(t["id"]) in revisions
                    or "alembic/versions/" in _text(t)):
                continue
            missing = [r for r in revisions if r not in (t.get("depends_on") or [])
                       and str(t["id"]) not in _upstream_of(tasks, r)]
            if missing:
                t["depends_on"] = list(dict.fromkeys([*(t.get("depends_on") or []), *missing]))
                changes.append(f"schema_first: {t.get('id')} stands on {', '.join(missing)}")

    # 6. §17.1780 — an outside API a code step calls is researched first, from its own documentation, and the steps
    #    that call it stand on that research. Live (job f00ea97f T7/T8, nine attempts each): with every naming defect
    #    fixed, the drafts still parsed Form 4 / 13F XML as JSON, called a made-up `…/submissions.json`, read
    #    `formType` where the SEC's key is `form`, and T18 read Alpaca fill fields from the wrong level -- facts the
    #    model did not have, which no refusal could teach. The research step is the engine's own web research (§17.1039:
    #    pages fetched, ranked, dated, the output checked against them).
    ids = {str(t.get("id")) for t in tasks}
    for api in _APIS_NAMED(tasks):
        key = f"API_{api['key']}"
        callers = [t for t in tasks if str(t.get("tool") or "").lower() == "codegen" and api["re"].search(_text(t))
                   and not str(t.get("notes") or "").startswith("An Alembic revision")]
        if not callers:
            continue
        # one hop: a code step standing on a caller consumes what the API returns (live f00ea97f T18, the fill
        # tracker over T5's Alpaca stream, read the fill fields from the wrong level and never named Alpaca)
        # -- only over a caller that IS the API's client or stream (not every step downstream of an ingester)
        direct = {str(c["id"]) for c in callers
                  if re.search(r"\b(?:client|stream|websocket|feed|api)\b", str(c.get("name") or ""), re.I)}
        callers += [t for t in tasks if str(t.get("tool") or "").lower() == "codegen" and t not in callers
                    and direct & {str(d) for d in (t.get("depends_on") or [])}
                    and not str(t.get("notes") or "").startswith("An Alembic revision")
                    and not str(t.get("id", "")).startswith("WIRE")]
        if key not in ids:
            tasks.append({
                "id": key, "name": f"Research the {api['name']} as its documentation states it", "type": "research",
                "tool": "SearXNG", "depends_on": [], "inputs": [], "outputs": [],
                # §17.1781 — its official documentation, read first (measured: each URL loads from the engine and
                # carries what the code steps guessed -- Alpaca's trade_updates fields, the SEC's User-Agent rule)
                **({"tool_config": {"urls": list(api["docs"])}} if api.get("docs") else {}),
                "notes": (f"Read the {api['name']}'s own documentation ({api['scope']}; official pages first) and state, "
                          "for what this lab calls: the base URLs; each endpoint (method, path, parameters); the headers "
                          "it requires (authentication, User-Agent / contact rules); the response FORMAT (JSON or XML) "
                          "and its exact field names and nesting, with a short real example; pagination; rate limits and "
                          "terms of use. Quote the documentation for each point and cite its URL; state nothing a "
                          "source does not say."),
            })
            ids.add(key)
            changes.append(f"api_research_added: {key} ({api['name']}) for {', '.join(str(c['id']) for c in callers)}")
        else:
            have = next(t for t in tasks if str(t.get("id")) == key)
            if api.get("docs") and not ((have.get("tool_config") or {}).get("urls")):
                have["tool_config"] = {**(have.get("tool_config") or {}), "urls": list(api["docs"])}
                changes.append(f"api_research_docs: {key} reads {len(api['docs'])} official pages")
        for c in callers:
            if key not in (c.get("depends_on") or []):
                c["depends_on"] = [*(c.get("depends_on") or []), key]
                changes.append(f"api_research_first: {c.get('id')} stands on {key}")

    # the operator's credentials come after the code that reads them, wired (0e)
    wires = [str(t["id"]) for t in tasks if str(t.get("id", "")).startswith("WIRE")]
    for t in secret_steps:
        want = wires or [str(m["id"]) for m in modules]
        t["depends_on"] = list(dict.fromkeys([*(t.get("depends_on") or []), *want]))
    return changes


#: §17.1780 — outside services a lab calls, by what a step's text says; plus any "<Name> API" a step names
_KNOWN_APIS = (
    {"key": "SEC_EDGAR", "name": "SEC EDGAR APIs", "re": re.compile(r"\bedgar\b|\bform\s*4\b|\b13f\b|\bsec\s+filings?\b", re.I),
     "docs": ["https://www.sec.gov/search-filings/edgar-application-programming-interfaces", "https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data", "https://www.sec.gov/divisions/investment/13ffaq"],
     "scope": "data.sec.gov submissions JSON, the Form 4 and 13F-HR filing documents, the fair-access User-Agent rule"},
    {"key": "ALPACA", "name": "Alpaca paper trading API", "re": re.compile(r"\balpaca\b", re.I),
     "docs": ["https://docs.alpaca.markets/docs/paper-trading", "https://docs.alpaca.markets/docs/authentication", "https://docs.alpaca.markets/docs/websocket-streaming"],
     "scope": "the paper REST API (account, orders, positions) and the trade_updates websocket stream"},
    {"key": "REDDIT", "name": "Reddit API", "re": re.compile(r"\breddit\b", re.I),
     "docs": ["https://github.com/reddit-archive/reddit/wiki/OAuth2", "https://support.reddithelp.com/hc/en-us/articles/16160319875092-Reddit-Data-API-Wiki"],
     "scope": "OAuth app-only access, listing endpoints, User-Agent and rate-limit rules, personal-use terms"},
    {"key": "FINRA", "name": "FINRA data APIs", "re": re.compile(r"\bfinra\b", re.I),
     "docs": ["https://developer.finra.org/docs"],
     "scope": "short interest and ATS / OTC transparency data: where it is published, its format and fields"},
    {"key": "CONGRESS_TRADES", "name": "Congressional trade disclosures",
     "re": re.compile(r"\bcongress(?:ional)?\b|\bsenate\b.{0,30}\bdisclos|\bhouse\b.{0,30}\bdisclos", re.I),
     "docs": ["https://disclosures-clerk.house.gov/FinancialDisclosure", "https://efdsearch.senate.gov/search/home/"],
     "scope": "the official House and Senate periodic transaction report sources: URLs, formats, fields"},
)
#: only a name introduced AS an API ("via the Polygon API", "the Tiingo REST API"); a bare "<Word> API" also matched
#: sentence-initial verbs (live f00ea97f T5: "Reads API keys…" → a "Reads API")
_GENERIC_API_RE = re.compile(r"\b(?:the|via|through|using|from|with|calls?|queries)\s+(?:the\s+)?([A-Z][A-Za-z0-9.]{2,})"
                             r"\s+(?:REST\s+|public\s+|web\s+|HTTP\s+)?API\b")
#: the engine's own services are not outside APIs (shown by §17.1749 / §17.1754 instead)
_ENGINE_SERVICES = {"searxng", "ollama", "scaffold", "engine", "openai", "the", "this", "rest", "public"}


def _APIS_NAMED(tasks: list[dict]) -> list[dict]:
    code = " ".join(_text(t) for t in tasks if str(t.get("tool") or "").lower() == "codegen")
    found = [a for a in _KNOWN_APIS if a["re"].search(code)]
    known = " ".join(a["name"].lower() for a in found)
    for m in _GENERIC_API_RE.finditer(code):
        name = m.group(1)
        if name.lower() in _ENGINE_SERVICES or name.lower() in known:
            continue
        key = re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_")
        if not any(a["key"] == key for a in found):
            found.append({"key": key, "name": f"{name} API", "re": re.compile(rf"\b{re.escape(name)}\b"),
                          "scope": "the endpoints and data this lab uses"})
            known += " " + name.lower()
    return found


def _upstream_of(tasks: list[dict], key: str) -> set[str]:
    """Every task ``key`` stands on, transitively."""
    deps = {str(t["id"]): [str(d) for d in (t.get("depends_on") or [])] for t in tasks}
    seen: set[str] = set()
    todo = list(deps.get(key, []))
    while todo:
        k = todo.pop()
        if k not in seen:
            seen.add(k)
            todo.extend(deps.get(k, []))
    return seen


def _drop(tasks: list[dict], gone: dict) -> None:
    """Remove ``gone``; each task that depended on it depends on what it depended on."""
    tasks.remove(gone)
    for u in tasks:
        if gone["id"] in (u.get("depends_on") or []):
            u["depends_on"] = list(dict.fromkeys(
                [d for d in u["depends_on"] if d != gone["id"]] + list(gone.get("depends_on") or [])))


# ── §17.1780 applying completion to a plan that has already started ─────────────────────────────────────────────

_TYPE_OF_NODE = {"decision": "decision", "checkpoint": "validation"}


async def apply_to_job(db, job_id: str, *, edited_by: str | None = None, only_reviews: bool = False) -> dict:
    """Run `complete_engine_plan` over a job's stored plan and apply what it adds to the steps that have not passed:
    new steps are inserted, and a pending or failed step takes its new text and dependencies; a finished step is
    never touched. Live (job f00ea97f): §17.1780 arrived with twelve steps done -- re-planning would have thrown
    them away. Returns the changes and what was applied."""
    import json as _json

    from sqlalchemy import text as _text_sql

    from app.modules import engine_source, node_editor
    job = (await db.execute(_text_sql("SELECT refined_brief, input_text FROM jobs WHERE id = :j"),
                            {"j": job_id})).mappings().first()
    if not job:
        return {"error": f"job {job_id} not found", "http_status": 404}
    brief = job["refined_brief"]
    brief = brief if isinstance(brief, dict) else (_json.loads(brief) if brief else {})
    # §17.1813 — a job refined before the stamp: its operator's words decide, and the stamp is kept on the job
    stamped = engine_source.brief_for_the_engine(brief, job.get("input_text"))
    if stamped.get("targets_engine") and not brief.get("targets_engine"):
        await db.execute(_text_sql(
            "UPDATE jobs SET refined_brief = jsonb_set(COALESCE(refined_brief, '{}'::jsonb), '{targets_engine}', "
            "'true'::jsonb) WHERE id = :j"), {"j": job_id})
        await db.commit()
    brief = stamped
    if not engine_source.targets_the_engine(brief):
        return {"error": "this job does not write code for the engine", "http_status": 409}
    rows = (await db.execute(_text_sql(
        "SELECT node_key, title, prompt_template, tool, node_type, status, depends_on, tool_config FROM dag_nodes "
        "WHERE job_id = :j ORDER BY execution_order"), {"j": job_id})).mappings().all()
    status = {r["node_key"]: r["status"] for r in rows}
    tasks = [{"id": r["node_key"], "name": r["title"], "notes": r["prompt_template"] or "", "tool": r["tool"],
              "status": r["status"],                                              # §17.1799 skipped entry points
              "type": _TYPE_OF_NODE.get(r["node_type"] or "", "action"), "depends_on": list(r["depends_on"] or []),
              **({"tool_config": (r["tool_config"] if isinstance(r["tool_config"], dict)
                                  else _json.loads(r["tool_config"]))} if r["tool_config"] else {})}
             for r in rows]
    before = {t["id"]: dict(t) for t in tasks}
    carried = await engine_source.carried_modules(db, job_id)
    changes = [] if only_reviews else complete_engine_plan(tasks, brief, carried)                   # §17.1814
    if carried:                                                                                     # §17.1818
        reviews = {r[0]: r[1] for r in (await db.execute(_text_sql(
            "SELECT node_key, output_text FROM dag_nodes WHERE job_id = :j AND status = 'done' "
            "AND lower(tool) = 'llm' AND output_text IS NOT NULL ORDER BY execution_order"), {"j": job_id})).all()
            if _REVIEW_STEP_RE.match(next((t["name"] for t in tasks if t["id"] == r[0]), "") or "")}
        changes += add_review_fixes(tasks, reviews, carried)
    inserted, edited, kept = [], [], []
    for t in tasks:                                       # new steps first: an edit may depend on one
        if t["id"] in before:
            continue
        out = await node_editor.insert_node(job_id, {
            "node_key": t["id"], "title": t["name"], "tool": t.get("tool") or "LLM",
            "node_type": {"decision": "decision", "validation": "checkpoint"}.get(str(t.get("type")), "task"),
            "depends_on": [d for d in t.get("depends_on") or [] if d in status or d in {x["id"] for x in tasks}],
            "prompt_template": t.get("notes") or "",
            **({"tool_config": t["tool_config"]} if t.get("tool_config") is not None else {})}, edited_by=edited_by, db=db)
        if out.get("error"):
            return {**out, "changes": changes, "inserted": inserted}
        inserted.append(t["id"])
        status[t["id"]] = "pending"
    for t in tasks:
        b = before.get(t["id"])
        if not b:
            continue
        fields = {}
        # §17.1818b — the title too: live (5b4880c0 T10), the first pass made "Run end-to-end paper trade" the operator's
        # "Are the … keys stored …?" step but kept the old title, so a second pass read "Run …" as runtime work and made
        # it a CodeGen module -- re-applying plan completion was not a no-op
        if (t.get("name") or "") != (b.get("name") or "") and t.get("name"):
            fields["title"] = t.get("name")
        if (t.get("notes") or "") != (b.get("notes") or ""):
            fields["prompt_template"] = t.get("notes") or ""
        if list(t.get("depends_on") or []) != list(b.get("depends_on") or []):
            fields["depends_on"] = list(t.get("depends_on") or [])
        if (t.get("tool") or "") != (b.get("tool") or ""):
            fields["tool"] = t.get("tool")
        if t.get("tool_config") != b.get("tool_config"):
            fields["tool_config"] = t.get("tool_config")
        if not fields:
            continue
        if status.get(t["id"]) not in ("pending", "failed"):
            kept.append(t["id"])
            continue
        out = await node_editor.edit_node(job_id, t["id"], fields, edited_by=edited_by, db=db)
        if out.get("error"):
            return {**out, "changes": changes, "inserted": inserted, "edited": edited}
        edited.append(t["id"])
    return {"changes": changes, "inserted": inserted, "edited": edited, "kept_finished": kept}


# ── §17.1831 a step whose tests fail inside an earlier module gets that module fixed first ──────────────────

_GAP_KEYS = ("producer", "consumer", "param", "missing", "carries")


def _callers(current: dict[str, str], mod: str) -> list[str]:
    """The finished modules that import `mod`, each with what it imports: "`paper_x` (`f`, `g`)"."""
    import ast as _ast
    out: list[str] = []
    for m, code in sorted(current.items()):
        try:
            tree = _ast.parse(code)
        except SyntaxError:
            continue
        names = sorted({a.name for n in tree.body if isinstance(n, _ast.ImportFrom) and n.module == f"app.modules.{mod}"
                        for a in n.names})
        if names:
            out.append(f"`{m}` ({', '.join(f'`{x}`' for x in names)})")
    return out


def _contract_prompt(mod: str, owner: str, found_by: str, gaps: list[dict], callers: list[str]) -> str:
    """§17.1833 — the text of a fix step for a module whose dicts lack a key the function they are handed to requires.

    §17.1835 — and the item is saved ONCE. Live (5b4880c0 T6U6, 2026-10-26): told to "save the item ... and carry the
    id", the generator saved a new `signal` row for every item on every call -- and the walk-forward backtester calls
    it once per window, `paper_entry_approval` once per run. The text never said an item already saved keeps its row,
    nor that other modules call the function. The operator's rule: save once per item; every caller reuses the row."""
    lines = "".join(
        f"- `{g['consumer']}` requires each item of its `{g['param']}` to carry "
        + " and ".join(" or ".join(f"`{k}`" for k in m) for m in g["missing"])
        + f" (it skips or fails an item without); step {found_by} hands it what `{g['producer']}` returns, and "
        f"every dict this module builds for that carries only {', '.join(f'`{k}`' for k in g['carries'])}.\n"
        for g in gaps)
    return (f"Python module `{mod}.py` -- step {owner}'s module: edit it in place (the whole file, its public names "
            f"kept). What it returns is handed on to a function that cannot use it:\n{lines}Make every dict it returns "
            "carry the key, from where the value really comes from: an id names a saved row -- save the item with the "
            "finished module that writes that table and carry the id it returns, with whatever that write needs; never "
            "a made-up value. A value no finished module's function returns (the module listings say what each returns) "
            "is read in this module with `async_session` + `text` -- never assumed to be in what a function returns. "
            "Save each item ONCE: an item already saved keeps its row and its id (look it up before "
            "writing), so calling this again -- the next run, a backtest -- writes no second row. "
            + (f"Other finished modules call it and get the same: {', '.join(callers)} -- each must keep working. "
               if callers else "")
            + "Its tests include one that calls it twice over the same items and finds one row per item. "
            + _tests_note(mod))


_CONTRACT_HEAD_RE = re.compile(r"^Python module `(?P<mod>\w+)\.py` -- step (?P<owner>[\w:]+)'s module: edit it in "
                               r"place \(the whole file, its public names kept\)\. What it returns is handed on")
_CONTRACT_LINE_RE = re.compile(r"^- `(?P<consumer>[\w.]+)` requires each item of its `(?P<param>\w+)` to carry "
                               r"(?P<alts>.+?) \(it skips or fails an item without\); step (?P<step>[\w:]+) hands it "
                               r"what `(?P<producer>[\w.]+)` returns, and every dict this module builds for that "
                               r"carries only (?P<carries>.+?)\.$", re.M)


async def refresh_contract_fix(db, job_id: str, node_key: str, asked_by: str) -> dict | None:
    """§17.1835 — asked again (reset) for a contract fix step: its text re-rendered from the current template and the
    module's current callers, so a corrected template reaches a fix step made before it. None when the step is not
    one; the gaps come from its `tool_config`, or -- a step made before they were kept there -- from its own text."""
    from sqlalchemy import text as _t

    from app.modules import engine_source, node_editor
    row = (await db.execute(_t("SELECT prompt_template, tool_config FROM dag_nodes WHERE job_id = :j AND node_key = :k"),
                            {"j": job_id, "k": node_key})).mappings().first()
    cfg = (row["tool_config"] if row and isinstance(row["tool_config"], dict) else {}) or {}
    if not cfg.get("contract"):
        return None
    prompt = str(row["prompt_template"] or "")
    head = _CONTRACT_HEAD_RE.match(prompt)
    gaps = cfg.get("gaps") or [
        {"producer": m["producer"], "consumer": m["consumer"], "param": m["param"],
         "missing": [[k.strip(" `") for k in alt.split(" or ")] for alt in m["alts"].split(" and ")],
         "carries": [k.strip(" `") for k in m["carries"].split(",")]}
        for m in _CONTRACT_LINE_RE.finditer(prompt)]
    mod, owner = cfg.get("fixes_module") or (head and head["mod"]), cfg.get("owner") or (head and head["owner"])
    if not (gaps and mod and owner and cfg.get("found_by")):
        return None
    rows = [r for r in await engine_source.code_rows(db, job_id) if r.get("node_key") != node_key]
    current = {m: c for m, (_p, c, _k) in engine_source.current_sources(rows).items()}
    new = _contract_prompt(str(mod), str(owner), str(cfg["found_by"]), gaps, _callers(current, str(mod)))
    if new == prompt:
        return {"node_key": node_key, "refreshed": False}
    out = await node_editor.edit_node(job_id, node_key, {"prompt_template": new, "tool_config": {
        **cfg, "owner": owner, "gaps": gaps}}, edited_by=asked_by, db=db)
    if out.get("error"):
        logger.warning("contract_refresh_failed job=%s node=%s err=%s", job_id, node_key, out)
        return None
    return {"node_key": node_key, "refreshed": True}


async def fix_upstream_defect(db, job_id: str, node_key: str, *, edited_by: str = "engine §17.1831") -> dict | None:
    """When a code step written for the engine has used its attempts and its sandbox refusals fail INSIDE another
    finished module -- a fault of that module (`engine_tests.CALLEE_CAUSES`), at its deepest frame -- the module gets a
    fix step (`<step>U<n>`: that module edited in place, with its tests, told the frame and the cause), and the step
    waits for it and starts again. A defect in an earlier module is fixed there, not worked around or retried
    forever: live (follow-up 5b4880c0 T6), f00ea97f:T4's `write_risk_check` bound a dict to a JSONB column and every
    attempt of the batch failed in it. §17.1833 — and when its code hands one finished module's dicts to another that
    requires a key none of them can carry (`engine_contracts.contract_gaps`), the module that builds those dicts gets
    the fix step, once per gap. None when nothing points at another module."""
    import json as _json

    from sqlalchemy import text as _t

    from app.modules import engine_source, node_editor
    from app.modules.engine_contracts import contract_gaps, payload_gaps
    from app.sandbox.engine_tests import callee_defects, files_of
    row = (await db.execute(_t(
        "SELECT n.id, n.title, n.prompt_template, n.tool, n.depends_on, n.output_text, j.refined_brief "
        "FROM dag_nodes n JOIN jobs j ON j.id = n.job_id WHERE n.job_id = :j AND n.node_key = :k"),
        {"j": job_id, "k": node_key})).mappings().first()
    if not row or str(row["tool"] or "").lower() != "codegen":
        return None
    brief = row["refined_brief"] if isinstance(row["refined_brief"], dict) else _json.loads(row["refined_brief"] or "{}")
    if not engine_source.targets_the_engine(brief):
        return None
    # §17.1832 — only the refusals since its last fix step finished: those before it are the defect already fixed.
    # Live (5b4880c0 T6, 2026-10-26): T6U1 fixed `write_risk_check`'s JSON bind; T6's next exhaustion re-read the
    # refusals from before it and added T6U2, then T6U3, T6U4 and T6U5 -- the same fixed frame, five times.
    reasons = (await db.execute(_t(
        "SELECT details->>'reason' FROM execution_logs WHERE job_id = :j AND node_id = :n AND message LIKE :f "
        "AND created_at > COALESCE((SELECT max(l.created_at) FROM execution_logs l JOIN dag_nodes u ON u.id = l.node_id "
        " WHERE u.job_id = :j AND u.tool_config->>'found_by' = :k AND l.message LIKE '%-> done'), '-infinity')"),
        {"j": job_id, "n": row["id"], "k": node_key, "f": "%-> failed"})).scalars().all()
    own = {engine_source.step_module({"prompt_template": row["prompt_template"], "title": row["title"]})} | \
        engine_source._written_modules(str(row["output_text"] or ""))
    owner: dict[str, str] = {}
    rows = await engine_source.code_rows(db, job_id)
    for r in rows:                                               # a finished module and the step that wrote it last
        if r.get("status") == "done" and r.get("node_key") != node_key:
            for m in engine_source._written_modules(str(r.get("output_text") or "")):
                owner[m] = str(r["node_key"])
    found: dict[str, list[dict]] = {}
    for reason in reasons:
        for d in callee_defects(reason or ""):
            if d["module"] in owner and d["module"] not in own and d not in found.get(d["module"], []):
                found.setdefault(d["module"], []).append(d)
    existing_rows = (await db.execute(_t("SELECT node_key, tool_config FROM dag_nodes WHERE job_id = :j"),
                                      {"j": job_id})).mappings().all()
    existing = {r["node_key"] for r in existing_rows}
    # §17.1833 — and the dicts its code hands one finished module from another that cannot carry a key the second
    # requires: fixed where those dicts are built, once per gap
    asked = {str((r["tool_config"] or {}).get("contract")) for r in existing_rows if isinstance(r["tool_config"], dict)}
    gaps: dict[str, list[dict]] = {}
    current = {m: c for m, (_p, c, k) in engine_source.current_sources(
        [r for r in rows if r.get("node_key") != node_key]).items()}
    for path, code in files_of({**dict(row), "node_key": node_key}).items():
        if not path.startswith("app/"):
            continue
        for g in contract_gaps(code, current):
            target = g["origin"][0] if len(g["origin"]) == 1 else g["producer"].split(".")[0]
            g["key"] = f"{g['consumer']}.{g['param']}:{'|'.join('/'.join(m) for m in g['missing'])}"
            if target in owner and target not in own and g["key"] not in asked:
                gaps.setdefault(target, []).append(g)
    # §17.1840 — and the kinds of item one finished module writes into a JSON column that another can never take a
    # field it requires from: the writers record it, then the reader reads it
    pgaps = [g for g in payload_gaps(current)
             if not any(a.startswith(f"payload:{g['table']}.{g['column']}:{g['by']}={g['kind']}:{g['field']}:")
                        for a in asked)
             and all(w in owner and w not in own for w in g["writers"])
             and g["reader"].split(".")[0] in owner and g["reader"].split(".")[0] not in own]
    if not found and not gaps and not pgaps:
        return None
    added: list[str] = []

    def _next_key() -> str:
        n = 1
        while f"{node_key}U{n}" in existing:
            n += 1
        existing.add(f"{node_key}U{n}")
        return f"{node_key}U{n}"

    for g in pgaps:
        contract = f"payload:{g['table']}.{g['column']}:{g['by']}={g['kind']}:{g['field']}"
        key = g["field"] if g["field"] in g["reads"] or not g["reads"] else g["reads"][0]
        rmod, rfn = g["reader"].split(".")
        took = (f"from {', '.join(f'`{k}`' for k in g['reads'])}" if g["reads"] else "from nothing (it gives every one "
                "an empty one)")
        wsteps: list[str] = []
        for wmod, wkeys in sorted(g["writers"].items()):
            fid = _next_key()
            out = await node_editor.insert_node(job_id, {
                "node_key": fid, "title": f"Fix {wmod} (its {g['kind']} items lack {key})", "tool": "CodeGen",
                "node_type": "task", "depends_on": [],
                "tool_config": {"fixes_module": wmod, "found_by": node_key, "contract": f"{contract}:{wmod}",
                                "owner": owner[wmod]},
                "prompt_template": (
                    f"Python module `{wmod}.py` -- step {owner[wmod]}'s module: edit it in place (the whole file, its "
                    f"public names kept). The `{g['kind']}` items it writes into `{g['table']}.{g['column']}` carry "
                    f"{', '.join(f'`{k}`' for k in wkeys)} -- and `{g['reader']}` ({rmod}) takes an item's "
                    f"`{g['field']}` {took}, so every one of them is dropped where `{g['field']}` is required. Record "
                    f"`{key}` in each item's {g['column']}, read from the item's own data (the document or record it "
                    "came from) -- never made up; an item that truly has none gets none. Keep every key it writes "
                    "now. " + _tests_note(wmod)),
            }, edited_by=edited_by, db=db)
            if out.get("error"):
                logger.warning("upstream_fix_insert_failed job=%s node=%s err=%s", job_id, node_key, out)
                return None
            wsteps.append(fid)
            added.append(fid)
        if key not in g["reads"]:
            fid = _next_key()
            out = await node_editor.insert_node(job_id, {
                "node_key": fid, "title": f"Fix {rmod} (read {g['kind']} items' {key})", "tool": "CodeGen",
                "node_type": "task", "depends_on": wsteps,
                "tool_config": {"fixes_module": rmod, "found_by": node_key, "contract": f"{contract}:{rmod}",
                                "owner": owner[rmod]},
                "prompt_template": (
                    f"Python module `{rmod}.py` -- step {owner[rmod]}'s module: edit it in place (the whole file, its "
                    f"public names kept). `{rfn}` takes a `{g['kind']}` item's `{g['field']}` {took}; its writers "
                    f"({', '.join(f'`{w}`' for w in sorted(g['writers']))}) now record it in the item's "
                    f"`{g['column']}` under `{key}` -- read it from there for `{g['kind']}` items, and keep every "
                    "other kind's rule. " + _tests_note(rmod)),
            }, edited_by=edited_by, db=db)
            if out.get("error"):
                logger.warning("upstream_fix_insert_failed job=%s node=%s err=%s", job_id, node_key, out)
                return None
            added.append(fid)
    for mod, gs in gaps.items():
        fid = _next_key()
        kept = [{k: g[k] for k in _GAP_KEYS} for g in gs]
        out = await node_editor.insert_node(job_id, {
            "node_key": fid, "title": f"Fix {mod} (what {node_key} hands on lacks a key)", "tool": "CodeGen",
            "node_type": "task", "depends_on": [],
            "tool_config": {"fixes_module": mod, "found_by": node_key, "contract": gs[0]["key"], "owner": owner[mod],
                            "gaps": kept},
            "prompt_template": _contract_prompt(mod, owner[mod], node_key, kept, _callers(current, mod)),
        }, edited_by=edited_by, db=db)
        if out.get("error"):
            logger.warning("upstream_fix_insert_failed job=%s node=%s err=%s", job_id, node_key, out)
            return None
        added.append(fid)
    for mod, defects in found.items():
        fid = _next_key()
        by_frame: dict[tuple[str, str], list[str]] = {}       # §17.1832 — one line per frame, its tests named once
        for d in defects:
            by_frame.setdefault((d["frame"], d["cause"]), []).append(d["test"])
        frames = "".join(f"- `{f}` (tests {', '.join(f'`{t}`' for t in tests[:4])}"
                         f"{f' and {len(tests) - 4} more' if len(tests) > 4 else ''}): {c}\n"
                         for (f, c), tests in list(by_frame.items())[:5])
        out = await node_editor.insert_node(job_id, {
            "node_key": fid, "title": f"Fix {mod} (found by {node_key}'s tests)", "tool": "CodeGen",
            "node_type": "task", "depends_on": [],
            "tool_config": {"fixes_module": mod, "found_by": node_key},
            "prompt_template": (
                f"Python module `{mod}.py` -- step {owner[mod]}'s module: edit it in place (the whole file, its public "
                f"names kept). Step {node_key}'s tests failed inside it, at its own code:\n{frames}Fix it there, in "
                f"every function of the module with the same fault. " + _tests_note(mod)),
        }, edited_by=edited_by, db=db)
        if out.get("error"):
            logger.warning("upstream_fix_insert_failed job=%s node=%s err=%s", job_id, node_key, out)
            return None
        added.append(fid)
    out2 = await node_editor.edit_node(job_id, node_key, {
        "depends_on": list(dict.fromkeys([*list(row["depends_on"] or []), *added]))}, edited_by=edited_by, db=db)
    if out2.get("error"):
        logger.warning("upstream_fix_edit_failed job=%s node=%s err=%s", job_id, node_key, out2)
        return None
    mods = sorted({*found, *gaps, *(w for g in pgaps for w in g["writers"]),
                   *(g["reader"].split(".")[0] for g in pgaps if (g["field"] if g["field"] in g["reads"]
                                                                 or not g["reads"] else g["reads"][0])
                     not in g["reads"])})
    logger.warning("engine_upstream_fix job=%s node=%s fixes=%s modules=%s", job_id, node_key, added, mods)
    return {"node_key": node_key, "fix_steps": added, "modules": mods}


# ── §17.1793 a step too big for one output is split ──────────────────────────────────────────────────────────

#: a refusal that says the step left out a part of its OWN task
#: §17.1795 — never "is missing required argument(s)" (a call-shape refusal, §17.1756): live (f00ea97f T25, the
#: integration step) its signature refusals read as omissions and split a wiring step into a "data" half
_OMISSION_RE = re.compile(r"\b(?:entirely absent|is absent|are absent|was not written|were not written|not written at "
                          r"all|never (?:written|implemented|rendered)|missing the (?:explicitly )?required [^.]{0,60}?\b(?:export|"
                          r"deliverable|tests?|file)|required (?:export|deliverable|tests?)[^.]{0,80}\babsent)\b", re.I)


def _entry_prompt(own: str, data_mod: str, data_key: str, task: str) -> str:
    """The text of a split step's entry half.

    §17.1828 — it used to say "import every query and data function from the data module; never re-implement them
    here", absolutely. Live (follow-up 5b4880c0 T6, 2026-10-26): the data step was finished, the task needed proposals
    written "with model and data timestamps" and the only writer (an earlier job's ``write_proposal``) takes neither;
    the verifier failed the attempts that left them out, the name check refused the one that passed them to
    ``write_proposal``, and the text forbade the query the name check's own advice names. What the data module HAS
    is imported; what it does not have is written here.

    §17.1829 — its tests used to "mock the data module's functions". Live (same step, next run): it PASSED reading
    ``context["equity"]``, a key ``load_batch_context`` never returns, and ``signal["id"]``, a key no signal has --
    in production it proposes nothing -- because a mocked data module returns whatever the test says. The split's
    boundary is exactly the contract the tests must cross, so they run through the real data functions against the
    scratch database the sandbox provides (§17.1783).

    §17.1834 — model calls are mocked too. Live (same step, tenth run): `propose_trades` writes its rationale with a
    model; the text allowed mocking only the broker, network and clock, so attempts mocked `propose_trades` -- and the
    data functions with it -- wholesale, and the verifier refused every one."""
    return (
        f"Python module `{own}.py`: the part of this task that USES the data -- rendering / the exported entry "
        f"points. Import every query and data function `{data_mod}.py` (step {data_key}) has; never re-implement "
        f"those here. A query or write the task needs that neither it nor another finished module offers is "
        f"written in this module with `async_session` + `text` -- never by passing a function parameters it does "
        f"not take. The task: {task} In this same step also write `tests/test_{own}.py`: pytest tests of the "
        f"rendering / entry points that run through the real `{data_mod}` functions against the scratch database "
        f"(insert the rows they read, call the entry point, check what it returns and the rows it writes); mock only "
        f"what reaches outside the engine -- the broker, the network, model calls, the clock.")


_ENTRY_RE = re.compile(r"^Python module `(?P<own>[\w]+)\.py`: the part of this task that USES the data\b.*?"
                       r"`(?P<data>[\w]+)\.py` \(step (?P<key>[\w:]+)\).*?The task: (?P<task>.*?) In this same step "
                       r"also write `tests/", re.S)


async def _refresh_split_entry(db, job_id: str, node_key: str, prompt: str, asked_by: str) -> dict | None:
    """§17.1828 — asked again for a step that is already split: its entry text re-rendered from the current
    template (the data step and the task kept), so a corrected template reaches a split made before it."""
    from app.modules import node_editor
    m = _ENTRY_RE.match(prompt.strip())
    if not m:
        return None
    new = _entry_prompt(m["own"], m["data"], m["key"], m["task"].strip())
    if new == prompt.strip():
        return {"node_key": node_key, "data_step": m["key"], "data_module": m["data"], "refreshed": False}
    out = await node_editor.edit_node(job_id, node_key, {"prompt_template": new}, edited_by=asked_by, db=db)
    if out.get("error"):
        logger.warning("split_refresh_failed job=%s node=%s err=%s", job_id, node_key, out)
        return None
    return {"node_key": node_key, "data_step": m["key"], "data_module": m["data"], "refreshed": True}


async def split_oversized_step(db, job_id: str, node_key: str, *, min_omissions: int = 2,
                               asked_by: str | None = None) -> dict | None:
    """When a code step written for the engine has used its attempts and its refusals keep saying it LEFT OUT part of
    its own task, split it: a new data step (`<module>_data.py`: every query and data function the task needs) and the
    step itself as the part that uses them (rendering / entry point), standing on the data step. Live (job f00ea97f T24
    "Implement operator dashboard", 23 attempts): six queries, an HTML/CLI view and tests in one output -- every draw
    dropped `render_dashboard()` or the tests ("entirely absent"). Returns the split, or None when it does not apply
    (not engine code, already split, too few omissions). The step keeps its key and its dependents; edit_node resets it."""
    import json as _json

    from sqlalchemy import text as _t

    from app.modules import engine_source, node_editor
    row = (await db.execute(_t(
        "SELECT n.id, n.title, n.prompt_template, n.tool, n.tool_config, n.depends_on, j.refined_brief "
        "FROM dag_nodes n JOIN jobs j ON j.id = n.job_id WHERE n.job_id = :j AND n.node_key = :k"),
        {"j": job_id, "k": node_key})).mappings().first()
    if not row or str(row["tool"] or "").lower() != "codegen":
        return None
    brief = row["refined_brief"] if isinstance(row["refined_brief"], dict) else _json.loads(row["refined_brief"] or "{}")
    cfg = row["tool_config"] if isinstance(row["tool_config"], dict) else _json.loads(row["tool_config"] or "{}")
    if cfg.get("split") and asked_by and engine_source.targets_the_engine(brief):
        return await _refresh_split_entry(db, job_id, node_key, str(row["prompt_template"] or ""), asked_by)
    if not engine_source.targets_the_engine(brief) or cfg.get("split") or cfg.get("split_from"):
        return None
    reasons = (await db.execute(_t(
        "SELECT details->>'reason' FROM execution_logs WHERE job_id = :j AND node_id = :n AND message LIKE :f"),
        {"j": job_id, "n": row["id"], "f": "%-> failed"})).scalars().all()
    omissions = sum(1 for r in reasons if r and _OMISSION_RE.search(r))
    # §17.1821 — the operator may ask for the split whatever the refusals say. Live (follow-up 5b4880c0 T6, the daily
    # batch over six freshly fixed modules): ~15 attempts, each failing differently (an old parameter name, a patched
    # function that does not exist, a dict into a JSON column, wrong counts) -- never "left out", so the engine's own
    # trigger never fired; the operator chose "split T6 into smaller steps".
    if omissions < min_omissions and not asked_by:
        return None
    own = engine_source.step_module({"prompt_template": row["prompt_template"], "title": row["title"]})
    if not own:
        return None
    data_mod, data_key = f"{own}_data", f"{node_key}D"
    task = re.sub(r"\s*In this same step also write `tests/[^`]+`:.*?(?=(?: The operator|$))", " ",
                  str(row["prompt_template"] or ""), flags=re.S).strip()
    task = re.sub(r"^Python module `[^`]+`\.\s*", "", task)
    out = await node_editor.insert_node(job_id, {
        "node_key": data_key, "title": f"{row['title']} -- data", "tool": "CodeGen", "node_type": "task",
        "depends_on": list(row["depends_on"] or []),
        "tool_config": {"split_from": node_key},
        "prompt_template": (
            f"Python module `{data_mod}.py`: the DATA part of this task -- every query and data function it needs, "
            f"returning plain values; nothing that renders, formats or presents. The task: {task} In this same step "
            f"also write `tests/test_{data_mod}.py`: pytest tests of each function against the scratch database "
            "(insert the rows, call the function, check the result)."),
    }, edited_by=asked_by or "engine §17.1793", db=db)
    if out.get("error"):
        logger.warning("split_insert_failed job=%s node=%s err=%s", job_id, node_key, out)
        return None
    out2 = await node_editor.edit_node(job_id, node_key, {
        "depends_on": [*list(row["depends_on"] or []), data_key],
        "tool_config": {**cfg, "split": True},
        "prompt_template": _entry_prompt(own, data_mod, data_key, task),
    }, edited_by=asked_by or "engine §17.1793", db=db)
    if out2.get("error"):
        logger.warning("split_edit_failed job=%s node=%s err=%s", job_id, node_key, out2)
        return None
    logger.warning("engine_step_split job=%s node=%s data_step=%s omissions=%d", job_id, node_key, data_key, omissions)
    return {"node_key": node_key, "data_step": data_key, "data_module": data_mod, "omissions": omissions}
