"""
Unit tests for ``app.sim.report`` — the regenerable-from-artifacts
terminal stage.

DB joins + Milvus chunk fetch are mocked; the tests focus on the
constraint-status classifier, the deterministic Markdown rendering,
and the graceful-degradation paths (missing chunks, non-converged
sizings).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from app.sim import report as report_mod
from app.sim.report import (
    ReportCitation,
    ReportConstraint,
    ReportDocument,
    ReportNotAvailableError,
    ReportSimRun,
    _classify_constraint,
    build_report,
    render_markdown,
)
from tests.conftest import make_mock_db


FIXED_TIME = datetime(2026, 5, 12, 22, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Constraint-status classifier
# ---------------------------------------------------------------------------

@pytest.mark.smoke
def test_classify_constraint_ok_within_tolerance():
    c = {
        "id": "fc",
        "kind": "electrical.frequency",
        "target": 1000.0,
        "tolerance_pct": 5.0,
        "unit": "Hz",
    }
    status, measured = _classify_constraint(c, {"fc": 1020.0})
    assert status == "ok"
    assert measured == 1020.0


@pytest.mark.smoke
def test_classify_constraint_out_of_tolerance():
    c = {
        "id": "fc", "kind": "electrical.frequency",
        "target": 1000.0, "tolerance_pct": 5.0, "unit": "Hz",
    }
    status, measured = _classify_constraint(c, {"fc": 1500.0})
    assert status == "out_of_tolerance"
    assert measured == 1500.0


@pytest.mark.smoke
def test_classify_constraint_violated_min():
    c = {"id": "rin", "kind": "electrical.impedance", "min": 10000.0, "unit": "ohm"}
    status, measured = _classify_constraint(c, {"rin": 5000.0})
    assert status == "violated_min"
    assert measured == 5000.0


@pytest.mark.smoke
def test_classify_constraint_violated_max():
    c = {"id": "vpp", "kind": "electrical.voltage", "max": 3.3, "unit": "V"}
    status, measured = _classify_constraint(c, {"vpp": 5.0})
    assert status == "violated_max"
    assert measured == 5.0


@pytest.mark.smoke
def test_classify_constraint_not_measured_required_measurable():
    c = {"id": "fc", "kind": "electrical.frequency", "target": 1000.0, "unit": "Hz"}
    status, measured = _classify_constraint(c, {})
    assert status == "not_measured"
    assert measured is None


@pytest.mark.smoke
def test_classify_constraint_skipped_non_measurable():
    """cost.* / physical.* don't fall in the oracle's surface — they're
    skipped, not flagged as not_measured."""
    c = {"id": "bom", "kind": "cost.bom_usd", "max": 5.0, "unit": "USD"}
    status, measured = _classify_constraint(c, {})
    assert status == "skipped"
    assert measured is None


# ---------------------------------------------------------------------------
# build_report — DB join + classification
# ---------------------------------------------------------------------------

def _patch_fetches(
    monkeypatch,
    *,
    sizing: dict | None = None,
    spec: dict | None = None,
    selection: dict | None = None,
    sim_run_map: dict | None = None,
    chunk_map: dict | None = None,
):
    async def _fake_sizing(db, sid):
        if sizing is None:
            raise ReportNotAvailableError(f"{sid} not found")
        return sizing

    async def _fake_spec(db, sid):
        if spec is None:
            raise ReportNotAvailableError(f"spec {sid} not found")
        return spec

    async def _fake_sel(db, sid):
        if selection is None:
            raise ReportNotAvailableError(f"sel {sid} not found")
        return selection

    async def _fake_sim_runs(db, ids):
        return sim_run_map or {}

    async def _fake_chunks(ids):
        return chunk_map or {}

    monkeypatch.setattr("app.sim.report._fetch_sizing", _fake_sizing)
    monkeypatch.setattr("app.sim.report._fetch_spec", _fake_spec)
    monkeypatch.setattr("app.sim.report._fetch_topology_selection", _fake_sel)
    monkeypatch.setattr("app.sim.report._fetch_sim_runs", _fake_sim_runs)
    monkeypatch.setattr("app.sim.report._fetch_chunk_content", _fake_chunks)


def _baseline_rows() -> tuple[dict, dict, dict, dict, dict]:
    """Return (sizing, spec, selection, sim_run_map, chunk_map) for
    a converged RC-LPF report."""
    sizing_id = uuid.uuid4()
    spec_id = uuid.uuid4()
    sel_id = uuid.uuid4()
    sim_id = uuid.uuid4()

    sizing = {
        "id": sizing_id,
        "spec_id": spec_id,
        "topology_selection_id": sel_id,
        "candidate_idx": 0,
        "final_params": {"R1": "1k", "C1": "159.155n"},
        "final_netlist": "* RC LPF\nV1 in 0 AC 1\n.end\n",
        "sim_run_ids": [sim_id],
        "converged": True,
        "iterations": 1,
        "model_used": "qwen3-vl:235b-instruct-cloud",
        "measurements_final": {"fc_3db": 1003.5},
        "errors": [],
        "created_at": FIXED_TIME,
    }
    spec = {
        "id": spec_id,
        "schema_version": "1.0.0",
        "spec_json": {
            "schema_version": "1.0.0",
            "design": {
                "name": "RC low-pass filter",
                "kind": "analog_circuit",
                "description": "First-order passive RC LPF.",
            },
            "constraints": [
                {
                    "id": "fc_3db",
                    "kind": "electrical.frequency",
                    "description": "-3 dB corner.",
                    "target": 1000.0,
                    "tolerance_pct": 5.0,
                    "unit": "Hz",
                    "criticality": "required",
                }
            ],
        },
    }
    selection = {
        "id": sel_id,
        "spec_id": spec_id,
        "candidates": [
            {
                "name": "RC low-pass",
                "description": "Resistor + cap.",
                "rationale": "Trivial fit.",
                "citations": ["chunk-A"],
            }
        ],
        "rag_chunk_ids": ["chunk-A"],
        "model_used": "qwen3-vl:235b-instruct-cloud",
    }
    sim_run_map = {
        sim_id: {
            "id": sim_id,
            "tool": "ngspice",
            "tool_version": "ngspice-44.2",
            "exit_code": 0,
            "timed_out": False,
            "duration_ms": 42,
            "measurements": {"fc_3db": 1003.5},
            "verdict": None,
        }
    }
    chunk_map = {
        "chunk-A": {
            "title": "RC low-pass filter design",
            "content": "An RC low-pass has a -3 dB corner at fc = 1/(2π RC).",
            "source_url": "https://example.test/rc",
        }
    }
    return sizing, spec, selection, sim_run_map, chunk_map


@pytest.mark.smoke
async def test_build_report_converged_full_join(monkeypatch):
    sizing, spec, selection, sim_run_map, chunk_map = _baseline_rows()
    _patch_fetches(
        monkeypatch,
        sizing=sizing, spec=spec, selection=selection,
        sim_run_map=sim_run_map, chunk_map=chunk_map,
    )
    db = make_mock_db()
    doc = await build_report(sizing["id"], db=db, generated_at=FIXED_TIME)

    assert doc.converged is True
    assert doc.iterations == 1
    assert doc.design_name == "RC low-pass filter"
    assert len(doc.constraints) == 1
    assert doc.constraints[0].id == "fc_3db"
    assert doc.constraints[0].status == "ok"
    assert doc.constraints[0].measured == 1003.5
    assert len(doc.citations) == 1
    assert doc.citations[0].available is True
    assert "fc = 1/(2π RC)" in doc.citations[0].snippet
    assert len(doc.sim_runs) == 1
    assert doc.sim_runs[0].iteration == 1
    assert doc.sim_runs[0].tool == "ngspice"


@pytest.mark.smoke
async def test_build_report_non_converged_renders(monkeypatch):
    """Per the §17.148 design choice, non-converged sizings ARE
    renderable — the report is the post-mortem artefact."""
    sizing, spec, selection, sim_run_map, chunk_map = _baseline_rows()
    sizing["converged"] = False
    sizing["measurements_final"] = {"fc_3db": 5000.0}  # way out of tolerance
    sizing["iterations"] = 3
    sizing["errors"] = ["budget exhausted after 3 iterations"]
    _patch_fetches(
        monkeypatch,
        sizing=sizing, spec=spec, selection=selection,
        sim_run_map=sim_run_map, chunk_map=chunk_map,
    )
    db = make_mock_db()
    doc = await build_report(sizing["id"], db=db, generated_at=FIXED_TIME)

    assert doc.converged is False
    assert doc.iterations == 3
    assert doc.constraints[0].status == "out_of_tolerance"
    assert "budget exhausted" in doc.errors[0]


@pytest.mark.smoke
async def test_build_report_missing_chunk_renders_unavailable(monkeypatch):
    """Milvus fetch returns empty map (e.g. corpus unreachable). The
    citation must render with available=False rather than fail the
    whole report."""
    sizing, spec, selection, sim_run_map, _ = _baseline_rows()
    _patch_fetches(
        monkeypatch,
        sizing=sizing, spec=spec, selection=selection,
        sim_run_map=sim_run_map, chunk_map={},  # empty
    )
    db = make_mock_db()
    doc = await build_report(sizing["id"], db=db, generated_at=FIXED_TIME)

    assert len(doc.citations) == 1
    assert doc.citations[0].available is False
    assert doc.citations[0].snippet == ""


@pytest.mark.smoke
async def test_build_report_sizing_not_found_raises(monkeypatch):
    _patch_fetches(monkeypatch)  # no rows
    db = make_mock_db()
    with pytest.raises(ReportNotAvailableError):
        await build_report(uuid.uuid4(), db=db)


@pytest.mark.smoke
async def test_build_report_unmeasured_required_status_not_measured(monkeypatch):
    """LLM forgot to .meas fc_3db; constraint should render as
    not_measured (the §17.147 lesson — the report must surface this
    rather than label the missing measurement as 'ok'.)"""
    sizing, spec, selection, sim_run_map, chunk_map = _baseline_rows()
    sizing["measurements_final"] = {}  # forgot to measure
    sizing["converged"] = False
    _patch_fetches(
        monkeypatch,
        sizing=sizing, spec=spec, selection=selection,
        sim_run_map=sim_run_map, chunk_map=chunk_map,
    )
    db = make_mock_db()
    doc = await build_report(sizing["id"], db=db, generated_at=FIXED_TIME)
    assert doc.constraints[0].status == "not_measured"


# ---------------------------------------------------------------------------
# render_markdown — pure, deterministic
# ---------------------------------------------------------------------------

def _baseline_doc(*, converged: bool = True, errors=None) -> ReportDocument:
    return ReportDocument(
        report_schema_version="1.0.0",
        generated_at=FIXED_TIME,
        sizing_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        spec_id=uuid.UUID("00000000-0000-0000-0000-000000000002"),
        topology_selection_id=uuid.UUID("00000000-0000-0000-0000-000000000003"),
        candidate_idx=0,
        converged=converged,
        iterations=1,
        design_name="RC LPF",
        design_kind="analog_circuit",
        design_description="First-order RC.",
        spec_schema_version="1.0.0",
        constraints=[
            ReportConstraint(
                id="fc_3db", kind="electrical.frequency",
                description="-3 dB corner.",
                target=1000.0, min=None, max=None,
                tolerance_pct=5.0, unit="Hz",
                criticality="required", measured=1003.5, status="ok",
            )
        ],
        interfaces=[],
        environment={},
        selected_topology={
            "name": "RC low-pass",
            "description": "R + C.",
            "rationale": "Trivial.",
        },
        citations=[
            ReportCitation(
                entry_id="chunk-A", title="t", snippet="snip", available=True
            )
        ],
        final_params={"R1": "1k", "C1": "159.155n"},
        final_netlist="* RC\nV1 in 0 AC 1\n.end",
        final_measurements={"fc_3db": 1003.5},
        sim_runs=[
            ReportSimRun(
                sim_run_id=uuid.UUID("00000000-0000-0000-0000-000000000099"),
                iteration=1,
                tool="ngspice", tool_version="ngspice-44.2",
                exit_code=0, timed_out=False, duration_ms=42,
                measurements={"fc_3db": 1003.5},
                verdict=None,
            )
        ],
        errors=list(errors or []),
        model_used="qwen3-vl:235b-instruct-cloud",
    )


@pytest.mark.smoke
def test_render_markdown_is_deterministic():
    """Same doc through render_markdown twice → byte-identical output.
    Critical for the regenerable-from-artifacts invariant: an
    operator who re-runs the GET must see the same bytes."""
    doc = _baseline_doc()
    a = render_markdown(doc)
    b = render_markdown(doc)
    assert a == b


@pytest.mark.smoke
def test_render_markdown_carries_key_fields():
    doc = _baseline_doc()
    md = render_markdown(doc)
    assert "# RC LPF" in md
    assert "## Spec" in md
    assert "## Topology" in md
    assert "## Sized Parameters" in md
    assert "## Measurements vs Targets" in md
    assert "## Sim Run Manifest" in md
    assert "## Final Netlist" in md
    assert "`fc_3db`" in md
    assert "**ok**" in md
    assert "ngspice-44.2" in md
    # Sized params table — both keys present.
    assert "`R1`" in md
    assert "`C1`" in md


@pytest.mark.smoke
def test_render_markdown_banner_on_non_converged():
    doc = _baseline_doc(converged=False, errors=["budget exhausted"])
    md = render_markdown(doc)
    assert "NOT CONVERGED" in md
    # Audit section appears only when there are errors.
    assert "## Audit — Diagnostics" in md
    assert "budget exhausted" in md


@pytest.mark.smoke
def test_render_markdown_no_banner_when_converged():
    doc = _baseline_doc(converged=True)
    md = render_markdown(doc)
    assert "NOT CONVERGED" not in md


@pytest.mark.smoke
def test_render_markdown_citation_unavailable_renders_marker():
    doc = _baseline_doc()
    doc.citations = [
        ReportCitation(entry_id="missing-chunk", available=False)
    ]
    md = render_markdown(doc)
    assert "`missing-chunk`" in md
    assert "[content unavailable]" in md


# ---------------------------------------------------------------------------
# §17.153 — digital sizing reports
# ---------------------------------------------------------------------------

def _digital_sizing_row(*, sizing_id, spec_id, sel_id, sim_id):
    return {
        "id": sizing_id,
        "kind": "digital",  # synthesized by _fetch_sizing on digital hit
        "spec_id": spec_id,
        "topology_selection_id": sel_id,
        "candidate_idx": 0,
        "final_params": {"WIDTH": "4", "CLK_PERIOD_NS": "10"},
        "final_netlist": "",  # not applicable to digital
        "final_sv_source": "module tb;\n  // counter testbench\n  initial begin $finish; end\nendmodule\n",
        "top_module": "tb",
        "sim_run_ids": [sim_id],
        "converged": True,
        "iterations": 1,
        "model_used": "qwen3-vl:235b-instruct-cloud",
        "measurements_final": {"wrap_count": 16.0},
        "errors": [],
        "created_at": FIXED_TIME,
    }


@pytest.mark.smoke
async def test_build_report_digital_full_join(monkeypatch):
    """A digital sizing row joins through to a kind='digital'
    ReportDocument with final_sv_source + top_module populated and
    final_netlist empty."""
    sizing_id = uuid.uuid4()
    spec_id = uuid.uuid4()
    sel_id = uuid.uuid4()
    sim_id = uuid.uuid4()

    sizing = _digital_sizing_row(
        sizing_id=sizing_id, spec_id=spec_id, sel_id=sel_id, sim_id=sim_id,
    )
    spec = {
        "id": spec_id,
        "schema_version": "1.0.0",
        "spec_json": {
            "schema_version": "1.0.0",
            "design": {
                "name": "Counter",
                "kind": "digital_logic",
                "description": "N-bit counter.",
            },
            "constraints": [
                {
                    "id": "wrap_count", "kind": "timing.latency",
                    "description": "Cycles to wrap.",
                    "target": 16.0, "tolerance_pct": 5.0,
                    "unit": "cycles", "criticality": "required",
                }
            ],
        },
    }
    selection = {
        "id": sel_id,
        "spec_id": spec_id,
        "candidates": [
            {
                "name": "Synchronous counter", "description": ".",
                "rationale": ".", "citations": ["chunk-D"],
            }
        ],
        "rag_chunk_ids": ["chunk-D"],
        "model_used": "test",
    }
    sim_run_map = {
        sim_id: {
            "id": sim_id,
            "tool": "verilator", "tool_version": "verilator-5.024",
            "exit_code": 0, "timed_out": False, "duration_ms": 200,
            "measurements": {"wrap_count": 16.0},
            "verdict": None,
        }
    }

    _patch_fetches(
        monkeypatch,
        sizing=sizing, spec=spec, selection=selection,
        sim_run_map=sim_run_map, chunk_map={},
    )
    db = make_mock_db()
    doc = await build_report(sizing_id, db=db, generated_at=FIXED_TIME)

    assert doc.kind == "digital"
    assert doc.final_sv_source.startswith("module tb;")
    assert doc.top_module == "tb"
    assert doc.final_netlist == ""
    assert doc.constraints[0].id == "wrap_count"
    assert doc.constraints[0].status == "ok"


@pytest.mark.smoke
def test_render_markdown_digital_uses_systemverilog_fence():
    """Digital reports render the source as a `systemverilog`
    fenced block with the top-module name in the section title."""
    doc = _baseline_doc()
    doc.kind = "digital"
    doc.final_netlist = ""
    doc.final_sv_source = "module tb;\n  initial $finish;\nendmodule\n"
    doc.top_module = "tb"

    md = render_markdown(doc)
    assert "## Final SystemVerilog Source (top: `tb`)" in md
    assert "```systemverilog" in md
    # Analog "Final Netlist" header MUST NOT appear on a digital report.
    assert "## Final Netlist" not in md
    # Kind line in header surfaces the discriminator.
    assert "- **Kind:** digital" in md


@pytest.mark.smoke
def test_render_markdown_analog_still_uses_spice_fence():
    """Sanity check that the analog path is untouched by the digital
    branching — same expectations as the §17.148 unit suite."""
    doc = _baseline_doc()
    md = render_markdown(doc)
    assert "## Final Netlist" in md
    assert "```spice" in md
    assert "## Final SystemVerilog Source" not in md
    assert "- **Kind:** analog" in md


# ---------------------------------------------------------------------------
# §17.416 — formal-verification section
# ---------------------------------------------------------------------------

@pytest.mark.smoke
def test_render_markdown_no_formal_section_when_unverified():
    """Analog / unverified designs (formal_verdict is None) render NO
    formal-verification section."""
    doc = _baseline_doc()
    md = render_markdown(doc)
    assert "## Formal Verification" not in md


@pytest.mark.smoke
def test_render_markdown_formal_section_pass():
    """A digital design that ran the verify stage renders a Formal
    Verification section with the verdict and the frozen SVA harness."""
    doc = _baseline_doc()
    doc.kind = "digital"
    doc.formal_verdict = "PASS"
    doc.formal_converged = True
    doc.formal_mode = "bmc"
    doc.formal_depth = 20
    doc.formal_depth_reached = 20
    doc.formal_engine = "smtbmc z3"
    doc.formal_iterations = 2
    doc.formal_properties = "module formal_top;\n  assert property (1);\nendmodule"

    md = render_markdown(doc)
    assert "## Formal Verification" in md
    assert "✅ PASS" in md
    assert "smtbmc z3" in md
    assert "### Properties (frozen SVA harness)" in md
    assert "```systemverilog" in md
    assert "assert property (1)" in md


@pytest.mark.smoke
def test_render_markdown_formal_section_fail():
    """A FAIL verdict renders the not-proven marker."""
    doc = _baseline_doc()
    doc.kind = "digital"
    doc.formal_verdict = "FAIL"
    doc.formal_converged = False
    doc.formal_mode = "bmc"
    doc.formal_depth = 20
    doc.formal_depth_reached = 3
    md = render_markdown(doc)
    assert "## Formal Verification" in md
    assert "❌ FAIL" in md
    assert "Depth reached:** 3" in md


@pytest.mark.smoke
def test_render_markdown_formal_section_is_deterministic():
    """The formal section preserves the byte-identical render invariant."""
    doc = _baseline_doc()
    doc.kind = "digital"
    doc.formal_verdict = "PASS"
    doc.formal_converged = True
    doc.formal_properties = "module formal_top; assert property(1); endmodule"
    assert render_markdown(doc) == render_markdown(doc)


# ---------------------------------------------------------------------------
# §17.319 — Milvus output_fields regression guard
# ---------------------------------------------------------------------------
# §17.148 shipped `_fetch_chunk_content` querying output_fields=[..., "content", ...].
# The toon_v2 collection's text column is named `canonical_text`, not `content`,
# so every call raised MilvusException 65535 ("field content not exist") and was
# swallowed by the broad try/except → every sim-report citation rendered with
# blank quote text. §17.318's full-system audit surfaced the warning line in
# orchestrator logs. These tests anchor the rename and would have caught it.


@pytest.mark.smoke
def test_fetch_chunk_content_requests_canonical_text_from_milvus(monkeypatch):
    """The Milvus output_fields list must request `canonical_text` (the toon_v2
    schema's text column) — NOT `content`, which does not exist on the
    collection. Catches a reintroduction of the §17.148→§17.318 silent bug."""
    import asyncio

    captured: dict[str, object] = {}

    class _FakeCollection:
        def query(self, *, expr, output_fields, limit):
            captured["expr"] = expr
            captured["output_fields"] = list(output_fields)
            captured["limit"] = limit
            return [
                {
                    "entry_id": "chunk-X",
                    "title": "T",
                    "canonical_text": "BODY",
                    "source_url": "u",
                }
            ]

    monkeypatch.setattr(
        "app.sim.report.get_collection", lambda: _FakeCollection()
    )

    result = asyncio.run(report_mod._fetch_chunk_content(["chunk-X"]))

    assert captured["output_fields"] == [
        "entry_id",
        "title",
        "canonical_text",
        "source_url",
    ]
    assert "content" not in captured["output_fields"]
    # Returned shape still exposes the public-facing "content" key to the
    # citation consumer (chunk["content"] at report.py L451) — the rename is
    # at the Milvus boundary only.
    assert result["chunk-X"]["content"] == "BODY"
    assert result["chunk-X"]["title"] == "T"
    assert result["chunk-X"]["source_url"] == "u"


@pytest.mark.smoke
def test_fetch_chunk_content_handles_missing_canonical_text(monkeypatch):
    """If a row from Milvus lacks the canonical_text field for any reason,
    the consumer dict's `content` value must be `""` (not the literal
    string "None" or a KeyError). Mirrors the `r.get(..., "") or ""` shape."""
    import asyncio

    class _FakeCollection:
        def query(self, *, expr, output_fields, limit):
            return [{"entry_id": "chunk-Y", "title": "T", "source_url": "u"}]

    monkeypatch.setattr(
        "app.sim.report.get_collection", lambda: _FakeCollection()
    )

    result = asyncio.run(report_mod._fetch_chunk_content(["chunk-Y"]))
    assert result["chunk-Y"]["content"] == ""
