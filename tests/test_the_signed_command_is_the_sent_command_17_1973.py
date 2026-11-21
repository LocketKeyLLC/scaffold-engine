"""§17.1973 — what the operator approves is what the engine signs and what the runner runs.

Live (job 0fb4ea7f LM5, 2026-11-19): the frame's `command -v ipmitool || echo 'ipmitool: not installed'` was signed
by `run_block`, then rewritten in transit by `mcp_client.call_tool` (§17.1475: `command -v` -> `which`) and sent as
`which ipmitool || echo …`. The runner answered "approval signature does not match this command", the SEL was never
read, and the step was failed. The rewrite now happens on the frame, before anything is signed.
"""
import asyncio
import importlib.util
import pathlib

from app.modules import assist_supervised as sw
from app.modules import mcp_client as mc
from app.modules import supervised_runs as sr

REPO = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("lr_17_1973", REPO / "scripts" / "local_runner_mcp.py")
lr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lr)

TOKEN = "made-up-runner-token"
LM5 = [
    "uptime -s; last -x reboot shutdown | head -n 6",
    "command -v ipmitool || echo 'ipmitool: not installed'",
    "ipmitool sel elist | tail -n 15",
]


class _Spec:
    name = "lab-runner"
    headers = {"X-Runner-Token": TOKEN}


def _runner(monkeypatch, received):
    """A session whose `run_supervised` is the RUNNER's own approval check over the bytes it was sent."""
    seen: dict = {}

    class S:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def call_tool(self, name, args):
            received.append((name, args["command"]))
            if name == "run_supervised":
                ok, why = lr.verify_approval(args["command"], args["approval"], TOKEN, seen=seen)
                text = "[exit 0]\n/usr/bin/ipmitool" if ok else f"(refused by the local runner: {why})"
            else:
                text = "[exit 0]\nok"

            class R:
                content, isError = [], False
                structured_content = {"result": text}
            return R()
    monkeypatch.setattr(mc, "_open_session", lambda spec: S())


def test_the_lm5_lookup_runs_with_the_signature_it_was_signed_with(monkeypatch):
    """The live shape: a supervised `command -v … || echo …` reaches the runner byte-equal and is NOT refused."""
    received: list = []
    _runner(monkeypatch, received)
    done = asyncio.run(sw.run_block(_Spec(), [LM5[1]]))
    assert received == [("run_supervised", LM5[1])], "a signed command is sent exactly as signed"
    assert done[0]["ok"] and not done[0]["refused"], done[0]["output"]


def test_a_frame_drawn_lookup_is_signed_in_its_sent_form(monkeypatch):
    """The frame's rewrite comes first, so the sent `which` form is the signed one too."""
    cmds, _verify, _notes = sr.lookups_as_programs(LM5, [])
    received: list = []
    _runner(monkeypatch, received)
    done = asyncio.run(sw.run_block(_Spec(), cmds))
    assert [c for _n, c in received] == cmds
    assert all(d["ok"] for d in done), [d["output"] for d in done]


def test_the_unsigned_read_is_still_rewritten(monkeypatch):
    """§17.1475 stands where nothing is signed: a probe through `run_readonly`."""
    received: list = []
    _runner(monkeypatch, received)
    asyncio.run(mc.call_tool(_Spec(), "run_readonly", {"command": "command -v ipmitool", "timeout_s": 20}))
    assert received == [("run_readonly", "which ipmitool")]


def test_the_frame_shows_the_program_and_says_why():
    cmds, verify, notes = sr.lookups_as_programs(LM5, ["type caddy"])
    assert cmds == [LM5[0], "which ipmitool || echo 'ipmitool: not installed'", LM5[2]]
    assert verify == ["which caddy"]
    assert len(notes) == 1 and "command -v ipmitool" in notes[0]["why"] and "(and 1 more)" in notes[0]["why"]


def test_a_command_with_no_lookup_is_left_alone():
    """The correct miss: no builtin at a segment's head (inside a guest's `sh -c`, or quoted text) -> no change, no note."""
    plain = ["uptime -s; last -x reboot shutdown | head -n 6",
             "pct exec 111 -- sh -c 'command -v node'",
             "grep 'command -v' /etc/x"]
    cmds, verify, notes = sr.lookups_as_programs(plain, ["qm config 110"])
    assert cmds == plain and verify == ["qm config 110"] and notes == []


def test_the_frame_builder_calls_it_before_the_gates():
    """Drawn where every other frame repair is, before `gate_block` judges the shape and the frame is stored."""
    src = pathlib.Path(sr.__file__).read_text(encoding="utf-8")
    i = src.index("cmds, verify, _lk_notes = lookups_as_programs(cmds, verify)")
    assert i < src.index("runnable, refused = gate_block(shape", i)
