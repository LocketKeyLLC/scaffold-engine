"""§17.1154 — the engine answers "can the host reach that guest?" itself."""
import inspect
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.modules import assist_guest_reach as gr

SM = {"110": {"kind": "vm", "attrs": {"name": "ai-vm", "boot": "order=scsi0;ide2", "status": "running"},
              "devices": {"net0": "virtio=BC:24:11:B4:AF:15,bridge=vmbr0", "ide2": "local:iso/ubuntu-22.04.3-live-server-amd64.iso,media=cdrom,size=2083390K"}},
      "111": {"kind": "ct", "attrs": {"hostname": "control-panel", "status": "running"}, "devices": {"net0": "name=eth0,bridge=vmbr0,hwaddr=BC:24:11:AA:BB:CC,ip=dhcp"}}}
PASTE = "root@pve:~# ssh aedefruscio@192.168.1.127 nvidia-smi\nssh: connect to host 192.168.1.127 port 22: No route to host\nroot@pve:~#"


def test_detect_and_find_guest():
    d = gr.detect(PASTE)
    assert d == {"ip": "192.168.1.127", "port": 22, "user": "aedefruscio", "symptom": "no_route"}
    assert gr.detect("ssh: connect to host 10.0.0.5 port 2222: Connection refused")["symptom"] == "refused"
    assert gr.detect("root@pve:~# qm status 110\nstatus: stopped") is None                   # not an ssh failure
    g = gr.find_guest(SM, "Make aiserver (VM 110) reachable on SSH port 22", PASTE)
    assert g["id"] == "110" and g["kind"] == "vm" and g["mac"] == "BC:24:11:B4:AF:15" and g["boot"] == "order=scsi0;ide2" and "ubuntu" in g["iso"]
    assert gr.find_guest(SM, "the ai-vm guest", "")["id"] == "110"                          # by name
    assert gr.find_guest(SM, "the control-panel container", "")["id"] == "111" and gr.find_guest(SM, "the control-panel container", "")["mac"] == "BC:24:11:AA:BB:CC"
    assert gr.find_guest(SM, "VM 999 is not in the map", "") is None
    assert gr.find_guest({}, "VM 110", "") is None


def test_every_probe_passes_both_read_only_gates():
    import importlib.util, pathlib
    from app.modules.assist_state_check import read_only_command
    root = pathlib.Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("lr", root / "scripts" / "local_runner_mcp.py")
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    for guest in (gr.find_guest(SM, "VM 110"), gr.find_guest(SM, "CT 111")):
        for pid, cmd in gr.probe_commands(guest, "192.168.1.127"):
            assert read_only_command(cmd), (pid, cmd)
            assert mod.read_only(cmd) == (True, ""), (pid, cmd)


AGENT_JSON = '[{"name":"lo","ip-addresses":[{"ip-address":"127.0.0.1","ip-address-type":"ipv4","prefix":8}]},{"name":"ens18","hardware-address":"bc:24:11:b4:af:15","ip-addresses":[{"ip-address":"192.168.1.140","ip-address-type":"ipv4","prefix":24}]}]'


@pytest.mark.parametrize("outputs,cls,found", [
    ({"status": "status: stopped"}, "stopped", None),
    # LIVE shape: running, no agent, MAC nowhere, no ping, port closed → no network identity at all
    ({"status": "status: running", "agent": "No QEMU guest agent configured", "neigh": "", "fdb": "", "ping": "1 packets transmitted, 0 received, 100% packet loss, time 0ms", "port": "rc=1"}, "no_ip", None),
    ({"status": "status: running", "agent": AGENT_JSON, "neigh": "", "fdb": "", "ping": "1 packets transmitted, 0 received", "port": "rc=1"}, "ip_differs", "192.168.1.140"),
    ({"status": "status: running", "agent": "No QEMU guest agent configured", "neigh": "192.168.1.140 dev vmbr0 lladdr bc:24:11:b4:af:15 REACHABLE", "fdb": "", "ping": "", "port": "rc=1"}, "ip_differs", "192.168.1.140"),
    ({"status": "status: running", "agent": "No QEMU guest agent configured", "neigh": "192.168.1.127 dev vmbr0 lladdr bc:24:11:b4:af:15 STALE", "fdb": "bc:24:11:b4:af:15 dev tap110i0 master vmbr0", "ping": "1 packets transmitted, 1 received, 0% packet loss", "port": "rc=1"}, "no_ssh", "192.168.1.127"),
    ({"status": "status: running", "agent": "No QEMU guest agent configured", "neigh": "", "fdb": "", "ping": "1 packets transmitted, 1 received", "port": "rc=0"}, "reachable", "192.168.1.127"),
    ({"status": "qm: not found", "agent": "qm: not found", "neigh": "", "fdb": "", "ping": "", "port": "nc: not found"}, "unknown", None),
])
def test_judge_is_deterministic_over_real_proxmox_shapes(outputs, cls, found):
    g = gr.find_guest(SM, "VM 110")
    v = gr.judge(g, "192.168.1.127", outputs)
    assert v["class"] == cls and v.get("found_ip") == found, v
    if cls == "no_ip":
        assert "no network identity" in v["detail"] and "sitting in the installer" in v["detail"]     # the boot order + ISO hint
    txt = gr.render(g, "192.168.1.127", v, outputs, ran_where="the host")
    assert txt.startswith("## 🔎 Can the host reach VM 110 (ai-vm)?") and "**Finding:**" in txt and "`qm status 110` →" in txt


def test_findings_become_plan_steps_and_the_marker_round_trips():
    g = gr.find_guest(SM, "VM 110")
    st = gr.plan_step(g, "192.168.1.127", {"class": "no_ip"})
    assert st["title"].startswith("Install the operating system on VM 110 (ai-vm)") and "Proxmox web console" in st["description"]
    assert "ubuntu-22.04.3-live-server-amd64.iso" in st["description"] and "re-run these checks" in st["description"]
    assert gr.guest_step_class(st["description"]) == ("vm", "110", "no_ip")
    st = gr.plan_step(g, "192.168.1.127", {"class": "stopped"})
    assert "qm start 110" in st["description"] and gr.guest_step_class(st["description"]) == ("vm", "110", "stopped")
    st = gr.plan_step(g, "192.168.1.127", {"class": "no_ssh", "found_ip": "192.168.1.140"})
    assert "192.168.1.140" in st["title"] and "openssh-server" in st["description"]
    assert gr.plan_step(g, "192.168.1.127", {"class": "reachable"}) is None and gr.plan_step(g, "192.168.1.127", {"class": "ip_differs", "found_ip": "x"}) is None
    assert gr.guest_step_class("plain step") is None


@pytest.mark.asyncio
async def test_check_and_act_probes_through_the_runner_and_inserts_the_step(monkeypatch):
    from app.modules import assist_notes, engine_setup as es
    monkeypatch.setattr(gr, "_session_bits", AsyncMock(return_value=(SM, "Make aiserver (VM 110) reachable on SSH port 22", "")))
    outputs = {"status": "status: running", "agent": "No QEMU guest agent configured", "neigh": "", "fdb": "", "ping": "0 received", "port": "rc=1"}
    executed = [{"id": k, "command": c, "ok": True, "chars": 1} for k, c in gr.probe_commands(gr.find_guest(SM, "VM 110"), "192.168.1.127")]
    monkeypatch.setattr(gr, "run_probes", AsyncMock(return_value=(outputs, "pve-runner", executed)))
    monkeypatch.setattr(gr, "open_guest_step", AsyncMock(return_value=None))
    calls = {}
    async def _add(**kw): calls["add"] = kw; return {"node_key": "ADD90"}
    async def _present(db, sid, nk): calls["present"] = nk
    monkeypatch.setattr(assist_notes, "add_step", _add); monkeypatch.setattr(es, "_present", _present)
    from app.modules import assist_agent
    monkeypatch.setattr(assist_agent, "ingest_turn", AsyncMock()); monkeypatch.setattr(assist_agent, "capture_assistant_reply", AsyncMock())
    res = await gr.check_and_act(db=MagicMock(), session_id="s", node_key="ADD49", error_text=PASTE)
    assert res["verdict"]["class"] == "no_ip" and res["step"] == "ADD90"
    assert assist_agent.capture_assistant_reply.await_count == 1                       # the check persists its own reply, once
    assert calls["add"]["before_node_key"] == "ADD49" and calls["add"]["steps"][0]["title"].startswith("Install the operating system on VM 110")
    assert calls["present"] == "ADD90" and "I added **ADD90" in res["text"]
    rec = assist_agent.ingest_turn.await_args.kwargs["content"]
    assert rec.startswith("[local-runner] the engine checked whether VM 110 (ai-vm) is reachable") and "$ qm status 110" in rec
    # an open step for the same finding is reused, never duplicated
    calls.clear(); monkeypatch.setattr(gr, "open_guest_step", AsyncMock(return_value="ADD90"))
    res = await gr.check_and_act(db=MagicMock(), session_id="s", node_key="ADD49", error_text=PASTE)
    assert res["step"] == "ADD90" and "add" not in calls and "already in the plan" in res["text"]
    # no runner → the block is handed to the operator, nothing inserted
    monkeypatch.setattr(gr, "run_probes", AsyncMock(return_value=None))
    res = await gr.check_and_act(db=MagicMock(), session_id="s", node_key="ADD49", error_text=PASTE)
    assert res["step"] is None and res["verdict"] is None and "```bash\nqm status 110" in res["text"] and "📍 On: the Proxmox host shell" in res["text"]
    # not an ssh-to-guest failure → None (the model fix runs as before)
    assert await gr.check_and_act(db=MagicMock(), session_id="s", node_key="ADD49", error_text="E: Unable to locate package foo") is None


@pytest.mark.asyncio
async def test_verify_guest_step_reprobes(monkeypatch):
    g = gr.find_guest(SM, "VM 110")
    desc = gr.plan_step(g, "192.168.1.127", {"class": "no_ip"})["description"]
    monkeypatch.setattr(gr, "_session_bits", AsyncMock(return_value=(SM, "", "")))
    fixed = {"status": "status: running", "agent": AGENT_JSON, "neigh": "", "fdb": "", "ping": "1 received", "port": "rc=1"}
    monkeypatch.setattr(gr, "run_probes", AsyncMock(return_value=(fixed, "pve-runner", [])))
    v = await gr.verify_guest_step(db=MagicMock(), session_id="s", node_key="ADD90", description=desc, evidence="installer finished, login prompt")
    assert v["outcome"] == "success" and v["recipe"] == "guest_reach"
    same = {"status": "status: running", "agent": "No QEMU guest agent configured", "neigh": "", "fdb": "", "ping": "0 received", "port": "rc=1"}
    monkeypatch.setattr(gr, "run_probes", AsyncMock(return_value=(same, "pve-runner", [])))
    v = await gr.verify_guest_step(db=MagicMock(), session_id="s", node_key="ADD90", description=desc, evidence="done")
    assert v["outcome"] == "incomplete" and v["exhausted"] is True and "Re-checked from the host" in v["reason"]
    monkeypatch.setattr(gr, "run_probes", AsyncMock(return_value=None))
    assert await gr.verify_guest_step(db=MagicMock(), session_id="s", node_key="ADD90", description=desc, evidence="done") is None


def test_wiring_fix_flow_fix_endpoint_verify_and_repoint():
    from app.modules import assist_turn, assist_agent, engine_setup as es
    src = inspect.getsource(assist_turn._fix_flow)
    assert src.index("check_and_act(") < src.index("run_step_fix(")            # ahead of the model fix
    assert '_claim_and_guide(session_id, _reach["step"]' in src
    assert src.count("capture_assistant_reply(") == 1                           # §17.1099 — the check persists its own reply
    src = inspect.getsource(assist_agent.run_step_fix)
    assert src.index("check_and_act(") < src.index("_assemble_ctx_for_node(") and '"inserted_step"' in src
    src = inspect.getsource(es.verify_recipe_submit)
    assert "guest_step_class(desc)" in src and "verify_guest_step(" in src
    src = inspect.getsource(es.repoint_after_repair)
    assert "Engine guest check:" in src                                        # a guest step's commit re-points; it is never a target
