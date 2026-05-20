"""§17.182 — orchestrator-side Verilator adapter (app/sim/verilator.py).

Verilator's two-phase contract (build → run) means more failure modes than
ngspice: build can fail with the run never starting, or build can succeed
and the run can fail. The wrapper surfaces both in the dataclass and writes
``BUILD FAILED:\\n<stderr>`` into the audit row's ``stderr`` column on
build-fail so an auditor sees the relevant phase first (§17.141).

Same isolation strategy as test_sim_ngspice_adapter.py — httpx.MockTransport
for the sidecar, monkeypatched _insert_sim_run for the DB.
"""
from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from app.sim import verilator as verilator_mod


class _FakeInsert:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.uuid = uuid.uuid4()

    async def __call__(self, db, **kwargs):
        self.calls.append(kwargs)
        return self.uuid


def _make_client(handler) -> httpx.AsyncClient:
    transport = httpx.MockTransport(handler)
    return httpx.AsyncClient(transport=transport, base_url="http://verilator:8002")


@pytest.fixture
def patched_insert(monkeypatch):
    fake = _FakeInsert()
    monkeypatch.setattr(verilator_mod, "_insert_sim_run", fake)
    return fake


# ---------------------------------------------------------------------------
# Success — both build and run pass; measurements flow through.
# ---------------------------------------------------------------------------

async def test_success_propagates_both_phase_fields(monkeypatch, patched_insert):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/run"
        body = request.read()
        assert b"sv_source" in body and b"top_module" in body
        return httpx.Response(200, json={
            "ok": True,
            "exit_code": 0,
            "stdout": "[KPI count=16]\n- $finish at simtime 200ns\n",
            "stderr": "",
            "build_stdout": "verilator -Wall --binary tb.sv\n",
            "build_stderr": "",
            "measurements": {"count": 16.0, "cycles": 200.0},
            "duration_ms": 80,
            "build_duration_ms": 4200,
            "tool_version": "Verilator 5.024",
            "timed_out": False,
            "build_failed": False,
            "seed": 0,
        })

    client = _make_client(handler)
    monkeypatch.setattr(verilator_mod, "get_verilator_client", lambda: client)

    result = await verilator_mod.run_verilator(
        "module tb; initial $finish; endmodule\n",
        top_module="tb", db=AsyncMock(),
    )

    assert result.ok is True
    assert result.exit_code == 0
    assert result.measurements == {"count": 16.0, "cycles": 200.0}
    assert result.build_duration_ms == 4200
    assert result.tool_version == "Verilator 5.024"
    assert result.build_failed is False
    assert result.sim_run_id == patched_insert.uuid
    # No "BUILD FAILED" prefix on a clean run.
    assert "BUILD FAILED" not in patched_insert.calls[0]["stderr"]


# ---------------------------------------------------------------------------
# Build failure — build_stderr is what the auditor cares about; the wrapper
# rewrites the audit row's stderr column to "BUILD FAILED:\n<build_stderr>"
# (run phase never executes).
# ---------------------------------------------------------------------------

async def test_build_failure_surfaces_build_stderr_in_audit_row(
    monkeypatch, patched_insert,
):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "ok": False,
            "exit_code": 1,
            "stdout": "",
            "stderr": "",
            "build_stdout": "",
            "build_stderr": "%Error: tb.sv:3: undeclared identifier 'wrong_id'\n",
            "measurements": {},
            "duration_ms": 0,
            "build_duration_ms": 850,
            "tool_version": "Verilator 5.024",
            "timed_out": False,
            "build_failed": True,
            "seed": None,
        })

    client = _make_client(handler)
    monkeypatch.setattr(verilator_mod, "get_verilator_client", lambda: client)

    result = await verilator_mod.run_verilator(
        "module tb; logic x = wrong_id; endmodule\n",
        top_module="tb", db=AsyncMock(),
    )

    assert result.ok is False
    assert result.build_failed is True
    # On build-fail the audit row's stderr is the build stderr with prefix.
    audit_stderr = patched_insert.calls[0]["stderr"]
    assert audit_stderr.startswith("BUILD FAILED:\n")
    assert "undeclared identifier" in audit_stderr


# ---------------------------------------------------------------------------
# Run failure (build succeeded, simulation exited non-zero) — stderr passes
# through to audit row WITHOUT the BUILD FAILED prefix.
# ---------------------------------------------------------------------------

async def test_run_failure_does_not_get_build_prefix(monkeypatch, patched_insert):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "ok": False,
            "exit_code": 134,  # SIGABRT
            "stdout": "",
            "stderr": "Assertion failed at tb.sv:42\n",
            "build_stdout": "verilator -Wall --binary tb.sv\n",
            "build_stderr": "",
            "measurements": {},
            "duration_ms": 50,
            "build_duration_ms": 3000,
            "tool_version": "Verilator 5.024",
            "timed_out": False,
            "build_failed": False,  # build succeeded; run failed
            "seed": None,
        })

    client = _make_client(handler)
    monkeypatch.setattr(verilator_mod, "get_verilator_client", lambda: client)

    result = await verilator_mod.run_verilator(
        "module tb; initial $fatal; endmodule\n",
        top_module="tb", db=AsyncMock(),
    )

    assert result.ok is False
    assert result.build_failed is False
    audit_stderr = patched_insert.calls[0]["stderr"]
    assert not audit_stderr.startswith("BUILD FAILED")
    assert "Assertion failed" in audit_stderr


# ---------------------------------------------------------------------------
# Sidecar /run 500 → unreachable contract.
# ---------------------------------------------------------------------------

async def test_sidecar_500_yields_unreachable_result(monkeypatch, patched_insert):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client = _make_client(handler)
    monkeypatch.setattr(verilator_mod, "get_verilator_client", lambda: client)

    result = await verilator_mod.run_verilator(
        "module tb; endmodule\n", top_module="tb", db=AsyncMock(),
    )

    assert result.ok is False
    assert result.exit_code == -1
    assert "unreachable" in result.stderr.lower()
    assert result.build_failed is False


# ---------------------------------------------------------------------------
# Connect error / timeout → same fallback path.
# ---------------------------------------------------------------------------

async def test_network_failure_yields_unreachable_result(monkeypatch, patched_insert):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("unreachable")

    client = _make_client(handler)
    monkeypatch.setattr(verilator_mod, "get_verilator_client", lambda: client)
    result = await verilator_mod.run_verilator(
        "module tb; endmodule\n", top_module="tb", db=AsyncMock(),
    )
    assert result.ok is False
    assert "unreachable" in result.stderr.lower()


async def test_timeout_yields_unreachable_result(monkeypatch, patched_insert):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timeout")

    client = _make_client(handler)
    monkeypatch.setattr(verilator_mod, "get_verilator_client", lambda: client)
    result = await verilator_mod.run_verilator(
        "module tb; endmodule\n", top_module="tb", db=AsyncMock(),
    )
    assert result.ok is False


# ---------------------------------------------------------------------------
# Malformed JSON → unreachable.
# ---------------------------------------------------------------------------

async def test_malformed_json_yields_unreachable_result(monkeypatch, patched_insert):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json")

    client = _make_client(handler)
    monkeypatch.setattr(verilator_mod, "get_verilator_client", lambda: client)
    result = await verilator_mod.run_verilator(
        "module tb; endmodule\n", top_module="tb", db=AsyncMock(),
    )
    assert result.ok is False


# ---------------------------------------------------------------------------
# Input validation — only failure mode that DOES raise.
# ---------------------------------------------------------------------------

async def test_empty_sv_source_raises_value_error():
    with pytest.raises(ValueError, match="sv_source"):
        await verilator_mod.run_verilator("", top_module="tb", db=AsyncMock())


async def test_empty_top_module_raises_value_error():
    with pytest.raises(ValueError, match="top_module"):
        await verilator_mod.run_verilator(
            "module tb; endmodule\n", top_module="", db=AsyncMock(),
        )
