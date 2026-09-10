"""§17.1004 — the three lists of "what the tests can see" must agree.

Runs in `make ci-tier-0` (host pytest, no docker): it reads the Dockerfile, the
dev compose file and the CI workflow straight from the checkout. Putting it in
the containerised lane would have required mounting those three files into the
container — i.e. adding entries to the very lists it exists to police.

A path the suite READS has to be declared in three places, and they are
hand-kept:

  1. Dockerfile          — COPY into the dev/test stage
  2. docker-compose.dev.yml — bind mounts for the local lane
  3. .github/workflows/test.yml — `-v` mounts for the CI lane, which PULLS a
     prebuilt image (§17.588) rather than building the PR

Miss one and the lanes disagree. This happened three times in a day: the local
lane passed while CI failed on a missing file, then on a STALE file, because a
bind mount masked what the image actually carried. Each time the fix was to add
the path to whichever list had missed it — the instance, not the class.

This is the class. It compares the `/code/...` paths the three lists grant and
fails when one has a path the others do not.
"""
import pathlib
import re

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent

# Host-only. The three files this reads are build/CI config, deliberately NOT
# mounted into the test container — mounting them would mean adding entries to
# the very lists this polices. `make ci-tier-0` runs it on the host by name,
# where they all exist; the containerised lane skips it rather than failing on
# inputs it was never meant to have.
_INPUTS = (_ROOT / "Dockerfile",
           _ROOT / "docker-compose.dev.yml",
           _ROOT / ".github" / "workflows" / "test.yml")
pytestmark = pytest.mark.skipif(
    not all(f.exists() for f in _INPUTS),
    reason="host-only static gate — runs in `make ci-tier-0`, not the container lane")

# Runtime state, not source: nothing in the suite reads these, and mounting a
# host path over them would be wrong.
_RUNTIME_STATE = {"/code/.cache"}


def _dockerfile_dev_paths() -> set[str]:
    """COPY targets in the dev/test stage (everything after its FROM)."""
    text = (_ROOT / "Dockerfile").read_text()
    idx = text.rfind("COPY --chown=root:root tests/")
    assert idx > 0, "could not locate the dev stage in the Dockerfile"
    stage = text[text.rfind("FROM", 0, idx):]
    return {m.rstrip("/") for m in re.findall(r"^COPY\s+[^\n]*?\s(/code/\S+)",
                                             stage, re.M)}


def _compose_dev_paths() -> set[str]:
    text = (_ROOT / "docker-compose.dev.yml").read_text()
    return {m.rstrip("/") for m in re.findall(r"-\s+\./\S+:(/code/\S+?):?r?o?\s*$",
                                             text, re.M)}


def _ci_workflow_paths() -> set[str]:
    text = (_ROOT / ".github" / "workflows" / "test.yml").read_text()
    return {m.rstrip("/") for m in re.findall(
        r'-v\s+"\$PWD/\S+?:(/code/\S+?)(?::ro)?"', text)}


def test_ci_mounts_everything_the_image_bakes():
    """The invariant, and it is automatic rather than a curated list.

    CI PULLS a prebuilt image (§17.588) instead of building the PR, so any file
    baked into that image is a file CI may read a STALE copy of. Mounting the
    checkout over every baked path is what makes the CI lane test the PR.

    The local lane may mount MORE than this (Dockerfile, .github, requirements)
    for its own conveniences — that direction is harmless and deliberately not
    asserted. What is not harmless is the image carrying something CI never
    overlays.
    """
    baked = _dockerfile_dev_paths() - _RUNTIME_STATE
    ci = _ci_workflow_paths()
    missing = sorted(baked - ci)
    assert not missing, (
        f"baked into the dev image but NOT mounted by the CI lane: {missing} — "
        "CI would read the prebuilt image's copy instead of the PR's. This bit "
        "three times in one day (§17.1002/§17.1004): first a missing file, then "
        "a stale one.")


def test_the_parsers_actually_found_something():
    """A regex that silently matches nothing would make both checks vacuous."""
    for name, found in (("Dockerfile", _dockerfile_dev_paths()),
                        ("compose.dev", _compose_dev_paths()),
                        ("ci workflow", _ci_workflow_paths())):
        assert len(found) >= 5, f"{name}: parsed only {found}"
    for required in ("/code/app", "/code/tests", "/code/presets"):
        assert required in _compose_dev_paths(), required
        assert required in _ci_workflow_paths(), required
        assert required in _dockerfile_dev_paths(), required
