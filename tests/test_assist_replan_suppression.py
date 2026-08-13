"""§17.771 (Phase 4) — re-plan thrash-suppression helpers.

Pins the signature (stable, order-independent over affected nodes, text-
normalizing) and the discarded-list parser (tolerant of str/None/garbage). The
stage→discard→suppress round-trip is DB-bound and verified live; these guard the
pure logic the suppression turns on.
"""
from __future__ import annotations

import json

from app.modules import assist_agent as a


def test_signature_is_order_independent_over_affected():
    s1 = a._replan_signature("use zfs", [{"node_key": "T3"}, {"node_key": "T1"}])
    s2 = a._replan_signature("use zfs", [{"node_key": "T1"}, {"node_key": "T3"}])
    assert s1 == s2


def test_signature_normalizes_whitespace_and_case():
    s1 = a._replan_signature("Use   ZFS  Instead", [{"node_key": "T1"}])
    s2 = a._replan_signature("use zfs instead", [{"node_key": "T1"}])
    assert s1 == s2


def test_signature_differs_on_different_nodes_or_text():
    base = a._replan_signature("use zfs", [{"node_key": "T1"}])
    assert base != a._replan_signature("use zfs", [{"node_key": "T2"}])
    assert base != a._replan_signature("use lvm", [{"node_key": "T1"}])


def test_signature_ignores_empty_and_malformed_affected():
    s = a._replan_signature("x", [{"node_key": "T1"}, {}, {"node_key": ""}, "junk"])
    assert s == "x|T1"


def test_discarded_list_parses_jsonb_string():
    meta = json.dumps({"discarded_replans": ["sigA", "sigB"]})
    assert a._discarded_replans_from_metadata(meta) == ["sigA", "sigB"]


def test_discarded_list_tolerates_none_and_non_list():
    assert a._discarded_replans_from_metadata(None) == []
    assert a._discarded_replans_from_metadata({"discarded_replans": "nope"}) == []
    assert a._discarded_replans_from_metadata("not json") == []
    # filters non-str entries
    assert a._discarded_replans_from_metadata(
        {"discarded_replans": ["ok", 5, None]}) == ["ok"]
