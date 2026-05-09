"""Unit tests for tests/benchmarks/bench_check.py — the regression gate.

The bench scripts themselves need live Ollama/Milvus, so they're
exercised via `make bench-*` (validate-marker territory). The check
tool is pure stdlib — testable in isolation.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


def _load_bench_check():
    """Load bench_check.py via importlib so we don't need it to be a
    package member. Mirrors the pattern in tests/_scaffold_router_setup.py."""
    src = Path(__file__).resolve().parent.parent / "tests" / "benchmarks" / "bench_check.py"
    spec = importlib.util.spec_from_file_location("bench_check", src)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load_bench_check()


# ---------------------------------------------------------------------------
# _resolve — dotted/indexed JSON path
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestResolve:
    def test_simple_key(self, mod):
        rec = {"summary": {"warm_mean_ms": 245.6}}
        assert mod._resolve(rec, "summary.warm_mean_ms") == 245.6

    def test_list_index(self, mod):
        rec = {"raw_inference": [{"eval_tps": 3.6}, {"eval_tps": 6.2}]}
        assert mod._resolve(rec, "raw_inference.0.eval_tps") == 3.6
        assert mod._resolve(rec, "raw_inference.1.eval_tps") == 6.2

    def test_missing_key_returns_none(self, mod):
        assert mod._resolve({"a": 1}, "b") is None
        assert mod._resolve({"a": {"b": 1}}, "a.c") is None

    def test_out_of_bounds_index_returns_none(self, mod):
        assert mod._resolve({"x": [1, 2]}, "x.5") is None

    def test_non_int_index_on_list_returns_none(self, mod):
        assert mod._resolve({"x": [1, 2]}, "x.foo") is None


# ---------------------------------------------------------------------------
# _is_regression — direction + threshold logic
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestIsRegression:
    def test_up_direction_within_threshold_no_regression(self, mod):
        # latency 1100 vs baseline 1000 with 1.5x cap → 1.1 ratio, OK.
        assert mod._is_regression(1100, 1000, 1.5, "up") is False

    def test_up_direction_exceeds_threshold_regression(self, mod):
        # latency 1600 vs baseline 1000 with 1.5x cap → 1.6 ratio, REGRESSION.
        assert mod._is_regression(1600, 1000, 1.5, "up") is True

    def test_up_direction_at_threshold_not_regression(self, mod):
        """Ratio == threshold → not strictly > threshold, so OK."""
        assert mod._is_regression(1500, 1000, 1.5, "up") is False

    def test_down_direction_within_threshold_no_regression(self, mod):
        # tps 8.0 vs baseline 10.0 with 0.7 cap → 0.8 ratio, above 0.7, OK.
        assert mod._is_regression(8.0, 10.0, 0.7, "down") is False

    def test_down_direction_below_threshold_regression(self, mod):
        # tps 6.0 vs baseline 10.0 with 0.7 cap → 0.6 ratio, REGRESSION.
        assert mod._is_regression(6.0, 10.0, 0.7, "down") is True

    def test_zero_baseline_never_regresses(self, mod):
        """A zero baseline (e.g. cached_mean_ms when cache is fully hot)
        would explode the ratio. Don't false-fire."""
        assert mod._is_regression(50, 0, 1.5, "up") is False

    def test_unknown_direction_returns_false(self, mod):
        """Defensive: don't fail-fire on a typo direction."""
        assert mod._is_regression(2000, 1000, 1.5, "sideways") is False


# ---------------------------------------------------------------------------
# Integration via the CLI main() function
# ---------------------------------------------------------------------------


def _write_jsonl(path: Path, records: list[dict]) -> None:
    with open(path, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


@pytest.mark.smoke
class TestMain:
    def test_skips_when_only_one_run(self, mod, tmp_path, monkeypatch, capsys):
        f = tmp_path / "results.jsonl"
        _write_jsonl(f, [{"summary": {"latency_ms": 100}}])
        monkeypatch.setattr(
            "sys.argv",
            ["bench_check", "--file", str(f), "--metric", "summary.latency_ms",
             "--threshold", "1.5", "--direction", "up"],
        )
        rc = mod.main()
        assert rc == 0
        out = capsys.readouterr().out
        assert "not enough runs" in out

    def test_returns_2_on_regression(self, mod, tmp_path, monkeypatch, capsys):
        """3 prior runs around 100ms; latest is 200ms (2.0x) > threshold 1.5x."""
        f = tmp_path / "results.jsonl"
        _write_jsonl(f, [
            {"summary": {"latency_ms": 95}},
            {"summary": {"latency_ms": 105}},
            {"summary": {"latency_ms": 100}},
            {"summary": {"latency_ms": 200}},  # latest — regressed
        ])
        monkeypatch.setattr(
            "sys.argv",
            ["bench_check", "--file", str(f), "--metric", "summary.latency_ms",
             "--threshold", "1.5", "--direction", "up"],
        )
        rc = mod.main()
        assert rc == 2
        out = capsys.readouterr().out
        assert "REGRESSION" in out
        assert "ratio=2.00" in out

    def test_returns_0_when_within_threshold(self, mod, tmp_path, monkeypatch, capsys):
        f = tmp_path / "results.jsonl"
        _write_jsonl(f, [
            {"summary": {"latency_ms": 95}},
            {"summary": {"latency_ms": 105}},
            {"summary": {"latency_ms": 100}},
            {"summary": {"latency_ms": 130}},  # latest — 1.3x, under 1.5x cap
        ])
        monkeypatch.setattr(
            "sys.argv",
            ["bench_check", "--file", str(f), "--metric", "summary.latency_ms",
             "--threshold", "1.5", "--direction", "up"],
        )
        rc = mod.main()
        assert rc == 0
        out = capsys.readouterr().out
        assert "OK on" in out

    def test_uses_median_of_prior_runs_not_last(self, mod, tmp_path, monkeypatch, capsys):
        """Median beats last-only because one outlier shouldn't tank the gate.
        Prior runs: [100, 100, 1000]. Median = 100. Latest = 200 → 2.0x → regression.
        If we'd compared to the last run (1000), 200/1000 = 0.2x → no regression
        — and that would be wrong, hiding a real regression behind an outlier.
        """
        f = tmp_path / "results.jsonl"
        _write_jsonl(f, [
            {"summary": {"latency_ms": 100}},
            {"summary": {"latency_ms": 100}},
            {"summary": {"latency_ms": 1000}},  # outlier prior run
            {"summary": {"latency_ms": 200}},   # latest
        ])
        monkeypatch.setattr(
            "sys.argv",
            ["bench_check", "--file", str(f), "--metric", "summary.latency_ms",
             "--threshold", "1.5", "--direction", "up"],
        )
        rc = mod.main()
        assert rc == 2

    def test_throughput_metric_with_down_direction(self, mod, tmp_path, monkeypatch, capsys):
        """Throughput regression: prior runs ~10 tps, latest 6 tps, threshold 0.7."""
        f = tmp_path / "results.jsonl"
        _write_jsonl(f, [
            {"raw_inference": [{"eval_tps": 9.5}]},
            {"raw_inference": [{"eval_tps": 10.5}]},
            {"raw_inference": [{"eval_tps": 10.0}]},
            {"raw_inference": [{"eval_tps": 6.0}]},  # 0.6x median, below 0.7
        ])
        monkeypatch.setattr(
            "sys.argv",
            ["bench_check", "--file", str(f),
             "--metric", "raw_inference.0.eval_tps",
             "--threshold", "0.7", "--direction", "down"],
        )
        rc = mod.main()
        assert rc == 2

    def test_skips_when_metric_missing(self, mod, tmp_path, monkeypatch, capsys):
        f = tmp_path / "results.jsonl"
        _write_jsonl(f, [{"summary": {}}, {"summary": {}}])
        monkeypatch.setattr(
            "sys.argv",
            ["bench_check", "--file", str(f),
             "--metric", "summary.does_not_exist",
             "--threshold", "1.5", "--direction", "up"],
        )
        rc = mod.main()
        assert rc == 0
        out = capsys.readouterr().out
        assert "not found" in out
