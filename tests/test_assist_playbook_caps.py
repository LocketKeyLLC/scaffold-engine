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


def test_ruled_out_has_a_larger_budget_than_proven():
    assert settings.assist_playbook_ruled_out_max > settings.assist_playbook_max


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
