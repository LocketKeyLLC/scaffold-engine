"""§17.1157 — OWUI plain-language turns and Guide ride the SERVER-SIDE turn loop when the valve is on."""
import json
from unittest.mock import patch

import pytest

from tests._scaffold_router_setup import Pipeline, _mod as _router_mod

_vendor = _router_mod._assist
_SID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def pipe():
    p = Pipeline()
    p.valves.keepalive_interval = 0.05
    return p


def _sse(pipe):
    return type(pipe).pipe.__globals__["_SSE"]


def _reader(frames):
    def fake_reader(url, body, q, *, stop_event=None, r_holder=None):
        fake_reader.calls.append((url, body))
        for ev, payload in frames:
            q.put(("event", ev, json.dumps(payload)))
        q.put(("done", None, None))
    fake_reader.calls = []
    return fake_reader


def test_valve_off_keeps_the_client_cascade(pipe):
    pipe.valves.assist_server_turn_loop = False
    with patch.object(_vendor, "assist_server_turn") as srv, patch.object(_vendor, "record_turn_bg") as rec, \
         patch.object(_vendor, "fast_classify_turn", return_value="status"), \
         patch.object(_vendor, "assist_status_cmd", return_value=iter(["status here"]), create=True):
        try:
            out = "".join(_vendor.assist_nl_turn(pipe, _SID, "status"))
        except Exception:
            out = ""
    assert not srv.called and rec.called


def test_valve_on_streams_the_server_loop_and_renders_frames(pipe):
    pipe.valves.assist_server_turn_loop = True
    S = _sse(pipe)
    frames = [
        (S.ASSIST_TURN_STATUS, {"text": "Reading that…"}),
        (S.ASSIST_TURN_STATUS, {"text": "Reading that…"}),                                  # a repeat is not re-printed
        (S.ASSIST_TURN_STATUS, {"text": "🔁 Your local runner is connected — running that read-only look-up myself through pve-runner…"}),
        (S.ASSIST_ANSWER, {"kind": "note", "text": "🔁 Your local runner is connected, so I ran that look-up myself (1 read-only command through pve-runner):\n\n```\n$ qm status 110\nstatus: running\n```"}),
        (S.ASSIST_ANSWER, {"kind": "fix", "text": "## 🔎 Can the host reach VM 110 (ai-vm)?\n\n**Finding:** VM 110 is running but has NO address anywhere the host can see."}),
        (S.ASSIST_STEP_OUTCOME, {"node_key": "ADD49", "status": "step_incomplete", "verify_reason": "not there yet"}),
        (S.ASSIST_GUIDE_DELTA, {"text": "## 👉 Do this next\n\nOpen the console"}),
        (S.ASSIST_GUIDE_DELTA, {"text": " and install."}),
        (S.ASSIST_GUIDE_DONE, {"status": "ready", "node_key": "ADD90"}),
        (S.ASSIST_STEP_OUTCOME, {"node_key": "ADD90", "status": "committed"}),
        (S.ASSIST_REPLAN_PROPOSAL, {"proposal": {"id": "x"}}),
        (S.ASSIST_TURN_PULSE, {"running_s": 42, "quiet_s": 30}),
        (S.ASSIST_TURN_DONE, {"handled": "fix+lookup"}),
    ]
    rd = _reader(frames)
    with patch.object(pipe, "_stream_sse_to_queue", side_effect=rd), patch.object(_vendor, "record_turn_bg") as rec, \
         patch.object(_vendor, "fast_classify_turn", side_effect=AssertionError("the cascade must not run")):
        out = "".join(_vendor.assist_nl_turn(pipe, _SID, "ssh: connect to host 192.168.1.127 port 22: No route to host", node_key="ADD49"))
    url, body = rd.calls[0]
    assert url.endswith(f"/assist/{_SID}/message") and body["command"] == "message" and body["node_key"] == "ADD49"
    assert body["message"].startswith("ssh: connect")
    assert not rec.called                                                # the server ingests the message — one capture per turn
    assert out.count("_Reading that…_") == 1 and "running that read-only look-up myself" in out
    assert "$ qm status 110" in out and "## 🔎 Can the host reach VM 110" in out
    assert "⚠ Not committed — not there yet" in out
    assert "## 🧭 How to do this step" in out and "Open the console and install." in out
    assert "✅ **Step ADD90 committed.**" in out
    assert "plan-change proposal is waiting" in out and "still working (42s)" in out


def test_guide_rides_the_server_loop_unless_it_is_a_refine(pipe):
    pipe.valves.assist_server_turn_loop = True
    S = _sse(pipe)
    rd = _reader([(S.ASSIST_GUIDE_DELTA, {"text": "## ✅ The engine reached your runner"}), (S.ASSIST_GUIDE_DONE, {"node_key": "ADD2"}), (S.ASSIST_TURN_DONE, {"handled": "guide"})])
    with patch.object(pipe, "_stream_sse_to_queue", side_effect=rd):
        out = "".join(_vendor.assist_guide_stream_cmd(pipe, _SID, node_key="ADD2"))
    assert rd.calls[0][1]["command"] == "guide" and rd.calls[0][0].endswith("/message")
    assert "The engine reached your runner" in out
    # a refine ("redo for macOS") is the guide endpoint's own feature — it stays on /guide/stream
    rd2 = _reader([(S.ASSIST_GUIDE_DELTA, {"text": "redone"}), (S.ASSIST_GUIDE_DONE, {"status": "ready", "guidance_meta": {}, "cached": False})])
    with patch.object(pipe, "_stream_sse_to_queue", side_effect=rd2):
        out = "".join(_vendor.assist_guide_stream_cmd(pipe, _SID, node_key="ADD2", refine="redo for macOS"))
    assert rd2.calls[0][0].endswith("/guide/stream") and "redone" in out


def test_http_and_transport_errors_are_said_plainly(pipe):
    pipe.valves.assist_server_turn_loop = True
    def bad(url, body, q, *, stop_event=None, r_holder=None):
        q.put(("http_error", 409, "session status 'completed' cannot take a turn"))
    with patch.object(pipe, "_stream_sse_to_queue", side_effect=bad):
        out = "".join(_vendor.assist_nl_turn(pipe, _SID, "hello"))
    assert out.startswith("❌ HTTP 409")
