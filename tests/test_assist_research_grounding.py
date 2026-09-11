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
    # §17.1018 — the call now also passes operator_notes, so it wraps across
    # lines. Assert the CALL is made, not how it happens to be formatted.
    assert "environment_block=render_research_grounding(" in window


def test_both_guide_paths_use_it():
    from app.modules import assist_guide

    for fn in (assist_guide.generate_guidance,
               assist_guide.generate_guidance_stream):
        src = inspect.getsource(fn)
        assert "render_research_grounding(" in src, fn.__name__  # §17.1018
        assert "environment_block=render_environment_block" not in src, fn.__name__


def test_the_decision_path_uses_it():
    from app.modules import assist_agent

    src = inspect.getsource(assist_agent.run_step_decision)
    assert "render_research_grounding(" in src  # §17.1018
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


# ── §17.976 — the guide paths must not diverge, and empty must be loud ────
#
# Measured before writing: 19 guided steps in the database carry an EMPTY
# `research_sources`, interleaved with steps carrying 2–6 — not a time-clustered
# outage, and no research failure was ever logged for any of them (0 ×
# confirm_query_failed, 0 × searxng_failed, 0 × detect_unknowns_failed). Source
# kinds where research did work: milvus 40, searxng 35, web 1 — the machinery is
# functional.
#
# The cause was a parity gap: `floor_when_empty=True` appeared at exactly ONE
# call site, the NON-stream guide. The stream path is the SPA path the operator
# actually uses, so when the query generator declined it produced a walkthrough
# with zero research — silently, because nothing logs a source COUNT.


def _prepass_call_args():
    import re

    from app.modules import assist_guide

    src = inspect.getsource(assist_guide)
    out = []
    for m in re.finditer(r"await _research_prepass\((.*?)\n        \)", src, re.S):
        body = re.sub(r"#[^\n]*", "", m.group(1))
        out.append(set(re.findall(r"(\w+)=", body)))
    return out


def test_there_are_exactly_three_prepass_sites_in_the_guide_module():
    """If a fourth appears, it has to be looked at against the others rather
    than inheriting whatever the nearest example happened to pass."""
    assert len(_prepass_call_args()) == 3


def test_both_guide_paths_ask_for_the_floor():
    """The stream path never did. §17.912's fallback existed only on the path
    the operator does not use."""
    calls = _prepass_call_args()
    with_floor = [c for c in calls if "floor_when_empty" in c]
    assert len(with_floor) == 2, "guide + guide_stream both need the floor"


def test_the_two_guide_calls_pass_the_same_flags():
    """Third instance of this divergence: §17.854 (environment_block),
    §17.975 (environment_block on fix), and now the floor. Pin the pair."""
    calls = _prepass_call_args()
    guides = [c for c in calls if "floor_when_empty" in c]
    assert guides[0] == guides[1], (guides[0] ^ guides[1])


def test_the_fix_path_keeps_its_deliberate_differences():
    """`deep=True` (§17.500 — troubleshooting wants doc content) and NO floor
    (§17.912 says only the guide path carries that defect) are intentional."""
    calls = _prepass_call_args()
    fix = [c for c in calls if "deep" in c]
    assert len(fix) == 1
    assert "floor_when_empty" not in fix[0]
    assert "environment_block" in fix[0]      # §17.975


def test_an_empty_research_result_is_logged_loudly():
    """A count at zero is the whole signal, and nothing recorded it."""
    from app.modules import assist_research_lib

    src = inspect.getsource(assist_research_lib)
    assert "assist_research_empty" in src
    i = src.index("assist_research_empty")
    window = src[i - 200:i + 260]
    assert "logger.warning" in window          # not info — this is a defect
    assert "queries=" in window and "node_key=" in window


def test_a_non_empty_result_records_the_count():
    from app.modules import assist_research_lib

    src = inspect.getsource(assist_research_lib)
    assert "assist_research_sources" in src
    i = src.index("assist_research_sources")
    assert "sources=" in src[i:i + 200]
