"""§17.1336 — a probe inside a guest that just started is not a verdict, and a
prerequisite the engine inserts belongs where it is needed.

Live, 2026-10-04. The operator approved ADD9 and the engine ran `qm start 106` at
00:27:44. The next pause measured VM 106 at 00:28:09 — twenty-five seconds later,
before systemd-resolved had finished starting — read `getent hosts
deb.debian.org` printing nothing as "this guest cannot resolve names", inserted
ADD136 "Give VM 106 a working nameserver" (which asks the operator for a
`<DNS_SERVER>` it does not need) and made ADD66, the step that brings the
PalWorld service up, wait for it.

Minutes later the same probe answered:

    2a04:4e42:84::644 debian.map.fastlydns.net deb.debian.org

And `insert_node` appends at the END of the execution order, so ADD136 landed at
order 165 while ADD66 sat at 111: every other step in the plan would have run
before the one the operator was waiting on.
"""
from __future__ import annotations

import pytest

from app.modules import machine_truth as mt

INV = {"vms": {"106": "running"}, "cts": {}, "names": {"106": "palworld-server"}}
#: what the probe printed at 00:28:09, and what it printed later
EMPTY = (True, "")
ANSWERED = (True, "2a04:4e42:84::644 debian.map.fastlydns.net deb.debian.org")
#: VM 106 had been up 25 s when the pause read it
JUST_STARTED, LONG_UP = 25.0, 4000.0


def _truth(dns, uptime):
    return mt.truth_from_texts("106", inventory=INV, dns=dns, guest_uptime=uptime)


def test_the_probe_that_started_this_is_no_longer_a_verdict():
    t = _truth(EMPTY, JUST_STARTED)
    assert t.resolves is None, "unknown, not False"
    assert "up only 25s" in t.reads["getent hosts"], t.reads["getent hosts"]


def test_nothing_is_inserted_and_nothing_waits_on_an_unknown():
    node = {"node_key": "ADD66", "title": "Bring the PalWorld service (palworld.service) up on UDP 8211",
            "description": "Inside VM 106, confirm palworld.service is active and listening on UDP 8211."}
    rows = mt.contradictions(node, _truth(EMPTY, JUST_STARTED), {"network", "running"}, [])
    assert not [r for r in rows if r["kind"] == "no_dns"], [r["kind"] for r in rows]


def test_a_guest_that_has_been_up_for_an_hour_is_still_read_as_before():
    """§17.1313 keeps biting: CT 120's resolver really was broken, for hours."""
    t = _truth(EMPTY, LONG_UP)
    assert t.resolves is False
    node = {"node_key": "ADD88", "title": "Install Caddy inside LXC 120",
            "description": "Inside container 120, apt-get install caddy."}
    rows = mt.contradictions(node, t, {"network", "running"}, [])
    assert [r for r in rows if r["kind"] == "no_dns"], [r["kind"] for r in rows]


def test_an_unreadable_uptime_changes_nothing():
    """Without the uptime there is only the failed probe, so the older rule stands."""
    assert _truth(EMPTY, None).resolves is False


def test_a_young_guest_that_answers_is_simply_fine():
    assert _truth(ANSWERED, JUST_STARTED).resolves is True


def test_uptime_is_read_off_proc_uptime():
    assert mt.uptime_s("25.14 90.20") == 25.14
    assert mt.uptime_s("4000 7777.1") == 4000.0
    assert mt.uptime_s("") is None and mt.uptime_s("not a number") is None
    assert mt.uptime_s(None) is None


#: the plan's real order when ADD136 was inserted: appended last, far behind ADD66
ORDER = ["T1", "ADD9", "ADD66", "ADD135", "ADD122", "ADD129", "ADD100", "ADD136"]


@pytest.mark.asyncio
async def test_an_inserted_prerequisite_is_moved_in_front_of_the_step_that_waits(monkeypatch):
    import app.database as _db
    from app.modules import node_editor
    sent = {}

    class _S:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
    monkeypatch.setattr(_db, "async_session", lambda: _S())

    async def fake_list(job_id, db):
        return {"nodes": [{"node_key": k} for k in ORDER]}

    async def fake_reorder(job_id, order, *, db, edited_by=None):
        sent["order"], sent["by"] = list(order), edited_by
        return {"status": "ok"}
    monkeypatch.setattr(node_editor, "list_nodes", fake_list)
    monkeypatch.setattr(node_editor, "reorder_nodes", fake_reorder)

    assert await mt.place_before("job", "ADD136", "ADD66") is True
    assert sent["order"].index("ADD136") == sent["order"].index("ADD66") - 1, sent["order"]
    assert sorted(sent["order"]) == sorted(ORDER), "a permutation, nothing lost"
    assert "ADD136 runs before ADD66" in sent["by"]


@pytest.mark.asyncio
async def test_a_key_that_is_not_in_the_plan_moves_nothing(monkeypatch):
    import app.database as _db
    from app.modules import node_editor
    calls = []

    class _S:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
    monkeypatch.setattr(_db, "async_session", lambda: _S())
    monkeypatch.setattr(node_editor, "list_nodes", lambda job_id, db: _done({"nodes": [{"node_key": "T1"}]}))
    monkeypatch.setattr(node_editor, "reorder_nodes",
                        lambda *a, **k: calls.append(a) or _done({"status": "ok"}))
    assert await mt.place_before("job", "ADD136", "ADD66") is False
    assert calls == [], "no reorder is sent for keys it cannot place"


async def _done(v):
    return v


def test_the_reconcile_places_what_it_inserts():
    """Verify the lane: the insert path calls it right after the dependency."""
    import inspect
    src = inspect.getsource(mt.reconcile_from_truth)
    i = src.index("now waits for {ins['node_key']}")
    assert "place_before(job_id, ins[\"node_key\"], cur)" in src[i:i + 300], src[i:i + 300]
