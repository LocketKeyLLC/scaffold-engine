"""§17.182 — orchestrator-side ngspice adapter (app/sim/ngspice.py).

The adapter marshals a SPICE netlist, posts to the scaffold-ngspice sidecar,
parses the JSON response into an ``NgspiceResult``, and writes a ``sim_runs``
audit row before returning. The contract (§17.140) is "never raise on sim
failure — every failure mode surfaces as ``ok=False`` with audit row intact."
These tests cover the failure modes the audit (AUDIT.md 3.1) flagged as
untested: success, sidecar 500, network timeout, malformed JSON, verdict /
measurement coercion, and input validation.

Implementation note. The sidecar HTTP layer is mocked via
``httpx.MockTransport`` — a real httpx.AsyncClient with a stubbed transport
exercises the adapter's exact error-handling path (``raise_for_status`` /
``.json()`` / ``httpx.HTTPError``). The DB write is monkeypatched at
``_insert_sim_run`` so tests don't need a real Postgres; the audit-row
contract is verified by asserting the patched helper was called with the
expected kwargs.
"""
from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from app.sim import ngspice as ngspice_mod


# ---------------------------------------------------------------------------
# Test fixtures: a real AsyncClient with a controllable MockTransport.
# ---------------------------------------------------------------------------

class _FakeClock:
    """Tracks calls to _insert_sim_run + returns a stable UUID."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.uuid = uuid.uuid4()

    async def __call__(self, db, **kwargs):  # noqa: D401 — matches signature
        self.calls.append(kwargs)
        return self.uuid


def _make_client(handler) -> httpx.AsyncClient:
    """Build an AsyncClient whose every request is dispatched to ``handler``."""
    transport = httpx.MockTransport(handler)
    return httpx.AsyncClient(transport=transport, base_url="http://ngspice:8001")


@pytest.fixture
def patched_insert(monkeypatch):
    fake = _FakeClock()
    monkeypatch.setattr(ngspice_mod, "_insert_sim_run", fake)
    return fake


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

async def test_success_propagates_sidecar_fields(monkeypatch, patched_insert):
    """Sidecar success body → NgspiceResult with all fields populated + audit row written."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/run"
        return httpx.Response(200, json={
            "ok": True,
            "exit_code": 0,
            "stdout": "Circuit: rc lpf\n",
            "stderr": "",
            "measurements": {"fc_3db": 1000.0, "gain_db": -3.01},
            "duration_ms": 42,
            "tool_version": "ngspice 44.2",
            "timed_out": False,
            "seed": 7,
        })

    client = _make_client(handler)
    monkeypatch.setattr(ngspice_mod, "get_ngspice_client", lambda: client)

    result = await ngspice_mod.run_ngspice(
        "R1 in out 1k\nC1 out 0 1u\n.end\n",
        db=AsyncMock(),
        timeout_s=10.0,
        seed=7,
    )

    assert result.ok is True
    assert result.exit_code == 0
    assert result.measurements == {"fc_3db": 1000.0, "gain_db": -3.01}
    assert result.tool_version == "ngspice 44.2"
    assert result.seed == 7
    assert result.netlist_sha256  # non-empty
    assert result.sim_run_id == patched_insert.uuid
    # Audit row was written exactly once, with the propagated fields.
    assert len(patched_insert.calls) == 1
    audit = patched_insert.calls[0]
    assert audit["exit_code"] == 0
    assert audit["measurements"] == {"fc_3db": 1000.0, "gain_db": -3.01}
    assert audit["timed_out"] is False


# ---------------------------------------------------------------------------
# Sidecar /run returns 5xx → contract: ok=False, sidecar marked unreachable,
# audit row written with the "unreachable" stderr.
# ---------------------------------------------------------------------------

async def test_sidecar_500_yields_unreachable_result(monkeypatch, patched_insert):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="ngspice sidecar crashed")

    client = _make_client(handler)
    monkeypatch.setattr(ngspice_mod, "get_ngspice_client", lambda: client)

    result = await ngspice_mod.run_ngspice("V1 0 1 1\n.end\n", db=AsyncMock())

    assert result.ok is False
    assert result.exit_code == -1
    assert "unreachable" in result.stderr.lower()
    assert result.measurements == {}
    assert result.sim_run_id == patched_insert.uuid
    assert patched_insert.calls[0]["stderr"] == "ngspice sidecar unreachable"


# ---------------------------------------------------------------------------
# Network failure (httpx.ConnectError) → same unreachable contract.
# ---------------------------------------------------------------------------

async def test_network_failure_yields_unreachable_result(monkeypatch, patched_insert):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    client = _make_client(handler)
    monkeypatch.setattr(ngspice_mod, "get_ngspice_client", lambda: client)

    result = await ngspice_mod.run_ngspice("R1 0 1 1\n.end\n", db=AsyncMock())
    assert result.ok is False
    assert "unreachable" in result.stderr.lower()


# ---------------------------------------------------------------------------
# Sidecar timeout → same unreachable contract (httpx.TimeoutException is an
# HTTPError subclass).
# ---------------------------------------------------------------------------

async def test_timeout_yields_unreachable_result(monkeypatch, patched_insert):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timeout")

    client = _make_client(handler)
    monkeypatch.setattr(ngspice_mod, "get_ngspice_client", lambda: client)

    result = await ngspice_mod.run_ngspice("R1 0 1 1\n.end\n", db=AsyncMock())
    assert result.ok is False
    assert result.timed_out is False  # transport-level, not sidecar-reported


# ---------------------------------------------------------------------------
# Malformed JSON in 200 response → ValueError on .json() falls into the
# same unreachable branch (the adapter's except clause catches it).
# ---------------------------------------------------------------------------

async def test_malformed_json_yields_unreachable_result(monkeypatch, patched_insert):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json{{{")

    client = _make_client(handler)
    monkeypatch.setattr(ngspice_mod, "get_ngspice_client", lambda: client)

    result = await ngspice_mod.run_ngspice("R1 0 1 1\n.end\n", db=AsyncMock())
    assert result.ok is False
    assert "unreachable" in result.stderr.lower()


# ---------------------------------------------------------------------------
# Non-zero exit code from a healthy sidecar = data, not exception.
# ---------------------------------------------------------------------------

async def test_nonzero_exit_is_data_not_exception(monkeypatch, patched_insert):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "ok": False,
            "exit_code": 1,
            "stdout": "",
            "stderr": "Error on line 3: undefined parameter\n",
            "measurements": {},
            "duration_ms": 5,
            "tool_version": "ngspice 44.2",
            "timed_out": False,
            "seed": None,
        })

    client = _make_client(handler)
    monkeypatch.setattr(ngspice_mod, "get_ngspice_client", lambda: client)

    result = await ngspice_mod.run_ngspice("invalid\n.end\n", db=AsyncMock())
    assert result.ok is False
    assert result.exit_code == 1
    assert "undefined parameter" in result.stderr
    # Audit row still written so an operator can see the failed attempt.
    assert patched_insert.calls[0]["exit_code"] == 1


# ---------------------------------------------------------------------------
# Measurement coercion: integers from JSON → floats in dataclass.
# ---------------------------------------------------------------------------

async def test_measurement_int_coerces_to_float(monkeypatch, patched_insert):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "ok": True, "exit_code": 0, "stdout": "", "stderr": "",
            "measurements": {"count": 5},  # int — must become 5.0
            "duration_ms": 1, "tool_version": "ngspice 44.2",
            "timed_out": False, "seed": None,
        })

    client = _make_client(handler)
    monkeypatch.setattr(ngspice_mod, "get_ngspice_client", lambda: client)

    result = await ngspice_mod.run_ngspice("R1 0 1 1\n.end\n", db=AsyncMock())
    assert result.measurements == {"count": 5.0}
    assert isinstance(result.measurements["count"], float)


# ---------------------------------------------------------------------------
# Empty / blank netlist is the only input-validation failure that DOES
# raise (caller bug, not sim failure).
# ---------------------------------------------------------------------------

async def test_empty_netlist_raises_value_error():
    with pytest.raises(ValueError, match="netlist must be non-empty"):
        await ngspice_mod.run_ngspice("", db=AsyncMock())
    with pytest.raises(ValueError, match="netlist must be non-empty"):
        await ngspice_mod.run_ngspice("   \n\t  ", db=AsyncMock())


# ---------------------------------------------------------------------------
# netlist_sha256 is stable across runs of the same content.
# ---------------------------------------------------------------------------

async def test_netlist_sha256_is_content_addressable(monkeypatch, patched_insert):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "ok": True, "exit_code": 0, "stdout": "", "stderr": "",
            "measurements": {}, "duration_ms": 0, "tool_version": "x",
            "timed_out": False, "seed": None,
        })

    client = _make_client(handler)
    monkeypatch.setattr(ngspice_mod, "get_ngspice_client", lambda: client)
    netlist = "R1 0 1 1k\n.end\n"
    r1 = await ngspice_mod.run_ngspice(netlist, db=AsyncMock())
    r2 = await ngspice_mod.run_ngspice(netlist, db=AsyncMock())
    assert r1.netlist_sha256 == r2.netlist_sha256
    # Each run gets its own audit row though.
    assert len(patched_insert.calls) == 2
