"""Tests for app/sim/_measure.coerce_finite_measurements (§17.412, ext-review E3).

The simulator sidecars return measurement values as JSON. A bare float()
either crashes on non-numeric values or silently admits NaN/inf — which read
as 'constraint met' because NaN comparisons are always False. The coercion
must drop both classes and keep only finite floats.
"""
from __future__ import annotations

import math

import pytest

from app.sim._measure import coerce_finite_measurements


def test_keeps_finite_floats_and_ints():
    out = coerce_finite_measurements({"gain": "12.5", "vout": 1, "ib": 3.3e-6})
    assert out == {"gain": 12.5, "vout": 1.0, "ib": 3.3e-6}
    assert all(isinstance(v, float) for v in out.values())


@pytest.mark.parametrize("bad", ["nan", "inf", "-inf", "Infinity", float("nan"), float("inf")])
def test_drops_non_finite(bad):
    out = coerce_finite_measurements({"good": "1.0", "bad": bad})
    assert "bad" not in out, f"non-finite {bad!r} must be dropped, not admitted"
    assert out == {"good": 1.0}


@pytest.mark.parametrize("bad", ["N/A", "", None, "abc", {"x": 1}, [1, 2]])
def test_drops_unparseable(bad):
    out = coerce_finite_measurements({"good": 2, "bad": bad})
    assert out == {"good": 2.0}


def test_empty_and_none_inputs():
    assert coerce_finite_measurements({}) == {}
    assert coerce_finite_measurements(None) == {}


def test_no_nan_survives_into_output():
    out = coerce_finite_measurements({"a": float("nan"), "b": "5"})
    assert not any(math.isnan(v) for v in out.values())
    assert out == {"b": 5.0}
