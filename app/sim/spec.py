"""
Spec-capture validator — front end of the engineering-design pipeline.

The contract: a *spec* is a JSON document describing a design's
quantitative requirements (constraints, interfaces, environment). The
JSON Schema at ``app/sim/spec_schema.json`` is the single source of
truth — it is what gets validated AND what gets handed to the (future)
LLM extractor as a prompt fragment, so there is no shape drift between
"what we accept" and "what the LLM produces."

Design contract (§17.143):

  * ``validate_spec(d)`` never raises on a bad payload. Validation
    failures surface as ``SpecValidationResult(ok=False, errors=[...])``
    with one ``SpecValidationError`` per schema violation, each carrying
    a JSON-pointer-style path and a human-readable reason. Callers (the
    extractor, the /confirm gate, ad-hoc operators) treat failures as
    data, not exceptions — same posture as the simulator wrappers
    (§17.140 / 141 / 142).
  * ``spec_sha256(d)`` produces a stable digest over the canonical JSON
    form (sorted keys, no whitespace). Two semantically-equal specs
    that differ only in key ordering hash identically — which is what
    the future ``specs.spec_sha256`` column needs for dedup / cache.
  * The schema is loaded once at import time. Re-reading on every
    validate call would invite filesystem races inside the orchestrator
    container's read-only rootfs.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

logger = logging.getLogger("scaffold")

SCHEMA_PATH = Path(__file__).parent / "spec_schema.json"

# Re-exports for callers that want to validate enum membership without
# loading the schema themselves. Kept in sync with spec_schema.json by
# the parity test in tests/test_spec.py.
DESIGN_KINDS = frozenset({
    "analog_circuit",
    "digital_logic",
    "mixed_signal",
    "pcb",
    "system",
})

CONSTRAINT_KINDS = frozenset({
    "electrical.voltage",
    "electrical.current",
    "electrical.power",
    "electrical.frequency",
    "electrical.impedance",
    "electrical.gain",
    "electrical.capacitance",
    "electrical.inductance",
    "electrical.resistance",
    "electrical.charge",
    "timing.frequency",
    "timing.period",
    "timing.setup",
    "timing.hold",
    "timing.latency",
    "timing.jitter",
    "timing.rise_time",
    "timing.fall_time",
    "thermal.max_temp",
    "thermal.ambient_temp",
    "thermal.dissipation",
    "signal.thd",
    "signal.snr",
    "signal.sndr",
    "signal.enob",
    "signal.sfdr",
    "signal.noise_floor",
    "physical.area",
    "physical.weight",
    "physical.height",
    "physical.width",
    "physical.length",
    "cost.bom_usd",
    "cost.manufacturing_usd",
})

CRITICALITY_LEVELS = frozenset({"required", "preferred", "best_effort"})
INTERFACE_DIRECTIONS = frozenset({"input", "output", "inout"})
INTERFACE_KINDS = frozenset({
    "analog_voltage", "analog_current", "digital_logic",
    "clock", "reset", "power", "ground", "bus",
})


def _load_schema() -> dict[str, Any]:
    with SCHEMA_PATH.open("r", encoding="utf-8") as fh:
        return json.load(fh)


SCHEMA: dict[str, Any] = _load_schema()
SCHEMA_VERSION: str = "1.0.0"

# Pre-compile the validator at import time so per-call validation is
# zero-allocation other than the result objects.
_VALIDATOR = Draft202012Validator(SCHEMA)


@dataclass(frozen=True)
class SpecValidationError:
    """One schema violation. ``path`` is a slash-joined JSON pointer
    (e.g. ``constraints/0/unit``); ``message`` is the human-readable
    reason from jsonschema."""
    path: str
    message: str


@dataclass
class SpecValidationResult:
    ok: bool
    errors: list[SpecValidationError] = field(default_factory=list)

    def first_error(self) -> SpecValidationError | None:
        return self.errors[0] if self.errors else None


def _format_path(path_parts: tuple[Any, ...]) -> str:
    if not path_parts:
        return "<root>"
    return "/".join(str(p) for p in path_parts)


def validate_spec(spec: dict[str, Any]) -> SpecValidationResult:
    """Validate ``spec`` against the JSON Schema.

    Never raises on a malformed payload. Returns a populated
    ``SpecValidationResult`` with one entry per violation. Cross-field
    rules beyond the schema (constraint range coherence, environment
    range coherence) are checked AFTER the schema pass so callers see
    the structural errors first.
    """
    schema_errors: list[SpecValidationError] = []
    for err in _VALIDATOR.iter_errors(spec):
        schema_errors.append(
            SpecValidationError(
                path=_format_path(tuple(err.absolute_path)),
                message=err.message,
            )
        )

    if schema_errors:
        return SpecValidationResult(ok=False, errors=schema_errors)

    semantic_errors = _check_semantics(spec)
    return SpecValidationResult(
        ok=(not semantic_errors),
        errors=semantic_errors,
    )


def _check_semantics(spec: dict[str, Any]) -> list[SpecValidationError]:
    """Cross-field rules JSON Schema cannot express cleanly.

    Currently:
      1. Constraint with both ``min`` and ``max``: ``min <= max``.
      2. Constraint id uniqueness within the spec.
      3. Interface id uniqueness within the spec.
      4. ``environment.*`` ranges: ``range[0] <= range[1]``.
    """
    errors: list[SpecValidationError] = []

    seen_constraint_ids: set[str] = set()
    for i, c in enumerate(spec.get("constraints", [])):
        cid = c.get("id")
        if cid and cid in seen_constraint_ids:
            errors.append(
                SpecValidationError(
                    path=f"constraints/{i}/id",
                    message=f"duplicate constraint id {cid!r}",
                )
            )
        elif cid:
            seen_constraint_ids.add(cid)
        if "min" in c and "max" in c and c["min"] > c["max"]:
            errors.append(
                SpecValidationError(
                    path=f"constraints/{i}",
                    message=f"min ({c['min']}) > max ({c['max']})",
                )
            )

    seen_interface_ids: set[str] = set()
    for i, iface in enumerate(spec.get("interfaces", []) or []):
        iid = iface.get("id")
        if iid and iid in seen_interface_ids:
            errors.append(
                SpecValidationError(
                    path=f"interfaces/{i}/id",
                    message=f"duplicate interface id {iid!r}",
                )
            )
        elif iid:
            seen_interface_ids.add(iid)

    env = spec.get("environment") or {}
    for key, rng in env.items():
        if isinstance(rng, list) and len(rng) == 2 and rng[0] > rng[1]:
            errors.append(
                SpecValidationError(
                    path=f"environment/{key}",
                    message=f"range[0] ({rng[0]}) > range[1] ({rng[1]})",
                )
            )

    return errors


def spec_sha256(spec: dict[str, Any]) -> str:
    """SHA-256 over the canonical JSON form of ``spec``.

    Canonical = sorted keys, no insignificant whitespace, no NaN. Two
    payloads that differ only in key ordering hash identically. Used
    for ``specs.spec_sha256`` (dedup + cache key).
    """
    canonical = json.dumps(
        spec,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
