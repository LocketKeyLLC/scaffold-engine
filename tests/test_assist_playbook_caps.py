"""§17.940 — `ruled_out` is a prohibition, not a convenience.

Audit of the bounded stores after §17.939. `environment.playbook` holds two
lists under ONE shared cap (`assist_playbook_max`, default 12), evicted
oldest-first. They are not worth the same:

  * `proven`    — a method that worked here. Losing it costs a re-derivation.
  * `ruled_out` — rendered by `render_playbook_block` as a BINDING block,
                  "Already failed here — do NOT prescribe these again".
                  Losing one silently deletes a prohibition and the engine
                  re-prescribes a known-failing approach.

Live evidence (session 613dd1df) — the three entries a shared cap would
eventually evict are exactly the ones that stop the §17.882 loop:
apt.servarr.com fails DNS, radarr.video URLs 404, GitHub 'latest' shortcuts
return 9-byte files. Same principle as §17.920: negative knowledge survives
the cap.
"""
import pytest

from app.config import settings
from app.modules.assist_render import render_playbook_block


def test_ruled_out_is_never_smaller_than_proven():
    """§17.940 gave `ruled_out` a bigger cap than `proven`. §17.941 raised
    `proven` to match once it became elastic at RENDER time, so the guarantee
    moved from cap size to render behaviour (below) — but a prohibition must
    still never be allowed to remember LESS than a convenience."""
    assert (settings.assist_playbook_ruled_out_max
            >= settings.assist_playbook_max)


def test_the_two_halves_are_capped_independently():
    """A shared cap is the bug: filling `proven` must not shorten the runway
    for prohibitions."""
    import inspect

    from app.modules import assist_environment

    src = inspect.getsource(assist_environment.set_environment)
    assert "assist_playbook_ruled_out_max" in src
    assert 'key == "ruled_out"' in src


def test_ruled_out_renders_as_a_binding_prohibition():
    """The reason the cap matters: this text is injected into every
    generation. An entry that falls off the list stops forbidding anything."""
    block = render_playbook_block({"playbook": {
        "proven": ["Servarr apps install via GitHub release tarballs"],
        "ruled_out": ["radarr.video download URLs — return HTTP 404"],
    }})
    assert "do NOT prescribe these again" in block
    assert "radarr.video" in block
    assert "BINDING" in block


def test_empty_playbook_renders_nothing():
    assert render_playbook_block({}) == ""
    assert render_playbook_block(None) == ""
    assert render_playbook_block({"playbook": {"proven": [], "ruled_out": []}}) == ""


@pytest.mark.parametrize("half", ["proven", "ruled_out"])
def test_each_half_renders_independently(half):
    """One half being empty must not suppress the other."""
    block = render_playbook_block({"playbook": {half: ["an entry"]}})
    assert "an entry" in block


# ── §17.941 — the protection moved to render time ─────────────────────────


def test_proven_is_elastic_under_budget_and_ruled_out_is_not():
    """The store cap governs MEMORY; the render budget governs the PROMPT.

    Before this, `assist_playbook_max` was doing both jobs: it sat at 12 not
    because twelve methods is the right thing to remember but because more
    crowded the injected block — so a 41-step session forgot methods it had
    proven. `proven` now trims to a share of the budget (newest kept);
    `ruled_out` never trims, because dropping a prohibition silently re-enables
    a known-failing approach.
    """
    env = {"playbook": {
        "proven": [f"proven method {i} " + "p" * 110 for i in range(20)],
        "ruled_out": [f"ruled out approach {i} " + "r" * 85 for i in range(10)],
    }}
    tight = render_playbook_block(env, proven_budget=1000)
    assert tight.count("- proven method") < 20      # trimmed
    assert "proven method(s) omitted" in tight      # and says so
    assert tight.count("- ruled out approach") == 10  # prohibitions all survive


def test_no_budget_renders_everything():
    env = {"playbook": {"proven": ["a", "b"], "ruled_out": ["c"]}}
    out = render_playbook_block(env)
    assert "- a" in out and "- b" in out and "- c" in out
    assert "omitted" not in out


def test_at_least_one_proven_survives_any_budget():
    """A share so small that nothing fits must still keep the newest method
    rather than silently emptying the section."""
    env = {"playbook": {"proven": ["x" * 500, "y" * 500], "ruled_out": []}}
    out = render_playbook_block(env, proven_budget=10)
    assert out.count("\n- ") == 1
    assert "y" * 500 in out          # newest kept


def test_the_playbook_survives_budget_pressure_whole():
    """§17.941 — the §17.881 promise ("never budget-dropped") was FALSE
    whenever a §17.914 state block existed: `direction_idx` was claimed by the
    state block, the playbook was left droppable, and the trim loop deleted it
    entirely — prohibitions and all — while the facts it outranks survived."""
    from app.modules.assist_render import render_session_memory

    env = {
        "profile": "root@pve",
        "system_state": {"106": {"kind": "vm", "attrs": {"boot": "order=scsi0"},
                                 "devices": {}, "source": "qm config 106"}},
        "facts": [f"fact {i} " + "f" * 90 for i in range(40)],
        "playbook": {
            "proven": [f"proven {i} " + "p" * 110 for i in range(12)],
            "ruled_out": [f"ruled {i} " + "r" * 85 for i in range(10)],
        },
    }
    out = render_session_memory(env, [], budget=4000)
    assert "Proven to work here" in out, "the playbook was dropped whole again"
    assert "do NOT prescribe these again" in out, "prohibitions were lost"
