"""§17.1345 — a block with no check proves nothing, so the frame refuses it.

Live, 2026-10-04: the step that sets Radarr's and Sonarr's root folders was
drafted with `verify: []` — two POSTs and nothing that reads the result — and
the frame suggested Run.

Every piece of machinery that confirms a step reads its checks. §17.1302 records
an already-met step from them. The post-run judge reads them. §17.1343's inert
key passed BECAUSE a check looked at the file instead of the section. A block
with no check at all is recorded done on exit codes alone, which is how a change
that did nothing passes.
"""
from __future__ import annotations

from app.modules import supervised_runs as sr

POLICY = {"allow": ["ANY"], "sudo": True, "secrets": ["MASS_PASSWORD"], "can_write_files": True}
NODE = {"node_key": "ADD131", "title": "Set Radarr's root folder to /media/movies",
        "description": "Radarr runs in container 103 on 7878."}
NO_CHECK = """## Run this

```bash
pct exec 103 -- sh -c 'curl -s -X POST -H "Content-Type: application/json" -d "{}" http://127.0.0.1:7878/api/v3/rootfolder'
```
"""
WITH_CHECK = NO_CHECK + """
## Verify

- Radarr lists it back: `pct exec 103 -- sh -c 'curl -s http://127.0.0.1:7878/api/v3/rootfolder'`
"""


def _frame(runbook):
    spec = type("S", (), {"name": "pve-runner", "headers": {}, "endpoint": "http://192.168.1.156:8790/mcp/"})()
    return sr.frame_run(NODE, runbook, spec, POLICY, env={"profile": "root@pve"},
                        inventory={"cts": {"103": "running"}, "vms": {}, "names": {"103": "radarr"}})


def test_a_block_with_no_check_is_refused():
    frame = _frame(NO_CHECK)
    assert frame["verify"] == []
    hits = [r for r in frame["refused"] if "no check at all" in r["why"]]
    assert len(hits) == 1, [r["why"][:80] for r in frame["refused"]]
    assert frame["suggested"] != "run", "Run is withheld"


def test_the_refusal_says_what_a_check_is_for():
    why = [r for r in _frame(NO_CHECK)["refused"] if "no check at all" in r["why"]][0]["why"]
    assert "READS the result" in why
    assert "not the command that made it" in why


def test_a_block_with_a_check_is_not_refused_for_this():
    frame = _frame(WITH_CHECK)
    assert frame["verify"], frame["verify"]
    assert not [r for r in frame["refused"] if "no check at all" in r["why"]], frame["refused"]


def test_the_refusal_redrafts_rather_than_parking():
    """§17.1301 — a refusal whose text is not in `_SHAPE_REFUSALS` parks the frame
    with Run greyed out instead of asking the drafter again."""
    assert any("has no check at all" in m for m in sr._SHAPE_REFUSALS)
    note = sr.shape_retry_note({"refused": [{"command": "x", "why": "this block has no check at all: nothing would confirm it"}],
                                "commands": ["x"]})
    assert note, "the chain retries"


def test_a_block_with_no_commands_is_not_accused_of_missing_a_check():
    """A plan-only runbook has nothing to confirm."""
    frame = _frame("## Why this cannot run yet\n\nThe guest is stopped.\n")
    assert frame["commands"] == []
    assert not [r for r in frame["refused"] if "no check at all" in r["why"]]
