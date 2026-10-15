r"""§17.1586 — a developed delivery is judged by its unit's journal too.

Live (c2792d4b T4, 2026-10-15): `/api/media-library` answered 200 after the delivery and the step would have been
recorded done, while the panel's journal said "Failed to fetch Radarr library: HTTP 401" five seconds after the
restart — the handler swallows the upstream failure and answers `"movies": []`. The route check cannot see that.
"""
from __future__ import annotations

import asyncio
import inspect

from app.modules import assist_state_check as asc
from app.modules import develop as dv

HOST = dv.Host(guest="111", workdir="/opt/control-panel-backend", unit="control-panel.service", vm=False)
ROUTE = "pct exec 111 -- curl -s --fail-with-body --max-time 20 http://127.0.0.1:3001/api/media-library"
ACTIVE = "pct exec 111 -- systemctl is-active control-panel.service"
JOURNAL = ("Stopped control-panel.service - Control Panel Backend.\nStarted control-panel.service - Control Panel Backend.\n"
           "Control panel backend listening on port 3001\nFailed to fetch Radarr library: HTTP 401:\n")
CLEAN = "Started control-panel.service - Control Panel Backend.\nControl panel backend listening on port 3001\n"


def test_the_journal_is_read_after_the_routes_and_before_is_active():
    j = dv.journal_check(HOST)
    assert j == "pct exec 111 -- sh -c 'journalctl -u control-panel.service --since -3min --no-pager -o cat | tail -n 40'"
    assert dv.with_journal([ROUTE, ACTIVE], HOST) == [ROUTE, j, ACTIVE]
    assert dv.with_journal(dv.with_journal([ROUTE, ACTIVE], HOST), HOST) == [ROUTE, j, ACTIVE]          # idempotent
    from app.modules.assist_state_check import read_only_command
    assert read_only_command(j)
    from app.modules import execution_agent as ea
    src = inspect.getsource(ea._pause_for_decision)
    assert "checks = develop.with_journal(develop.done_checks(run_node, host, _derived), host)" in src
    assert "_checks = develop.with_journal(develop.done_checks(run_node, host, _own), host) if _own else checks" in src


def test_an_error_line_in_the_journal_contradicts_the_step_without_a_judge():
    j = dv.journal_check(HOST)
    why = asc.negative_evidence(j, JOURNAL, "Fix Radarr endpoint backend: return JSON data or a clear JSON error")
    assert why and "Failed to fetch" in why and "journal" in why          # the leftmost error phrase on the line
    assert asc.negative_evidence(j, CLEAN, "Fix Radarr endpoint backend") == ""
    for line in ("connect ECONNREFUSED 192.168.1.25:8192", "UnhandledPromiseRejection", "Main process exited, code=exited, status=1/FAILURE"):
        assert asc.negative_evidence(j, CLEAN + line + "\n", "fix it"), line
    # a curl's own output is still the curl branch's; a journal line about something undone stays the judge's
    assert "journal" not in asc.negative_evidence(ROUTE, "HTTP 401", "fix")
    assert asc.negative_evidence(j, JOURNAL, "stop the panel until it has a key") == ""     # an undoing claim: not this rule


def test_the_judge_reads_it_end_to_end(monkeypatch):
    from app import model_router
    called = []

    async def _no_judge(*a, **k):
        called.append(1); raise RuntimeError("the judge must not be asked")
    monkeypatch.setattr(model_router, "tool_call", _no_judge)
    j = dv.journal_check(HOST)
    probes = [{"id": "V:1", "kind": "state", "claim": "Fix Radarr endpoint backend", "command": j, "expect": ""}]
    out = asyncio.run(asc.judge_outputs(probes, "== V:1 ==\n" + JOURNAL))
    assert out[0]["verdict"] == "contradicted" and "journal" in out[0]["reason"] and not called
