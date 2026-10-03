"""§17.1334 — a split's guard, numbering and stated counts all read the plan, so the
plan read has to carry them.

Two findings, 2026-10-03, on the live home-lab job:

1. `already_split(plan, key)` is the guard that stops a step being split twice. It
   looks for `[Engine split of ADD100 — n of N]` in a row's **description**, and
   `_pause_for_decision` read the plan as `SELECT node_key, title, status,
   completed_at` with no ORDER BY. The guard could never fire, and the renumbering
   had no order to be right in.

2. A sibling can state how many capability steps there are. ADD126 was titled "the
   single-page frontend rendering the three capabilities" and said it "renders
   exactly three sections", while ADD100's own text asks for FOUR: *"That makes
   FOUR capabilities, not three: Palworld settings, the media request box,
   scaffold-engine, and this network view."* Four capability children stand in the
   plan. A stale count is how a capability the operator asked for goes missing with
   nothing refused.

The fixtures are the eight real children: `add100_split_children_2026_10_03.json`
as stored, and `add100_split_preimage_2026_10_03.json` with ADD126's description as
it stood before the correction.
"""
from __future__ import annotations

import inspect
import json
import pathlib

from app.modules import execution_agent as ea
from app.modules import step_decomposition as sd

FX = pathlib.Path(__file__).parent / "fixtures"
NOW = json.loads((FX / "add100_split_children_2026_10_03.json").read_text(encoding="utf-8"))
PRE = json.loads((FX / "add100_split_preimage_2026_10_03.json").read_text(encoding="utf-8"))
#: the columns the pause USED to read — the pre-image of finding 1
OLD_COLUMNS = ("node_key", "title", "status", "completed_at")


def _as_pause_read(plan, columns):
    return [{k: v for k, v in n.items() if k in columns} for n in plan]


def test_the_guard_was_blind_to_rows_that_carry_no_description():
    """The pre-image: eight stamped children in the plan, and the guard sees none."""
    assert len(sd.already_split(NOW, "ADD100")) == 8, "the stamps are really there"
    blind = _as_pause_read(NOW, OLD_COLUMNS)
    assert sd.already_split(blind, "ADD100") == [], "which is why a second split was possible"


def test_the_pause_reads_the_field_the_guard_needs_and_in_order():
    """Verify the LANE: the guard works only if this query carries it."""
    src = inspect.getsource(ea._pause_for_decision)
    sel = [ln for ln in src.split("\n") if "FROM dag_nodes WHERE job_id = :j" in ln or "SELECT node_key, title" in ln]
    joined = " ".join(sel)
    assert "description" in joined, joined
    assert "ORDER BY execution_order" in src, "a renumbering needs an order to be right in"


def test_the_guard_fires_on_the_rows_the_pause_now_reads():
    rows = _as_pause_read(NOW, ("node_key", "title", "description", "status", "completed_at", "execution_order"))
    assert sd.already_split(rows, "ADD100") == [f"ADD{n}" for n in range(121, 129)]


def test_a_child_knows_its_parent_and_a_stranger_does_not():
    assert sd.parent_of(NOW[5]) == "ADD100"
    assert sd.parent_of({"description": "Start VM 106"}) == ""
    assert sd.parent_of(None) == ""


def test_the_numbering_is_recomputed_over_the_children_that_remain():
    """Live, ADD121 read `1 of 7` beside `2 of 8` after a child was removed and
    re-added by hand. The engine does not depend on a hand edit being complete."""
    hits = sd.stamp_edits(NOW, "ADD100")
    assert [(h["node_key"], h["says"], h["should_be"]) for h in hits] == [("ADD121", "1 of 7", "1 of 8")]
    fixed = [dict(n, description=next((h["description"] for h in hits if h["node_key"] == n["node_key"]),
                                      n["description"])) for n in NOW]
    assert sd.stamp_edits(fixed, "ADD100") == [], "and it settles"


def test_only_the_capability_steps_are_counted():
    """The word alone is not the signal: ADD121 builds a "capability registry" and
    ADD126 renders "the three capabilities". Neither IS a capability."""
    caps = [n["node_key"] for n in NOW if sd._CAPABILITY_RE.search(n["title"])]
    assert caps == ["ADD122", "ADD123", "ADD124", "ADD125"], caps


def test_a_sibling_that_counts_three_is_corrected_to_four():
    hits = {h["node_key"]: h for h in sd.count_edits(PRE, "ADD100")}
    assert "ADD126" in hits, list(hits)
    h = hits["ADD126"]
    assert h["says"] == "three capabilities" and h["should_be"] == "four capabilities"
    assert h["siblings"] == 4
    assert "ENGINE MEASURED: the plan holds 4 capability steps" in h["description"]
    assert "ADD125 network-view" in h["description"], "it names every sibling to cover"
    assert h["description"].startswith(PRE[5]["description"].rstrip()[:60]), "appended, never rewritten"


def test_a_finished_sibling_is_reported_and_never_rewritten():
    """ADD121 is `done` and its text lists the three it wrote by NAME. Swapping its
    number would leave three names behind a word saying four, and would falsify a
    record of what ran."""
    h = {x["node_key"]: x for x in sd.count_edits(NOW, "ADD100")}["ADD121"]
    assert h.get("description") is None and "note" in h
    assert "THREE capabilities" in h["says"]
    assert next(n for n in NOW if n["node_key"] == "ADD121")["status"] == "done"


def test_a_split_with_one_capability_counts_nothing():
    plan = [n for n in NOW if n["node_key"] in ("ADD121", "ADD122", "ADD126")]
    assert sd.count_edits(plan, "ADD100") == []
    assert sd.count_edits([], "ADD100") == [] and sd.count_edits(None, "ADD100") == []


def test_nothing_is_claimed_for_a_parent_with_no_children():
    assert sd.split_children(NOW, "ADD66") == []
    assert sd.stamp_edits(NOW, "ADD66") == [] and sd.count_edits(NOW, "ADD66") == []
