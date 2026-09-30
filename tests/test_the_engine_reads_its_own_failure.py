"""§17.1247 — a failed run never reached the next draft, so a person was the loop.

ADD111 ("set up Pi-hole") needed five runs. Four of them re-made a mistake the
engine had already seen: a template version it had invented, a `pveam download`
it had dropped, a privileged read that had already come back refused. The engine
held every one of those facts — the reason on `dag_nodes.last_verification_reason`,
the diagnosis in `output_text`, and `pending_hands_on` even SELECTing
`retry_count` and `last_verification_reason` onto the dict it hands the drafter.

`draft_runbook` read none of it. It calls `build_base_prompt` directly, so it
never sees `_format_reviewer_feedback` — which only the ordinary LLM node path
uses — and every redraft of a failed hands-on step was a FIRST attempt as far as
the model could tell. The only path from a failure to the next draft was a human
pasting it into the step's description.

And wiring the existing helper in would not have worked: `_format_reviewer_feedback`
is gated on `retry_count > 0`, while `reset_node` deliberately does not bump the
counter — and a reset is how a supervised step gets another attempt.
"""
from __future__ import annotations

import inspect

from app.modules import supervised_runs as sr

REPORT = """## Run this

```bash
pveam download local debian-12-standard_12.12-1_amd64.tar.zst
```

## Executed on pve-runner (supervised, operator-approved)
$ pveam download local debian-12-standard_12.12-1_amd64.tar.zst
downloading ... OK
$ pct create 130 local:vztmpl/debian-12-standard_12.12-1_amd64.tar.zst --hostname pihole
  Logical volume "vm-130-disk-0" created.
$ pct start 130
$ pct exec 130 -- bash -c "curl -sSL https://install.pi-hole.net | bash /dev/stdin --unattended"
  Pi-hole Automated Installer  Welcome
rc=1

## Verify results
$ pct status 130
status: running
"""


def test_the_commands_of_the_last_attempt_are_read_back_in_order():
    got = sr.executed_commands(REPORT)
    assert len(got) == 4
    assert got[0].startswith("pveam download")
    assert got[-1].startswith("pct exec 130")
    # the Verify section's reads are NOT part of what was sent
    assert not any(c == "pct status 130" for c in got)


def test_a_report_with_no_executed_section_yields_nothing():
    assert sr.executed_commands("## Run this\n\nstuff\n") == []
    assert sr.executed_commands("") == []


def test_the_block_names_where_it_stopped_and_what_had_already_happened():
    """`run_block` stops at the first failure, so the last command is the one
    that failed and everything before it is done — which is why a redraft that
    starts over dies on its own earlier `pct create`."""
    b = sr.attempt_feedback({"last_verification_reason": "exited 1: the installer showed its dialog",
                             "output_text": REPORT})
    assert "PREVIOUS ATTEMPT AT THIS STEP FAILED" in b
    assert "exited 1: the installer showed its dialog" in b
    assert "[FAILED HERE] pct exec 130" in b
    assert "[ok] pveam download" in b
    assert "FINISH FROM THERE" in b
    assert "pct status N >/dev/null 2>&1 || pct create N" in b


def test_it_is_gated_on_the_reason_not_on_retry_count():
    """`reset_node` does not bump retry_count, and a reset is how a supervised
    step gets another attempt — so `retry_count > 0` would never fire here."""
    assert sr.attempt_feedback({"output_text": REPORT}) == ""
    assert sr.attempt_feedback({"last_verification_reason": "   ", "output_text": REPORT}) == ""
    # retry_count 0 with a reason MUST still produce the block
    b = sr.attempt_feedback({"retry_count": 0, "last_verification_reason": "it failed",
                             "output_text": REPORT})
    assert b and "it failed" in b


def test_a_reason_with_no_prior_report_still_says_why():
    b = sr.attempt_feedback({"last_verification_reason": "the runner could not read that"})
    assert "the runner could not read that" in b
    assert "FAILED HERE" not in b          # nothing to list
    assert "read it" in b                  # the standing correction still lands


def test_the_drafter_actually_reads_it():
    src = inspect.getsource(sr.draft_runbook)
    assert "attempt_feedback(node)" in src
    assert src.index("attempt_feedback(node)") < src.index("stored_values_block()")


def test_the_node_handed_to_the_drafter_carries_the_prior_report():
    """The block can only name the failing command if `output_text` is selected —
    the fields were already being fetched and simply never used."""
    src = inspect.getsource(sr.pending_hands_on)
    for col in ("last_verification_reason", "output_text"):
        assert f"n.{col}" in src, col


def test_the_reason_is_truncated_so_one_failure_cannot_eat_the_prompt():
    long_reason = "x" * 5000
    b = sr.attempt_feedback({"last_verification_reason": long_reason, "output_text": REPORT})
    assert len(b) < 3000
