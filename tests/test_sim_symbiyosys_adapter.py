"""§17.182 — orchestrator-side SymbiYosys adapter (app/sim/symbiyosys.py).

SymbiYosys is the only sim adapter with a verdict column (PASS / FAIL /
UNKNOWN / TIMEOUT / ERROR — §17.142). The wrapper enforces three invariants
the audit (AUDIT.md 3.1) flagged as untested:

  * ``ok`` is True ONLY when verdict == "PASS" (no other value qualifies).
  * Unknown verdicts from the sidecar are coerced to "ERROR" (never trusted
    as-is, never silently passed through).
  * depth_reached, when present, surfaces as a "depth_reached" measurement
    in the audit row.

Test isolation: ``httpx.MockTransport`` for the sidecar, monkeypatched
``_insert_sim_run`` for the DB write.
"""
from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from app.sim import symbiyosys as sby_mod
from app.sim.symbiyosys import (
    VERDICT_ERROR,
    VERDICT_FAIL,
    VERDICT_PASS,
    VERDICT_TIMEOUT,
    VERDICT_UNKNOWN,
)


class _FakeInsert:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.uuid = uuid.uuid4()

    async def __call__(self, db, **kwargs):
        self.calls.append(kwargs)
        return self.uuid


def _make_client(handler) -> httpx.AsyncClient:
    transport = httpx.MockTransport(handler)
    return httpx.AsyncClient(transport=transport, base_url="http://symbiyosys:8003")


@pytest.fixture
def patched_insert(monkeypatch):
    fake = _FakeInsert()
    monkeypatch.setattr(sby_mod, "_insert_sim_run", fake)
    return fake


# ---------------------------------------------------------------------------
# PASS verdict — ok=True; depth_reached propagates to measurements.
# ---------------------------------------------------------------------------

async def test_pass_verdict_sets_ok_true_and_depth_measurement(
    monkeypatch, patched_insert,
):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/run"
        return httpx.Response(200, json={
            "verdict": "PASS",
            "exit_code": 0,
            "stdout": "Status: PASSED\n",
            "stderr": "",
            "duration_ms": 600,
            "tool_version": "sby 0.40 (yosys 0.40)",
            "timed_out": False,
            "seed": None,
            "depth_reached": 20,
            "counterexample_vcd_b64": None,
        })

    client = _make_client(handler)
    monkeypatch.setattr(sby_mod, "get_symbiyosys_client", lambda: client)

    result = await sby_mod.run_symbiyosys(
        "module tb; default clocking @(posedge clk); endclocking; endmodule\n",
        top_module="tb", db=AsyncMock(), mode="bmc", depth=20,
    )

    assert result.ok is True
    assert result.verdict == VERDICT_PASS
    assert result.depth_reached == 20
    # depth_reached must reach the audit row as a measurement.
    assert patched_insert.calls[0]["measurements"] == {"depth_reached": 20.0}
    assert patched_insert.calls[0]["verdict"] == VERDICT_PASS


# ---------------------------------------------------------------------------
# FAIL verdict — ok=False; counterexample_vcd_b64 set on dataclass but NOT
# persisted to sim_runs (waveform-artifact deferral per §17.140).
# ---------------------------------------------------------------------------

async def test_fail_verdict_carries_counterexample_in_dataclass_only(
    monkeypatch, patched_insert,
):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "verdict": "FAIL",
            "exit_code": 2,
            "stdout": "Status: FAILED\n",
            "stderr": "",
            "duration_ms": 800,
            "tool_version": "sby 0.40",
            "timed_out": False,
            "seed": None,
            "depth_reached": 7,
            "counterexample_vcd_b64": "VkNEX1ZFUlNJT05fMS4w",
        })

    client = _make_client(handler)
    monkeypatch.setattr(sby_mod, "get_symbiyosys_client", lambda: client)

    result = await sby_mod.run_symbiyosys(
        "module tb; assert property (1 == 0); endmodule\n",
        top_module="tb", db=AsyncMock(),
    )

    assert result.ok is False
    assert result.verdict == VERDICT_FAIL
    assert result.counterexample_vcd_b64 == "VkNEX1ZFUlNJT05fMS4w"
    # The audit row's measurement carries depth_reached but the VCD
    # blob does not appear anywhere in the persisted kwargs.
    audit = patched_insert.calls[0]
    assert audit["measurements"] == {"depth_reached": 7.0}
    assert "counterexample" not in str(audit).lower()


# ---------------------------------------------------------------------------
# Unknown verdict string from the sidecar → coerced to ERROR (defensive).
# ---------------------------------------------------------------------------

async def test_unrecognized_verdict_coerced_to_error(monkeypatch, patched_insert):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "verdict": "MAYBE",  # not in VALID_VERDICTS
            "exit_code": 99,
            "stdout": "",
            "stderr": "",
            "duration_ms": 10,
            "tool_version": "sby 0.40",
            "timed_out": False,
            "seed": None,
            "depth_reached": None,
        })

    client = _make_client(handler)
    monkeypatch.setattr(sby_mod, "get_symbiyosys_client", lambda: client)

    result = await sby_mod.run_symbiyosys(
        "module tb; endmodule\n", top_module="tb", db=AsyncMock(),
    )

    assert result.ok is False
    assert result.verdict == VERDICT_ERROR  # coerced
    assert patched_insert.calls[0]["verdict"] == VERDICT_ERROR


# ---------------------------------------------------------------------------
# Verdict casing: lowercase "pass" from a future sidecar variant is upper-
# cased before the VALID_VERDICTS check.
# ---------------------------------------------------------------------------

async def test_verdict_normalized_to_uppercase(monkeypatch, patched_insert):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "verdict": "pass",  # lowercase
            "exit_code": 0, "stdout": "", "stderr": "",
            "duration_ms": 1, "tool_version": "sby 0.40",
            "timed_out": False, "seed": None, "depth_reached": 5,
        })

    client = _make_client(handler)
    monkeypatch.setattr(sby_mod, "get_symbiyosys_client", lambda: client)

    result = await sby_mod.run_symbiyosys(
        "module tb; endmodule\n", top_module="tb", db=AsyncMock(),
    )
    assert result.verdict == VERDICT_PASS
    assert result.ok is True


# ---------------------------------------------------------------------------
# TIMEOUT verdict — ok stays False even though sidecar didn't crash.
# ---------------------------------------------------------------------------

async def test_timeout_verdict_keeps_ok_false(monkeypatch, patched_insert):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "verdict": "TIMEOUT",
            "exit_code": 8,
            "stdout": "",
            "stderr": "Engine timed out after 60s\n",
            "duration_ms": 60000,
            "tool_version": "sby 0.40",
            "timed_out": True,
            "seed": None,
            "depth_reached": 15,
        })

    client = _make_client(handler)
    monkeypatch.setattr(sby_mod, "get_symbiyosys_client", lambda: client)

    result = await sby_mod.run_symbiyosys(
        "module tb; endmodule\n", top_module="tb", db=AsyncMock(),
    )
    assert result.ok is False
    assert result.verdict == VERDICT_TIMEOUT
    assert result.timed_out is True


# ---------------------------------------------------------------------------
# UNKNOWN verdict (from the sby semantics, not the "verdict string we don't
# recognize" case) — keep ok=False, propagate verdict verbatim.
# ---------------------------------------------------------------------------

async def test_unknown_verdict_kept_as_unknown(monkeypatch, patched_insert):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "verdict": "UNKNOWN",
            "exit_code": 4, "stdout": "", "stderr": "",
            "duration_ms": 100, "tool_version": "sby 0.40",
            "timed_out": False, "seed": None, "depth_reached": None,
        })

    client = _make_client(handler)
    monkeypatch.setattr(sby_mod, "get_symbiyosys_client", lambda: client)

    result = await sby_mod.run_symbiyosys(
        "module tb; endmodule\n", top_module="tb", db=AsyncMock(),
    )
    assert result.ok is False
    assert result.verdict == VERDICT_UNKNOWN


# ---------------------------------------------------------------------------
# Sidecar 500 → unreachable contract; verdict = ERROR.
# ---------------------------------------------------------------------------

async def test_sidecar_500_yields_error_verdict(monkeypatch, patched_insert):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client = _make_client(handler)
    monkeypatch.setattr(sby_mod, "get_symbiyosys_client", lambda: client)

    result = await sby_mod.run_symbiyosys(
        "module tb; endmodule\n", top_module="tb", db=AsyncMock(),
    )
    assert result.ok is False
    assert result.verdict == VERDICT_ERROR
    assert "unreachable" in result.stderr.lower()


async def test_network_failure_yields_error_verdict(monkeypatch, patched_insert):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    client = _make_client(handler)
    monkeypatch.setattr(sby_mod, "get_symbiyosys_client", lambda: client)
    result = await sby_mod.run_symbiyosys(
        "module tb; endmodule\n", top_module="tb", db=AsyncMock(),
    )
    assert result.verdict == VERDICT_ERROR
    assert result.ok is False


async def test_timeout_yields_error_verdict(monkeypatch, patched_insert):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timeout")

    client = _make_client(handler)
    monkeypatch.setattr(sby_mod, "get_symbiyosys_client", lambda: client)
    result = await sby_mod.run_symbiyosys(
        "module tb; endmodule\n", top_module="tb", db=AsyncMock(),
    )
    assert result.verdict == VERDICT_ERROR


async def test_malformed_json_yields_error_verdict(monkeypatch, patched_insert):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json")

    client = _make_client(handler)
    monkeypatch.setattr(sby_mod, "get_symbiyosys_client", lambda: client)
    result = await sby_mod.run_symbiyosys(
        "module tb; endmodule\n", top_module="tb", db=AsyncMock(),
    )
    assert result.verdict == VERDICT_ERROR


# ---------------------------------------------------------------------------
# Input validation.
# ---------------------------------------------------------------------------

async def test_empty_sv_source_raises_value_error():
    with pytest.raises(ValueError, match="sv_source"):
        await sby_mod.run_symbiyosys("", top_module="tb", db=AsyncMock())


async def test_empty_top_module_raises_value_error():
    with pytest.raises(ValueError, match="top_module"):
        await sby_mod.run_symbiyosys(
            "module tb; endmodule\n", top_module="", db=AsyncMock(),
        )


# ---------------------------------------------------------------------------
# depth_reached=None → measurements stays empty (no spurious 0.0 entry).
# ---------------------------------------------------------------------------

async def test_no_depth_means_empty_measurements(monkeypatch, patched_insert):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "verdict": "PASS",
            "exit_code": 0, "stdout": "", "stderr": "",
            "duration_ms": 1, "tool_version": "sby 0.40",
            "timed_out": False, "seed": None,
            "depth_reached": None,
        })

    client = _make_client(handler)
    monkeypatch.setattr(sby_mod, "get_symbiyosys_client", lambda: client)
    result = await sby_mod.run_symbiyosys(
        "module tb; endmodule\n", top_module="tb", db=AsyncMock(),
    )
    assert result.depth_reached is None
    assert patched_insert.calls[0]["measurements"] == {}
