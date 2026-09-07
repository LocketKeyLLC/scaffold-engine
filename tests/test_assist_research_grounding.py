"""§17.975 — what a research QUERY knows about the operator's system.

Operator: *"What about the research for the dag as well as other components.
This is a large component that drives the engine."*

Two gaps, both verified against the running code before writing any of this:

* the playbook — `ruled_out` is called a BINDING prohibition in its own source
  and is rendered into the model's prompt by §17.751 — never reached ANY
  research query, at any call site;
* `generate_fix` passed no environment grounding at all. §17.771 gave it to
  guide/decision and §17.854 restored it on the stream path; the fix call was
  never included, so every troubleshooting query across T31–T35 was generated
  without knowing the operator is on a Proxmox host running Debian 12 LXCs —
  the one path that only runs when something is already wrong.
"""
import inspect

from app.modules.assist_render import (
    render_environment_block,
    render_research_grounding,
)

_ENV = {
    "facts": ["Proxmox host at 192.168.1.156", "Debian 12 LXC containers"],
    "playbook": {
        "proven": ["LXC creation via pct create with the debian-12 template"],
        "ruled_out": ["apt.servarr.com apt repository — fails DNS inside container 102"],
    },
}


def test_the_plain_environment_block_never_carried_the_playbook():
    """Pins the defect, so this cannot silently regress into 'it was always
    there' — the whole point is that the query generator was blind to it."""
    base = render_environment_block(_ENV)
    assert "Proxmox host" in base
    assert "servarr" not in base
    assert "pct create" not in base


def test_ruled_out_reaches_the_query_grounding():
    out = render_research_grounding(_ENV)
    assert "servarr" in out
    assert "do NOT search for these" in out


def test_proven_reaches_it_as_something_to_build_on():
    out = render_research_grounding(_ENV)
    assert "pct create" in out
    assert "build on these" in out


def test_the_facts_survive():
    """This wraps the §17.771 grounding, it does not replace it."""
    out = render_research_grounding(_ENV)
    assert "Proxmox host at 192.168.1.156" in out
    assert render_environment_block(_ENV).strip() in out


def test_an_empty_environment_is_safe():
    assert render_research_grounding({}) == ""
    assert render_research_grounding(None) == ""
    assert render_research_grounding({"playbook": {}}) == ""


def test_a_playbook_with_no_environment_still_grounds():
    out = render_research_grounding({"playbook": {"ruled_out": ["x fails here"]}})
    assert "x fails here" in out


def test_malformed_playbook_does_not_raise():
    for bad in ({"playbook": "nonsense"}, {"playbook": {"ruled_out": None}},
                {"playbook": {"proven": ["", "  "]}}):
        assert isinstance(render_research_grounding(bad), str)


# ── every prepass site now grounds the same way ───────────────────────────


def test_the_fix_path_now_grounds_its_research():
    """The gap that mattered most: troubleshooting queries were system-blind."""
    from app.modules import assist_guide

    src = inspect.getsource(assist_guide.generate_fix)
    i = src.index("_research_prepass(")
    window = src[i:i + 1200]
    assert "environment_block=render_research_grounding(environment)" in window


def test_both_guide_paths_use_it():
    from app.modules import assist_guide

    for fn in (assist_guide.generate_guidance,
               assist_guide.generate_guidance_stream):
        src = inspect.getsource(fn)
        assert "render_research_grounding(environment)" in src, fn.__name__
        assert "environment_block=render_environment_block" not in src, fn.__name__


def test_the_decision_path_uses_it():
    from app.modules import assist_agent

    src = inspect.getsource(assist_agent.run_step_decision)
    assert "render_research_grounding(mem.environment)" in src
    assert "render_environment_block(mem.environment)" not in src


def test_no_prepass_site_is_left_on_the_old_grounding():
    """The sweep itself — a new call site added later must not quietly drop
    back to the environment-only block."""
    from app.modules import assist_agent, assist_guide

    for mod in (assist_guide, assist_agent):
        src = inspect.getsource(mod)
        for line in src.splitlines():
            if "environment_block=" in line and "render_environment_block" in line:
                raise AssertionError(f"{mod.__name__}: {line.strip()}")
