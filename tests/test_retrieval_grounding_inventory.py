"""§17.1020 — every retrieval path must be grounded in the operator's system.

This gate exists because the same defect was fixed five times on five different
paths, and each fix was verified on the path it touched:

  §17.854  environment grounding — stream guide missing it
  §17.975  research grounding — fix path missing it
  §17.976  the §17.912 floor — stream guide missing it
  §17.1018 named hardware — guide/fix/decision prepasses
  §17.1019 named hardware — the ASK path, which none of the above covered

The operator's summary was fair: "the lack of researching and applying the most
up to date information has been lacking and constantly needing repair." The
repair kept landing on one path at a time because nothing enumerated them.

A retrieval site is GROUNDED when the function that runs it also builds the
operator's system into the query — by any of the approved builders below. New
retrieval path, no grounding → this fails, with the site named.
"""
import ast
import pathlib

import pytest

# Functions that actually reach the search/KB stack.
RETRIEVAL_CALLS = {"_research_prepass", "_confirm_query", "research_one"}

# Ways a caller may supply the operator's system to the query.
GROUNDING_BUILDERS = {
    "render_research_grounding",   # §17.975 — facts/profile/playbook (+§17.1018 hardware)
    "_kb_hint_from",               # §17.650/§17.1019 — the ask path's retrieval hint
    "hardware_for_text",           # §17.1020 — subject-matched models, deterministic queries
    "blocker_research_query",      # §17.918 — grounds via facts/notes, hardware since §17.1020
    "_error_focus_query",          # §17.882 — grounds via the symptom, hardware since §17.1020
}

# Sites that legitimately carry no grounding, with the reason. Adding to this
# list is a deliberate act and should be argued in review.
EXEMPT = {
    # The fan-out INSIDE _research_prepass: the queries it dispatches were
    # already generated from the grounded prompt one frame up.
    ("assist_research_lib.py", "_research_prepass"),
    # research_one's own dispatch — grounded by the context_hint its CALLER
    # builds (run_step_research → _kb_hint_from), asserted separately below.
    ("assist_research_lib.py", "research_one"),
}

APP = pathlib.Path(__file__).resolve().parents[1] / "app"


def _sites():
    """(file, enclosing function, lineno) for every retrieval call in app/."""
    out = []
    for path in sorted(APP.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for sub in ast.walk(node):
                if not isinstance(sub, ast.Call):
                    continue
                fn = sub.func
                name = getattr(fn, "id", None) or getattr(fn, "attr", None)
                if name in RETRIEVAL_CALLS:
                    out.append((path, node, sub.lineno))
    return out


def test_retrieval_sites_are_discoverable():
    """If this finds nothing the gate is inert and everything below is vacuous."""
    assert len(_sites()) >= 7, f"only found {len(_sites())} retrieval sites"


def test_every_retrieval_path_grounds_its_query():
    ungrounded = []
    for path, fn, lineno in _sites():
        if (path.name, fn.name) in EXEMPT:
            continue
        body = ast.unparse(fn)
        if not any(b in body for b in GROUNDING_BUILDERS):
            ungrounded.append(f"{path.name}:{lineno} in {fn.name}()")
    assert not ungrounded, (
        "retrieval runs here without building the operator's system into the "
        "query — the §17.854/975/976/1018/1019 defect, once per path:\n  "
        + "\n  ".join(ungrounded)
        + "\n\nPass one of: " + ", ".join(sorted(GROUNDING_BUILDERS))
        + "\nor add an argued exemption to EXEMPT in this file."
    )


def test_the_ask_path_hint_carries_notes():
    """research_one is exempt above because its caller supplies the hint; that
    delegation is only worth anything if the caller actually passes notes."""
    src = (APP / "modules" / "assist_agent.py").read_text(encoding="utf-8")
    assert "_kb_hint_from(" in src and "mem.operator_notes" in src  # §17.1023


@pytest.mark.parametrize("builder", sorted(GROUNDING_BUILDERS))
def test_every_named_builder_exists(builder):
    """A typo in GROUNDING_BUILDERS would make the gate pass on nothing."""
    found = any(builder in p.read_text(encoding="utf-8")
                for p in APP.rglob("*.py"))
    assert found, f"{builder} is named by the gate but does not exist"


def test_the_deterministic_builders_reach_hardware():
    """§17.882 and §17.918 are the LAST line of grounding — they run when the
    LLM query generator declines, and they had no hardware until §17.1020."""
    src = (APP / "modules" / "assist_guide.py").read_text(encoding="utf-8")
    for fn in ("_error_focus_query", "blocker_research_query"):
        i = src.index(f"def {fn}(")
        j = src.index("\ndef ", i + 1)
        assert "hardware_for_text" in src[i:j], f"{fn} cannot reach named hardware"
