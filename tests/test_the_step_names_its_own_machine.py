"""§17.1275 — the step's own text names the machine and the account; the host
hands over its own public key; VMs are guests too.

Live, ADD26 ("Install the SSH public key on the AI VM (192.168.1.129)") parked
asking for three values the engine already had evidence for. Its own hints said
so — "task line suggests `aedefruscio`", "task line says `192.168.1.129`" — and
the frame offered `root` (the Proxmox shell's user) for the VM's account, nothing
for the IP, and asked for <OPERATOR_PUBLIC_KEY> as an encrypted secret while the
host it was going to run on holds `/root/.ssh/id_rsa.pub`, readable through the
channel the engine had used all evening.

    "The engine should have all of that info for the AI VM from when we created it."
"""
from __future__ import annotations

import ast
import pathlib
from unittest.mock import MagicMock, patch

import pytest

from app.modules import runbook_discovery as rd
from app.modules import runbook_inputs as ri
from app.modules import supervised_runs as sr

# verbatim from the operator's job
ADD26 = {
    "node_key": "ADD26",
    "title": "Install the SSH public key on the AI VM (192.168.1.129)",
    "description": ("Add the operator's SSH public key to the authorized_keys of the account used to reach the AI VM "
                    "at 192.168.1.129 (VM 110), so the Proxmox host can connect without a password. Done when "
                    "`ssh aedefruscio@192.168.1.129` (or the intended user) authenticates via key and no longer "
                    "returns 'Permission denied (publickey,password)'.\n\n[Plan update — operator constraint]: The "
                    "Caddyfile/caddy absence does not affect SSH key installation on the AI VM; this step is "
                    "unrelated to the fact."),
}
# verbatim from the operator's host, through the engine's own runner
QM_LIST = """      VMID NAME                 STATUS     MEM(MB)    BOOTDISK(GB) PID       
       100 gpu-vm               stopped    16384             40.00 0         
       106 palworld-server      stopped    8192             100.00 0         
       110 ai-vm                stopped    16384            100.00 0         
"""
ID_RSA_PUB = "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAACAQCrD5xn6976YO6y3l8gucPeYqql2ErgozFVzkfq/xkvdaAgaR9TBi61Mgpcz root@pve"
CAT_ALL = ("cat: /root/.ssh/id_ed25519.pub: No such file or directory\n" + ID_RSA_PUB + "\n"
           "cat: /root/.ssh/id_ecdsa.pub: No such file or directory\n")
ENV = {"profile": "You work as root@pve in ONE interactive shell on the Proxmox host."}


def _inputs(*names):
    return [{"name": n, "hint": "", "secret": ri.secret_name(n), "value": "", "suggestions": []} for n in names]


# ───── the step's own text

def test_the_live_step_prefills_its_own_user_and_ip():
    out = ri.suggest_inputs(_inputs("AI_VM_USER", "AI_VM_IP"), ENV, text=sr.step_text(ADD26))
    user = next(i for i in out if i["name"] == "AI_VM_USER")
    ip = next(i for i in out if i["name"] == "AI_VM_IP")
    assert user["value"] == "aedefruscio", user
    assert "the step's own text" in user["suggestions"][0]["source"]
    assert ip["value"] == "192.168.1.129", ip


def test_the_shells_user_is_not_offered_for_an_account_the_step_names():
    """`root` is the Proxmox shell; the step says whose account on the VM."""
    out = ri.suggest_inputs(_inputs("AI_VM_USER"), ENV, text=sr.step_text(ADD26))
    assert [s["value"] for s in out[0]["suggestions"]] == ["aedefruscio"]


def test_without_a_named_account_the_shells_user_is_still_offered():
    """§17.1188's behaviour is kept where the step says nothing."""
    out = ri.suggest_inputs(_inputs("TARGET_USER"), ENV, text="Rotate the log files on the host.")
    assert [s["value"] for s in out[0]["suggestions"]] == ["root"]
    assert out[0]["value"] == "", "the shell's user is offered, never prefilled"


def test_two_accounts_in_the_text_are_both_offered_and_neither_prefilled():
    text = "Copy the key from admin@192.168.1.5 to media@192.168.1.20."
    out = ri.suggest_inputs(_inputs("TARGET_USER"), ENV, text=text)
    assert sorted(s["value"] for s in out[0]["suggestions"]) == ["admin", "media"]
    assert out[0]["value"] == ""


def test_an_ip_near_the_placeholders_own_word_comes_first():
    text = "Point Radarr (192.168.1.22) at Prowlarr (192.168.1.21) and Sonarr (192.168.1.23)."
    out = ri.suggest_inputs(_inputs("PROWLARR_IP"), {}, text=text)
    assert out[0]["value"] == "192.168.1.21", out[0]
    out = ri.suggest_inputs(_inputs("TARGET_IP"), {}, text=text)
    assert out[0]["value"] == "" and len(out[0]["suggestions"]) == 3, "no distinguishing word: all offered, none chosen"


def test_a_pin_still_outranks_the_step_text():
    env = {**ENV, "substitutions": {"AI_VM_IP": "192.168.1.200"}}
    out = ri.suggest_inputs(_inputs("AI_VM_IP"), env, text=sr.step_text(ADD26))
    assert out[0]["value"] == "192.168.1.200"
    assert out[0]["suggestions"][0]["confidence"] == "pinned"


def test_a_placeholder_in_the_text_is_not_an_account():
    """`ssh <AI_VM_USER>@<AI_VM_IP>` in a runbook must not read as a user."""
    out = ri.suggest_inputs(_inputs("AI_VM_USER"), {}, text="Run `ssh <AI_VM_USER>@<AI_VM_IP> true` first.")
    assert out[0]["suggestions"] == []


def test_step_text_is_the_steps_words_not_the_runbook():
    """The runbook carries the drafter's `root@pve` execution-context line;
    reading it as the step's account would re-create the `root` offer."""
    assert "root@pve" not in sr.step_text(ADD26)
    assert sr.step_text({"title": "t", "description": "d", "prompt_template": "d"}) == "t\nd", "deduplicated"


def test_frame_run_hands_the_steps_text_to_the_suggestions():
    tree = ast.parse(pathlib.Path(sr.__file__).read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "frame_run")
    calls = [c for c in ast.walk(fn) if isinstance(c, ast.Call) and getattr(c.func, "id", None) == "suggest_inputs"]
    assert calls and any(k.arg == "text" for k in calls[0].keywords), "suggest_inputs is called without the step's text"


# ───── a public key is not a secret

@pytest.mark.parametrize("name", ["OPERATOR_PUBLIC_KEY", "SSH_PUBLIC_KEY", "HOST_PUBKEY", "PUB_KEY"])
def test_a_public_key_is_not_a_secret(name):
    assert ri.secret_name(name) is False


@pytest.mark.parametrize("name", ["PROWLARR_API_KEY", "ROOT_PASSWORD", "WG_PRIVATE_KEY", "API_TOKEN"])
def test_a_real_secret_still_is(name):
    assert ri.secret_name(name) is True


def test_inputs_for_uses_the_one_definition():
    inputs = sr.inputs_for(['ssh u@h "echo <OPERATOR_PUBLIC_KEY> >> k"', "curl -H 'X: <PROWLARR_API_KEY>'"], [], "")
    by = {i["name"]: i["secret"] for i in inputs}
    assert by == {"OPERATOR_PUBLIC_KEY": False, "PROWLARR_API_KEY": True}
    src = pathlib.Path(sr.__file__).read_text(encoding="utf-8")
    i = src.index("def inputs_for(")
    assert "secret_name(" in src[i:src.index("\ndef ", i + 10)], "inputs_for must call runbook_inputs.secret_name"


# ───── the host hands over its own key

def _spec():
    s = MagicMock(); s.name = "pve-runner"; return s


def _runner(outputs: dict, sent: list | None = None):
    async def fake(spec, tool, args):
        if sent is not None:
            sent.append(args["command"])
        r = MagicMock(); r.structured = None; r.is_error = False
        r.text = outputs.get(args["command"], "")
        return r
    return fake


def test_key_lines_are_parsed_and_error_lines_are_not():
    assert rd.public_keys(CAT_ALL) == [ID_RSA_PUB]
    assert rd.public_keys("cat: /root/.ssh/id_rsa.pub: No such file or directory\n") == []


@pytest.mark.asyncio
async def test_the_hosts_public_key_is_read_off_the_machine():
    inputs = _inputs("OPERATOR_PUBLIC_KEY")
    sent: list = []
    with patch("app.modules.mcp_client.call_tool", new=_runner({"cat " + " ".join(rd._PUBKEY_PATHS): CAT_ALL}, sent)):
        out = await rd.discover_inputs(inputs, ["ssh u@h true"], _spec())
    k = out[0]
    assert k["value"] == ID_RSA_PUB, k
    assert k["secret"] is False
    assert "this host's own public key" in k["suggestions"][0]["source"]
    assert all(c.startswith("cat /root/.ssh/") for c in sent), sent


@pytest.mark.asyncio
async def test_two_host_keys_are_offered_and_neither_prefilled():
    two = CAT_ALL + "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGb root@pve\n"
    with patch("app.modules.mcp_client.call_tool", new=_runner({"cat " + " ".join(rd._PUBKEY_PATHS): two})):
        out = await rd.discover_inputs(_inputs("OPERATOR_PUBLIC_KEY"), [], _spec())
    assert len(out[0]["suggestions"]) == 2 and out[0]["value"] == ""


@pytest.mark.asyncio
async def test_a_host_without_a_key_leaves_the_ask_alone():
    with patch("app.modules.mcp_client.call_tool", new=_runner({})):
        out = await rd.discover_inputs(_inputs("OPERATOR_PUBLIC_KEY"), [], _spec())
    assert out[0]["value"] == "" and out[0]["suggestions"] == []


# ───── VMs are guests too

def test_qm_list_is_parsed_with_its_own_column_order():
    assert rd.vms_by_name(QM_LIST) == {"gpu-vm": ("100", "stopped"), "palworld-server": ("106", "stopped"),
                                       "ai-vm": ("110", "stopped")}


@pytest.mark.asyncio
async def test_a_vm_id_is_resolved_off_qm_list():
    with patch("app.modules.mcp_client.call_tool", new=_runner({"pct list": "VMID Status Lock Name\n", "qm list": QM_LIST})):
        out = await rd.discover_inputs(_inputs("AI_VM_VMID"), [], _spec())
    assert out[0]["value"] == "110", out[0]


@pytest.mark.asyncio
async def test_a_stopped_vm_has_no_address_to_read():
    sent: list = []
    with patch("app.modules.mcp_client.call_tool", new=_runner({"pct list": "VMID Status Lock Name\n", "qm list": QM_LIST}, sent)):
        out = await rd.discover_inputs(_inputs("AI_VM_IP"), [], _spec())
    assert out[0]["value"] == "" and out[0]["suggestions"] == []
    assert not any("qm agent" in c for c in sent), "nothing is asked of a stopped VM"


@pytest.mark.asyncio
async def test_a_running_vm_answers_through_its_agent():
    running = QM_LIST.replace("110 ai-vm                stopped", "110 ai-vm                running")
    agent = ('[{"name":"lo","ip-addresses":[{"ip-address":"127.0.0.1","ip-address-type":"ipv4"}]},'
             '{"name":"ens18","ip-addresses":[{"ip-address":"192.168.1.129","ip-address-type":"ipv4"},'
             '{"ip-address":"fe80::1","ip-address-type":"ipv6"}]}]')
    with patch("app.modules.mcp_client.call_tool", new=_runner({"pct list": "VMID Status Lock Name\n", "qm list": running,
                                                                "qm agent 110 network-get-interfaces": agent})):
        out = await rd.discover_inputs(_inputs("AI_VM_IP"), [], _spec())
    assert out[0]["value"] == "192.168.1.129", out[0]
    assert "qm agent 110" in out[0]["suggestions"][0]["source"]


@pytest.mark.asyncio
async def test_a_container_still_wins_over_a_vm_of_the_same_name():
    """The pct path is untouched: a name `pct list` resolves is never re-asked of qm."""
    sent: list = []
    with patch("app.modules.mcp_client.call_tool", new=_runner({
            "pct list": "VMID       Status     Lock         Name\n102        running                 prowlarr\n",
            "qm list": QM_LIST, "pct exec 102 -- hostname -I": "192.168.1.21 \n"}, sent)):
        out = await rd.discover_inputs(_inputs("PROWLARR_IP"), [], _spec())
    assert out[0]["value"] == "192.168.1.21"
    assert not any(c.startswith("qm agent") for c in sent)


@pytest.mark.asyncio
async def test_only_read_only_commands_are_sent():
    from app.modules.assist_state_check import read_only_command
    sent: list = []
    running = QM_LIST.replace("110 ai-vm                stopped", "110 ai-vm                running")
    with patch("app.modules.mcp_client.call_tool", new=_runner({"qm list": running, "pct list": "VMID Status Lock Name\n"}, sent)):
        await rd.discover_inputs(_inputs("AI_VM_IP", "AI_VM_VMID", "OPERATOR_PUBLIC_KEY"), [], _spec())
    assert sent, "something must have been asked"
    for c in sent:
        assert read_only_command(c), f"not read-only: {c}"


# ───── §17.1286 — a VM with no guest agent is still reachable

QM_CONFIG_106 = "agent: 1\nname: palworld-server\nnet0: virtio=BC:24:11:E8:9F:7A,bridge=vmbr0\nscsi0: oasis:vm-106-disk-0,size=100G\n"
IP_NEIGH = ("192.168.1.23 dev vmbr0 lladdr bc:24:11:b1:b3:b4 STALE\n"
            "192.168.1.129 dev vmbr0 lladdr bc:24:11:b4:af:15 STALE\n"
            "192.168.1.44 dev vmbr0 lladdr bc:24:11:e8:9f:7a REACHABLE\n")


def test_the_mac_is_read_off_qm_config_and_matched_in_the_neighbour_table():
    assert rd.vm_macs(QM_CONFIG_106) == ["bc:24:11:e8:9f:7a"]
    assert rd.address_by_mac(IP_NEIGH, ["BC:24:11:E8:9F:7A"]) == ["192.168.1.44"]
    assert rd.address_by_mac(IP_NEIGH, ["bc:24:11:00:00:00"]) == []


@pytest.mark.asyncio
async def test_a_running_vm_without_an_agent_is_found_by_its_mac():
    running = QM_LIST.replace("106 palworld-server      stopped", "106 palworld-server      running")
    with patch("app.modules.mcp_client.call_tool", new=_runner({
            "pct list": "VMID Status Lock Name\n", "qm list": running,
            "qm agent 106 network-get-interfaces": "No QEMU guest agent configured",
            "qm config 106": QM_CONFIG_106, "ip neigh show": IP_NEIGH})):
        out = await rd.discover_inputs(_inputs("PALWORLD_VM_IP"), [], _spec())
    assert out[0]["value"] == "192.168.1.44", out[0]
    assert "ip neigh" in out[0]["suggestions"][0]["source"]


def test_the_rules_and_the_refusal_say_how_to_reach_a_vm_without_an_agent():
    assert "A VM WITH NO GUEST AGENT IS STILL REACHABLE" in sr.CHANNEL_RULES
    assert 'MASS_PASSWORD="$MASS_PASSWORD" bash /tmp/' in sr.CHANNEL_RULES and "ip neigh show" in sr.CHANNEL_RULES
    found = sr.commands_never_reach_the_guest(["sudo apt-get install -y qemu-guest-agent"],
                                             {"title": "Install and enable QEMU Guest Agent in VM 106"})
    assert found and "would have installed" in found[0]["why"] and "net0 MAC" in found[0]["why"]
    assert "live, this installed" not in found[0]["why"]


def test_a_script_that_reaches_the_guest_satisfies_the_guest_gate():
    node = {"title": "Install and enable QEMU Guest Agent in VM 106"}
    script = ('set -e\nqm start 106 || true\nfor i in $(seq 1 12); do ip neigh show | grep -qi bc:24:11:e8:9f:7a && break; sleep 5; done\n'
              'IP=$(ip neigh show | grep -i bc:24:11:e8:9f:7a | awk \'{print $1}\')\n'
              'SSHPASS="$MASS_PASSWORD" sshpass -e ssh-copy-id -o StrictHostKeyChecking=accept-new aedefruscio@$IP\n'
              'ssh aedefruscio@$IP "sudo apt-get install -y qemu-guest-agent && sudo systemctl enable --now qemu-guest-agent"\n')
    files = [{"path": "/tmp/vm106_agent.sh", "content": script}]
    assert sr.commands_never_reach_the_guest(['MASS_PASSWORD="$MASS_PASSWORD" bash /tmp/vm106_agent.sh'], node, files) == []
    assert sr.commands_never_reach_the_guest(["bash /tmp/vm106_agent.sh"], node), "without the file in view the command reaches nothing"


def test_a_bash_file_is_syntax_checked_and_its_secret_must_be_passed():
    good = [{"path": "/tmp/a.sh", "content": 'IP=$(ip neigh | head -1)\nssh u@$IP "echo $MASS_PASSWORD" \n'}]
    bad = [{"path": "/tmp/b.sh", "content": "if [ -f x ]; then\n  echo unterminated\n"}]
    assert sr.file_writes_will_not_work(good) == []
    found = sr.file_writes_will_not_work(bad)
    assert found and "not valid bash" in found[0]["why"]
    missing = sr.script_secret_not_passed(["bash /tmp/a.sh"], good)
    assert missing and 'MASS_PASSWORD="$MASS_PASSWORD" bash /tmp/a.sh' in missing[0]["why"]
    assert sr.script_secret_not_passed(['MASS_PASSWORD="$MASS_PASSWORD" bash /tmp/a.sh'], good) == []
