"""§17.289 — `_render_cost_rollup` surfaces the `data_source` flag.

§17.280-UX-3 audit-tail concern: §17.284 added `data_source: "ok" |
"error"` to every rollup return dict, but the CLI's
`_render_cost_rollup` still rendered zero-totals from an error-source
rollup identically to a real "no calls yet" rollup. A busy job on a
broken telemetry path looked like a fresh job at the CLI surface.

§17.289 prints a one-line warning above the numbers when
`data_source == "error"`. The chat side (scaffold_router `_handle_cost`)
gets the matching ⚠️ banner — pinned in tests/test_cost_data_source_surfaces.py
in the orchestrator test suite.
"""
from __future__ import annotations

import click
from click.testing import CliRunner

from scaffold_cli.main import _render_cost_rollup


def _payload(*, data_source: str = "ok", calls: int = 0, cost: float = 0.0):
    return {
        "job_id": "job-1",
        "total_cost_usd": cost,
        "total_prompt_tokens": 0,
        "total_completion_tokens": 0,
        "total_latency_ms": 0,
        "call_count": calls,
        "by_provider": [],
        "by_kind": [],
        "data_source": data_source,
    }


def _run(costs_data=None, status_costs=None) -> str:
    """Invoke _render_cost_rollup inside a Click runner so its echo
    output is captured to a string we can assert against."""
    status_data = {"costs": status_costs} if status_costs is not None else {}
    runner = CliRunner()

    @click.command()
    def _cmd():
        _render_cost_rollup(status_data, costs_data)

    return runner.invoke(_cmd).output


class TestDataSourceWarning:
    """§17.289 — CLI surfaces the data_source=error case."""

    def test_ok_source_no_warning_line(self):
        out = _run(costs_data=_payload(data_source="ok", calls=0))
        assert "telemetry query failed" not in out

    def test_error_source_renders_warning_line(self):
        """The CLI prefixes the warning above the totals so a scripted
        consumer reading the output line-by-line can grep for the
        marker."""
        out = _run(costs_data=_payload(data_source="error", calls=0))
        assert "⚠ telemetry query failed" in out
        # The warning sits BETWEEN the "costs:" header and the "total:"
        # line so a tail-N scrape still surfaces it.
        idx_header = out.index("costs:")
        idx_warning = out.index("telemetry query failed")
        idx_total = out.index("total:")
        assert idx_header < idx_warning < idx_total

    def test_error_source_from_lightweight_status_block(self):
        """When the dedicated /jobs/{id}/costs call is skipped (no
        --costs flag), the helper falls back to status_data['costs'].
        That lightweight shape also carries data_source per §17.284.
        Pin that the warning fires from the fallback path too."""
        out = _run(
            costs_data=None,
            status_costs=_payload(data_source="error", calls=2, cost=0.0),
        )
        assert "telemetry query failed" in out

    def test_missing_data_source_defaults_to_no_warning(self):
        """Older orchestrators omit data_source — keep silent default."""
        payload = _payload(calls=1, cost=0.001)
        del payload["data_source"]
        out = _run(costs_data=payload)
        assert "telemetry query failed" not in out


class TestSourceShapeRegressionGuard:
    """§17.289 — anchor the CLI source so a drive-by refactor that
    removes the warning shows up in the test suite, not in production.
    """

    def test_cli_render_cost_rollup_reads_data_source(self):
        from scaffold_cli import main as cli_main

        with open(cli_main.__file__, encoding="utf-8") as f:
            src = f.read()

        assert 'totals.get("data_source") == "error"' in src, (
            "§17.289 regression: CLI `_render_cost_rollup` no longer "
            "checks `data_source`. The CLI cost output would stop "
            "distinguishing real-zero from fail-open-error rollups."
        )
        assert "telemetry query failed" in src, (
            "§17.289 regression: the CLI warning line for "
            "`data_source=error` is gone from cli/scaffold_cli/main.py."
        )
