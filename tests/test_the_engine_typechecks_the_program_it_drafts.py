"""§17.1380 — the engine type-checks the program it drafts, before offering Run.

ADD132 drafted fourteen times across §17.1343–1379. The shell-side defects were
about machines and `service_truth` owns them; the Python-side defects were about
the program, and each one arrived LIVE on the operator's host and earned one
narrow static gate. This is the general check behind those specific ones.

The corpus measurement is `tests/fixtures/add132_typecheck_corpus_2026_10_05.json`,
taken in the production image over ADD132's own thirteen drafts through the
engine's own extractor: 14 programs, 13 parseable, and — with no allowlist at
all — exactly three mypy errors, every one real, zero false positives.
"""
from __future__ import annotations

import json
import logging
import pathlib
import re

import pytest

from app.modules import program_check as pc
from app.modules import supervised_runs as sr
from app.modules.program_check import a_program_the_type_checker_says_will_raise as GATE
from app.modules.supervised_runs import (FILE_RULES, _inline_programs,
                                         a_call_its_own_function_cannot_accept,
                                         a_program_that_uses_a_name_it_never_defines)

FIX = pathlib.Path(__file__).parent / "fixtures"
CORPUS = json.loads((FIX / "add132_typecheck_corpus_2026_10_05.json").read_text())
#: §17.1379's draft — the password set in `fields`, the whole object POSTed to
#: `/test`, `priority` raised only when below 1, and `input=` passed to a
#: two-argument helper at both call sites.
TYPEERROR = json.loads((FIX / "add132_call_its_own_function_rejects_2026_10_05.json").read_text())
#: §17.1377's draft — a heredoc whose only import was `json`, reaching for `os`.
NAMEERROR = json.loads((FIX / "add132_body_on_the_wrong_machine_2026_10_05.json").read_text())


def _progs(fixture: dict) -> list[tuple]:
    """Through the engine's own extractor, exactly as frame_run feeds the gate."""
    return _inline_programs([str(c) for c in (fixture.get("commands") or [])],
                            fixture.get("files") or [])


def _one(src: str) -> list[dict]:
    return GATE([("/tmp/prog.py", src)])


# ── the checker has to be there ──────────────────────────────────────────────

def test_mypy_is_installed_in_this_image():
    """§17.1359 — a gate that is off because a tool is missing fails HERE, loudly,
    rather than reporting every program clean in production."""
    import importlib.util
    assert importlib.util.find_spec("mypy") is not None, (
        "mypy is pinned in requirements.txt as a RUNTIME dependency of this gate "
        "(§17.1380). Without it the engine type-checks nothing.")


# ── the two defects that cost a live run each ───────────────────────────────

def test_the_live_typeerror_is_refused_at_both_call_sites():
    refused = GATE(_progs(TYPEERROR))
    assert len(refused) == 2, refused
    assert {r["command"] for r in refused} == {
        "/tmp/fix_download_clients.py: line 42",
        "/tmp/fix_download_clients.py: line 55"}
    for r in refused:
        assert 'Unexpected keyword argument "input" for "pct_exec"' in r["why"]
        assert "Python raises TypeError the moment it runs" in r["why"]
        # §17.1375 — the remedy, with the measurement that justifies it
        assert "def pct_exec(ctid, cmd, input=None)" in r["why"]


def test_the_live_nameerror_is_refused():
    refused = GATE(_progs(NAMEERROR))
    assert len(refused) == 1, refused
    assert 'Name "os" is not defined' in refused[0]["why"]
    assert "Python raises NameError the moment it runs" in refused[0]["why"]
    assert "import json, os" in refused[0]["why"]


def test_what_the_typeerror_draft_got_right_is_not_refused_for_anything_else():
    """The draft had three entries' worth of lessons taken. The gate must say
    only what it is for — a later reading of this test should not mistake the
    type error for a verdict on the rest of the program."""
    refused = GATE(_progs(TYPEERROR))
    assert all("input" in r["why"] for r in refused)
    assert TYPEERROR["what_it_got_right"]


# ── the point: shapes the two shipped gates cannot see ──────────────────────

MISSING_POSITIONAL = """
import subprocess
def pct_exec(ctid, cmd):
    return subprocess.run(["pct", "exec", str(ctid), "--", "sh", "-c", cmd])
pct_exec(103)
"""

USED_BEFORE_DEF = """
import json
print(json.dumps(payload()))
def payload():
    return {"id": 1}
"""


@pytest.mark.parametrize("src,needle", [
    (MISSING_POSITIONAL, 'Missing positional argument "cmd" in call to "pct_exec"'),
    (USED_BEFORE_DEF, 'Name "payload" is used before definition'),
])
def test_a_shape_neither_shipped_gate_catches_is_refused(src, needle):
    """§17.1379 reads too MANY positionals and a bad keyword; §17.1377 reads
    undefined names at module level. Neither sees these, and both raise at the
    line that runs them. This is the whole reason the general check exists."""
    refused = _one(src)
    assert len(refused) == 1, refused
    assert needle in refused[0]["why"]
    f = [{"path": "/tmp/prog.py", "content": src}]
    assert a_call_its_own_function_cannot_accept([], f) == []
    assert a_program_that_uses_a_name_it_never_defines([], f) == []


# ── everything it must NOT claim ─────────────────────────────────────────────

CORRECTED = """
import subprocess
def pct_exec(ctid, cmd, input=None):
    return subprocess.run(["pct", "exec", str(ctid), "--", "sh", "-c", cmd],
                          input=input, capture_output=True, text=True)
r = pct_exec(103, "curl -s -d @- http://127.0.0.1:7878/x", input='{"id": 1}')
print(r.stdout)
"""

THIRD_PARTY_IMPORT = """
import requests
r = requests.put("http://127.0.0.1:7878/api/v3/downloadclient/1", json={"id": 1}, timeout=10)
print(r.status_code)
"""

A_TYPE_OPINION = """
import json
port: int = "7878"
print(json.dumps({"port": port}))
"""

DYNAMIC_DICT_WORK = """
import json, os, sys
obj = json.load(sys.stdin)
for f in obj["fields"]:
    if f["name"] == "password":
        f["value"] = os.environ["MASS_PASSWORD"]
print(json.dumps(obj))
"""


@pytest.mark.parametrize("name,src", [
    ("the corrected helper", CORRECTED),
    ("a module the IMAGE lacks and the GUEST has", THIRD_PARTY_IMPORT),
    ("a type opinion, not a crash", A_TYPE_OPINION),
    ("the dynamic dict work every one of these drafts does", DYNAMIC_DICT_WORK),
])
def test_says_nothing_about(name, src):
    assert _one(src) == [], name


def test_an_unparseable_program_is_not_judged():
    """A heredoc the shell interpolates (`$VAR` where Python wants a name) is
    §17.1364's judgment, not this one — and MEASURED on the real corpus,
    batching one unparseable program with the rest suppressed mypy's findings on
    every other program in the batch."""
    assert GATE([("python3 <<EOF", "import json\nkey = $RADARR_KEY\n")]) == []
    assert CORPUS["corpus"]["programs_dropped_unparseable"] == 1


def test_no_programs_is_no_work():
    assert GATE([]) == []
    assert GATE([("x.py", "   ")]) == []


def test_only_three_codes_can_ever_be_refused():
    """The allowlist bounds what the gate can claim. Widening it is a decision
    with a measurement behind it, not a side effect."""
    assert set(pc._FATAL_CODES) == {"name-defined", "used-before-def", "call-arg"}
    assert set(pc._REMEDY) == set(pc._FATAL_CODES)


# ── fail-soft, but never silently ────────────────────────────────────────────

def test_a_missing_checker_refuses_nothing_and_says_so(monkeypatch, caplog):
    import importlib.util
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    with caplog.at_level(logging.WARNING, logger="scaffold"):
        assert GATE(_progs(TYPEERROR)) == []
    assert "program_typecheck_unavailable" in caplog.text


def test_exit_one_with_no_output_is_not_a_clean_program(monkeypatch, caplog):
    """MEASURED on this gate's own first run: `python -m mypy` with no mypy
    installed exits 1 — the same code as "errors found" — and prints nothing, so
    parsing the output read as "clean". The gate reported fourteen programs
    clean including the fixture that IS the §17.1379 TypeError. The §17.1359
    shape, caught in the gate built to end it."""
    import subprocess as _sp

    class _R:
        returncode, stdout, stderr = 1, "", "No module named mypy"

    monkeypatch.setattr(_sp, "run", lambda *a, **k: _R())
    with caplog.at_level(logging.WARNING, logger="scaffold"):
        assert GATE(_progs(TYPEERROR)) == []
    assert "program_typecheck_unavailable" in caplog.text


def test_a_timeout_refuses_nothing(monkeypatch, caplog):
    import subprocess as _sp

    def _boom(*a, **k):
        raise _sp.TimeoutExpired(cmd="mypy", timeout=pc._TIMEOUT_S)

    monkeypatch.setattr(_sp, "run", _boom)
    with caplog.at_level(logging.WARNING, logger="scaffold"):
        assert GATE(_progs(TYPEERROR)) == []
    assert "program_typecheck_timeout" in caplog.text


# ── wired where it has to be ─────────────────────────────────────────────────

def test_the_gate_runs_in_frame_run_fed_by_the_one_extractor():
    """§17.1379 — two extractors is how one judgment sees a shape the other
    misses. The type check reads the same programs the other two do."""
    import inspect
    src = inspect.getsource(sr.frame_run)
    assert "a_program_the_type_checker_says_will_raise(" in src
    assert "_inline_programs(cmds, shape_files)" in src


def test_the_refusal_drives_a_redraft():
    """§17.1269 — unregistered, this would park the frame with Run greyed out
    instead of handing the drafter the type error to fix."""
    refused = GATE(_progs(TYPEERROR))
    assert refused
    assert sr.shape_retry_note({"kind": "run", "refused": refused})


def test_mypy_is_pinned_as_a_runtime_dependency():
    reqs = (pathlib.Path(__file__).parents[1] / "requirements.txt").read_text()
    assert re.search(r"^mypy==\d+\.\d+\.\d+$", reqs, re.M), (
        "the gate runs inside the orchestrator, so mypy belongs in "
        "requirements.txt — not requirements-dev.txt")


# ── the rules must not teach what the gate refuses ──────────────────────────

def test_every_python_example_in_the_rules_passes_the_gate():
    """§17.1377's lesson, and the one that bit while this was written: FILE_RULES'
    batching example looped over an `items` nothing defined, so the engine's own
    rules taught a program its new gate refuses."""
    blocks = re.findall(r"```python\n(.*?)```", FILE_RULES, re.S)
    assert len(blocks) >= 4
    for i, b in enumerate(blocks):
        assert GATE([(f"FILE_RULES block {i}", b)]) == [], b
    assert CORPUS["file_rules_examples_refused"] == 0


def test_the_rules_say_the_program_is_type_checked():
    assert "TYPE-CHECKS every program you write here" in FILE_RULES
    assert "a call its own `def` rejects" in FILE_RULES


# ── the measurement is the fixture ───────────────────────────────────────────

def test_the_corpus_measurement_is_recorded_and_matches():
    """A finding per draft, read and real — and the gate still produces exactly
    those on exactly those programs."""
    assert CORPUS["false_positives"] == 0
    assert CORPUS["mypy_with_no_allowlist_at_all"]["_every_other_code"] == 0
    by_fixture: dict[str, list] = {}
    for flag in CORPUS["flags"]:
        by_fixture.setdefault(flag["fixture"], []).append(flag)
    assert len(CORPUS["flags"]) == 3
    for name, flags in by_fixture.items():
        fixture = json.loads((FIX / f"{name}.json").read_text())
        refused = GATE(_progs(fixture))
        assert len(refused) == len(flags), (name, refused)
        for flag in flags:
            assert any(f"line {flag['line']}" in r["command"]
                       and flag["message"] in r["why"] for r in refused), (name, flag)


def test_the_other_tools_measured_on_the_same_corpus_are_recorded():
    """Why mypy and not the alternatives — the negative results matter most:
    shellcheck had nothing to say about the nine shell drafts, so the shell side
    of this arc needed the machine gates it got, not a linter."""
    other = CORPUS["other_tools_measured_on_the_same_corpus"]
    assert "the same three findings" in other["pyright_2026_10"]
    assert "no arity check" in other["pyflakes"]
    assert "0 error, 0 warning" in other["shellcheck_0_10_0"]
