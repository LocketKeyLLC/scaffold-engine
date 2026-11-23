# tests/test_paper_serve_static_asset_under.py
"""Tests for app/modules/paper_serve_static_asset_under.py.

Static serving only: the module reads no table, opens no socket and calls no
model, so every test builds its own static root under ``tmp_path`` and drives a
FastAPI app in-process.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, Response
from fastapi.testclient import TestClient

from app.modules import paper_serve_static_asset_under as serving

BUNDLE_NAME = "chart.umd.min.js"
PINNED_URL = "https://cdn.jsdelivr.net/npm/chart.js@4.5.1/dist/chart.umd.min.js"
BUNDLE_BODY = "/* Chart.js v4.5.1 */\nwindow.Chart = window.Chart || function () {};\n"
EXPECTED_ASSET_URL = "/labs/paper/static/vendor/chart.umd.min.js"


def _write(path: Path, payload) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
    else:
        path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@pytest.fixture
def static_root(tmp_path: Path) -> Path:
    """A delivered-looking lab static root: vendor.json plus the pinned bundle."""
    root = tmp_path / "paper"
    _write(root / "vendor" / "vendor.json", {BUNDLE_NAME: PINNED_URL})
    _write(root / "vendor" / BUNDLE_NAME, BUNDLE_BODY)
    return root


def _app() -> FastAPI:
    app = FastAPI()

    @app.get("/labs/paper/view")
    async def view() -> HTMLResponse:  # the lab's pre-existing route, untouched by this step
        return HTMLResponse("<h1>operator view</h1>")

    return app


def _mounts(app: FastAPI):
    return [r for r in app.routes if getattr(r, "path", None) == serving.STATIC_MOUNT_PATH]


# --- constants and paths ---------------------------------------------------


def test_static_mount_sits_under_the_existing_lab_router_prefix():
    assert serving.ROUTER_PREFIX == "/labs/paper"
    assert serving.STATIC_MOUNT_PATH == "/labs/paper/static"
    assert serving.STATIC_MOUNT_PATH.startswith(serving.ROUTER_PREFIX + "/")
    assert serving.VENDOR_URL_PREFIX == serving.STATIC_MOUNT_PATH + "/vendor"


def test_vendor_url_matches_the_pinned_bundle_from_t4():
    assert serving.BUNDLE_FILENAME == BUNDLE_NAME
    assert serving.vendor_url(serving.BUNDLE_FILENAME) == EXPECTED_ASSET_URL


def test_default_paths_point_at_the_engines_lab_static_root():
    module_file = Path(serving.__file__).resolve()
    assert module_file.parent.name == "modules"
    assert serving.lab_static_dir() == module_file.parent.parent / "static" / "paper"
    assert serving.vendor_dir() == serving.lab_static_dir() / "vendor"
    assert serving.manifest_path() == serving.lab_static_dir() / "vendor" / "vendor.json"
    assert serving.lab_static_dir().is_absolute()


def test_paths_follow_an_explicit_root(static_root: Path):
    assert serving.lab_static_dir(static_root) == static_root
    assert serving.vendor_dir(static_root) == static_root / "vendor"
    assert serving.manifest_path(static_root) == static_root / "vendor" / "vendor.json"


# --- manifest --------------------------------------------------------------


def test_load_manifest_returns_the_pinned_mapping(static_root: Path):
    manifest = serving.load_manifest(serving.manifest_path(static_root))
    assert manifest == {BUNDLE_NAME: PINNED_URL}
    assert serving.pinned_urls(static_root) == manifest
    assert serving.vendored_files(static_root) == [BUNDLE_NAME]


def test_load_manifest_sorts_entries_by_name(tmp_path: Path):
    path = _write(tmp_path / "vendor.json", {"zeta.js": PINNED_URL, "alpha.js": PINNED_URL})
    assert list(serving.load_manifest(path)) == ["alpha.js", "zeta.js"]


def test_load_manifest_defaults_to_the_labs_vendor_manifest(static_root: Path, monkeypatch):
    original = serving.manifest_path
    monkeypatch.setattr(serving, "manifest_path", lambda root=None: original(static_root))
    assert serving.load_manifest() == {BUNDLE_NAME: PINNED_URL}


def test_load_manifest_missing_file_raises(tmp_path: Path):
    with pytest.raises(serving.VendorManifestError) as excinfo:
        serving.load_manifest(tmp_path / "vendor" / "vendor.json")
    assert "missing" in str(excinfo.value)


def test_load_manifest_rejects_invalid_json(tmp_path: Path):
    path = _write(tmp_path / "vendor.json", "{not json")
    with pytest.raises(serving.VendorManifestError):
        serving.load_manifest(path)


def test_load_manifest_rejects_a_non_object_document(tmp_path: Path):
    path = _write(tmp_path / "vendor.json", [BUNDLE_NAME])
    with pytest.raises(serving.VendorManifestError):
        serving.load_manifest(path)


@pytest.mark.parametrize("bad_name", ["../escape.js", "nested/bundle.js", "..", ""])
def test_load_manifest_rejects_unsafe_names(tmp_path: Path, bad_name: str):
    path = _write(tmp_path / "vendor.json", {bad_name: PINNED_URL})
    with pytest.raises(serving.VendorManifestError):
        serving.load_manifest(path)


@pytest.mark.parametrize("bad_url", ["", "   ", None, 7])
def test_load_manifest_rejects_entries_without_a_url(tmp_path: Path, bad_url):
    path = _write(tmp_path / "vendor.json", {BUNDLE_NAME: bad_url})
    with pytest.raises(serving.VendorManifestError):
        serving.load_manifest(path)


# --- asset names and paths -------------------------------------------------


@pytest.mark.parametrize("name", [BUNDLE_NAME, "vendor.json", "paper-chart.v2.min.js"])
def test_safe_name_accepts_plain_file_names(name: str):
    assert serving.safe_name(name) == name


@pytest.mark.parametrize("name", ["", "   ", ".", "..", "../x.js", "a/b.js", "a\\b.js", "/etc/passwd", ".hidden"])
def test_safe_name_rejects_anything_that_could_escape(name: str):
    with pytest.raises(ValueError):
        serving.safe_name(name)


def test_safe_name_strips_surrounding_whitespace():
    assert serving.safe_name(f"  {BUNDLE_NAME}  ") == BUNDLE_NAME


def test_vendor_url_rejects_unsafe_names():
    with pytest.raises(ValueError):
        serving.vendor_url("../vendor.json")


def test_asset_path_resolves_inside_the_vendor_directory(static_root: Path):
    assert serving.asset_path(BUNDLE_NAME, static_root) == (static_root / "vendor" / BUNDLE_NAME).resolve()
    assert serving.asset_path(BUNDLE_NAME, static_root).is_file()


def test_asset_path_rejects_traversal(static_root: Path):
    with pytest.raises(ValueError):
        serving.asset_path("../vendor.json", static_root)


def test_find_missing_reports_manifest_entries_with_no_file(static_root: Path):
    _write(
        static_root / "vendor" / "vendor.json",
        {BUNDLE_NAME: PINNED_URL, "not-delivered.js": PINNED_URL},
    )
    assert serving.find_missing(static_root) == ["not-delivered.js"]


def test_find_missing_without_a_manifest_returns_empty(tmp_path: Path):
    assert serving.find_missing(tmp_path / "paper") == []


# --- headers ---------------------------------------------------------------


def test_security_headers_permit_only_same_origin_scripts():
    headers = serving.security_headers()
    csp = headers[serving.CSP_HEADER]
    assert "script-src 'self'" in csp
    assert "default-src 'self'" in csp
    assert "object-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "cdn.jsdelivr.net" not in csp
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert "immutable" in headers["Cache-Control"]


def test_apply_headers_does_not_clobber_values_already_set():
    response = Response(content="x", headers={serving.CSP_HEADER: "default-src 'none'"})
    serving.apply_headers(response)
    assert response.headers[serving.CSP_HEADER] == "default-src 'none'"
    assert response.headers["X-Content-Type-Options"] == "nosniff"


# --- install / serving -----------------------------------------------------


def test_install_mounts_once_and_is_idempotent(static_root: Path):
    app = _app()
    assert serving.is_mounted(app) is False
    serving.install(app, root=static_root)
    serving.install(app, root=static_root)
    mounts = _mounts(app)
    assert len(mounts) == 1
    assert mounts[0].name == serving.MOUNT_NAME
    assert serving.is_mounted(app) is True


def test_install_serves_the_pinned_bundle_with_the_csp(static_root: Path):
    app = _app()
    serving.install(app, root=static_root)
    response = TestClient(app).get(EXPECTED_ASSET_URL)
    assert response.status_code == 200
    assert response.text == BUNDLE_BODY
    assert "javascript" in response.headers["content-type"].lower()
    assert response.headers[serving.CSP_HEADER] == serving.CSP_POLICY
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "immutable" in response.headers["cache-control"]


def test_install_returns_404_for_an_unknown_asset(static_root: Path):
    app = _app()
    serving.install(app, root=static_root)
    assert TestClient(app).get("/labs/paper/static/vendor/missing.js").status_code == 404


def test_install_survives_a_static_directory_that_was_never_delivered(tmp_path: Path):
    app = _app()
    serving.install(app, root=tmp_path / "not-delivered-yet")
    assert serving.is_mounted(app) is True
    assert TestClient(app).get(EXPECTED_ASSET_URL).status_code == 404


def test_install_leaves_the_existing_lab_routes_untouched(static_root: Path):
    app = _app()
    client = TestClient(app)
    before = client.get("/labs/paper/view")
    serving.install(app, root=static_root)
    after = client.get("/labs/paper/view")
    assert before.status_code == after.status_code == 200
    assert after.text == before.text
    assert serving.CSP_HEADER.lower() not in {key.lower() for key in after.headers}


def test_install_adds_no_dashboard_route_of_its_own(static_root: Path):
    app = _app()
    serving.install(app, root=static_root)
    paths = {getattr(route, "path", "") for route in app.routes}
    assert "/labs/paper/dashboard" not in paths
    assert "/labs/paper/view" in paths


# --- describe --------------------------------------------------------------


def test_describe_reports_paths_urls_and_missing_files(static_root: Path):
    _write(
        static_root / "vendor" / "vendor.json",
        {BUNDLE_NAME: PINNED_URL, "not-delivered.js": PINNED_URL},
    )
    info = serving.describe(static_root)
    assert info["mount_path"] == serving.STATIC_MOUNT_PATH
    assert info["vendor_url_prefix"] == serving.VENDOR_URL_PREFIX
    assert info["assets"] == [BUNDLE_NAME, "not-delivered.js"]
    assert info["urls"][BUNDLE_NAME] == EXPECTED_ASSET_URL
    assert info["missing"] == ["not-delivered.js"]
    assert info["error"] is None


def test_describe_reports_a_missing_manifest_as_an_error(tmp_path: Path):
    info = serving.describe(tmp_path / "paper")
    assert info["assets"] == []
    assert info["missing"] == []
    assert info["error"] and "missing" in info["error"]
