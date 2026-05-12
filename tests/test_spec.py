"""
Unit tests for ``app.sim.spec`` — JSON-Schema-driven spec validation.

These are pure-Python tests (no DB, no sidecars), so they run in the
ci-smoke tier as well as the dev image. The fixtures below construct
specs by mutation from a known-valid baseline so each failure case
isolates exactly one rule violation.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from app.sim.spec import (
    CONSTRAINT_KINDS,
    CRITICALITY_LEVELS,
    DESIGN_KINDS,
    INTERFACE_DIRECTIONS,
    INTERFACE_KINDS,
    SCHEMA,
    SCHEMA_PATH,
    SCHEMA_VERSION,
    spec_sha256,
    validate_spec,
)


# ---------------------------------------------------------------------------
# Baseline valid specs
# ---------------------------------------------------------------------------

@pytest.fixture
def minimal_valid_spec() -> dict:
    """The smallest spec that passes validation: one design, one
    constraint, no interfaces / environment."""
    return {
        "schema_version": SCHEMA_VERSION,
        "design": {
            "name": "RC low-pass filter",
            "kind": "analog_circuit",
            "description": "First-order passive low-pass for audio band roll-off.",
        },
        "constraints": [
            {
                "id": "fc_3db",
                "kind": "electrical.frequency",
                "description": "-3 dB corner frequency.",
                "target": 1000.0,
                "tolerance_pct": 5.0,
                "unit": "Hz",
                "criticality": "required",
            }
        ],
    }


@pytest.fixture
def full_valid_spec(minimal_valid_spec: dict) -> dict:
    """A spec exercising every optional surface — multiple constraints,
    interfaces, environment ranges."""
    s = copy.deepcopy(minimal_valid_spec)
    s["constraints"].extend([
        {
            "id": "vpp_max",
            "kind": "electrical.voltage",
            "description": "Maximum peak-to-peak signal voltage.",
            "max": 3.3,
            "unit": "V",
            "criticality": "required",
        },
        {
            "id": "rin",
            "kind": "electrical.impedance",
            "description": "Input impedance must be at least 10 kohm.",
            "min": 10000.0,
            "unit": "ohm",
            "criticality": "preferred",
        },
    ])
    s["interfaces"] = [
        {
            "id": "vin",
            "direction": "input",
            "kind": "analog_voltage",
            "voltage_range_v": [0.0, 5.0],
        },
        {
            "id": "vout",
            "direction": "output",
            "kind": "analog_voltage",
        },
    ]
    s["environment"] = {
        "temperature_c": [0.0, 70.0],
        "supply_v": [3.0, 3.6],
    }
    return s


# ---------------------------------------------------------------------------
# Parity guard — Python enums must match the schema file.
# ---------------------------------------------------------------------------

@pytest.mark.smoke
def test_python_enums_mirror_schema_file():
    """The constants re-exported from ``app.sim.spec`` must stay in
    sync with ``spec_schema.json``. If a future commit adds a new
    constraint kind to the JSON but forgets to update the Python
    frozenset, callers that pattern-match against ``CONSTRAINT_KINDS``
    silently miss the new kind."""
    raw = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert set(raw["properties"]["design"]["properties"]["kind"]["enum"]) == DESIGN_KINDS
    constraint_enum = set(raw["$defs"]["constraint"]["properties"]["kind"]["enum"])
    assert constraint_enum == CONSTRAINT_KINDS
    assert set(raw["$defs"]["constraint"]["properties"]["criticality"]["enum"]) == CRITICALITY_LEVELS
    assert set(raw["$defs"]["interface"]["properties"]["direction"]["enum"]) == INTERFACE_DIRECTIONS
    assert set(raw["$defs"]["interface"]["properties"]["kind"]["enum"]) == INTERFACE_KINDS


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------

@pytest.mark.smoke
def test_minimal_spec_validates(minimal_valid_spec):
    result = validate_spec(minimal_valid_spec)
    assert result.ok is True
    assert result.errors == []


@pytest.mark.smoke
def test_full_spec_validates(full_valid_spec):
    result = validate_spec(full_valid_spec)
    assert result.ok is True, result.errors


@pytest.mark.smoke
def test_constraint_with_only_min_validates(minimal_valid_spec):
    minimal_valid_spec["constraints"][0] = {
        "id": "rin",
        "kind": "electrical.impedance",
        "description": "Input impedance lower bound.",
        "min": 10000.0,
        "unit": "ohm",
    }
    assert validate_spec(minimal_valid_spec).ok is True


@pytest.mark.smoke
def test_constraint_with_only_max_validates(minimal_valid_spec):
    minimal_valid_spec["constraints"][0] = {
        "id": "vpp_max",
        "kind": "electrical.voltage",
        "description": "Max output swing.",
        "max": 3.3,
        "unit": "V",
    }
    assert validate_spec(minimal_valid_spec).ok is True


# ---------------------------------------------------------------------------
# Structural error paths
# ---------------------------------------------------------------------------

@pytest.mark.smoke
def test_missing_schema_version_fails(minimal_valid_spec):
    del minimal_valid_spec["schema_version"]
    result = validate_spec(minimal_valid_spec)
    assert result.ok is False
    assert any("schema_version" in e.message for e in result.errors)


@pytest.mark.smoke
def test_missing_design_fails(minimal_valid_spec):
    del minimal_valid_spec["design"]
    result = validate_spec(minimal_valid_spec)
    assert result.ok is False
    assert any("design" in e.message for e in result.errors)


@pytest.mark.smoke
def test_empty_constraints_fails(minimal_valid_spec):
    minimal_valid_spec["constraints"] = []
    result = validate_spec(minimal_valid_spec)
    assert result.ok is False


@pytest.mark.smoke
def test_constraint_missing_unit_fails(minimal_valid_spec):
    del minimal_valid_spec["constraints"][0]["unit"]
    result = validate_spec(minimal_valid_spec)
    assert result.ok is False
    assert any("unit" in e.message for e in result.errors)


@pytest.mark.smoke
def test_constraint_missing_id_fails(minimal_valid_spec):
    del minimal_valid_spec["constraints"][0]["id"]
    result = validate_spec(minimal_valid_spec)
    assert result.ok is False


@pytest.mark.smoke
def test_constraint_without_target_min_max_fails(minimal_valid_spec):
    """A constraint that names a unit but provides no numeric anchor
    has nothing for verification to check against."""
    minimal_valid_spec["constraints"][0] = {
        "id": "vague",
        "kind": "electrical.voltage",
        "description": "Voltage matters here.",
        "unit": "V",
    }
    result = validate_spec(minimal_valid_spec)
    assert result.ok is False


@pytest.mark.smoke
def test_invalid_constraint_kind_rejected(minimal_valid_spec):
    minimal_valid_spec["constraints"][0]["kind"] = "electrical.magic"
    result = validate_spec(minimal_valid_spec)
    assert result.ok is False


@pytest.mark.smoke
def test_invalid_criticality_rejected(minimal_valid_spec):
    minimal_valid_spec["constraints"][0]["criticality"] = "must-have"
    result = validate_spec(minimal_valid_spec)
    assert result.ok is False


@pytest.mark.smoke
def test_tolerance_pct_out_of_range_rejected(minimal_valid_spec):
    minimal_valid_spec["constraints"][0]["tolerance_pct"] = 150.0
    result = validate_spec(minimal_valid_spec)
    assert result.ok is False


@pytest.mark.smoke
def test_constraint_id_pattern_rejected(minimal_valid_spec):
    """IDs must be lower-case snake — spec_sha256 dedup depends on
    them being deterministic and human-typeable."""
    minimal_valid_spec["constraints"][0]["id"] = "Fc-3dB"  # caps + hyphen
    result = validate_spec(minimal_valid_spec)
    assert result.ok is False


@pytest.mark.smoke
def test_invalid_schema_version_pattern_rejected(minimal_valid_spec):
    minimal_valid_spec["schema_version"] = "1.0"  # not semver
    result = validate_spec(minimal_valid_spec)
    assert result.ok is False


@pytest.mark.smoke
def test_unknown_top_level_property_rejected(minimal_valid_spec):
    """``additionalProperties: false`` everywhere is what blocks the
    extractor from sneaking unvalidated fields past us."""
    minimal_valid_spec["extra_metadata"] = {"foo": "bar"}
    result = validate_spec(minimal_valid_spec)
    assert result.ok is False


# ---------------------------------------------------------------------------
# Semantic (cross-field) error paths — JSON Schema can't express these.
# ---------------------------------------------------------------------------

@pytest.mark.smoke
def test_min_greater_than_max_rejected(minimal_valid_spec):
    minimal_valid_spec["constraints"][0] = {
        "id": "absurd",
        "kind": "electrical.voltage",
        "description": "Min above max — impossible to satisfy.",
        "min": 10.0,
        "max": 5.0,
        "unit": "V",
    }
    result = validate_spec(minimal_valid_spec)
    assert result.ok is False
    assert any("min" in e.message and "max" in e.message for e in result.errors)


@pytest.mark.smoke
def test_duplicate_constraint_ids_rejected(minimal_valid_spec):
    minimal_valid_spec["constraints"].append(
        copy.deepcopy(minimal_valid_spec["constraints"][0])
    )
    result = validate_spec(minimal_valid_spec)
    assert result.ok is False
    assert any("duplicate constraint id" in e.message for e in result.errors)


@pytest.mark.smoke
def test_duplicate_interface_ids_rejected(full_valid_spec):
    full_valid_spec["interfaces"].append(
        copy.deepcopy(full_valid_spec["interfaces"][0])
    )
    result = validate_spec(full_valid_spec)
    assert result.ok is False
    assert any("duplicate interface id" in e.message for e in result.errors)


@pytest.mark.smoke
def test_environment_inverted_range_rejected(full_valid_spec):
    full_valid_spec["environment"]["temperature_c"] = [70.0, 0.0]
    result = validate_spec(full_valid_spec)
    assert result.ok is False
    assert any("temperature_c" in e.path for e in result.errors)


# ---------------------------------------------------------------------------
# spec_sha256 stability
# ---------------------------------------------------------------------------

@pytest.mark.smoke
def test_spec_sha256_stable_across_key_order(minimal_valid_spec):
    """Two specs that differ only in dict key ordering must hash the
    same — the digest is the dedup key for the future specs table."""
    h1 = spec_sha256(minimal_valid_spec)
    reordered = {
        "constraints": minimal_valid_spec["constraints"],
        "design": minimal_valid_spec["design"],
        "schema_version": minimal_valid_spec["schema_version"],
    }
    h2 = spec_sha256(reordered)
    assert h1 == h2


@pytest.mark.smoke
def test_spec_sha256_differs_when_payload_changes(minimal_valid_spec):
    h1 = spec_sha256(minimal_valid_spec)
    minimal_valid_spec["constraints"][0]["target"] = 2000.0
    h2 = spec_sha256(minimal_valid_spec)
    assert h1 != h2


@pytest.mark.smoke
def test_spec_sha256_rejects_nan():
    import math
    with pytest.raises(ValueError):
        spec_sha256({"x": math.nan})


# ---------------------------------------------------------------------------
# Error-payload shape
# ---------------------------------------------------------------------------

@pytest.mark.smoke
def test_validation_result_paths_populated(minimal_valid_spec):
    del minimal_valid_spec["constraints"][0]["unit"]
    result = validate_spec(minimal_valid_spec)
    assert result.ok is False
    err = result.first_error()
    assert err is not None
    # Path should include the index into constraints[].
    assert "constraints" in err.path or err.path == "constraints/0"
