"""§17.909 — research the operator's SYMPTOM, not the step's task.

§17.882 built the deterministic fix query as `step title + first error line`.
On the live T23 marathon (session 613dd1df) that produced, for six consecutive
blocker reports:

    Install PalWorld server curtin command in-target'
    Install PalWorld server when attempting to set up the ubuntu server, it
      continues to hang. I thing it would be bes
    Install PalWorld server It got hung up on install again, however it appears
      to be when installing the openssh-serv

Research DID run every time. It just researched PalWorld and SteamCMD while the
operator was fighting the Ubuntu installer. A blocker is frequently UPSTREAM of
the step it blocks.
"""
from __future__ import annotations

import pytest

from app.modules.assist_guide import _error_focus_query as q

pytestmark = pytest.mark.asyncio

TITLE = "Install PalWorld server"

LIVE = [
    "in the install it appears to be hung on the 'downloading and installing "
    "security update curtin command in-target'",
    "when attempting to set up the ubuntu server, it continues to hang. I thing "
    "it would be best to remove this VM and start over from the beginning.",
    "It got hung up on install again, however it appears to be when installing "
    "the openssh-server. It gets stuck on downloading and installing security updates.",
]


@pytest.mark.parametrize("msg", LIVE)
def test_live_blockers_no_longer_anchor_on_the_wrong_subject(msg):
    out = q(TITLE, msg).lower()
    assert "palworld" not in out, out
    assert "steamcmd" not in out, out


def test_the_diagnostic_phrase_survives_the_cap():
    """The operator named the exact phase; §17.882's query dropped it."""
    out = q(TITLE, LIVE[0]).lower()
    assert "downloading and installing security update" in out


def test_the_operators_proposal_is_not_the_symptom():
    """'destroy this VM and start over' steered retrieval at how to delete a VM
    — four of six live queries carried that tail."""
    out = q(TITLE, LIVE[1]).lower()
    assert "start over" not in out
    assert "remove this vm" not in out
    assert "hang" in out  # the actual symptom survives


def test_hang_vocabulary_is_recognised_as_the_symptom_line():
    """REGRESSION — 'hung'/'stuck'/'frozen' were absent from the §17.882 line
    detector, so a HANG (the most common non-error blocker) never registered."""
    out = q("Install X", "everything looks fine\nthe installer is hung on step 3")
    assert "hung on step 3" in out


@pytest.mark.parametrize("title,err,keep", [
    ("Install Radarr", "Radarr service failed to start: exit code 1", "radarr"),
    ("Configure Prowlarr indexers", "Prowlarr returns HTTP 401", "prowlarr"),
])
def test_on_topic_titles_are_still_kept(title, err, keep):
    """Dropping the title unconditionally would lose real context — it is
    dropped only when the operator shares none of its DISTINCTIVE vocabulary."""
    assert keep in q(title, err).lower()


@pytest.mark.parametrize("title,err,drop", [
    ("Install PalWorld server", "the ubuntu installer hangs on security updates", "palworld"),
    ("Install PalWorld server", "grub rescue prompt after reboot", "palworld"),
])
def test_off_topic_blockers_drop_the_title(title, err, drop):
    assert drop not in q(title, err).lower()


def test_generic_title_words_never_anchor_a_query():
    """'server'/'install' are in every other step title; anchoring on them would
    make every blocker look on-topic."""
    from app.modules.assist_guide import _distinctive
    assert _distinctive("Install PalWorld server") == {"palworld"}
    assert _distinctive("Set up the system") == set()


@pytest.mark.parametrize("title,err", [("Install X", ""), ("", ""), ("Install X", "   ")])
def test_degenerate_input_is_safe(title, err):
    out = q(title, err)
    assert isinstance(out, str) and len(out) <= 150
