"""§17.2056 — the engine never drafts a check its own secret-read gate refuses, and one gate judges a block's checks
where the frame is built AND where /decide runs it.

Live (tests/integration/test_auto_mode_live.py, 2 of 5 runs on main 734943c5): step H3 says
"Run this: printf '%s' '<ITEST_SECRET>' | tee …/stage1/token.txt … Done when `ls -l …/stage1/token.txt` shows the file".
The drafter added the check `cat …/stage1/token.txt`; §17.1603's gate refused it ("a file named as a secret"), the one
redraft reached for `python3` (off the allow-list) and was rejected, so H3 parked `refused=1` with no "run" option and
`POST /decide run` answered 409 "the engine cannot run this block". The refusal names its own remedy (`stat` the file):
a check that only READS a secret-named file is the engine's to replace, not a reason to withhold a runnable block.

And the step's own "Done when" checks were appended AFTER the gate (§17.1630) and again at the run (§17.1470) with no
gate at either place, so a "Done when `cat /root/api_key.txt`" would have printed the key into the run's record.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules import supervised_runs as sr

MARK = "/tmp/scaffold-auto-itest"
POLICY = {"allow": [f"mkdir -p {MARK}/", f"tee {MARK}/", f"touch {MARK}/"], "sudo": False,
          "secrets": ["ITEST_SECRET"], "can_write_files": True}
H3 = {"node_key": "H3", "title": f"Record the library token at {MARK}/stage1/token.txt",
      "description": (f"On the host, write the library token into the staging directory.\n\n"
                      f"Run this:\n\n```bash\nprintf '%s' '<ITEST_SECRET>' | tee {MARK}/stage1/token.txt\n```\n\n"
                      f"## Inputs needed\n- <ITEST_SECRET> the library token\n\n"
                      f"Done when `ls -l {MARK}/stage1/token.txt` shows the file.")}
H3["prompt_template"] = H3["description"]

#: the live H3 draft: the step's command, plus the `cat` check the drafter added
LIVE_DRAFT = f"""## Run this

```bash
printf '%s' "$ITEST_SECRET" | tee {MARK}/stage1/token.txt
```

## Verify

- `cat {MARK}/stage1/token.txt`
- `ls -l {MARK}/stage1/token.txt`
"""


def _spec():
    return type("S", (), {"name": "itest-auto-runner", "headers": {}, "endpoint": "http://127.0.0.1:8792/mcp/"})()


def _frame(node, runbook):
    return sr.frame_run(node, runbook, _spec(), POLICY, env={"profile": "user@laptop"})


# ── the repair: a bare read of a secret-named file becomes its stat ──────────────────────────────────────────────

def test_a_bare_read_of_a_secret_file_becomes_its_stat_and_nothing_else_moves():
    verify = [f"cat {MARK}/stage1/token.txt",                                      # hit
              'pct exec 111 -- bash -c "cat /root/panel-api-token.txt"',            # hit, wrapped (the §17.1603 live check)
              "sudo head -n 3 /root/api_key.txt",                                  # hit, sudo + options
              "cat /etc/hostname",                                                  # miss: not named as a secret
              f"ls -l {MARK}/stage1/token.txt",                                     # miss: does not read it
              'curl -s -H "X-Api-Key: $(cat /root/api_key.txt)" http://127.0.0.1:8989/api/v3/system/status']   # miss: uses it
    out, notes = sr.a_check_that_reads_a_secret_file(verify)
    assert out[0] == f"stat -c %a:%U {MARK}/stage1/token.txt"
    assert out[1] == 'pct exec 111 -- bash -c "stat -c %a:%U /root/panel-api-token.txt"'
    assert out[2] == "sudo stat -c %a:%U /root/api_key.txt"
    assert out[3:] == verify[3:], "a check that is not a bare read of a secret file is left exactly as drafted"
    assert len(notes) == 3 and all("named as a secret" in n["why"] for n in notes)
    assert sr.prints_a_secret([], out[:3]) == [], "the replacement is one the gate accepts"


def test_the_repair_never_weakens_the_gate():
    """A shape the repair does not own is still refused: the gate is untouched."""
    for still in ["cat /root/token.txt | head -c 8", "head -n 1 /etc/ssl/private/site.key; echo ok", "xxd /root/api_key.bin >&2"]:
        assert sr.a_check_that_reads_a_secret_file([still])[0] == [still]
        assert sr.prints_a_secret([], [still]) != [], still
    assert sr.prints_a_secret(["cat /root/token.txt"]) != [], "a COMMAND that prints the secret is refused, never rewritten"


# ── the frame: the live H3 draft is offered "run", with the stat in place of the cat ─────────────────────────────

def test_the_live_h3_draft_is_offered_run_with_the_stat_check():
    frame = _frame(H3, LIVE_DRAFT)
    assert frame["refused"] == [], [r["why"][:120] for r in frame["refused"]]
    assert "run" in [o["id"] for o in frame["options"]] and frame["suggested"] == "run"
    assert f"stat -c %a:%U {MARK}/stage1/token.txt" in frame["verify"]
    assert not any(v.startswith("cat ") for v in frame["verify"]), frame["verify"]
    assert f"ls -l {MARK}/stage1/token.txt" in frame["verify"], "the step's own Done-when check stands"
    assert any("named as a secret" in w for w in frame["engine_fixed"]), "the operator is told the check changed"


def test_a_done_when_that_reads_a_secret_file_is_never_carried_to_the_run():
    """The step's OWN check is appended after the gate (§17.1630) and again at the run (§17.1470)."""
    node = {"node_key": "K1", "title": f"Write the API key to {MARK}/api_key.txt",
            "description": (f"Run this:\n\n```bash\nprintf '%s' \"$ITEST_SECRET\" | tee {MARK}/api_key.txt\n```\n\n"
                            f"Done when `cat {MARK}/api_key.txt` prints the key and `ls -l {MARK}` lists it.")}
    runbook = (f"## Run this\n\n```bash\nprintf '%s' \"$ITEST_SECRET\" | tee {MARK}/api_key.txt\n```\n\n"
               f"## Verify\n\n- `ls -l {MARK}/api_key.txt`\n")
    frame = _frame(node, runbook)
    assert not any(v.startswith("cat ") for v in frame["verify"]), frame["verify"]
    assert f"stat -c %a:%U {MARK}/api_key.txt" in frame["verify"], frame["verify"]
    assert f"ls -l {MARK}" in frame["verify"], "the other own check is kept"
    # the run re-derives the own checks from the step's text: the same repair, so the same list
    again = sr.with_own_checks(node, frame["verify"], frame["commands"])
    assert again == frame["verify"], again


# ── one gate, both ends ──────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("runbook,node", [
    (LIVE_DRAFT, H3),
    (f"## Run this\n\n```bash\nmkdir -p {MARK}/stage1\n```\n\n## Verify\n\n- `ls -ld {MARK}/stage1`\n",
     {"node_key": "H1", "title": "mkdir", "description": f"Done when `ls -ld {MARK}/stage1` prints it."}),
    (f"## Run this\n\n```bash\ntouch {MARK}/stage1/secret.env\n```\n\n## Verify\n\n- `head -n 1 {MARK}/stage1/secret.env`\n",
     {"node_key": "H4", "title": "touch", "description": "Make the env file."}),
])
def test_a_frame_that_offers_run_is_one_the_decide_gate_accepts(runbook, node):
    frame = _frame(node, runbook)
    offered = "run" in [o["id"] for o in frame["options"]]
    run_verify = sr.with_own_checks({**node, "prompt_template": node.get("description")}, frame["verify"],
                                    frame["commands"])
    leaks = sr.secret_output_refusals(frame["commands"], run_verify, frame.get("files"))
    assert offered and leaks == [], (offered, leaks, frame["refused"])


def _db(rowcount=0):
    db = AsyncMock()
    res = MagicMock()
    res.rowcount = rowcount
    db.execute = AsyncMock(return_value=res)
    return db


async def _decide(waiting):
    db = _db(rowcount=0)                                # the claim finds no pending node: `node_gone` right after it
    with patch.object(sr, "channel", new=AsyncMock(return_value=(_spec(), POLICY))), \
         patch("app.modules.rewrite_guard.destructive_rewrites", new=AsyncMock(return_value=[])), \
         patch("app.modules.address_watch.around_run", new=AsyncMock(return_value=([], {}))), \
         patch("app.modules.assist_supervised.run_block", new=AsyncMock()) as rb:
        out = await sr.resolve_run(db, "j", "K2", "run", waiting, inputs={})
    rb.assert_not_awaited()
    return out, db


@pytest.mark.asyncio
async def test_decide_refuses_a_check_that_prints_a_secret_before_the_node_is_claimed():
    """A frame parked without the gate (a frame from before it, an own check the frame never saw) is judged again
    where it runs, by the same function -- and the refusal leaves the node `pending`."""
    waiting = {"kind": "run", "title": "Store the key", "description": "Done when `ls -l /root/api_key.txt` lists it.",
               "commands": [f"touch {MARK}/x"], "refused": [], "inputs": [],
               "verify": ["cat /root/api_key.txt | head -c 8", "ls -l /root/api_key.txt"]}
    out, db = await _decide(waiting)
    assert out["outcome"] == "not_runnable", out
    assert any("named as a secret" in r["why"] for r in out["refused"]), out["refused"]
    db.execute.assert_not_awaited(), "the node was not claimed"


@pytest.mark.asyncio
async def test_decide_lets_a_clean_block_through_to_the_claim():
    waiting = {"kind": "run", "title": "Store the key", "description": "Done when `ls -l /root/api_key.txt` lists it.",
               "commands": [f"touch {MARK}/x"], "refused": [], "inputs": [],
               "verify": ["ls -l /root/api_key.txt", "stat -c %a:%U /root/api_key.txt"]}
    out, db = await _decide(waiting)
    assert out["outcome"] == "node_gone", out          # it reached the claim: nothing refused it
    db.execute.assert_awaited()


def test_frame_run_and_resolve_run_call_the_same_gate():
    import inspect
    assert "secret_output_refusals(cmds, verify, shape_files)" in inspect.getsource(sr.frame_run)
    assert "secret_output_refusals(commands, verify_cmds" in inspect.getsource(sr.resolve_run)
    src = inspect.getsource(sr.frame_run)
    assert src.index("verify = with_own_checks(node, verify, cmds)") < src.index("secret_output_refusals(cmds, verify, shape_files)"), \
        "the frame judges the checks the run will make, the step's own included"
