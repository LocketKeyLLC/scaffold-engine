"""§17.1862 — a test is shown the kind a parameter takes.

Live (follow-up 5b4880c0 T5, every round on both models, 2026-10-28/29): the job-modules index read
`handle_approval_answer(job_id, node_key, answer, payload)`; the tests passed a string for `answer`; the §17.1761 gate
refused "`answer` is `dict`; it was given a `str`" -- the annotation the gate read was never shown. And with the kinds
shown, the index grew past its budget: the cut fell after 13 of 21 modules, the approval gate among the lost."""
from __future__ import annotations

import ast
import inspect

from app.modules import engine_source as es
from app.modules.engine_contracts import step_targets

GATE = '''# app/modules/paper_operator_approval_gate.py
from typing import Any
async def request_approval(proposal: dict[str, Any], *, db=None) -> dict[str, Any]:
    return {"job_id": "j", "proposal": proposal, "proposal_id": 1, "status": "pending"}
async def handle_approval_answer(job_id: str, node_key: str, answer: dict[str, Any], payload: dict[str, Any]) -> None:
    if not answer.get("approved"):
        return None
def frame(node: dict, *, brief: str | None = None, mode: str = "fast", n=3):
    if mode not in ("fast", "slow"):
        raise ValueError(mode)
'''


def _fn(name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    return next(n for n in ast.parse(GATE).body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)


def test_both_renderers_show_the_kind_the_gate_checks():
    h = _fn("handle_approval_answer")
    assert es._signature(h) == "async def handle_approval_answer(job_id: str, node_key: str, answer: dict, payload: dict)"
    # §17.1866 -- the job-module index adds the keys the function reads from a dict parameter
    assert es._shape_signature("handle_approval_answer", es._call_shape(h)) == \
        "async handle_approval_answer(job_id: str, node_key: str, answer: dict{approved}, payload: dict)"
    assert "handle_approval_answer(job_id: str, node_key: str, answer: dict, payload: dict)" in es._public_api(GATE)


def test_a_default_an_optional_a_value_set_and_no_annotation_each_render_their_way():
    f = _fn("frame")
    sh = es._call_shape(f, {}, {"frame": f})
    # a value set wins over the kind; an optional kind puts None last; a default with a kind reads Python-style
    assert es._shape_signature("frame", sh) == "frame(node: dict, *, brief: str | None = …, mode:fast|slow=…, n=…)"
    assert es._signature(f) == "def frame(node: dict, *, brief: str | None, mode: str, n)"
    bare = ast.parse("def go(a, *, b=1): pass").body[0]
    assert es._shape_signature("go", es._call_shape(bare)) == "go(a, *, b=…)"


def test_the_finding_and_the_signature_spell_a_kind_the_same_way():
    code = '''# tests/test_x.py
from app.modules.paper_operator_approval_gate import frame
def test_it():
    frame({}, brief=3)
'''
    defs = es.sibling_definitions([{"node_key": "T16", "status": "done", "output_text": "```python\n" + GATE + "```"}])
    found = [f for f in es.sibling_refs("```python\n" + code + "```", defs) if "given a" in f]
    assert found and "`brief` is `str | None`; it was given a `int`" in found[0]
    assert "brief: str | None = …" in found[0]


def test_the_leading_modules_survive_the_budget_cut():
    defs = {}
    for i in range(12):
        src = f"# app/modules/m{i:02d}.py\n" + "".join(f"def f{i}_{j}(a: dict, b: str, *, c: int = 1): pass\n" for j in range(6))
        defs.update(es.sibling_definitions([{"node_key": f"T{i}", "status": "done", "output_text": "```python\n" + src + "```"}]))
    lead = {"m09", "m11"}
    out = es._render_job_modules(defs, "", budget=900, first=lead)
    shown = [ln.split(":")[0].strip() for ln in out.splitlines()[1:] if ln.startswith("    ") and ln.strip() != "…"]
    assert shown[:2] == ["m09", "m11"] and out.rstrip().endswith("…")
    assert "f9_5(a: dict, b: str, *, c: int = …)" in out and "f11_5(" in out     # whole lines, not cut mid-way
    assert len(out) < 900 + 600                                                  # the rest is what the budget bounds
    assert es._render_job_modules(defs, "", budget=900, first=set()).count("\n") < out.count("\n") + 3


def test_a_test_step_naming_no_module_leads_with_the_untested_ones():
    mods = {"paper_a": "", "paper_b": "", "paper_c": "", "paper_d": ""}
    t5 = "Python test module `tests/test_paper_twelve_modules.py` (pytest). Write unit tests for each untested module."
    assert step_targets(t5, set(), mods, tested={"paper_a", "paper_c"}) == {"paper_b", "paper_d"}
    assert step_targets(t5, {"paper_d"}, mods, tested={"paper_a", "paper_c"}) == {"paper_b"}
    assert step_targets(t5, set(), mods) == set()                                # the inventory unknown: as before
    assert step_targets("tests/test_paper_b.py: tests for paper_b", set(), mods, tested=set()) == {"paper_b"}
    assert step_targets("Write the module.", set(), mods, tested=set()) == set()


def test_the_executor_hands_the_inventory_to_step_targets():
    from app.modules import execution_agent
    src = inspect.getsource(execution_agent)
    assert "tested=_es.tested_modules(_done)" in src
    rows = [{"status": "done", "output_text": "```python\n# tests/test_paper_a.py\nimport pytest\n```"},
            {"status": "done", "output_text": "```python\n# app/modules/paper_b.py\nX = 1\n```"},
            {"status": "failed", "output_text": "```python\n# tests/test_paper_b.py\nimport pytest\n```"}]
    assert es.tested_modules(rows) == {"paper_a"}
