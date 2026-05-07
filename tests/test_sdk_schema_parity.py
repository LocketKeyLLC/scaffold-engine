"""Enforce byte-equality between ``app/schemas.py`` and the SDK's vendored
copy at ``sdk/scaffold_client/schemas.py``.

The SDK ships its own copy of the Pydantic models so that ``pip install
scaffold-engine-client`` doesn't require the orchestrator's runtime deps.
The two files must stay in lockstep — drift produces clients that
serialize fields the orchestrator no longer accepts (or vice versa).

Regenerate the vendored copy with ``make sync-schemas``.
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE = REPO_ROOT / "app" / "schemas.py"
VENDORED = REPO_ROOT / "sdk" / "scaffold_client" / "schemas.py"


def test_schemas_files_exist():
    assert SOURCE.is_file(), f"missing {SOURCE}"
    assert VENDORED.is_file(), f"missing {VENDORED}"


def test_schemas_byte_equal():
    src_bytes = SOURCE.read_bytes()
    vendored_bytes = VENDORED.read_bytes()
    if src_bytes != vendored_bytes:
        # Helpful hint instead of a raw diff dump — keeps the failure
        # actionable for someone who just edited app/schemas.py.
        raise AssertionError(
            f"{VENDORED.relative_to(REPO_ROOT)} has drifted from "
            f"{SOURCE.relative_to(REPO_ROOT)}. Run `make sync-schemas` to "
            "refresh the vendored SDK copy, then re-run the suite."
        )
