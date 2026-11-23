# app/modules/paper_serve_static_asset_under.py
"""Serve the paper lab's pinned vendor bundle under the lab's router prefix.

T4 pins the charting bundle: ``app/static/paper/vendor/vendor.json`` maps
``chart.umd.min.js`` to the jsDelivr release the delivery flow fetches into
``app/static/paper/vendor/chart.umd.min.js``. T8 serves the dashboard HTML at
``/labs/paper/dashboard``. Neither of them can hand that file to the browser,
so this module mounts the lab's static root at ``/labs/paper/static`` and
stamps those responses with a same-origin CSP that permits the vendored script.

The bundle is served without the lab's API-key dependency on purpose: a browser
fetching a ``<script src>`` does not carry the key header the operator uses for
the JSON routes, and the file is a byte-for-byte public CDN release -- gating it
would break the render and protect nothing. Nothing here reads the database,
adds a route, or edits a template.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi.staticfiles import StaticFiles

logger = logging.getLogger("scaffold")

__all__ = [
    "BUNDLE_FILENAME",
    "CACHE_CONTROL",
    "CSP_HEADER",
    "CSP_POLICY",
    "LAB_SLUG",
    "MOUNT_NAME",
    "ROUTER_PREFIX",
    "STATIC_MOUNT_PATH",
    "VENDOR_DIRNAME",
    "VENDOR_MANIFEST",
    "VENDOR_URL_PREFIX",
    "VendorManifestError",
    "VendorStaticFiles",
    "apply_headers",
    "asset_path",
    "describe",
    "find_missing",
    "install",
    "is_mounted",
    "lab_static_dir",
    "load_manifest",
    "manifest_path",
    "pinned_urls",
    "safe_name",
    "security_headers",
    "vendor_dir",
    "vendor_url",
    "vendored_files",
]

LAB_SLUG = "paper"
ROUTER_PREFIX = f"/labs/{LAB_SLUG}"
STATIC_MOUNT_PATH = f"{ROUTER_PREFIX}/static"
VENDOR_URL_PREFIX = f"{STATIC_MOUNT_PATH}/vendor"

VENDOR_DIRNAME = "vendor"
VENDOR_MANIFEST = "vendor.json"
BUNDLE_FILENAME = "chart.umd.min.js"

MOUNT_NAME = "paper_static"
_STATE_FLAG = "paper_static_mounted"

CSP_HEADER = "Content-Security-Policy"
CSP_POLICY = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "font-src 'self'; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "frame-ancestors 'none'"
)
CACHE_CONTROL = "public, max-age=31536000, immutable"

_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class VendorManifestError(RuntimeError):
    """The vendor manifest is missing, unreadable, or lists an unsafe entry."""


def lab_static_dir(root: Optional[Path] = None) -> Path:
    """The lab's static root: ``app/static/paper`` unless a root is given."""
    if root is not None:
        return Path(root)
    return Path(__file__).resolve().parent.parent / "static" / LAB_SLUG


def vendor_dir(root: Optional[Path] = None) -> Path:
    """Where the delivery flow writes the pinned bundle."""
    return lab_static_dir(root) / VENDOR_DIRNAME


def manifest_path(root: Optional[Path] = None) -> Path:
    """The ``name -> upstream URL`` manifest written by the delivery flow."""
    return vendor_dir(root) / VENDOR_MANIFEST


def safe_name(filename: str) -> str:
    """Return the file name unchanged, or raise ``ValueError`` if it can escape the vendor dir."""
    name = str(filename or "").strip()
    if not name or name in {".", ".."}:
        raise ValueError(f"unsafe vendored asset name: {filename!r}")
    if name != Path(name).name or not _SAFE_NAME.match(name):
        raise ValueError(f"unsafe vendored asset name: {filename!r}")
    return name


def asset_path(filename: str, root: Optional[Path] = None) -> Path:
    """Absolute path of one vendored asset; refuses anything outside ``vendor/``."""
    name = safe_name(filename)
    base = vendor_dir(root).resolve()
    target = (base / name).resolve()
    try:
        target.relative_to(base)
    except ValueError as exc:
        raise ValueError(f"vendored asset escapes {base}: {filename!r}") from exc
    return target


def vendor_url(filename: str) -> str:
    """The URL the dashboard ``<script src>`` must use for one vendored asset."""
    return f"{VENDOR_URL_PREFIX}/{safe_name(filename)}"


def load_manifest(path: Optional[Path] = None) -> Dict[str, str]:
    """Read the vendor manifest as ``{file name: upstream URL}``, sorted by name."""
    manifest = Path(path) if path is not None else manifest_path()
    try:
        raw = manifest.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise VendorManifestError(f"vendor manifest missing: {manifest}") from exc
    except OSError as exc:
        raise VendorManifestError(f"vendor manifest unreadable: {manifest}: {exc}") from exc
    try:
        data = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise VendorManifestError(f"vendor manifest is not valid JSON: {manifest}") from exc
    if not isinstance(data, dict):
        raise VendorManifestError(f"vendor manifest must be an object of name -> url: {manifest}")
    entries: Dict[str, str] = {}
    for key, value in data.items():
        try:
            name = safe_name(key)
        except ValueError as exc:
            raise VendorManifestError(str(exc)) from exc
        if not isinstance(value, str) or not value.strip():
            raise VendorManifestError(f"vendor manifest entry {name!r} has no URL")
        entries[name] = value.strip()
    return dict(sorted(entries.items()))


def pinned_urls(root: Optional[Path] = None) -> Dict[str, str]:
    """The manifest at the lab's own vendor directory."""
    return load_manifest(manifest_path(root))


def vendored_files(root: Optional[Path] = None) -> List[str]:
    """Names the manifest promises, sorted."""
    return list(pinned_urls(root))


def find_missing(root: Optional[Path] = None) -> List[str]:
    """Manifest names with no file on disk; an unreadable manifest yields ``[]``."""
    try:
        names = vendored_files(root)
    except VendorManifestError as exc:
        logger.debug("paper lab vendor manifest unreadable, nothing to compare: %s", exc)
        return []
    base = vendor_dir(root)
    return [name for name in names if not (base / name).is_file()]


def security_headers() -> Dict[str, str]:
    """Headers stamped on every vendored asset response."""
    return {
        CSP_HEADER: CSP_POLICY,
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "Cross-Origin-Resource-Policy": "same-origin",
        "Cache-Control": CACHE_CONTROL,
    }


def apply_headers(response: Any) -> Any:
    """Stamp the static headers on a response without clobbering what it already set."""
    for key, value in security_headers().items():
        response.headers.setdefault(key, value)
    return response


class VendorStaticFiles(StaticFiles):
    """``StaticFiles`` that adds the CSP/nosniff/cache headers to what it serves."""

    async def get_response(self, path: str, scope: Any) -> Any:
        response = await super().get_response(path, scope)
        return apply_headers(response)


def is_mounted(app: Any) -> bool:
    """True when this app already carries the lab's static mount."""
    state = getattr(app, "state", None)
    if state is not None and getattr(state, _STATE_FLAG, False):
        return True
    for route in getattr(app, "routes", None) or []:
        if getattr(route, "path", None) == STATIC_MOUNT_PATH:
            return True
    return False


def install(app: Any, root: Optional[Path] = None) -> None:
    """Mount the lab's static root at ``/labs/paper/static``; safe to call twice."""
    if is_mounted(app):
        logger.debug("paper lab static already mounted at %s", STATIC_MOUNT_PATH)
        return
    directory = lab_static_dir(root)
    if not directory.is_dir():
        logger.warning(
            "paper lab static directory %s is missing; %s will 404 until delivery writes it",
            directory,
            STATIC_MOUNT_PATH,
        )
        directory.mkdir(parents=True, exist_ok=True)
    app.mount(
        STATIC_MOUNT_PATH,
        VendorStaticFiles(directory=str(directory), check_dir=False, html=False),
        name=MOUNT_NAME,
    )
    state = getattr(app, "state", None)
    if state is not None:
        setattr(state, _STATE_FLAG, True)


def describe(root: Optional[Path] = None) -> Dict[str, Any]:
    """What the delivery/review step needs to see: paths, URLs, and anything still missing."""
    directory = lab_static_dir(root)
    info: Dict[str, Any] = {
        "mount_path": STATIC_MOUNT_PATH,
        "vendor_url_prefix": VENDOR_URL_PREFIX,
        "static_dir": str(directory),
        "vendor_dir": str(vendor_dir(root)),
        "manifest": str(manifest_path(root)),
        "assets": [],
        "urls": {},
        "missing": [],
        "error": None,
    }
    try:
        manifest = load_manifest(manifest_path(root))
    except VendorManifestError as exc:
        info["error"] = str(exc)
        return info
    info["assets"] = sorted(manifest)
    info["urls"] = {name: vendor_url(name) for name in sorted(manifest)}
    info["missing"] = [name for name in sorted(manifest) if not (vendor_dir(root) / name).is_file()]
    return info
