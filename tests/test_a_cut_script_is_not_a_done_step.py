"""§17.1310 — a cut script is not a done step.

Live, 2026-10-03 11:58 UTC: ADD100 "Rebuild the control panel" was approved from
the UI. The model's content carried a "```bash" line; the template wrote it into
the script's fenced block; `file_writes` closed the file there (9 of 29 lines);
bash warned "here-document at line 7 delimited by end-of-file (wanted `REMOTE')"
and exited 0; the verify answered "Cannot GET /api/health"; the step was
recorded done. Container 111 has no /opt/control-panel."""
from __future__ import annotations

import pathlib

import pytest

from app.modules import assist_state_check as sc
from app.modules import runbook_templates as rt
from app.modules import supervised_runs as sr

FX = pathlib.Path(__file__).parent / "fixtures"
RECORD = (FX / "add100_output_truncated_2026_10_03.md").read_text(encoding="utf-8")
RUNBOOK = RECORD.split("## Executed on")[0]
POLICY = {"allow": ["ANY"], "sudo": True, "helper": "19", "secrets": ["MASS_PASSWORD"], "can_write_files": True}
NODE = {"node_key": "ADD100", "title": "Rebuild the control panel to do what was chosen in ADD99",
        "description": "Rework the control-panel backend and frontend in LXC 111."}


def test_the_live_record_shows_the_cut():
    files = sr.file_writes(RUNBOOK)
    assert len(files) == 1 and files[0]["content"].count("\n") <= 9, "the file block closed at the model's fence marker"
    assert sr.unterminated_heredoc(files[0]["content"]) == "REMOTE"


def test_a_file_cut_inside_a_heredoc_is_refused_before_it_is_sent():
    spec = type("S", (), {"name": "pve-runner", "headers": {}})()
    frame = sr.frame_run(NODE, RUNBOOK, spec, POLICY, env={"profile": "root@pve"})
    hits = [r for r in frame["refused"] if "ends inside a heredoc" in r["why"]]
    assert len(hits) == 1 and "REMOTE" in hits[0]["why"] and "/tmp/in_ct_111.sh" in hits[0]["command"], [r["why"][:90] for r in frame["refused"]]
    assert "run" not in {o["id"] for o in frame["options"]}
    assert sr.shape_retry_note({"refused": hits, "commands": frame["commands"]}), "registered: the chain redrafts"


def test_closed_heredocs_and_comments_are_fine():
    ok = "cat > /tmp/x <<'REMOTE'\nset -e\necho hi\nREMOTE\npct push 111 /tmp/x /root/x\n# a <<'COMMENT' in a comment\ntee /etc/f <<EOF\nline\nEOF\n"
    assert sr.unterminated_heredoc(ok) is None
    assert sr.file_writes_will_not_work([{"path": "/tmp/x.sh", "content": ok}]) == []
    assert sr.unterminated_heredoc("tee /etc/f <<EOF\nline\n") == "EOF"


def test_model_fence_never_lets_a_fence_marker_into_the_content():
    assert rt.model_fence("Here you go:\n```bash\napt-get update\n```\nand then\n```bash\napt-get install -y x\n```") == "apt-get update"
    assert rt.model_fence("apt-get update\n```bash\napt-get install -y x\n```") == "apt-get install -y x", "prose before a paired fence is prose"
    assert rt.model_fence("apt-get update\n```bash\napt-get install -y x") == "apt-get update\napt-get install -y x", "an UNPAIRED fence (the live case) keeps the lines and drops the marker"
    assert "```" not in rt.model_fence("```\n$ echo a\n```bash\necho b\n```")
    rb = rt.render(rt.RUN_IN_CONTAINER, rt.values_for(rt.RUN_IN_CONTAINER, NODE, None, {}, {"REMOTE_COMMANDS": rt.model_fence("```bash\napt-get update\n```"), "VERIFY_INSIDE": "true"}))
    files = sr.file_writes(rb)
    assert files and files[0]["content"].rstrip().endswith("pct exec \"$GID\" -- bash /root/.scaffold_step.sh"), "the whole script survives the parser"
    assert sr.unterminated_heredoc(files[0]["content"]) is None


def test_the_runs_exit_code_is_the_warnings_not_the_works():
    executed = [{"command": "write /tmp/in_ct_111.sh (345 bytes)", "output": "wrote /tmp/in_ct_111.sh", "exit": 0, "ok": True},
                {"command": "bash /tmp/in_ct_111.sh", "output": "/tmp/in_ct_111.sh: line 9: warning: here-document at line 7 delimited by end-of-file (wanted `REMOTE')", "exit": 0, "ok": True}]
    why = sr.script_was_cut(executed)
    assert why and "REMOTE" in why and "bash /tmp/in_ct_111.sh" in why
    assert sr.script_was_cut([{"command": "bash x", "output": "done", "exit": 0, "ok": True}]) == ""
    src = pathlib.Path(sr.__file__).read_text(encoding="utf-8")
    i = src.index("async def resolve_run(")
    body = src[i:src.index("\nasync def ", i + 10)]
    assert "script_was_cut(executed)" in body and body.index("script_was_cut(executed)") < body.index("if ok or confirmed_after_drop:")


def test_negative_evidence_settles_a_health_check_without_a_model():
    assert sc.negative_evidence('pct exec 111 -- bash -c "curl -s http://localhost:3001/api/health"',
                                "<!DOCTYPE html>\n<title>Error</title>\n<pre>Cannot GET /api/health</pre>")
    assert sc.negative_evidence("pct exec 111 -- systemctl is-active control-panel", "inactive\n")
    assert sc.negative_evidence("ls /opt/control-panel", "ls: cannot access '/opt/control-panel': No such file or directory")
    assert sc.negative_evidence("pct exec 111 -- systemctl is-active control-panel", "active\n") == ""
    assert sc.negative_evidence("curl -s http://localhost:3001/api/health", '{"ok": true}') == ""
    assert sc.negative_evidence("cat /etc/motd", "Cannot GET anything here is just text") == "", "an HTTP phrase in a non-HTTP check proves nothing"


@pytest.mark.asyncio
async def test_the_judge_contradicts_the_live_verify_without_a_model():
    import unittest.mock as um
    import app.model_router as mr

    async def no_model(*a, **k):
        raise AssertionError("the model must not be asked")
    with um.patch.object(mr, "tool_call", new=no_model):
        verdicts = await sr._verify_verdicts("Rebuild the control panel", ['pct exec 111 -- bash -c "curl -s http://localhost:3001/api/health"'],
                                             "== V1 ==\n<!DOCTYPE html>\n<html><head><title>Error</title></head><body><pre>Cannot GET /api/health</pre></body></html>\n")
    assert [v["verdict"] for v in verdicts] == ["contradicted"], verdicts
    assert sr.contradicted(verdicts)
