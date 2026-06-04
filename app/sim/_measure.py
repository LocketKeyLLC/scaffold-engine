"""Shared measurement coercion for the simulator sidecar wrappers (§17.412).

The ngspice/verilator/symbiyosys sidecars return measurement values as JSON.
Converting them with a bare ``float(v)`` has two failure modes:

  1. A non-numeric value (``"N/A"``, ``None``) raises ``ValueError``/``TypeError``
     uncaught, crashing the sizing iteration with an opaque trace.
  2. A non-finite value (``"nan"``, ``"inf"``) converts *successfully* and flows
     into the constraint checker, where ``nan`` comparisons are always ``False``
     — a failing constraint can silently read as 'met'.

``coerce_finite_measurements`` drops any key whose value is non-numeric or
non-finite, logging it, so only clean finite floats reach the constraint logic.
"""
from __future__ import annotations

import logging
import math
from typing import Any

logger = logging.getLogger("scaffold.sim")


def coerce_finite_measurements(raw: dict[str, Any] | None) -> dict[str, float]:
    """Coerce sidecar measurements to finite floats, dropping bad/non-finite keys."""
    out: dict[str, float] = {}
    for k, v in (raw or {}).items():
        try:
            f = float(v)
        except (TypeError, ValueError):
            logger.warning("sim_measurement_unparseable: key=%s value=%r", k, v)
            continue
        if not math.isfinite(f):
            logger.warning("sim_measurement_non_finite: key=%s value=%r", k, v)
            continue
        out[k] = f
    return out
