"""Generate or verify the committed OpenAPI snapshot (Sprint J.1.a).

The snapshot at ``docs/openapi.json`` is the stability anchor for the
``scaffold-engine-client`` SDK. Every contract change shows up as a diff in
PR review, which forces a deliberate decision about backward compatibility
and version bumps.

Usage (run inside the orchestrator container — needs the same Python env
as the running app). The Makefile wraps these::

    # Write JSON to stdout — Makefile redirects to docs/openapi.json on the host
    docker exec scaffold-orchestrator python scripts/openapi_snapshot.py

    # Compare the live spec against the committed snapshot mounted in
    docker exec scaffold-orchestrator python scripts/openapi_snapshot.py --check

The write path uses stdout (not a file write) so the host process owns the
file — bind-mount permissions stay clean. ``--check`` reads the committed
file directly through the read-side of the bind mount.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT_PATH = REPO_ROOT / "docs" / "openapi.json"


def _dump(spec: dict) -> str:
    """Deterministic JSON serialization. Sorted keys + 2-space indent +
    trailing newline so the file is `git diff`-friendly."""
    return json.dumps(spec, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def _generate() -> str:
    # Imported here so ``--help`` works even if the app fails to import.
    from app.main import app

    return _dump(app.openapi())


def _check(current: str) -> int:
    if not SNAPSHOT_PATH.exists():
        print(
            f"ERROR: {SNAPSHOT_PATH.relative_to(REPO_ROOT)} is missing. "
            "Run `make openapi-snapshot` to create it.",
            file=sys.stderr,
        )
        return 2

    committed = SNAPSHOT_PATH.read_text(encoding="utf-8")
    if committed == current:
        print(f"OK: {SNAPSHOT_PATH.relative_to(REPO_ROOT)} matches the live OpenAPI spec.")
        return 0

    print(
        f"DRIFT: {SNAPSHOT_PATH.relative_to(REPO_ROOT)} disagrees with the live OpenAPI spec.\n"
        "If the change is intentional, regenerate with `make openapi-snapshot`,\n"
        "review the diff, and bump the FastAPI `version=` field if it is a breaking change.",
        file=sys.stderr,
    )
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify docs/openapi.json matches the live spec; exit non-zero on drift.",
    )
    args = parser.parse_args(argv)

    # §17.442 — generating the spec imports app.main, which configures logging to
    # bind a StreamHandler to sys.stdout AND (with OTEL_ENABLED, §17.438) logs an
    # `otel_fastapi_instrumented` line plus config-validator warnings at import
    # time. Those would land on stdout and corrupt the `> docs/openapi.json`
    # redirect (the snapshot's first line became a log JSON object — §17.441 #6).
    # Point stdout at stderr for the duration of generation so the StreamHandler
    # binds to stderr and only the spec reaches real stdout.
    real_stdout = sys.stdout
    sys.stdout = sys.stderr
    try:
        current = _generate()
    finally:
        sys.stdout = real_stdout

    if args.check:
        return _check(current)

    sys.stdout.write(current)
    return 0


if __name__ == "__main__":
    sys.exit(main())
