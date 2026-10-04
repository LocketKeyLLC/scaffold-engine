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


# ── §17.1248: a pipe after `pct exec` runs on the HOST ────────────────────

import pytest
from types import SimpleNamespace

BAD = "pct exec 130 -- curl -sSL https://install.pi-hole.net | bash /dev/stdin --unattended"
GOOD = 'pct exec 130 -- bash -c "curl -sSL https://install.pi-hole.net | bash /dev/stdin --unattended"'


def test_the_live_draft_that_would_have_installed_pihole_on_the_host():
    """§17.1247's resuming draft produced BAD. The shell splits it into
    `pct exec 130 -- curl …` and `bash /dev/stdin --unattended`, so the installer
    fetched inside 130 would have been executed by the Proxmox host — taking port
    53 and the host's resolver with it. The attempt before had it right."""
    got = sr.pipe_escapes_the_guest([BAD])
    assert len(got) == 1
    why = got[0]["why"]
    assert "ON THE HOST" in why and "130" in why
    assert 'pct exec 130 -- bash -c' in why          # the correction is named


def test_the_correct_form_is_allowed():
    assert sr.pipe_escapes_the_guest([GOOD]) == []
    assert sr.pipe_escapes_the_guest(['pct exec 130 -- bash -c "a | python3 -"']) == []


def test_a_host_side_filter_of_guest_output_is_fine():
    """`grep`/`awk` reading what a guest printed is the ordinary, correct shape —
    only an INTERPRETER on the right-hand side executes what it is handed."""
    for ok in ("pct exec 130 -- ss -tlnp | grep :53",
               "pct exec 130 -- cat /etc/hosts | awk '{print $1}'",
               "qm guest exec 106 -- ls | wc -l"):
        assert sr.pipe_escapes_the_guest([ok]) == [], ok


def test_sequencing_is_not_piping():
    """`&&` and `;` run host commands in order and carry no data across the
    boundary, which is a different and legitimate thing."""
    for ok in ("pct exec 130 -- true && pct start 131",
               "pct exec 130 -- true ; echo done"):
        assert sr.pipe_escapes_the_guest([ok]) == [], ok


def test_a_pipe_with_no_guest_entry_is_not_its_business():
    assert sr.pipe_escapes_the_guest(["curl -s http://x | bash"]) == []
    assert sr.pipe_escapes_the_guest(["pct list | grep pihole"]) == []


def test_the_redraft_treats_it_as_a_shape_it_can_fix():
    note = sr.shape_retry_note({"refused": sr.pipe_escapes_the_guest([BAD])})
    assert note and "ON THE HOST" in note
    assert "ON THE HOST" in sr._SHAPE_REFUSALS


def test_frame_run_withholds_run_for_such_a_block():
    frame = sr.frame_run({"node_key": "ADD111", "title": "Set up Pi-hole"},
                         f"## Run this\n\n```bash\n{BAD}\n```\n",
                         SimpleNamespace(name="pve-runner"), {"allow": ["pct"]})
    assert frame["refused"] and frame["suggested"] == "myself"
    frame_ok = sr.frame_run({"node_key": "ADD111", "title": "Set up Pi-hole"},
                            # §17.1345 — with a check, because a block that has none is
                            # refused for that alone; this test is about the error read.
                            f"## Run this\n\n```bash\n{GOOD}\n```\n\n"
                            f"## Verify\n\n- Pi-hole answers: `pct exec 130 -- systemctl is-active pihole-FTL`\n",
                            SimpleNamespace(name="pve-runner"), {"allow": ["pct"]})
    assert frame_ok["refused"] == [] and frame_ok["suggested"] == "run"
