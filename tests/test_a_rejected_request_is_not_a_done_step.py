"""§17.1233–1234 — judging the transport instead of the answer.

Live, ADD96, immediately after §17.1227–1232 had turned it from an unrunnable
UI walkthrough into nine real API calls: every one of those nine `curl -s -X
POST` calls was answered by Prowlarr with a validation error array

    'App Profile Id' must be greater than '0'

nothing was added, and the step was recorded **done** — because `curl -s` exits
0 when it successfully fetches a 400. All four verify checks came back EMPTY,
sitting in the same record, and nothing read them: §17.1225 taught the engine to
judge its checks only when a response was LOST.

Two fixes. §17.1233 judges the checks on the success path too, downgrading only
on a positive `contradicted` verdict. §17.1234 removes the root cause: a
write-shaped `curl` with no `--fail-with-body` is refused for its SHAPE, which
the §17.1196 redraft already knows how to fix.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules import supervised_runs as sr

POST = 'curl -s -X POST "http://192.168.1.21:9696/api/v1/indexer" -H "X-Api-Key: $K" -d \'{"name":"1337x"}\''
POST_OK = 'curl -s --fail-with-body -X POST "http://192.168.1.21:9696/api/v1/indexer" -H "X-Api-Key: $K" -d \'{}\''


# ── §17.1234: make the exit code mean something ──────────────────────────


def test_the_live_command_is_caught():
    got = sr.curl_writes_without_fail([POST])
    assert len(got) == 1
    assert "cannot report an HTTP error" in got[0]["why"]
    assert "--fail-with-body" in got[0]["why"]


def test_a_write_that_fails_loudly_is_fine():
    for ok in (POST_OK,
               "curl -sf -X POST http://h/api -d '{}'",
               "curl --fail -X DELETE http://h/api/1",
               "curl -s --fail-with-body -T /tmp/f http://h/up"):
        assert sr.curl_writes_without_fail([ok]) == [], ok


def test_a_read_is_left_alone():
    """A plain `curl -s` under Verify is a read; its output is the evidence and
    a non-zero exit would be noise."""
    for read in ("curl -s http://h/api/v1/indexer",
                 "curl -s -X GET http://h/api",
                 'curl -s "http://h/api" -H "X-Api-Key: $K" | grep -o \'"name"\''):
        assert sr.curl_writes_without_fail([read]) == [], read


def test_non_curl_commands_are_not_its_business():
    assert sr.curl_writes_without_fail(["pct start 111", "qm set 106 --scsi0 x", "wget -O /tmp/x http://h"]) == []


def test_the_body_flags_count_as_a_write_even_without_an_explicit_method():
    for body in ("curl -s -d 'a=1' http://h/x", "curl -s --data-raw '{}' http://h/x",
                 "curl -s -F file=@/tmp/x http://h/up"):
        assert sr.curl_writes_without_fail([body]), body


def test_the_redraft_treats_it_as_a_shape_it_can_fix():
    note = sr.shape_retry_note({"refused": sr.curl_writes_without_fail([POST])})
    assert note and "--fail-with-body" in note
    assert "cannot report an HTTP error" in sr._SHAPE_REFUSALS


def test_frame_run_turns_run_off_for_such_a_block():
    frame = sr.frame_run({"node_key": "ADD96", "title": "t"},
                         f"## Run this\n\n```bash\n{POST}\n```\n",
                         SimpleNamespace(name="pve-runner"), {"allow": ["curl"]})
    assert frame["refused"], "a write that cannot report an error must not be offered as runnable"
    assert frame["suggested"] == "myself"
    assert "Run it through pve-runner" not in [o.get("label") for o in frame["options"]]


def test_frame_run_offers_the_corrected_block():
    """The vacuity check: with the flag present, Run comes back."""
    frame = sr.frame_run({"node_key": "ADD96", "title": "t"},
                         f"## Run this\n\n```bash\n{POST_OK}\n```\n",
                         SimpleNamespace(name="pve-runner"), {"allow": ["curl"]})
    assert frame["refused"] == [] and frame["suggested"] == "run"


def test_the_rule_is_in_the_channel_rules():
    assert "--fail-with-body" in sr.CHANNEL_RULES
    assert "exits 0 having fetched an error page" in sr.CHANNEL_RULES


# ── §17.1233: judge the checks on the success path too ───────────────────


def _executed_ok():
    return [{"command": POST, "ok": True, "exit": 0,
             "output": '[{"errorMessage":"\'App Profile Id\' must be greater than \'0\'"}]'}]


async def _run(verdicts, *, verify=("curl -s http://h/api/v1/indexer",)):
    db = AsyncMock()
    claim = MagicMock(); claim.rowcount = 1
    db.execute = AsyncMock(return_value=claim); db.commit = AsyncMock()
    waiting = {"kind": "run", "node_key": "ADD96", "title": "Add the search sources to Prowlarr",
               "runbook": "## Run this", "commands": [POST], "verify": list(verify), "refused": []}
    with patch.object(sr, "channel", AsyncMock(return_value=(SimpleNamespace(name="pve-runner"), {"allow": ["curl"]}))), \
         patch("app.modules.assist_supervised.gate_block", return_value=([POST], [])), \
         patch("app.modules.assist_supervised.run_block", new=AsyncMock(return_value=_executed_ok())), \
         patch("app.modules.assist_local_runner.run_probes",
               new=AsyncMock(return_value=("== V1 ==\n", [{"id": "V1", "command": verify[0]}] if verify else []))), \
         patch.object(sr, "_verify_verdicts", AsyncMock(return_value=verdicts)), \
         patch.object(sr, "diagnose_failure", AsyncMock(return_value="")):
        out = await sr.resolve_run(db, "j", "ADD96", "run", waiting)
    return out, db


@pytest.mark.asyncio
async def test_exit_zero_with_contradicting_checks_is_not_done():
    """The live case: nine rejected POSTs, four empty checks, recorded done."""
    out, db = await _run([{"verdict": "contradicted",
                           "reason": "the indexer list came back empty", "claim": "c"}])
    assert out["outcome"] == "failed" and out["node_status"] == "failed"
    assert "every command exited 0" in out["reason"]
    assert "the indexer list came back empty" in out["reason"]
    assert "still exits 0" in out["reason"]
    assert "status = 'failed'" in " ".join(str(c.args[0]) for c in db.execute.await_args_list)


@pytest.mark.asyncio
async def test_an_ambiguous_check_never_fails_work_that_happened():
    """Asymmetric on purpose: evidence AGAINST downgrades, absence does not."""
    for verdicts in ([{"verdict": "unknown", "reason": "no output"}],
                     [],
                     [{"verdict": "confirmed", "reason": "it is there"}]):
        out, _ = await _run(verdicts)
        assert out["outcome"] == "ran", verdicts
        assert out["node_status"] == "done"


@pytest.mark.asyncio
async def test_a_step_with_no_checks_is_unaffected():
    out, _ = await _run([{"verdict": "contradicted", "reason": "x"}], verify=())
    assert out["outcome"] == "ran"          # nothing to judge, nothing to downgrade


@pytest.mark.asyncio
async def test_both_paths_build_the_probes_the_same_way():
    """One builder, so the drop path and the success path read the SAME evidence
    the same way — the asymmetry that hid this bug was two code paths."""
    import inspect
    src = inspect.getsource(sr.resolve_run)
    assert src.count("_verify_verdicts(") == 1          # the success path
    assert "_goal_confirmed(" in src                     # the drop path
    assert "_verify_verdicts(" in inspect.getsource(sr._goal_confirmed)


@pytest.mark.asyncio
async def test_only_contradicted_counts():
    assert sr.contradicted([{"verdict": "contradicted"}, {"verdict": "unknown"},
                            {"verdict": "confirmed"}]) == [{"verdict": "contradicted"}]
    assert sr.contradicted([]) == [] and sr.contradicted(None) == []


# ── §17.1235: a step that says it is blocked is not finished ──────────────


ADD98 = """## Prerequisites

This proof is blocked until the download client decision from ADD102 is resolved and ADD97 steps 1-2 are complete. The undecided items from the broader brief do not affect this film request/verify path.

- Radarr is running at `192.168.1.22:7878` with `$RADARR_API_KEY` available.
"""

ADD97 = """## Prerequisites

- Radarr container 103 is running and reachable at `192.168.1.22:7878`.
- Sonarr container 104 is running and reachable at `192.168.1.23:8989`.
- The download client is running, its WireGuard/VPN interface is up.

## Run this

1. Add the download client to Radarr.
"""


def test_the_live_sentence_is_found_and_quoted():
    got = sr.declares_itself_blocked(ADD98)
    assert got and got.startswith("This proof is blocked until the download client decision")
    assert "ADD97 steps 1-2 are complete." in got


def test_a_runbook_that_merely_lists_prerequisites_still_passes():
    """The narrowness that matters: describing a state you expect is not
    announcing a failure. ADD97 lists three prerequisites and is not blocked."""
    assert sr.declares_itself_blocked(ADD97) is None


def test_the_other_phrasings_a_model_reaches_for():
    for txt in ("This step cannot proceed until the VPN is up.",
                "Verification is blocked on ADD50.",
                "This cannot be verified until the container starts.",
                "We must wait for the disk resize to finish.",
                "Testing is not possible until Prowlarr has indexers."):
        assert sr.declares_itself_blocked(txt), txt


def test_ordinary_output_is_never_flagged():
    for txt in ("", None, "## Run this\n\npct start 111\n",
                "The firewall blocked port 8080, so I opened it.",
                "Radarr is running and reachable.",
                "Blocked ports are listed below for reference."):
        assert sr.declares_itself_blocked(txt) is None, txt


def test_it_only_reads_the_opening_of_a_long_output():
    """A self-declaration belongs at the top. A passing mention 20 paragraphs
    down must not fail a step that did its work."""
    long_tail = "\n\n".join(["Step completed successfully."] * 10 + ["This is blocked until later."])
    assert sr.declares_itself_blocked(long_tail) is None


def test_the_executor_checks_it_before_every_verifier():
    import inspect
    from app.modules import execution_agent as _ea
    src = inspect.getsource(_ea.execute_next_node)
    assert "declares_itself_blocked(output)" in src
    # before the verifier branches, and it wins over skip_verify
    assert src.index("declares_itself_blocked(output)") < src.index("elif skip_verify:")
    assert 'verify_status = "fail"' in src[src.index("declares_itself_blocked(output)"):
                                          src.index("elif skip_verify:")]
    assert "nothing was executed, so it is not finished" in src


# ── §17.1238: Run and Verify disagreed about what a command looks like ────


ADD110_RUNBOOK = """## Inputs needed

None — this step only starts an existing container and confirms its state.

## Run this

`pct start 120`

## Verify

`pct status 120` — expect `status: running` in the output.
"""


def test_an_inline_run_command_is_extracted():
    """Live ADD110, a one-command step drafted correctly: the Verify check was
    extracted and the Run command was not, so the frame carried ZERO commands and
    the step could not be carried out. §17.1227's redraft could not help — the
    second draft was just as correct and just as invisible."""
    assert sr.runbook_commands(ADD110_RUNBOOK) == ["pct start 120"]


def test_the_two_siblings_now_agree():
    """`verify_commands` always accepted inline literals; `runbook_commands`
    never did. That disagreement is the bug."""
    assert sr.verify_commands(ADD110_RUNBOOK) == ["pct status 120"]
    assert sr.runbook_commands(ADD110_RUNBOOK)


def test_a_fenced_runbook_is_unchanged():
    fenced = "## Run this\n\n```bash\npct start 120\npct status 120\n```\n"
    assert sr.runbook_commands(fenced) == ["pct start 120", "pct status 120"]


def test_inline_is_only_a_fallback_so_a_fenced_runbook_gains_no_prose_commands():
    """Only when the fences yielded nothing — a good fenced runbook must never
    have prose-derived commands mixed in."""
    mixed = ("## Run this\n\n```bash\npct start 120\n```\n\n"
             "Then check it with `pct status 120` if you like.\n")
    assert sr.runbook_commands(mixed) == ["pct start 120"]


def test_prose_that_is_not_a_command_yields_nothing():
    """A backticked path and an assignment are not commands."""
    assert sr.runbook_commands(
        "## Run this\n\nWrite the file at `/etc/pve/firewall/120.fw` and set `enable: 1`.\n") == []
    assert sr.runbook_commands("## Run this\n\nThe panel lives in `/opt/panel`.\n") == []


def test_a_heredoc_fence_is_still_kept_whole():
    """§17.1189's behaviour must survive the fallback being added."""
    hd = ("## Run this\n\n```bash\ncat <<EOF > /tmp/x\n[Unit]\nExecStart=/bin/true\nEOF\n```\n")
    got = sr.runbook_commands(hd)
    assert len(got) == 1 and "ExecStart=/bin/true" in got[0]


def test_the_frame_offers_run_for_the_inline_one_liner():
    """End to end: the frame that carried zero commands now offers Run."""
    frame = sr.frame_run({"node_key": "ADD110", "title": "Start container 120 (caddy-proxy)"},
                         ADD110_RUNBOOK, SimpleNamespace(name="pve-runner"), {"allow": ["pct"]})
    assert frame["commands"] == ["pct start 120"]
    assert frame["refused"] == [] and frame["suggested"] == "run"


# ── §17.1239: an unreadable check is not evidence against the work ────────


_DROP = ("(runner error: mcp server 'pve-runner' tool 'run_supervised': "
         "MCPError: SSE stream ended without a response)")


def test_evidence_means_a_marker_arrived():
    ran = [{"id": "V1", "command": "pct status 120"}]
    assert sr._has_evidence("== V1 ==\nstatus: running\n", ran) is True
    # a marker with nothing under it IS evidence — "the list is empty"
    assert sr._has_evidence("== V1 ==\n", ran) is True
    # a marker that never arrived is not
    assert sr._has_evidence("", ran) is False
    assert sr._has_evidence("some unrelated text", ran) is False
    assert sr._has_evidence("== V1 ==\n", []) is False


def test_the_probe_report_renders_identically_for_a_recheck():
    ran = [{"id": "V1", "command": "pct status 120"}, {"id": "V2", "command": "ls /x"}]
    got = sr._probe_report("== V1 ==\nstatus: running\n", ran)
    assert "$ pct status 120\nstatus: running" in got
    assert "$ ls /x\n(no output)" in got


@pytest.mark.asyncio
async def test_a_blank_check_is_retried_once_and_the_second_read_wins(monkeypatch):
    """Live ADD110: `pct start 120` lost its response, `pct status 120` then
    printed nothing because the runner was still recovering, the step was
    recorded failed — and container 120 was running."""
    monkeypatch.setattr(sr, "_RECHECK_DELAY_S", 0)
    db = AsyncMock(); claim = MagicMock(); claim.rowcount = 1
    db.execute = AsyncMock(return_value=claim); db.commit = AsyncMock()
    spec = SimpleNamespace(name="pve-runner")
    executed = [{"command": "pct start 120", "ok": False, "exit": None,
                 "output": _DROP, "unreachable": True}]
    waiting = {"kind": "run", "node_key": "ADD110", "title": "Start container 120 (caddy-proxy)",
               "runbook": "## Run this", "commands": ["pct start 120"],
               "verify": ["pct status 120"], "refused": []}
    ran = [{"id": "V1", "command": "pct status 120"}]
    probes = AsyncMock(side_effect=[("", ran), ("== V1 ==\nstatus: running\n", ran)])
    with patch.object(sr, "channel", AsyncMock(return_value=(spec, {"allow": ["pct"]}))), \
         patch("app.modules.assist_supervised.gate_block", return_value=(["pct start 120"], [])), \
         patch("app.modules.assist_supervised.run_block", new=AsyncMock(return_value=executed)), \
         patch("app.modules.assist_local_runner.run_probes", probes), \
         patch.object(sr, "_goal_confirmed", AsyncMock(return_value=True)), \
         patch.object(sr, "diagnose_failure", AsyncMock(return_value="")):
        out = await sr.resolve_run(db, "j", "ADD110", "run", waiting)
    assert probes.await_count == 2, "a blank check must be read again"
    assert out["outcome"] == "ran" and out["node_status"] == "done"
    assert out["confirmed_after_drop"] is True


@pytest.mark.asyncio
async def test_still_blank_after_the_recheck_says_UNKNOWN_not_failed_verification(monkeypatch):
    """The wording matters: claiming the checks "did NOT show its goal met" when
    they printed nothing at all is a false statement about the machine."""
    monkeypatch.setattr(sr, "_RECHECK_DELAY_S", 0)
    db = AsyncMock(); claim = MagicMock(); claim.rowcount = 1
    db.execute = AsyncMock(return_value=claim); db.commit = AsyncMock()
    spec = SimpleNamespace(name="pve-runner")
    executed = [{"command": "pct start 120", "ok": False, "exit": None,
                 "output": _DROP, "unreachable": True}]
    waiting = {"kind": "run", "node_key": "ADD110", "title": "t", "runbook": "r",
               "commands": ["pct start 120"], "verify": ["pct status 120"], "refused": []}
    ran = [{"id": "V1", "command": "pct status 120"}]
    goal = AsyncMock(return_value=True)          # would pass if it were consulted
    with patch.object(sr, "channel", AsyncMock(return_value=(spec, {"allow": ["pct"]}))), \
         patch("app.modules.assist_supervised.gate_block", return_value=(["pct start 120"], [])), \
         patch("app.modules.assist_supervised.run_block", new=AsyncMock(return_value=executed)), \
         patch("app.modules.assist_local_runner.run_probes", new=AsyncMock(return_value=("", ran))), \
         patch.object(sr, "_goal_confirmed", goal), \
         patch.object(sr, "diagnose_failure", AsyncMock(return_value="")):
        out = await sr.resolve_run(db, "j", "ADD110", "run", waiting)
    assert out["outcome"] == "failed"
    assert "UNKNOWN" in out["reason"]
    assert "came back empty twice" in out["reason"]
    assert "did NOT show its goal met" not in out["reason"]
    goal.assert_not_awaited()                    # nothing to judge


@pytest.mark.asyncio
async def test_a_check_that_answers_is_not_read_twice(monkeypatch):
    monkeypatch.setattr(sr, "_RECHECK_DELAY_S", 0)
    db = AsyncMock(); claim = MagicMock(); claim.rowcount = 1
    db.execute = AsyncMock(return_value=claim); db.commit = AsyncMock()
    spec = SimpleNamespace(name="pve-runner")
    executed = [{"command": "pct start 120", "ok": False, "exit": None,
                 "output": _DROP, "unreachable": True}]
    waiting = {"kind": "run", "node_key": "X", "title": "t", "runbook": "r",
               "commands": ["pct start 120"], "verify": ["pct status 120"], "refused": []}
    ran = [{"id": "V1", "command": "pct status 120"}]
    probes = AsyncMock(return_value=("== V1 ==\nstatus: stopped\n", ran))
    with patch.object(sr, "channel", AsyncMock(return_value=(spec, {"allow": ["pct"]}))), \
         patch("app.modules.assist_supervised.gate_block", return_value=(["pct start 120"], [])), \
         patch("app.modules.assist_supervised.run_block", new=AsyncMock(return_value=executed)), \
         patch("app.modules.assist_local_runner.run_probes", probes), \
         patch.object(sr, "_goal_confirmed", AsyncMock(return_value=False)), \
         patch.object(sr, "diagnose_failure", AsyncMock(return_value="")):
        out = await sr.resolve_run(db, "j", "X", "run", waiting)
    assert probes.await_count == 1
    assert out["outcome"] == "failed" and "did NOT show its goal met" in out["reason"]


# ── §17.1252: the operator approves what they READ ────────────────────────


def test_a_short_runbook_is_carried_whole():
    rb = "## Run this\n\n```bash\npct start 130\n```\n"
    assert sr._runbook_for_display(rb, ["pct start 130"]) == rb


def test_a_cut_runbook_says_so_and_names_what_is_missing():
    """Live ADD96: its runbook is over 12,000 characters because every indexer is
    a `curl` with a JSON body, so the stored prose stopped mid-command. Parsing
    the STORED text back yields 19 commands while the block that would actually
    run has 24 — five commands were going to execute that the operator had not
    been shown, with nothing saying the text had been cut."""
    long_rb = "## Run this\n\n" + ("x" * (sr.RUNBOOK_DISPLAY_CHARS + 4000))
    out = sr._runbook_for_display(long_rb, ["cmd"] * 24)
    assert "cut off here" in out
    assert "24 commands" in out
    assert "that list is what runs" in out
    # and the shown prose is still the beginning of the real thing
    assert out.startswith("## Run this")


def test_the_frame_carries_the_notice_not_a_bare_slice():
    import inspect
    src = inspect.getsource(sr.frame_run)
    assert "_runbook_for_display(runbook, cmds)" in src
    assert "runbook[:12000]" not in src, "a bare slice hides the cut"


def test_the_command_list_is_never_truncated_by_the_display_cap():
    """The prose is for reading; the list is what runs. The cap must touch only
    the prose."""
    long_rb = "## Run this\n\n```bash\n" + "\n".join(f"pct exec 130 -- echo {i}" for i in range(30)) + "\n```\n"
    assert len(long_rb) < sr.RUNBOOK_DISPLAY_CHARS      # the prose fits
    cmds = sr.runbook_commands(long_rb)
    assert len(cmds) == min(30, sr.MAX_RUN_COMMANDS)    # bounded by MAX_RUN_COMMANDS, not the display cap
    assert sr.MAX_RUN_COMMANDS != sr.RUNBOOK_DISPLAY_CHARS


# ── §17.1254: a block that only looks, for a step that must change ────────


ADD96_NODE = {
    "title": "Add the search sources to Prowlarr and connect it to Radarr and Sonarr",
    "tool": "LLM",
    "description": "Add every working public indexer. Done when the indexer list is populated.",
    "prompt_template": "Run `curl -X POST http://h:9696/api/v1/indexer` for each one.",
}
SCHEMA_READS = ['curl -s -H "X-Api-Key: $K" "http://h:9696/api/v1/indexer/schema"',
                'curl -s -H "X-Api-Key: $K" "http://h:9696/api/v1/indexer/schema" | head -c 100']


def test_the_live_two_read_draft_is_caught():
    """One draft of ADD96 produced 24 commands — read the schema, then a POST per
    tracker. The next draft of the SAME step produced these two reads. Run was
    offered and the gate was clean; approving it would have marked the step done
    having added nothing."""
    note = sr.all_reads_for_a_changing_step(SCHEMA_READS, ADD96_NODE)
    assert note and "ONLY LOOKS AT THINGS" in note
    assert "indexer/schema" in note                  # it quotes what was written
    assert "--fail-with-body" in note                # and names the correction


def test_a_block_that_changes_something_is_silent():
    writes = SCHEMA_READS + ['curl -s --fail-with-body -X POST "http://h:9696/api/v1/indexer" -d "{}"']
    assert sr.all_reads_for_a_changing_step(writes, ADD96_NODE) == ""


def test_the_empty_case_belongs_to_17_1227():
    assert sr.all_reads_for_a_changing_step([], ADD96_NODE) == ""
    assert sr.all_reads_for_a_changing_step(["   "], ADD96_NODE) == ""


def test_a_step_that_only_reads_may_only_read():
    """ADD113 ("review what the lab exposes") is reads by design — the gate must
    be silent unless step_classify says the step CHANGES a machine."""
    reading = {"title": "Review what the home lab exposes", "tool": "LLM",
               "description": "Read the listening ports and report them."}
    assert sr.all_reads_for_a_changing_step(SCHEMA_READS, reading) == ""


def test_a_write_hidden_in_a_later_segment_still_counts():
    """Segments are judged individually, so a pipeline whose second half writes
    is not an all-reads block."""
    mixed = ['curl -s "http://h/api" | tee /etc/thing.conf']
    assert sr.all_reads_for_a_changing_step(mixed, ADD96_NODE) == ""


def test_the_executor_redrafts_and_only_trades_up():
    import inspect
    from app.modules import execution_agent as ea
    src = inspect.getsource(ea._pause_for_decision)
    assert "all_reads_for_a_changing_step(frame.get(\"commands\") or [], run_node)" in src
    # the replacement must itself not be all-reads
    assert "not supervised_runs.all_reads_for_a_changing_step(" in src
    assert src.index("all_reads_for_a_changing_step") < src.index("runbook_coverage")


# ── §17.1255: an inline script whose quoting cannot work ──────────────────


BAD_INLINE = ("curl -s http://h/api | python3 -c 'import sys, json; "
              'data=json.load(sys.stdin); print(f"Name: {d[\\"name\\"]}")\'')


def test_the_live_one_liner_is_refused():
    """ADD96's read step was otherwise right — one read-only command, no
    refusals — and died on its own quoting:
        SyntaxError: unexpected character after line continuation character
    Inside single quotes the shell passes a backslash through untouched, so
    Python received a literal \\" ."""
    got = sr.inline_script_quoting([BAD_INLINE])
    assert len(got) == 1
    why = got[0]["why"]
    assert "cannot parse" in why
    assert "-o /tmp/x.json" in why or "write the output to a file" in why


def test_a_double_quoted_payload_is_left_alone():
    """There `\\"` is how you escape, so it is correct."""
    assert sr.inline_script_quoting(['python3 -c "import json; print(json.dumps({\\"a\\": 1}))"']) == []


def test_plain_quoting_inside_single_quotes_is_fine():
    ok = "curl -s http://h/api | python3 -c 'import sys, json; print(json.load(sys.stdin)[\"name\"])'"
    assert sr.inline_script_quoting([ok]) == []


def test_commands_with_no_inline_script_are_not_its_business():
    for c in ("pct status 130", "curl -s http://h/api | grep name",
              "pct exec 130 -- bash -c 'echo hi'"):
        assert sr.inline_script_quoting([c]) == [], c


def test_perl_and_node_count_too():
    for interp in ("perl -e", "ruby -e", "node -e"):
        c = f"""{interp} 'print \\"x\\"'"""
        assert sr.inline_script_quoting([c]), c


def test_the_redraft_treats_it_as_a_shape_it_can_fix():
    note = sr.shape_retry_note({"refused": sr.inline_script_quoting([BAD_INLINE])})
    assert note and "cannot parse" in note
    assert "cannot parse" in sr._SHAPE_REFUSALS


# ── §17.1255b: the same quoting bug in a script being WRITTEN ─────────────

BAD_SCRIPT = """printf '%s\\n' 'import json' 'print(f"x {d[\\"name\\"]}")' | tee /tmp/add.py"""


def test_a_script_written_with_escaped_quotes_is_refused():
    """§17.1255 refused `python3 -c '… \\" …'`. The next draft did the right thing
    structurally — printf | tee a file, then run it — and put the SAME escapes in
    the lines it wrote, so the file landed with literal backslashes and Python
    would refuse it. The gate only looked at `-c` payloads."""
    got = sr.code_written_with_escaped_quotes([BAD_SCRIPT])
    assert len(got) == 1
    why = got[0]["why"]
    assert "/tmp/add.py" in why
    assert "backslash is literal" in why


def test_a_clean_script_is_allowed():
    ok = """printf '%s\\n' 'import json' 'print("hello")' | tee /tmp/add.py"""
    assert sr.code_written_with_escaped_quotes([ok]) == []


def test_a_json_body_may_legitimately_escape_a_quote():
    """`curl -d '{"a": "x\\"y"}'` wants that backslash — it is JSON, not code."""
    assert sr.code_written_with_escaped_quotes(
        ['curl -s -X POST http://h/api -d \'{"a": "x\\"y"}\'']) == []


def test_a_non_code_file_is_not_its_business():
    """Only .py/.sh/.pl/.rb/.js targets — a Caddyfile or nginx conf is not code
    this rule understands."""
    assert sr.code_written_with_escaped_quotes(
        ["printf '%s\\n' 'server { }' | tee /etc/nginx/x.conf"]) == []


def test_the_shape_marker_lets_the_redraft_fix_it():
    note = sr.shape_retry_note({"refused": sr.code_written_with_escaped_quotes([BAD_SCRIPT])})
    assert note and "cannot parse" in note
