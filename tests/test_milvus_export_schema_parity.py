"""§17.854 (audit H5) — backup/restore field-parity guard.

scripts/milvus_export.py is the SOLE recovery path for the known
corpus-loss-on-`compose down` failure, yet it had zero tests, and its
``FIELDS`` list is a *variable* — invisible to the static
``output_fields=[...]`` literal scan in
tests/integration/test_milvus_schema_parity.py (which also doesn't scan
scripts/). So a toon_v2 schema migration that adds a field could leave FIELDS
stale, `make backup` would run green forever, and the missing field's data would
be silently lost — discovered only during a disaster recovery.

This asserts the export field list is exactly the canonical schema's column set
(minus BM25's auto-generated sparse vector, which is regenerated on insert). Pure
schema construction — no live Milvus, fits the core/ci lane.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from app.utils.milvus_utils import build_toon_v2_schema

_EXPORT = Path(__file__).resolve().parent.parent / "scripts" / "milvus_export.py"

# BM25's sparse vector is a FUNCTION output auto-generated from canonical_text on
# insert — it is neither exported nor restored (it regenerates), so it's excluded.
_FUNCTION_GENERATED = {"sparse_bm25"}


def _export_fields() -> list[str]:
    spec = importlib.util.spec_from_file_location("milvus_export", _EXPORT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return list(mod.FIELDS)


def _schema_field_names() -> set[str]:
    schema = build_toon_v2_schema(bm25=True)  # widest schema
    return {f.name for f in schema.fields} - _FUNCTION_GENERATED


@pytest.mark.smoke
def test_export_fields_are_all_real_schema_columns():
    """Every exported field must exist in the canonical schema — a rename that
    misses milvus_export.FIELDS would export a column that no longer exists."""
    schema_fields = _schema_field_names()
    export_fields = _export_fields()
    unknown = [f for f in export_fields if f not in schema_fields]
    assert not unknown, (
        f"milvus_export.FIELDS references non-schema columns {unknown}; "
        f"schema has {sorted(schema_fields)}"
    )


@pytest.mark.smoke
def test_export_covers_every_persisted_schema_column():
    """Every persisted schema column must be exported — a NEW field the backup
    forgot means restore silently drops that column's data (the H5 data-loss)."""
    schema_fields = _schema_field_names()
    missing = sorted(schema_fields - set(_export_fields()))
    assert not missing, (
        f"milvus_export.FIELDS is missing schema columns {missing} — a backup "
        f"would silently drop them on restore. Add them to scripts/milvus_export.py."
    )
