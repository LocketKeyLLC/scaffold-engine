r"""§17.1372 — an empty check is not a contradiction, and §17.1372b — a diagnosis
that contradicts the record is refused.

Live, 2026-10-05. ADD135 set the boot flags the operator had just asked for, every
command exited 0, and the step was recorded **failed**:

    every command exited 0, but this step's own verify checks say the work did not
    land: no output — neither onboot nor startup found in 100.conf or 110.conf

That fourth check exists to confirm VMs 100 and 110 were **left alone** — the
step's own text says *"Leave VM 100 gpu-vm and VM 110 ai-vm alone: they are
workbenches the operator starts when needed"* — so finding nothing is the correct
answer. Measured on the machine immediately afterwards:

    /etc/pve/qemu-server/106.conf:onboot: 1   startup: order=10
    /etc/pve/lxc/111.conf:        onboot: 1   startup: order=20
    /etc/pve/lxc/120.conf:        onboot: 1   startup: order=30
    VMs 100 and 110:              (nothing, as intended)

The work had landed exactly as drafted. A check that printed nothing is
AMBIGUOUS, and the judge's own comment already says an ambiguous check must never
fail work that really happened — so an empty answer is `unknown` unless the
runbook said what the check should print.

Then the engine diagnosed its own false failure, wrongly:

    "The most likely cause is that the command was run through the local runner
     (pve-runner), which refuses mutation verbs — it returned a success-looking
     exit code without actually changing anything."

Nothing in the record carries a runner refusal, every command exited 0, and the
first printed the tool's own confirmation: `update VM 106: -onboot 1 -startup
order=10`. §17.1366 feeds diagnoses into the NEXT draft, so a false cause does
not just mislead the operator — it steers the retry.
"""
from __future__ import annotations

import json
import pathlib

from app.modules.supervised_runs import (diagnosis_contradicts_the_record,
                                         empty_checks)

RUN = json.loads((pathlib.Path(__file__).parent / "fixtures"
                  / "add135_false_fail_2026_10_05.json").read_text())
#: the paste shape ADD135's four checks produced: three answered, one silent
PASTE = ("== V1 ==\n/etc/pve/qemu-server/106.conf:onboot: 1\n"
         "== V2 ==\n/etc/pve/lxc/111.conf:onboot: 1\n"
         "== V3 ==\n/etc/pve/lxc/120.conf:onboot: 1\n"
         "== V4 ==\n\n")


# ------------------------------------------- which checks said nothing at all


def test_the_silent_check_is_the_one_that_was_meant_to_find_nothing():
    assert empty_checks(PASTE) == {"V4"}


def test_both_marker_spellings_are_read():
    """`run_probes` writes `== V1 ==`; the judge's own ids are `V:1` (§17.1302b)."""
    assert empty_checks("== V:1 ==\nout\n== V:2 ==\n   \n") == {"V2"}


def test_a_paste_where_everything_answered_has_no_empty_checks():
    assert empty_checks("== V1 ==\na\n== V2 ==\nb\n") == set()
    assert empty_checks("") == set()
    assert empty_checks(None) == set()


def test_whitespace_is_not_an_answer():
    assert empty_checks("== V1 ==\n\n   \n\t\n") == {"V1"}


def test_the_last_section_counts_too():
    """The silent check was the LAST one — an off-by-one here hides the bug."""
    assert "V3" in empty_checks("== V1 ==\na\n== V2 ==\nb\n== V3 ==\n")


# --------------------------------------------- the judge must not fail on silence


def test_the_downgrade_is_wired_into_the_verdicts():
    import inspect

    from app.modules import supervised_runs as sr
    src = inspect.getsource(sr._verify_verdicts)
    assert "empty_checks(pasted)" in src
    assert 'v["verdict"] = "unknown"' in src
    assert "verify_empty_not_contradicted" in src
    # a stated expectation still lets an empty answer contradict it
    assert "_expects.get(_cmd)" in src
    # and only `contradicted` is touched — a confirmed check is left alone
    assert 'str(v.get("verdict") or "") != "contradicted"' in src


def test_only_contradicted_verdicts_still_downgrade_a_step():
    """§17.1233's asymmetry is unchanged: `unknown` leaves the outcome alone, so
    turning the empty check into `unknown` is exactly what stops the false fail."""
    import inspect

    from app.modules import supervised_runs as sr
    assert "contradicted" in inspect.getsource(sr.contradicted)
    assert sr.contradicted([{"verdict": "unknown"}, {"verdict": "confirmed"}]) == []
    assert len(sr.contradicted([{"verdict": "contradicted"}])) == 1


# ------------------------------- §17.1372b — the diagnosis against the record


def test_the_live_diagnosis_is_refused_by_the_record():
    why = diagnosis_contradicts_the_record(RUN["executed"], RUN["diagnosis"])
    assert why, RUN["diagnosis"][:200]
    assert "3 command(s) ran, every one exited 0" in why
    assert "none is marked refused" in why
    assert "update VM 106" in why          # the tool's own output, quoted back


def test_a_record_that_really_holds_a_refusal_is_left_alone():
    refused = [{"command": "pct set 111 --onboot 1",
                "output": "(refused by the local runner: pct set is not allowed)",
                "exit": 0, "refused": True}]
    assert diagnosis_contradicts_the_record(refused, RUN["diagnosis"]) == ""
    # also by the marker alone, with the flag unset
    marked = [{"command": "x", "output": "(refused by the local runner: nope)", "exit": 0}]
    assert diagnosis_contradicts_the_record(marked, RUN["diagnosis"]) == ""


def test_a_real_failure_is_not_this_checks_business():
    failed = [{"command": "qm set 106 --onboot 1", "output": "boom", "exit": 1, "ok": False}]
    assert diagnosis_contradicts_the_record(failed, RUN["diagnosis"]) == ""


def test_a_diagnosis_that_claims_nothing_of_the_kind_passes():
    for ok in ("The config file has no [Preferences] section, so the append landed elsewhere.",
               "Radarr requires configContract on every download client.",
               ""):
        assert diagnosis_contradicts_the_record(RUN["executed"], ok) == "", ok


def test_every_phrasing_of_the_claim_is_caught():
    for claim in ("the runner refuses mutation verbs",
                  "it returned a success-looking exit code",
                  "the command ran without actually changing anything",
                  "the runner did not actually apply it",
                  "the runner silently did nothing"):
        assert diagnosis_contradicts_the_record(RUN["executed"], claim), claim


def test_the_replacement_names_the_record_and_not_a_cause():
    import inspect

    from app.modules import supervised_runs as sr
    src = inspect.getsource(sr.diagnose_failure)
    assert "diagnosis_contradicts_the_record(executed, out)" in src
    assert "supervised_run_diagnosis_refused" in src
    assert "What the record actually says" in src
    assert "with nothing inferred" in src
    # and it points at the likelier cause: a wrong CHECK, not wrong work
    assert "a check that prints nothing says nothing" in src


def test_the_fixture_is_the_real_run():
    assert len(RUN["executed"]) == 3
    assert all(e["exit"] == 0 for e in RUN["executed"])
    assert "update VM 106: -onboot 1 -startup order=10" in RUN["executed"][0]["output"]
    assert "refuses mutation verbs" in RUN["diagnosis"]
    assert "no output" in RUN["reason"]
