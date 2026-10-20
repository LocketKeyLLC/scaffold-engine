"""§17.1653 — a report step reads every settled step of the job, each labelled by its title, with room to read it.

Live (job 5db1b088 T31, 2026-10-18): the report was handed `### T11 (finished …)` / `403` with no title, called T11 an
"API key check" that failed (it was qBittorrent reachability), called T26 a "route/kill-switch check" (the panel's 17
routes), never saw the three fixes inserted while the job ran (RK1/PK1/IB1), never saw a skipped step's evidence (T15:
82 import-blocked items), and at 8,000 chars over ~45 sections each result kept ~40 characters.
"""
from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.modules import execution_agent as ea


def test_each_section_says_which_step_it_is():
    block = ea._format_upstream_block({"T11": ("403\n403", None)}, "T31",
                                      labels={"T11": "Verify qBittorrent is reachable — done"})
    assert "### T11 — Verify qBittorrent is reachable — done" in block


def test_a_report_budget_keeps_each_result():
    outs = {f"T{i}": ("x" * 300 + f"\nPASS result {i}\n" + "x" * 600, None) for i in range(40)}
    small = ea._format_upstream_block(outs, "T99", budget=8000)      # §17.1687 — the default is no longer small
    big = ea._format_upstream_block(outs, "T99", budget=40000)
    assert "PASS result 7" not in small and all(f"PASS result {i}" in big for i in range(40))


@pytest.mark.asyncio
async def test_skipped_steps_and_inserted_fixes_reach_the_report():
    db = MagicMock()
    rows = MagicMock(); rows.fetchall.return_value = [MagicMock(node_key="RK1"), MagicMock(node_key="T15")]
    db.execute = AsyncMock(return_value=rows)
    assert await ea._settled_keys(db, "j", "T31") == ["RK1", "T15"]
    sql = str(db.execute.await_args.args[0])
    assert "'skipped'" in sql and "node_key <> :nk" in sql
    src = inspect.getsource(ea._fetch_upstream_outputs)
    assert "status IN ('done', 'skipped')" in src


def test_wired_for_report_steps_only():
    src = inspect.getsource(ea)
    assert "_context_keys = list(dict.fromkeys(list(depends_on) + await _settled_keys(db, job_id, node_key)))" in src
    assert "budget=settings.max_report_upstream_chars if _reports else None" in src


def test_the_report_budget_holds_the_live_plan_inputs():
    """§17.1700 — live, job e5e22b92 P1's ten inputs were 88,816 chars; at 40,000 its key input lost a series"""
    from app.config import Settings
    assert Settings.model_fields["max_report_upstream_chars"].default >= 88_816
