"""§17.1998 — the runtime image carries the labs' tests, where a lab fix step's keep-list reads them.

Host-only (`make ci-tier-0`): the Dockerfile is not mounted in the container lane. Live (69fe61c4 T8, 2026-11-22):
§17.1992's `engine_source.module_tests` read the lab's test file from `ENGINE_PKG.parent / "tests" / "labs"`, which
the production stage never copied (no tests at all), so a follow-up job's fix step was told of none of the 8 and
dropped them all. The suite runs in the dev image, which carries every test -- it hid the gap. This reads the
production stage itself."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_DOCKERFILE = _ROOT / "Dockerfile"
pytestmark = pytest.mark.skipif(not _DOCKERFILE.exists(),
                                reason="host-only static gate — runs in `make ci-tier-0`, not the container lane")


def _runtime_copies() -> dict[str, str]:
    m = re.search(r"^FROM [^\n]* AS runtime\n(.*?)(?=^FROM )", _DOCKERFILE.read_text(), re.M | re.S)
    assert m, "Dockerfile has no runtime stage"
    return {src.rstrip("/"): dst.rstrip("/")
            for src, dst in re.findall(r"^COPY\s+(?:--\S+\s+)*(\S+)\s+(\S+)\s*$", m.group(1), re.M)}


def test_the_runtime_image_carries_the_labs_tests_beside_its_app():
    copies = _runtime_copies()
    # `engine_source._engine_test_file` reads `ENGINE_PKG.parent / "tests"`: beside the image's `app/`
    assert copies.get("app") == "/code/app" and copies.get("tests/labs") == "/code/tests/labs", copies


def test_the_runtime_image_carries_no_other_tests():
    assert not any(src == "tests" or (src.startswith("tests/") and src != "tests/labs") for src in _runtime_copies())
