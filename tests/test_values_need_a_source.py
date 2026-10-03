"""§17.1312 — a value written into a file needs a source; cut content is refused.

Live, 2026-10-03 13:18 UTC: ADD100's template frame (Run suggested, every gate
green) wrote a 139-line server.js with Pi-hole at 192.168.1.130 (the container's
ID), the engine at 192.168.1.110:8080, and ended mid-line: `const runRes = await
axios.post(SCAFFOLD_ENGINE_URL` -- the draw hit its 1500-token cap and the
template closed its own heredoc after the cut."""
from __future__ import annotations

import json
import pathlib

import pytest

from app.modules import runbook_templates as rt
from app.modules import supervised_runs as sr

FX = pathlib.Path(__file__).parent / "fixtures"
FRAME = json.loads((FX / "add100_frame_template_2026_10_03.json").read_text(encoding="utf-8"))
POLICY = {"allow": ["ANY"], "sudo": True, "helper": "19", "secrets": ["MASS_PASSWORD"], "can_write_files": True}
NODE = {"node_key": "ADD100", "title": "Rebuild the control panel to do what was chosen in ADD99",
        "description": "Rework the control-panel backend and frontend in LXC 111."}
ENV = {"profile": "root@pve", "facts": ["Prowlarr is 192.168.1.21, Radarr 192.168.1.22, Sonarr 192.168.1.23"],
       "substitutions": {"PALWORLD_IP": "192.168.1.106"},
       "system_state": {"111": {"kind": "ct", "attrs": {"hostname": "control-panel", "ip": "192.168.1.25"}}}}


def _frame(rb, env=ENV, upstream=""):
    spec = type("S", (), {"name": "pve-runner", "headers": {}})()
    return sr.frame_run(NODE, rb, spec, POLICY, env=env, upstream=upstream)


def test_the_live_frame_is_refused_for_the_addresses_it_invented():
    frame = _frame(FRAME["runbook"])
    hits = [r for r in frame["refused"] if "appears in nothing the engine holds" in r["why"]]
    assert len(hits) == 1, [r["why"][:100] for r in frame["refused"]]
    assert "192.168.1.130" in hits[0]["why"] and "192.168.1.110" in hits[0]["why"]
    for sourced in ("192.168.1.106", "192.168.1.21", "192.168.1.22", "192.168.1.23"):
        assert sourced not in hits[0]["why"], sourced
    assert "run" not in {o["id"] for o in frame["options"]}
    assert sr.shape_retry_note({"refused": hits, "commands": frame["commands"]}), "registered: the chain redrafts"


def test_an_upstream_output_or_a_fact_is_a_source():
    files = [{"path": "/etc/x.conf", "content": "upstream 192.168.1.130:80;\n"}]
    assert sr.unsourced_addresses_in_files([], files, ENV, NODE)
    assert sr.unsourced_addresses_in_files([], files, ENV, NODE, upstream="ADD111: Pi-hole answers at 192.168.1.130") == []
    assert sr.unsourced_addresses_in_files([], files, {**ENV, "facts": ENV["facts"] + ["pihole is 192.168.1.130"]}, NODE) == []
    assert sr.unsourced_addresses_in_files([], files, ENV, {**NODE, "description": "Pi-hole (192.168.1.130) stats"}) == []
    assert sr.unsourced_addresses_in_files(["curl http://127.0.0.1:3001/", "ip route add 0.0.0.0/0 via 192.168.1.1"], [], ENV, NODE) == [], "loopback, any-address and a .1 gateway need no source"
    assert sr.unsourced_addresses_in_files([], files, {"profile": "root@pve"}, NODE) == [], "no ledger at all: nothing to compare against, nothing refused"
    assert sr.unsourced_addresses_in_files([], [{"path": "/x", "content": "mask 255.255.255.0 net 192.168.1.0\n"}], ENV, NODE) == []


def test_cut_content_is_detected_retried_once_and_then_refused(monkeypatch):
    assert rt.content_is_cut("```bash\nconst x = axios.post(URL") is True
    assert rt.content_is_cut("```bash\napt-get update\n```") is False and rt.content_is_cut("apt-get update") is False


@pytest.mark.asyncio
async def test_the_draw_retries_at_three_times_the_cap_then_raises(monkeypatch):
    import app.utils.llm_retry as lr
    caps = []

    async def cut(gen, prompt, params, *, system, **kw):
        caps.append(kw.get("max_tokens"))
        return type("R", (), {"text": "```bash\ntee /opt/x/server.js <<'EOF'\nconst runRes = await axios.post(URL"})()
    monkeypatch.setattr(lr, "generate_until_nonempty", cut)
    with pytest.raises(rt.ContentCut) as ei:
        await rt.fill_free_params(rt.RUN_IN_CONTAINER, NODE, "brief")
    assert caps == [1500, 4500] and "cut mid-line" in str(ei.value) and "split the step" in str(ei.value)

    whole = []

    async def ok_second(gen, prompt, params, *, system, **kw):
        whole.append(kw.get("max_tokens"))
        if kw.get("max_tokens") == 1500 and "REMOTE_COMMANDS" in kw.get("label", ""):
            return type("R", (), {"text": "```bash\ntee /x <<'EOF'\nhalf"})()
        return type("R", (), {"text": "```bash\ntee /x <<'EOF'\nwhole\nEOF\n```"})()
    monkeypatch.setattr(lr, "generate_until_nonempty", ok_second)
    vals = await rt.fill_free_params(rt.RUN_IN_CONTAINER, NODE, "brief")
    assert vals["REMOTE_COMMANDS"].endswith("tee /x <<'EOF'\nwhole\nEOF") and whole[:2] == [1500, 4500]   # §17.1314 prepends the backup line


@pytest.mark.asyncio
async def test_a_cut_template_draw_becomes_a_refused_frame_not_a_model_draft(monkeypatch):
    async def raise_cut(*a, **k):
        raise rt.ContentCut("the model's REMOTE_COMMANDS for ADD100 was cut mid-line at 4500 tokens (9000 chars): the step's content is too large for one draw -- split the step, or write the file in parts")
    monkeypatch.setattr(rt, "fill_free_params", raise_cut)
    from app.modules import machine_truth as mt
    rb = await sr.draft_runbook(NODE, {"description": "brief"}, "", spec=None, environment=ENV, truth=mt.GuestTruth(gid="111", kind="ct", status="running"))
    assert "content cut" in rb and "split the step" in rb
    frame = _frame(rb)
    assert "run" not in {o["id"] for o in frame["options"]}, frame["options"]


def test_every_frame_in_the_pause_carries_the_upstream_block():
    from app.modules import execution_agent as ea
    src = pathlib.Path(ea.__file__).read_text(encoding="utf-8")
    i = src.index("async def _pause_for_decision(")
    body = src[i:src.index("\nasync def ", i + 10)]
    assert body.count("supervised_runs.frame_run(") >= 8 and body.count("upstream=up_block") >= body.count("supervised_runs.frame_run(")
