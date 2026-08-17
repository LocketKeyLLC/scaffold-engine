"""Tests for app/utils/progress.py — ProgressTracker / EmitThrottle (§17.811)."""
import pytest

from app.utils.progress import EmitThrottle, ProgressTracker, humanize_ms


class FakeClock:
    """Injectable monotonic clock for deterministic ETA math."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> float:
        self.t += dt
        return self.t


# ---------------------------------------------------------------------------
# humanize_ms
# ---------------------------------------------------------------------------
@pytest.mark.smoke
@pytest.mark.parametrize(
    "ms,expected",
    [
        (None, "unknown"),
        (0, "0s"),
        (5000, "5s"),
        (65000, "1m 05s"),
        (200000, "3m 20s"),
        (3900000, "1h 05m"),
        (-100, "0s"),
    ],
)
def test_humanize_ms(ms, expected):
    assert humanize_ms(ms) == expected


# ---------------------------------------------------------------------------
# Cold start
# ---------------------------------------------------------------------------
@pytest.mark.smoke
def test_cold_start_has_no_eta():
    clk = FakeClock()
    pt = ProgressTracker(4, phase="executing", unit="nodes", clock=clk)
    snap = pt.snapshot()
    assert snap["completed"] == 0
    assert snap["pct"] == 0
    assert snap["eta_ms"] is None
    assert snap["eta_human"] is None
    assert snap["summary"] == "0/4 nodes · 0%"


# ---------------------------------------------------------------------------
# EWMA ETA
# ---------------------------------------------------------------------------
@pytest.mark.smoke
def test_eta_from_uniform_rate():
    clk = FakeClock()
    pt = ProgressTracker(4, phase="executing", unit="nodes", alpha=0.5, clock=clk)
    clk.advance(10)
    snap = pt.tick(1, current_item="auth", done_items=["scaffold"])
    # 10s for the first unit → 3 remaining × 10s = 30s
    assert snap["completed"] == 1
    assert snap["pct"] == 25
    assert snap["eta_ms"] == 30000
    assert snap["eta_human"] == "~30s"
    assert snap["current_item"] == "auth"
    assert snap["done_items"] == ["scaffold"]

    clk.advance(10)
    snap = pt.tick(2)
    assert snap["completed"] == 2
    assert snap["eta_ms"] == 20000  # steady 10s/unit × 2 remaining
    assert snap["summary"] == "2/4 nodes · 50% · ~20s left"


@pytest.mark.smoke
def test_ewma_reacts_to_slowdown():
    clk = FakeClock()
    pt = ProgressTracker(10, phase="x", alpha=0.5, clock=clk)
    clk.advance(2)
    pt.tick(1)  # ewma = 2s
    clk.advance(10)
    snap = pt.tick(2)  # ewma = 0.5*10 + 0.5*2 = 6s → 8 remaining × 6s = 48s
    assert snap["eta_ms"] == 48000


@pytest.mark.smoke
def test_default_tick_increments():
    clk = FakeClock()
    pt = ProgressTracker(3, phase="x", clock=clk)
    clk.advance(5)
    snap = pt.tick()  # no arg → +1
    assert snap["completed"] == 1


@pytest.mark.smoke
def test_stale_count_updates_labels_without_perturbing_rate():
    clk = FakeClock()
    pt = ProgressTracker(4, phase="x", alpha=0.5, clock=clk)
    clk.advance(10)
    pt.tick(1)
    eta_before = pt.snapshot()["eta_ms"]
    clk.advance(100)  # long idle, but no forward progress
    snap = pt.tick(1, current_item="still-node-1")  # same count
    assert snap["current_item"] == "still-node-1"
    assert snap["eta_ms"] == eta_before  # rate untouched by a non-advancing tick


# ---------------------------------------------------------------------------
# Soft total
# ---------------------------------------------------------------------------
@pytest.mark.smoke
def test_soft_total_renders_upper_bound():
    clk = FakeClock()
    pt = ProgressTracker(
        10, phase="researching", unit="iterations", soft_total=True, clock=clk
    )
    clk.advance(20)
    snap = pt.tick(1)
    assert snap["soft"] is True
    assert snap["eta_human"].startswith("≤")


# ---------------------------------------------------------------------------
# Unknown / zero total
# ---------------------------------------------------------------------------
@pytest.mark.smoke
def test_zero_total_degrades_gracefully():
    clk = FakeClock()
    pt = ProgressTracker(0, phase="x", unit="docs", clock=clk)
    assert pt.snapshot()["pct"] is None
    clk.advance(5)
    snap = pt.tick(1)
    assert snap["eta_ms"] is None
    assert snap["summary"] == "1/? docs"


# ---------------------------------------------------------------------------
# Completion
# ---------------------------------------------------------------------------
@pytest.mark.smoke
def test_completion_zeroes_eta():
    clk = FakeClock()
    pt = ProgressTracker(2, phase="x", clock=clk)
    clk.advance(5)
    pt.tick(1)
    clk.advance(5)
    snap = pt.tick(2)
    assert snap["pct"] == 100
    assert snap["eta_ms"] == 0


# ---------------------------------------------------------------------------
# EmitThrottle
# ---------------------------------------------------------------------------
@pytest.mark.smoke
def test_throttle_first_and_interval_and_final():
    clk = FakeClock()
    thr = EmitThrottle(5.0, clock=clk)
    assert thr.ready() is True  # first always fires
    assert thr.ready() is False  # too soon
    clk.advance(5)
    assert thr.ready() is True  # interval elapsed
    clk.advance(1)
    assert thr.ready(final=True) is True  # final always fires
    assert thr.ready() is False  # ...but final didn't reset early


@pytest.mark.smoke
def test_throttle_zero_interval_always_ready():
    clk = FakeClock()
    thr = EmitThrottle(0.0, clock=clk)
    assert thr.ready() is True
    assert thr.ready() is True
