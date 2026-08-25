#!/usr/bin/env python3
"""§17.823 (audit M15 / plan 6.5) — .env.example completeness gate.

Every ``${VAR}`` / ``${VAR:-default}`` / ``${VAR:?msg}`` interpolation in the
compose files must be documented in .env.example (as ``VAR=`` or ``# VAR=``).
Before this gate, 42 operator-tunable vars (the whole assist feature-flag
surface, MCP, research caps, sidecar tags…) were reachable only by reading
docker-compose.yml — and the set drifted silently every time a flag landed.

Wired into ``make ci-tier-0`` (static parity tier — no services needed).
Comment lines in compose are ignored (a literal ``${VAR}`` appears in prose).

Exit 0 = complete; exit 1 = lists the missing vars.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
COMPOSE_FILES = ["docker-compose.yml", "docker-compose.dev.yml"]

# Compose-internal knobs an operator never sets per-install via .env.example
# documentation (add sparingly, with a reason):
ALLOWLIST: set[str] = set()


def compose_vars() -> dict[str, str]:
    """{var: 'file:line'} for every interpolation outside comment lines."""
    found: dict[str, str] = {}
    for name in COMPOSE_FILES:
        path = REPO / name
        if not path.exists():
            continue
        for i, line in enumerate(path.read_text().splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            for var in re.findall(r"\$\{([A-Z][A-Z0-9_]+)[:\}?]", line):
                found.setdefault(var, f"{name}:{i}")
    return found


def documented_vars() -> set[str]:
    text = (REPO / ".env.example").read_text()
    return set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]+)=", text, re.M))


def main() -> int:
    compose = compose_vars()
    documented = documented_vars()
    missing = sorted(v for v in compose if v not in documented and v not in ALLOWLIST)
    if missing:
        print(f"✗ .env.example is missing {len(missing)} compose-interpolated var(s):")
        for v in missing:
            print(f"    {v:<40} first seen {compose[v]}")
        print("  Document each (a commented '# VAR=default  # meaning' line is enough).")
        return 1
    print(f"✓ .env.example documents all {len(compose)} compose-interpolated vars")
    return 0


if __name__ == "__main__":
    sys.exit(main())
