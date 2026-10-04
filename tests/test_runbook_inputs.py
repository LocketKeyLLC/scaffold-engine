"""§17.1187 — a runbook that needs values asks for them inline at the run
pause; the values are checked, spliced into the commands, gated again, and
a secret never lands in the record."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules import supervised_runs as sr

RUNBOOK = """## Inputs needed
- <HOST_IP> — the AI VM's address on the LAN
- <ADMIN_PASSWORD>: the panel's admin password

## Run this
```bash
ssh-copy-id -i /root/.ssh/id_ed25519.pub aedefruscio@<HOST_IP>
pct set 111 --nameserver <DNS_IP>
```

## Verify
- `ssh aedefruscio@<HOST_IP> hostname` prints the VM's name.
"""
POLICY = {"allow": ["ssh-copy-id", "pct set"], "sudo": False, "helper": "11"}


def _spec():
    s = MagicMock(); s.name = "pve-runner"; s.headers = {"X-Runner-Token": "tok"}; return s


def test_placeholders_and_hints_are_found_in_order():
    cmds = sr.runbook_commands(RUNBOOK)
    assert sr.placeholders(cmds) == ["HOST_IP", "DNS_IP"]
    hints = sr.input_hints(RUNBOOK)
    assert hints["HOST_IP"] == "the AI VM's address on the LAN" and hints["ADMIN_PASSWORD"] == "the panel's admin password"
    inputs = sr.inputs_for(cmds, sr.verify_commands(RUNBOOK), RUNBOOK)
    assert [i["name"] for i in inputs] == ["HOST_IP", "DNS_IP"]           # only names the commands USE
    assert inputs[0]["hint"] == "the AI VM's address on the LAN" and inputs[1]["hint"] == ""
    assert sr.inputs_for(["echo <ADMIN_PASSWORD>"], [], RUNBOOK)[0]["secret"] is True


def test_frame_gates_the_shape_and_asks_for_the_values():
    f = sr.frame_run({"node_key": "ADD26", "title": "Install the SSH key"}, RUNBOOK, _spec(), POLICY)
    assert [o["id"] for o in f["options"]] == ["run", "myself", "skip"]        # the shape is allowed with dummy values
    assert [i["name"] for i in f["inputs"]] == ["HOST_IP", "DNS_IP"] and "needs 2 values from you" in f["question"]
    assert f["commands"][0].endswith("aedefruscio@<HOST_IP>")                   # raw — the operator sees the placeholder
    f2 = sr.frame_run({"node_key": "x", "title": "x"}, RUNBOOK, _spec(), {"allow": ["pct set"], "sudo": False})
    assert [o["id"] for o in f2["options"]] == ["myself", "skip"]              # a refused shape stays refused


def test_values_are_checked_and_spliced():
    clean, problems = sr.check_inputs(["HOST_IP", "DNS_IP"], {"HOST_IP": "192.168.1.129"})
    assert clean == {"HOST_IP": "192.168.1.129"} and problems == [{"name": "DNS_IP", "why": "missing"}]
    _, problems = sr.check_inputs(["HOST_IP"], {"HOST_IP": "1.2.3.4; rm -rf /"})
    assert problems[0]["name"] == "HOST_IP" and "no spaces" in problems[0]["why"]
    _, problems = sr.check_inputs(["HOST_IP"], {"HOST_IP": "$(hostname)"})
    assert problems and problems[0]["name"] == "HOST_IP"
    assert sr.substitute(["ssh a@<HOST_IP> <UNKNOWN>"], {"HOST_IP": "10.0.0.5"}) == ["ssh a@10.0.0.5 <UNKNOWN>"]
    assert sr.mask_secrets("pw is hunter2 here", {"ADMIN_PASSWORD": "hunter2"}, [{"name": "ADMIN_PASSWORD", "secret": True}]) == "pw is *** here"


@pytest.mark.asyncio
async def test_run_refuses_until_the_values_are_given_then_runs_the_filled_commands():
    waiting = {"kind": "run", "runbook": RUNBOOK, "commands": sr.runbook_commands(RUNBOOK),
               "verify": sr.verify_commands(RUNBOOK), "refused": [],
               "inputs": [{"name": "HOST_IP", "hint": "", "secret": False}, {"name": "DNS_IP", "hint": "", "secret": False}]}
    db = AsyncMock()
    out = await sr.resolve_run(db, "j", "ADD26", "run", waiting, inputs={"HOST_IP": "192.168.1.129"})
    assert out["outcome"] == "inputs_missing" and out["problems"] == [{"name": "DNS_IP", "why": "missing"}]
    db.execute.assert_not_awaited()

    claim = MagicMock(); claim.rowcount = 1
    done = MagicMock(); done.rowcount = 1
    db.execute = AsyncMock(side_effect=[claim, done])
    executed = [{"command": "ssh-copy-id -i /root/.ssh/id_ed25519.pub aedefruscio@192.168.1.129", "output": "added", "exit": 0, "ok": True, "approval_id": "a", "refused": False},
                {"command": "pct set 111 --nameserver 192.168.1.1", "output": "", "exit": 0, "ok": True, "approval_id": "b", "refused": False}]
    with patch.object(sr, "channel", new=AsyncMock(return_value=(_spec(), POLICY))), \
         patch("app.modules.assist_supervised.run_block", new=AsyncMock(return_value=executed)) as rb, \
         patch("app.modules.assist_local_runner.run_probes", new=AsyncMock(return_value=("== V1 ==\naiserver\n", [{"id": "V1", "command": "ssh aedefruscio@192.168.1.129 hostname", "ok": True, "chars": 8}]))) as probes:
        out = await sr.resolve_run(db, "j", "ADD26", "run", waiting, inputs={"HOST_IP": "192.168.1.129", "DNS_IP": "192.168.1.1"})
    assert out["outcome"] == "ran"
    assert rb.await_args.args[1] == ["ssh-copy-id -i /root/.ssh/id_ed25519.pub aedefruscio@192.168.1.129", "pct set 111 --nameserver 192.168.1.1"]
    assert probes.await_args.args[1][0]["command"] == "ssh aedefruscio@192.168.1.129 hostname"      # verify filled too
    assert "<HOST_IP>" not in db.execute.await_args_list[1].args[1]["out"].split("## Executed")[1]


@pytest.mark.asyncio
async def test_a_secret_value_is_not_taken_when_the_frame_said_it_cannot_be(caplog):
    """§17.1193 — the engine WILL take a secret now (asked once, kept
    encrypted, delivered out of band). What it still refuses is a frame that
    already declared the name undeliverable — the runbook put the placeholder
    where the shell will not expand a reference. A value sent on the wire for
    such a name is ignored, not spliced: masking the record was never enough,
    because the substituted command would still reach the runner's log line and
    the target's process table."""
    waiting = {"kind": "run", "runbook": "## Run this\n```\npct set 111 --password <ADMIN_PASSWORD>\n```\n",
               "commands": ["pct set 111 --password <ADMIN_PASSWORD>"], "verify": [], "refused": [],
               "inputs": [{"name": "ADMIN_PASSWORD", "hint": "", "secret": True}],
               "secrets_missing": ["ADMIN_PASSWORD"]}
    db = AsyncMock()
    with patch.object(sr, "channel", new=AsyncMock(return_value=(_spec(), {"allow": ["pct set"], "sudo": False}))), \
         patch("app.modules.assist_supervised.run_block", new=AsyncMock()) as rb:
        out = await sr.resolve_run(db, "j", "T1", "run", waiting, inputs={"ADMIN_PASSWORD": "hunter2"})
    assert out["outcome"] == "not_runnable"
    assert out["secrets_missing"] == ["ADMIN_PASSWORD"]
    rb.assert_not_awaited()                       # nothing was sent to the runner
    db.execute.assert_not_awaited()               # and the node was not claimed


def test_a_secret_the_runner_holds_becomes_a_reference_not_a_question():
    """The value stays on the target: the engine writes `$NAME`, asks nothing,
    and the runner expands it as an environment variable when it runs."""
    runbook = ("## Run this\n```bash\npct exec 120 -- app-cli --key <API_TOKEN> --host <APP_HOST>\n```\n"
               "## Inputs needed\n- <API_TOKEN> the upstream token\n- <APP_HOST> where the app runs\n"
               # §17.1345 — a block with no check is refused for that alone; these
               # tests are about the secret, so give them the check they should have had.
               "## Verify\n- the app answers: `pct exec 120 -- systemctl is-active app-cli`\n")
    spec = _spec()
    frame = sr.frame_run({"node_key": "T1", "title": "wire it"}, runbook, spec,
                         {"allow": ["pct exec"], "secrets": ["API_TOKEN"]})
    assert frame["commands"] == ["pct exec 120 -- app-cli --key $API_TOKEN --host <APP_HOST>"]
    assert [i["name"] for i in frame["inputs"]] == ["APP_HOST"]     # the secret is not asked for
    assert frame["secrets_resolved"] == ["API_TOKEN"] and frame["secrets_missing"] == []
    assert "run" in [o["id"] for o in frame["options"]]


def test_a_secret_nothing_holds_yet_is_asked_for_once():
    """§17.1193 supersedes §17.1191's refusal. Every tool this engine is
    measured against — Ansible Vault, Actions secrets, Jenkins credentials —
    asks once, keeps the value encrypted and injects it at run time. Refusing
    to hold it bought nothing that out-of-band delivery does not already buy,
    and made the operator edit a file on the target by hand."""
    runbook = ("## Run this\n```bash\npct exec 120 -- app-cli --key <API_TOKEN>\n```\n"
               "## Inputs needed\n- <API_TOKEN> the upstream token\n"
               # §17.1345 — a block with no check is refused for that alone; these
               # tests are about the secret, so give them the check they should have had.
               "## Verify\n- the app answers: `pct exec 120 -- systemctl is-active app-cli`\n")
    frame = sr.frame_run({"node_key": "T1", "title": "wire it"}, runbook, _spec(),
                         {"allow": ["pct exec"], "secrets": []})
    asked = [i for i in frame["inputs"] if i["name"] == "API_TOKEN"]
    assert asked, "the operator was not asked for a value nothing holds"
    assert asked[0]["secret"] is True and asked[0]["store"] == "engine"
    assert asked[0]["kept_encrypted"] is True
    assert asked[0]["hint"] == "the upstream token"      # the runbook's own words survive
    assert not frame["secrets_missing"], frame["secrets_missing"]
    assert "run" in [o["id"] for o in frame["options"]] and frame["suggested"] == "run"
    assert not frame["refused"], frame["refused"]
    assert "<API_TOKEN>" in frame["commands"][0]     # still a placeholder until the answer arrives


def test_the_decide_path_carries_inputs_and_stores_only_their_names():
    import inspect
    from app.modules import decision_pause as dp
    import app.routers.jobs as jr
    from app.schemas import DecideInput
    assert "inputs" in DecideInput.model_fields
    assert "inputs=body.inputs" in inspect.getsource(jr.decide_endpoint) and "inputs_missing" in inspect.getsource(jr.decide_endpoint)
    src = inspect.getsource(dp.resolve_decision)
    assert "inputs=inputs" in src and 'sorted((inputs or {}).keys())' in src
