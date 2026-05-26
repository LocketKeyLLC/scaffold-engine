"""§17.336 — Schema-introspection regression guard.

Prevents the §17.319 class of bug: production code queries Milvus with
``output_fields=[...]`` containing field names that don't exist in the
live collection schema. §17.319 was silent for ~5 days because the
broad ``try/except`` in ``_fetch_chunk_content`` swallowed the
``MilvusException``; the only evidence was a ``warning`` line in the
orchestrator log that the §17.318 audit happened to grep.

This test scans every ``output_fields=[...]`` literal in ``app/``,
parses out the field names, and asserts each is a member of the live
toon_v2 ``Collection.schema.fields``. A future field-rename that
updates one call site but misses another fails CI loudly.

Skips when Milvus is unreachable (per the existing integration-test
convention).

Limitations:
- Static scan only catches literal lists. ``output_fields=some_var``
  where ``some_var`` is built dynamically can't be checked here — those
  are rare and tend to be unit-tested separately.
- ``"count(*)"`` and other aggregation pseudo-fields are excluded from
  the parity check (they're parsed by Milvus, not column names).
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import httpx
import pytest

from app.config import settings


ORCHESTRATOR_URL = "http://scaffold-orchestrator:8000"
APP_ROOT = Path(__file__).resolve().parent.parent.parent / "app"


# Match `output_fields=[...]` across line breaks. The `[^\]]+` body is
# greedy within the bracket pair; re.DOTALL is unnecessary because
# `[^\]]` already matches newlines.
_OUTPUT_FIELDS_LITERAL_RE = re.compile(
    r"output_fields\s*=\s*\[([^\]]+)\]"
)
# Field name inside the list — single or double quoted, lowercase
# alpha + underscore (matches the Milvus column naming convention).
# Pseudo-fields like `"count(*)"` are excluded by the trailing-char
# requirement.
_FIELD_NAME_RE = re.compile(r"""["']([a-z_][a-z0-9_]*)["']""")


async def _milvus_collection_fields() -> set[str] | None:
    """Connect to Milvus and return the set of field names on toon_v2.
    Returns None if Milvus is unreachable — the caller skips."""
    try:
        # pymilvus is sync; the test is async only because
        # tests/integration/conftest.py runs in asyncio mode. The
        # actual probe is a sync call but cheap.
        from pymilvus import Collection, connections
        connections.connect(
            alias="schema_parity_probe",
            host="milvus-standalone",
            port="19530",
        )
        try:
            c = Collection("toon_v2", using="schema_parity_probe")
            c.load()
            return {f.name for f in c.schema.fields}
        finally:
            connections.disconnect("schema_parity_probe")
    except Exception:
        return None


def _scan_output_fields_literals() -> list[tuple[Path, int, str]]:
    """Walk app/ and return every (file, line_no, field_name) tuple for
    output_fields literals. line_no is the line on which the
    output_fields=[ starts (or wherever the body's first quoted field
    sits — close enough for grep-friendly diagnostics)."""
    out: list[tuple[Path, int, str]] = []
    for py_file in APP_ROOT.rglob("*.py"):
        text = py_file.read_text()
        for m in _OUTPUT_FIELDS_LITERAL_RE.finditer(text):
            body = m.group(1)
            # Line number of the match start, 1-indexed.
            line_no = text.count("\n", 0, m.start()) + 1
            for fm in _FIELD_NAME_RE.finditer(body):
                out.append((py_file, line_no, fm.group(1)))
    return out


@pytest.mark.smoke
@pytest.mark.timeout(60)
@pytest.mark.asyncio
async def test_output_fields_match_toon_v2_schema():
    if os.environ.get("SCAFFOLD_SKIP_LIVE_LLM") == "1":
        pytest.skip("SCAFFOLD_SKIP_LIVE_LLM=1")

    schema_fields = await _milvus_collection_fields()
    if schema_fields is None:
        pytest.skip("milvus unreachable")

    matches = _scan_output_fields_literals()
    assert matches, (
        "expected at least one output_fields=[...] literal in app/ — "
        "if the scan returned empty, the regex broke"
    )

    drift = [
        (str(f.relative_to(APP_ROOT)), line_no, fname)
        for f, line_no, fname in matches
        if fname not in schema_fields
    ]
    assert not drift, (
        f"output_fields drift detected — these literals query field "
        f"names that don't exist in toon_v2's schema "
        f"({sorted(schema_fields)}):\n"
        + "\n".join(
            f"  app/{path}:{line_no}  → {fname!r}"
            for path, line_no, fname in drift
        )
        + "\n\nThis is the §17.319 class of bug — a field-rename that "
        "updated some call sites but not others. Either rename the "
        "field everywhere or update the schema."
    )


@pytest.mark.smoke
def test_scan_finds_known_call_sites():
    """Sanity-check the scanner: it must find the canonical sim/report
    fetch + the rag_pipeline search/query literals. Catches a regex
    drift that silently returns 0 matches and lets the parity assertion
    above pass vacuously."""
    matches = _scan_output_fields_literals()
    paths_found = {str(f.relative_to(APP_ROOT)) for f, _, _ in matches}

    # The post-§17.319 sim/report.py call site
    assert "sim/report.py" in paths_found, (
        f"scanner missed sim/report.py — got {sorted(paths_found)}"
    )
    # The largest known call site (rag_pipeline.py, multi-line literal)
    assert "modules/rag_pipeline.py" in paths_found, (
        f"scanner missed modules/rag_pipeline.py"
    )

    # The §17.319 fix specifically renamed "content" → "canonical_text"
    # in sim/report.py. Anchor that field is in our scan output for
    # that file — if the rename gets reverted, the parity test would
    # catch it, but this anchor catches a scanner regex that loses
    # field names inside multi-line bodies.
    sim_report_fields = {
        fname for f, _, fname in matches
        if str(f.relative_to(APP_ROOT)) == "sim/report.py"
    }
    assert "canonical_text" in sim_report_fields, (
        f"scanner did not extract 'canonical_text' from sim/report.py — "
        f"got {sorted(sim_report_fields)}"
    )
    assert "content" not in sim_report_fields, (
        "sim/report.py still references the pre-§17.319 'content' field "
        "name — §17.319 regression"
    )
