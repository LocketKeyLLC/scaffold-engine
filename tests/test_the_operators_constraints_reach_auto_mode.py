"""§17.1250 — the operator's environment was invisible to auto mode.

Every consumer of `environment.profile` was an assist module —
`assist_draft`, `assist_evidence`, `assist_render`, `assist_placeholders`,
`assist_state_check`. `prompt_assembly` never mentioned it, so
`build_base_prompt` — the one place every step's guide AND runbook is assembled —
never saw it, and neither did the autonomous executor or the runbook drafter.

The profile is where the operator's standing constraints live: "root@pve in ONE
interactive shell" (§17.700) and, recorded this session after they said it twice,
"the router is reached through the My Spectrum APP, not a LAN web page — do NOT
write a step that says open a browser to 192.168.1.1 and log in".

ADD112 then wrote exactly that:

    check the router's admin page directly … Open a browser to
    `http://<gateway-ip>` and log in with the credentials printed on the
    router's label.

Not disobedience. The constraint had no path to the prompt — the same shape as
§17.1237's missing description.
"""
from __future__ import annotations

import ast
import inspect

from app.modules.prompt_assembly import build_base_prompt

PROFILE = ("Operator runs commands as root@pve in ONE interactive shell. "
           "THE ROUTER IS NOT ADMINISTRABLE THE USUAL WAY: its settings are reached through the "
           "My Spectrum APP, not a LAN web page. Do NOT write a step that says \"open a browser "
           "to 192.168.1.1 and log in to the router\".")


def test_the_profile_is_rendered_into_the_prompt():
    p = build_base_prompt({"title": "Point the router's DNS at Pi-hole", "prompt_template": "Do it."},
                          {"description": "Secure Proxmox HomeLab"},
                          {"profile": PROFILE})
    assert "My Spectrum APP" in p
    assert "open a browser" in p
    assert "THE OPERATOR'S ENVIRONMENT" in p
    assert "standing constraints, not suggestions" in p


def test_no_environment_changes_nothing():
    base = build_base_prompt({"title": "t", "prompt_template": "Do it."}, {})
    for env in (None, {}, {"profile": ""}, {"profile": "   "}):
        assert build_base_prompt({"title": "t", "prompt_template": "Do it."}, {}, env) == base


def test_the_substitutions_are_not_dumped_into_the_prompt():
    """Placeholders are handled by §17.1188/1212; only the PROFILE belongs here."""
    p = build_base_prompt({"title": "t", "prompt_template": "Do it."}, {},
                          {"profile": "x", "substitutions": {"SECRET_ISH": "value-not-for-prompts"}})
    assert "value-not-for-prompts" not in p


def test_the_executor_passes_the_environment_on_the_node_path():
    from app.modules import execution_agent as ea
    src = inspect.getsource(ea.execute_next_node)
    assert "job_environment(_edb, job_id)" in src
    assert "_build_prompt(node_snapshot, brief, _node_env)" in src


def test_every_draft_call_site_passes_the_environment():
    """Enumerated off the AST, like §17.1232's `spec` check — a redraft added
    later that forgets it would quietly go back to drafting without the
    operator's constraints."""
    src = open("app/modules/execution_agent.py").read()
    calls = [n for n in ast.walk(ast.parse(src))
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "draft_runbook"]
    # §17.1254 added a fifth site and this gate caught it — which is what it is
    # for. Assert the property, not the number: every site must carry both.
    assert len(calls) >= 4, f"expected at least the 4 known draft sites, found {len(calls)}"
    for c in calls:
        kw = [k.arg for k in c.keywords]
        assert "environment" in kw, f"draft_runbook at line {c.lineno} drops environment"
        assert "spec" in kw, f"draft_runbook at line {c.lineno} drops spec"


def test_the_assist_path_supplies_it_too():
    """It always had the db and the job_id; it simply never asked."""
    from app.modules import prompt_assembly as pa
    src = inspect.getsource(pa.assemble_step_context)
    assert "job_environment(db, job_id)" in src
    assert "build_base_prompt(node, brief, _env)" in src


def test_the_drafter_takes_it():
    from app.modules import supervised_runs as sr
    sig = inspect.signature(sr.draft_runbook)
    assert "environment" in sig.parameters
    assert "build_base_prompt(node, b, environment)" in inspect.getsource(sr.draft_runbook)


def test_the_environment_is_read_before_the_first_draft_uses_it():
    """My own §17.1250 patch passed `environment=_env` to the FIRST draft two
    lines before `_env` was assigned — a NameError on every supervised pause.
    `make test` missed it (the path needs a live channel); ruff's F821 in the
    pre-push hook caught it. This test is the one that should have.

    Checked by line number off the AST, because the bug was purely an ordering
    one and reads perfectly well either way."""
    src = open("app/modules/execution_agent.py").read()
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
              and n.name == "_pause_for_decision")
    assigns = [n.lineno for n in ast.walk(fn)
               if isinstance(n, ast.Assign)
               and any(isinstance(t, ast.Name) and t.id == "_env" for t in n.targets)]
    uses = [n.lineno for n in ast.walk(fn)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == "draft_runbook"]
    assert assigns and uses, (assigns, uses)
    assert min(assigns) < min(uses), (
        f"_env is assigned at line {min(assigns)} but first used at {min(uses)} — "
        "every supervised pause would raise NameError")
