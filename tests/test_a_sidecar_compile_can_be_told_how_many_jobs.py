"""§17.1497 — the verilator sidecar's compile takes its job count from compose.

Deploying #877 on the operator's laptop: `make -j "$(nproc)"` at -j8 and at -j3 segfaulted g++
five times in five different files; the same source and compiler built clean at -j1. The only
way to cap the jobs was the deprecated legacy builder with `--cpuset-cpus` (BuildKit ignores the
flag). Now `VERILATOR_MAKE_JOBS=1 docker compose --profile sandbox build scaffold-verilator`
does it, and an unset variable still means `nproc`.
"""
from __future__ import annotations

import pathlib
import re
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
DOCKERFILE = ROOT / "docker" / "verilator" / "Dockerfile"


def test_the_dockerfile_declares_the_arg_in_both_scopes_and_uses_it():
    if not DOCKERFILE.exists():
        pytest.skip("docker/ is not mounted in this lane (CI's unit container); checked by make test on the host")
    src = DOCKERFILE.read_text(encoding="utf-8")
    assert re.search(r"^ARG MAKE_JOBS=$", src, re.M), "global ARG with an empty default (= nproc)"
    builder = src[src.index("AS builder"):src.index("# Stage 2")]
    assert re.search(r"^ARG MAKE_JOBS$", builder, re.M), "re-declared inside the builder stage, where RUN reads it"
    assert 'make -j "${MAKE_JOBS:-$(nproc)}"' in builder
    assert 'make -j "$(nproc)"' not in src


def test_compose_passes_the_operators_value_and_the_variable_is_documented():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    block = compose[compose.index("  scaffold-verilator:"):compose.index("container_name: scaffold-verilator")]
    assert "MAKE_JOBS: ${VERILATOR_MAKE_JOBS:-}" in block
    env = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert re.search(r"^# VERILATOR_MAKE_JOBS=$", env, re.M)


def test_the_shell_expansion_falls_back_to_nproc_only_when_unset_or_empty():
    """The exact word the Dockerfile hands make, run for real."""
    word = '"${MAKE_JOBS:-$(nproc)}"'
    nproc = subprocess.run(["nproc"], capture_output=True, text=True).stdout.strip()
    for env, want in (({}, nproc), ({"MAKE_JOBS": ""}, nproc), ({"MAKE_JOBS": "1"}, "1"), ({"MAKE_JOBS": "3"}, "3")):
        out = subprocess.run(["sh", "-c", f"printf '%s' {word}"], capture_output=True, text=True,
                             env={"PATH": "/usr/bin:/bin", **env}).stdout
        assert out == want, (env, out)
