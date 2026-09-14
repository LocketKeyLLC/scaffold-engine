"""§17.1057 — the ast-grep rule set bites, and the repo is clean against it.

Two halves, both required: the repo scan reports zero errors, AND each rule
hits its known-hit fixture. The second half is the §17.906 lesson — a gate
that is green because its pattern matches nothing is worse than no gate.
Part of ci-tier-0; the binary is `ast-grep` (pip: ast-grep-cli, or the
release binary as `sg`/`ast-grep` on PATH).
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "ast_grep"


def _find_bin() -> str | None:
    """`ast-grep` by name; `sg` only when it really is ast-grep — on Debian
    `/usr/bin/sg` is shadow-utils' switch-group, which the first CI run
    happily invoked (0 fixture hits, "pattern is inert")."""
    b = shutil.which("ast-grep")
    if b:
        return b
    sg = shutil.which("sg")
    if sg:
        try:
            out = subprocess.run([sg, "--version"], capture_output=True, text=True, timeout=10)
            if "ast-grep" in (out.stdout + out.stderr).lower():
                return sg
        except Exception:  # noqa: BLE001
            pass
    return None


BIN = _find_bin()
# The gate is enforced where ci-tier-0 runs (the pre-push hook and the CI
# smoke job set SCAFFOLD_REQUIRE_AST_GREP=1 and install the binary). Other
# lanes without it skip LOUDLY rather than pass vacuously.
REQUIRED = os.environ.get("SCAFFOLD_REQUIRE_AST_GREP") == "1"

EXPECTED_HITS = {
    "capture-assistant-reply-needs-node-key": 1,
    "replan-add-step-anchors-on-original-step": 1,
    "decide-tool-call-thinks-not": 1,
    "local-storage-read-needs-try": 1,
}


def _scan(cwd: pathlib.Path, config: pathlib.Path) -> list[dict]:
    if not BIN:
        msg = ("ast-grep is not installed — `pip install ast-grep-cli==0.45.3` or put the "
               "release binary on PATH (§17.1057)")
        if REQUIRED:
            raise AssertionError(msg + "; this gate must not be skipped in ci-tier-0")
        pytest.skip(msg + "; enforced in ci-tier-0")
    out = subprocess.run([BIN, "scan", "-c", str(config), "--json"], cwd=cwd,
                         capture_output=True, text=True)
    assert out.returncode in (0, 1), out.stderr[-800:]
    return json.loads(out.stdout or "[]")


def test_every_rule_hits_its_fixture():
    hits = _scan(FIXTURES, FIXTURES / "sgconfig.yml")
    by_rule: dict[str, int] = {}
    for h in hits:
        by_rule[h["ruleId"]] = by_rule.get(h["ruleId"], 0) + 1
    for rule, n in EXPECTED_HITS.items():
        assert by_rule.get(rule, 0) == n, f"{rule}: expected {n} fixture hit(s), got {by_rule.get(rule, 0)} — the pattern is inert or over-matching"


def test_repo_has_no_rule_errors():
    hits = _scan(ROOT, ROOT / "sgconfig.yml")
    errors = [h for h in hits if h.get("severity") == "error"
              and not str(h.get("file", "")).startswith("tests/fixtures/")]
    assert not errors, "\n".join(f"{h['file']}:{h['range']['start']['line'] + 1} {h['ruleId']}" for h in errors)
