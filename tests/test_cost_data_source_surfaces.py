"""§17.289 — `/cost` chat command surfaces the `data_source` flag.

§17.280-UX-3 audit-tail concern: §17.284 added `data_source: "ok" |
"error"` to every rollup return dict, but `pipelines/scaffold_router.py
::_handle_cost` still rendered zero-totals from an error-source rollup
identically to a real "no calls yet" rollup. A busy job on a broken
telemetry path looked like a fresh job.

§17.289 surfaces the flag in the chat by prepending a ⚠️ banner to the
cost header when `data_source == "error"`, with a "re-run or check
logs" hint. The CLI side (`scaffold_cli/_render_cost_rollup`) gets the
matching warning line — pinned in `cli/tests/test_cost_data_source.py`
which runs in the CLI's separate venv via `make test-cli`.

These tests pin the chat surface against drift.
"""
from unittest.mock import MagicMock, patch

import pytest

from tests._scaffold_router_setup import Pipeline


@pytest.fixture
def pipe():
    return Pipeline()


def _cost_payload(*, data_source: str = "ok", calls: int = 0, cost: float = 0.0):
    """Build a /jobs/{id}/costs response payload."""
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


@pytest.mark.smoke
class TestChatCostHandlerSurface:
    """§17.289 — `/cost` chat command surfaces data_source."""

    def test_ok_source_zero_calls_emits_no_warning(self, pipe):
        """data_source="ok" + 0 calls → "no LLM calls logged" message,
        NO error banner (zeros are real)."""
        with patch("scaffold_router._HTTP_SESSION.get") as mg:
            resp = MagicMock(status_code=200)
            resp.json.return_value = _cost_payload(data_source="ok", calls=0)
            mg.return_value = resp
            out = pipe._handle_cost(["/cost", "job-fresh-1"])
        assert "no LLM calls logged" in out
        assert "Telemetry query failed" not in out

    def test_error_source_zero_calls_emits_warning_banner(self, pipe):
        """§17.289 load-bearing case: zero numbers PLUS data_source="error"
        means the rollup query failed and the zeros are a fallback. The
        operator must see the warning so they don't read it as "no
        calls yet".
        """
        with patch("scaffold_router._HTTP_SESSION.get") as mg:
            resp = MagicMock(status_code=200)
            resp.json.return_value = _cost_payload(data_source="error", calls=0)
            mg.return_value = resp
            out = pipe._handle_cost(["/cost", "job-broken-1"])
        assert "Telemetry query failed" in out
        # The remediation hint should name the same job_id so the
        # operator can copy-paste the re-run line.
        assert "/cost job-broken-1" in out
        # The "no calls" fallback message can coexist — it's still true
        # that no calls were COUNTED. But the operator now sees BOTH
        # the warning AND that context.
        assert "no LLM calls logged" in out

    def test_error_source_with_real_data_still_warns(self, pipe):
        """When the rollup DID get partial data (e.g. composite-error
        case from §17.284 where totals succeeded but breakdown failed),
        the chat surface still warns — the operator should not trust the
        figures without context.
        """
        payload = _cost_payload(data_source="error", calls=5, cost=0.0234)
        payload["total_prompt_tokens"] = 5000
        payload["total_completion_tokens"] = 2000
        payload["total_latency_ms"] = 12000
        with patch("scaffold_router._HTTP_SESSION.get") as mg:
            resp = MagicMock(status_code=200)
            resp.json.return_value = payload
            mg.return_value = resp
            out = pipe._handle_cost(["/cost", "job-partial-1"])
        assert "Telemetry query failed" in out
        # Real numbers still rendered.
        assert "0.0234" in out
        assert "5 calls" in out

    def test_ok_source_with_real_data_emits_no_warning(self, pipe):
        """Happy path — data_source="ok" with real numbers renders the
        standard cost block, no warning banner."""
        payload = _cost_payload(data_source="ok", calls=10, cost=0.05)
        payload["total_latency_ms"] = 30000
        with patch("scaffold_router._HTTP_SESSION.get") as mg:
            resp = MagicMock(status_code=200)
            resp.json.return_value = payload
            mg.return_value = resp
            out = pipe._handle_cost(["/cost", "job-healthy-1"])
        assert "Telemetry query failed" not in out
        assert "0.0500" in out
        assert "10 calls" in out

    def test_missing_data_source_defaults_to_ok_behavior(self, pipe):
        """Older orchestrators (pre-§17.284) won't emit `data_source`.
        The chat surface must default to the no-warning shape — silent
        backward compatibility for the pre-flag payload."""
        payload = _cost_payload(calls=3, cost=0.001)
        del payload["data_source"]
        with patch("scaffold_router._HTTP_SESSION.get") as mg:
            resp = MagicMock(status_code=200)
            resp.json.return_value = payload
            mg.return_value = resp
            out = pipe._handle_cost(["/cost", "job-legacy-1"])
        assert "Telemetry query failed" not in out


@pytest.mark.smoke
class TestSourceShapeRegressionGuard:
    """§17.289 — anchor the chat surface so a drive-by refactor that
    removes the warning shows up in the test suite, not in production.
    The CLI-side anchor lives in cli/tests/test_cost_data_source.py
    (runs in the CLI's own venv via `make test-cli`).
    """

    def test_chat_handle_cost_reads_data_source(self):
        from pipelines import scaffold_router

        with open(scaffold_router.__file__, encoding="utf-8") as f:
            src = f.read()

        assert "data_source = data.get(\"data_source\", \"ok\")" in src, (
            "§17.289 regression: `_handle_cost` no longer reads "
            "`data_source` from the response. The chat surface would "
            "stop distinguishing real-zero from fail-open-error rollups."
        )
        assert "Telemetry query failed" in src, (
            "§17.289 regression: the operator-facing warning banner "
            "(`Telemetry query failed`) is gone from scaffold_router.py."
        )
