"""§17.1188 — placeholder values offered from the environment ledger: pins
prefill, the system map and the facts are offered with their source, a
secret is never prefilled. Deterministic; no model."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules import runbook_inputs as ri
from app.modules import supervised_runs as sr

ENV = {
    "profile": "Operator runs commands as root@pve in ONE interactive shell.",
    "substitutions": {"JELLYFIN_IP": "192.168.1.20", "MEDIA_VLAN_ID": "20", "TEMPLATE_NAME": "debian-12-standard"},
    "system_state": {
        "host": {"kind": "host", "attrs": {"ip": "192.168.1.156"}, "source": "ip addr"},
        "101": {"kind": "ct", "attrs": {"hostname": "jellyfin", "status": "running"}, "source": "pct list"},
        "106": {"kind": "vm", "attrs": {"name": "palworld-server"}, "source": "qm config 106"},
        "110": {"kind": "vm", "attrs": {"name": "ai-vm"}, "source": "qm list"},
    },
    "facts": [
        "LXC 103 (radarr) is running: net0 eth0 on vmbr0 gw 192.168.1.1 ip 192.168.1.22/24.",
        "pct exec 101 -- getent hosts defrusciohomelab.duckdns.org returns 67.240.32.243 (DNS resolution works).",
        "ssh aedefruscio@192.168.1.127 nvidia-smi from the Proxmox host fails with 'No route to host'.",
        "The control-panel backend listens on port 3001 inside container 111.",
    ],
}


def test_a_pin_with_the_same_name_is_prefilled():
    s = ri.suggest_for("JELLYFIN_IP", ENV)
    assert s[0] == {"value": "192.168.1.20", "source": "pinned as JELLYFIN_IP", "confidence": "pinned"}
    out = ri.suggest_inputs([{"name": "JELLYFIN_IP", "hint": "", "secret": False}], ENV)
    assert out[0]["value"] == "192.168.1.20" and out[0]["suggestions"][0]["confidence"] == "pinned"


def test_the_system_map_offers_ids_names_and_addresses_by_the_names_words():
    vm = ri.suggest_for("PALWORLD_VMID", ENV)
    assert vm[0]["value"] == "106" and "palworld-server" in vm[0]["source"] and vm[0]["confidence"] == "map"
    bare = [x["value"] for x in ri.suggest_for("VMID", ENV)]
    assert bare[:3] == ["101", "106", "110"] and "103" in bare      # no word → every guest on the map, then ids the facts name
    host = ri.suggest_for("HOST_IP", ENV)
    assert host[0]["value"] == "192.168.1.156" and host[0]["source"].startswith("system map: host")
    assert ri.suggest_for("JELLYFIN_HOSTNAME", ENV)[0]["value"] == "jellyfin"
    assert ri.suggest_for("SSH_USER", ENV)[0] == {"value": "root", "source": "the shell you work in", "confidence": "map"}


def test_facts_offer_typed_values_only_when_they_mention_the_names_words():
    gw = ri.suggest_for("RADARR_GATEWAY_IP", ENV)
    assert [x["value"] for x in gw] == ["192.168.1.1", "192.168.1.22"] and gw[0]["confidence"] == "fact"
    assert [x["value"] for x in ri.suggest_for("BACKEND_PORT", ENV)] == ["3001"]
    assert ri.suggest_for("NVIDIA_IP", ENV)[0]["value"] == "192.168.1.127"
    assert ri.suggest_for("RADARR_PREFIX", ENV)[0]["value"] == "24"
    assert ri.suggest_for("UNRELATED_THING", ENV) == []                        # a text name with no source is not guessed


def test_a_secret_is_never_prefilled_or_suggested():
    env = {**ENV, "substitutions": {"ADMIN_PASSWORD": "hunter2"}}
    assert ri.suggest_for("ADMIN_PASSWORD", env) == []
    out = ri.suggest_inputs([{"name": "ADMIN_PASSWORD", "hint": "", "secret": True}], env)
    assert out[0]["value"] == "" and out[0]["suggestions"] == []


def test_the_frame_carries_the_suggestions():
    runbook = "## Run this\n```\npct set <VMID> --nameserver <DNS_IP>\n```\n"
    spec = MagicMock(); spec.name = "pve-runner"
    f = sr.frame_run({"node_key": "x", "title": "x"}, runbook, spec, {"allow": ["pct set"], "sudo": False}, env=ENV)
    names = {i["name"]: i for i in f["inputs"]}
    assert [x["value"] for x in names["VMID"]["suggestions"]][:3] == ["101", "106", "110"]
    assert names["DNS_IP"]["suggestions"] and names["DNS_IP"]["value"] == ""


@pytest.mark.asyncio
async def test_job_environment_prefers_the_sessions_ledger_then_the_job_row():
    db = AsyncMock()
    sess = MagicMock(); sess.mappings.return_value.first.return_value = {"metadata": {"environment": ENV}}
    db.execute = AsyncMock(return_value=sess)
    env = await ri.job_environment(db, "j")
    assert env["substitutions"]["JELLYFIN_IP"] == "192.168.1.20"
    assert "assist_sessions WHERE job_id" in db.execute.await_args.args[0].text
    none = MagicMock(); none.mappings.return_value.first.return_value = None
    job = MagicMock(); job.mappings.return_value.first.return_value = {"metadata": {"environment": {"facts": ["x"]}}}
    db.execute = AsyncMock(side_effect=[none, job])
    assert (await ri.job_environment(db, "j"))["facts"] == ["x"]


def test_the_pause_passes_the_environment_to_the_frame():
    import inspect
    from app.modules import execution_agent as ea
    src = inspect.getsource(ea._pause_for_decision)
    assert "job_environment(db, job_id)" in src and "frame_run(run_node, runbook, spec, policy, env=_env)" in src
