"""§17.297 — STATUS_ICONS is single-source-of-truth across all 5 pipelines.

§17.280-🟢-2 audit-tail concern: pre-§17.297 the ``STATUS_ICONS`` dict
was inlined in five pipeline files with a "keep in sync" comment per
the §17.212 OWUI auto-discovery constraint. Adding or renaming a status
required patching 5 files in one commit or icons drifted per-pipeline.

§17.297 hoists the dict into ``pipelines/_vendor/_status_icons.py``
(invisible to OWUI auto-discovery since `_vendor/` is underscore-
prefixed). Each pipeline loads it via the importlib.util shim
established by §17.190 / §17.296.

These tests pin:

  1. The vendor module is the canonical source — exports a non-empty
     ``STATUS_ICONS`` dict with both node-level and job-level keys.
  2. All 5 pipelines expose ``STATUS_ICONS`` and it's the SAME dict
     (identity check) as the vendor's.
  3. The pre-§17.297 inline dict literals are gone from each pipeline
     (source-shape regression guards).
"""
from __future__ import annotations

import importlib.util
import pathlib

import pytest


_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_VENDOR_PATH = _REPO_ROOT / "pipelines" / "_vendor" / "_status_icons.py"
_PIPELINE_FILES = [
    _REPO_ROOT / "pipelines" / "scaffold_router.py",
    _REPO_ROOT / "pipelines" / "execution_handler.py",
    _REPO_ROOT / "pipelines" / "dag_viewer.py",
    _REPO_ROOT / "pipelines" / "gt_browser.py",
    _REPO_ROOT / "pipelines" / "prompt_inspector.py",
]


def _load_vendor_module():
    """Load the canonical _status_icons.py vendor module via importlib —
    same pattern the pipelines use at module-init."""
    spec = importlib.util.spec_from_file_location(
        "test_status_icons_vendor_canonical", _VENDOR_PATH,
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.smoke
class TestVendorModuleContract:
    """§17.297 — the canonical dict and its required keys."""

    def test_vendor_file_exists(self):
        assert _VENDOR_PATH.exists(), (
            f"§17.297: {_VENDOR_PATH} is missing. All 5 pipelines "
            "depend on it; their import bootstrap will crash at module "
            "load."
        )

    def test_vendor_exports_status_icons_dict(self):
        mod = _load_vendor_module()
        assert hasattr(mod, "STATUS_ICONS")
        assert isinstance(mod.STATUS_ICONS, dict)
        assert mod.STATUS_ICONS, "STATUS_ICONS must be non-empty"

    def test_vendor_carries_node_level_keys(self):
        """The 5 node-level keys are the minimum every pipeline needs."""
        mod = _load_vendor_module()
        for k in ("done", "failed", "running", "pending", "skipped"):
            assert k in mod.STATUS_ICONS, (
                f"§17.297: vendor STATUS_ICONS missing node-level key {k!r}. "
                f"Every pipeline relies on this baseline."
            )

    def test_vendor_carries_job_level_keys(self):
        """The 5 extra job-level keys are needed by execution_handler.py.
        Pre-§17.297 execution_handler had its own extended copy; §17.297
        UNION'd them into the vendor so all pipelines see the same dict.
        """
        mod = _load_vendor_module()
        for k in ("executing", "planning", "blocked", "completed", "cancelled"):
            assert k in mod.STATUS_ICONS, (
                f"§17.297: vendor STATUS_ICONS missing job-level key {k!r}. "
                f"execution_handler.py renders this status; pre-§17.297 "
                f"it carried its own extended copy."
            )


@pytest.mark.smoke
class TestPipelinesUseVendorDict:
    """§17.297 — every pipeline's STATUS_ICONS is the vendor's dict."""

    @pytest.mark.parametrize("pipeline_file", _PIPELINE_FILES)
    def test_pipeline_status_icons_matches_vendor(self, pipeline_file):
        """Load each pipeline module via importlib + assert its
        ``STATUS_ICONS`` equals the vendor's dict. Identity (``is``)
        would be stronger but fails legitimately here because each
        ``spec_from_file_location`` call gives a fresh module instance
        with its own dict copy — the canonical dict and the pipeline's
        dict are the SAME VALUES but distinct Python objects under
        test-side reloading.

        Equality + the source-shape regression guards below collectively
        prove "the values match" AND "the values came from the vendor
        bootstrap" — covering the audit invariant (single source) with
        two complementary asserts.
        """
        vendor = _load_vendor_module()
        spec = importlib.util.spec_from_file_location(
            f"test_status_icons_{pipeline_file.stem}", pipeline_file,
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert hasattr(mod, "STATUS_ICONS"), (
            f"§17.297: {pipeline_file.name} no longer exposes "
            f"STATUS_ICONS at module level. Pipeline-internal call sites "
            f"(`STATUS_ICONS.get(...)`) depend on it."
        )
        assert mod.STATUS_ICONS == vendor.STATUS_ICONS, (
            f"§17.297 regression: {pipeline_file.name}'s STATUS_ICONS "
            f"diverges from the vendor's canonical dict. Either the "
            f"vendor-load bootstrap is broken or the pipeline has been "
            f"hand-edited to drop a key. Run the bootstrap restoration "
            f"or update the vendor file with the new key."
        )


@pytest.mark.smoke
class TestSourceShapeRegressionGuard:
    """§17.297 — the inline literal dict is gone from every pipeline."""

    @pytest.mark.parametrize("pipeline_file", _PIPELINE_FILES)
    def test_no_inline_status_icons_literal(self, pipeline_file):
        """A future "let me just inline this" refactor would re-introduce
        the keep-in-sync burden. The pre-§17.297 literal anchor — the
        "─── SHARED:" comment block surrounding the dict — must NOT
        reappear in any pipeline."""
        src = pipeline_file.read_text(encoding="utf-8")
        assert "─── SHARED: status icons" not in src, (
            f"§17.297 regression: {pipeline_file.name} carries the "
            f"pre-§17.297 inline STATUS_ICONS literal block. The "
            f"vendor file at pipelines/_vendor/_status_icons.py is "
            f"the single source of truth; inline copies must be loaded "
            f"via the importlib bootstrap."
        )

    @pytest.mark.parametrize("pipeline_file", _PIPELINE_FILES)
    def test_pipeline_references_vendor_bootstrap(self, pipeline_file):
        """The vendor-load bootstrap (importlib.util spec_from_file_location
        pointing at `_vendor/_status_icons.py`) must be present in each
        pipeline. Scaffold_router uses its existing `_load_vendor` helper;
        the other 4 inline the bootstrap directly."""
        src = pipeline_file.read_text(encoding="utf-8")
        assert "_status_icons.py" in src, (
            f"§17.297 regression: {pipeline_file.name} no longer "
            f"references `_status_icons.py` — the vendor-load bootstrap "
            f"is gone. STATUS_ICONS will be undefined at module init."
        )
