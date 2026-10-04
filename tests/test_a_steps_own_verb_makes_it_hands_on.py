"""§17.1349 — a step whose own title names a service and opens with a changing verb
is hands-on, command in its text or not.

Live, 2026-10-04: "Add Jellyfin libraries for /media/movies and /media/tv" carries
only paths in its backticks, so nothing said "work". The executor would have run
it as a model task and failed it for writing instructions — the way ADD131 burned
three attempts before §17.1347 fixed the reading of its commands.

The signal is the step's OWN first verb, after an optional `LXC 111:` prefix,
because the leading verb is what the step does. "List the public indexers this
Prowlarr can add" opens with `List` and changes nothing, so it is not claimed,
while "Add Jellyfin libraries …" is. `document`, `decide`, `review`, `research`,
`list`, `prove` and `verify` are deliberately absent.

An earlier, blunter version of this rule read the whole description and claimed 60
of 165 steps, attributing a documentation step to Caddy. That one was measured and
thrown away (§17.1347); this one is measured too, and claims 32, all of them
machine changes.
"""
from __future__ import annotations

import json
import pathlib

from app.modules.step_classify import step_is_hands_on

FX = pathlib.Path(__file__).parent / "fixtures"
PLAN = json.loads((FX / "homelab_plan_host_side_2026_10_03.json").read_text(encoding="utf-8"))


def _verdict(title: str, description: str = "", **kw):
    return step_is_hands_on({"node_key": "X", "title": title, "description": description, "tool": "LLM", **kw})


def test_the_live_step_is_hands_on_by_its_own_words():
    on, why = _verdict("Add Jellyfin libraries for /media/movies and /media/tv",
                       "A library is a directory: `/var/lib/jellyfin/root/default/Media/` holds `media.mblink`.")
    assert on is True
    assert why == "verb:add service:jellyfin", why


def test_a_reading_verb_is_not_claimed():
    """The false positive the blunt version produced."""
    on, why = _verdict("List the public indexers this Prowlarr can add")
    assert on is False, why


def test_a_documentation_step_is_not_claimed():
    on, why = _verdict("Document architecture and setup",
                       "Describe how Caddy, Jellyfin and Radarr fit together.")
    assert on is False, why


def test_a_decision_is_not_claimed_by_its_words():
    on, why = _verdict("Decide: how should films and TV actually arrive?",
                       "Radarr and Sonarr are one option.")
    assert on is False, why


def test_the_prefix_form_is_read_through():
    on, why = _verdict("LXC 111: implement the Palworld settings capability")
    assert on is True and why.startswith("verb:implement"), why


def test_a_verb_with_no_known_service_is_not_claimed():
    on, why = _verdict("Configure HP switch VLANs")
    assert on is False, why


def test_a_service_with_no_leading_verb_is_not_claimed():
    """Its commands catch it instead; this rule does not guess."""
    on, why = _verdict("Bring the PalWorld service (palworld.service) up on UDP 8211")
    assert on is False, why


def test_a_command_in_the_text_still_gives_the_precise_reason():
    """The rule is a FALLBACK: it must not shadow what the commands say."""
    on, why = _verdict("Add Jellyfin libraries for /media/movies",
                       "Run `pct exec 101 -- sh -c 'mkdir -p /var/lib/jellyfin/root/default/Movies'`.")
    assert on is True
    assert why.startswith("writes:") or why.startswith("observed:"), why


def test_it_claims_only_machine_changes_across_the_plan():
    """Measured over the stored plan: every step this rule decides alone is a
    create, install, start, stop, set, mount, expand, point or give."""
    import re
    decided = [(n["node_key"], n["title"]) for n in PLAN
               if step_is_hands_on({"node_key": n["node_key"], "title": n["title"],
                                    "description": n["description"], "tool": "LLM"})[1].startswith("verb:")]
    assert decided, "the rule decides something in this plan"
    allowed = re.compile(r"^\s*(?:[A-Za-z]+\s+\d{3,5}\s*:\s*)?(?:create|install|mount|configure|stop|start|"
                         r"give|set|expand|point|add|implement|make|write|enable|forward|fix|build)\b", re.I)
    for key, title in decided:
        assert allowed.match(title), (key, title)
