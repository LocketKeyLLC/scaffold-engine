"""Uninstall dev-only deps in the runtime image.

Reads requirements-dev.txt, extracts package names (ignoring comments,
markers, version pins, extras), and uninstalls them via pip.
"""
from __future__ import annotations
import re
import subprocess
import sys
from pathlib import Path


def extract_names(req_file: Path) -> list[str]:
    names: list[str] = []
    for raw in req_file.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        name = re.split(r"[<>=!~\[\s;]", line, 1)[0].strip()
        if name:
            names.append(name)
    return names


def main() -> int:
    req = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/requirements-dev.txt")
    names = extract_names(req)
    if not names:
        return 0
    return subprocess.call(["pip", "uninstall", "-y", *names])


if __name__ == "__main__":
    sys.exit(main())
