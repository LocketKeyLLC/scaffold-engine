"""§17.2007 — a step the engine inserted to edit one of the job's modules is the engine's own work.

Live (69fe61c4 NOKEY2, 2026-11-24): §17.2006 added "the page the operator opens is that template" to the no-key fix
step's brief; `operator_steps._OPERATORS_RE` read "the operator opens" as work only the operator's hands can do, and the
CodeGen step was parked as "Your turn" with nothing written.
"""
from app.modules import engine_plan, operator_steps

_LIVE_BRIEF = ("Edit `paper_wire_engine_entry_module.py` in place -- the module as it is now is shown whole -- so that "
               "each view below answers when the operator has not stored the secret it reaches. When the view renders "
               "a page template (shown below when it does), the page the operator opens is that template: it must say "
               "the part is unavailable too -- rewrite the template whole (§17.2006).")


def _node(tool_config):
    return {"node_key": "NOKEY2", "tool": "CodeGen", "title": "Make GET /dashboard answer without the secrets it reaches",
            "description": _LIVE_BRIEF, "tool_config": tool_config}


def test_the_live_brief_without_the_guard_reads_as_the_operators():
    # the detector bites on the live text: what the guard has to stop
    assert operator_steps.is_wholly_operators(_node({}))


def test_a_fix_step_is_never_handed_to_the_operator():
    cfg = {"fixes_module": "paper_wire_engine_entry_module", "answers_without_secrets": "paper_wire_engine_entry_module"}
    assert operator_steps.operators_part(_node(cfg)) == ""
    assert not operator_steps.is_wholly_operators(_node(cfg))


def test_the_guard_reads_tool_config_stored_as_json_text():
    assert not operator_steps.is_wholly_operators(_node('{"fixes_module": "paper_wire_engine_entry_module"}'))


def test_a_human_step_is_still_the_operators():
    assert operator_steps.is_wholly_operators({"tool": "human", "description": "Store the keys.",
                                               "tool_config": {"fixes_module": "x"}})


def test_no_brief_the_engine_writes_for_its_own_work_reads_as_the_operators():
    # the operator's own steps say so on purpose; every other note is a brief for the engine's work
    operators = {"_RUN_NOTE", "_KEYS_NOTE"}
    notes = {n: getattr(engine_plan, n) for n in dir(engine_plan)
             if n.endswith("_NOTE") and isinstance(getattr(engine_plan, n), str)}
    assert {"_NOKEY_NOTE", "_REACH_NOTE", "_INSTALL_NOTE"} <= set(notes)
    hits = {n: [s for s in operator_steps._SENTENCE_RE.findall(t) if operator_steps._OPERATORS_RE.search(s)]
            for n, t in notes.items() if n not in operators}
    assert not {n: h for n, h in hits.items() if h}
