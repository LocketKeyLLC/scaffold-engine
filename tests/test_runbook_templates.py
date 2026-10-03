"""§17.1290 — templates the drafter fills: every template passes every gate,
selection follows the measured guest, the model fills only the free parameter."""
from __future__ import annotations

import pathlib

from app.modules import machine_truth as mt
from app.modules import runbook_templates as rt
from app.modules import supervised_runs as sr

POLICY = {"allow": ["ANY"], "sudo": True, "helper": "19", "secrets": ["MASS_PASSWORD"], "can_write_files": True}
ENV = {"profile": "root@pve", "system_state": {"host": {"kind": "host", "attrs": {"ip": "192.168.1.156"}},
                                                "106": {"kind": "vm", "attrs": {"name": "palworld-server"}},
                                                "111": {"kind": "ct", "attrs": {"hostname": "control-panel"}}}}
ADD117 = {"node_key": "ADD117", "title": "Install Ubuntu 22.04 on VM 106 unattended (cloud image + cloud-init)",
          "description": "VM 106 (palworld-server) has NO operating system … install Ubuntu Server 22.04 unattended."}
ADD82 = {"node_key": "ADD82", "title": "Install and enable QEMU Guest Agent in VM 106",
         "description": "Install qemu-guest-agent inside the palworld-server guest (VM 106) and confirm it answers."}
ADD100 = {"node_key": "ADD100", "title": "Rebuild the control panel to do what was chosen in ADD99",
          "description": "Rework the control-panel backend and frontend in LXC 111 so it does the things chosen."}
ADD94 = {"node_key": "ADD94", "title": "Re-attach VM 106's detached disk and grow it to 100G", "description": "qm set / qm resize on the host."}
REMOTE = "apt-get update\napt-get install -y qemu-guest-agent\nsystemctl enable --now qemu-guest-agent"
ADD84 = {"node_key": "ADD84", "title": "Grow the VM 106 filesystem to fill the disk",
         "description": "Inside the palworld-server guest, grow the partition and filesystem so the OS sees the full ~100GB. "
                        "Done when `df -h` inside the guest reports the root filesystem at ~100GB."}


def _truth(kind, agent=False, key=None):
    return mt.GuestTruth(gid="106" if kind == "vm" else "111", kind=kind, status="running", agent=agent, key_known_by=key)


def _frame(runbook, node):
    spec = type("S", (), {"name": "pve-runner", "headers": {}})()
    # §17.1312 — the gate compares every written address with the ledger; the live ledger pins PALWORLD_IP
    return sr.frame_run(node, runbook, spec, POLICY, env={**ENV, "substitutions": {"PALWORLD_IP": "192.168.1.106", **(ENV.get("substitutions") or {})}})


def test_every_template_passes_every_gate():
    cases = [
        (rt.INSTALL_OS_CLOUDINIT, ADD117, _truth("vm"), {}),
        (rt.REACH_VM_SSH_AND_RUN, ADD82, _truth("vm"), {"REMOTE_COMMANDS": REMOTE}),
        (rt.REACH_VM_SSH_AND_RUN, ADD82, _truth("vm", key="ADD26 · Install the SSH public key"), {"REMOTE_COMMANDS": REMOTE}),
        (rt.RUN_IN_CONTAINER, ADD100, _truth("ct"), {"REMOTE_COMMANDS": "apt-get update\napt-get install -y nodejs", "VERIFY_INSIDE": "systemctl is-active control-panel"}),
        (rt.RUN_IN_VM_VIA_AGENT, ADD84, _truth("vm", agent=True), {"REMOTE_COMMANDS": "growpart /dev/sda 1\nresize2fs /dev/sda1", "VERIFY_INSIDE": "df -h /"}),
    ]
    for tpl, node, truth, model_vals in cases:
        rb = rt.render(tpl, rt.values_for(tpl, node, truth, ENV, model_vals))
        assert rt.template_of(rb) == tpl.name
        frame = _frame(rb, node)
        assert frame["refused"] == [], (tpl.name, [r["why"][:120] for r in frame["refused"]])
        assert frame["commands"] and frame["files"] and "run" in {o["id"] for o in frame["options"]}
        assert any("drafted from the engine's template" in w for w in frame["engine_fixed"]), tpl.name
        if tpl in (rt.RUN_IN_CONTAINER, rt.RUN_IN_VM_VIA_AGENT):
            assert frame["inputs"] == [], (tpl.name, frame["inputs"])
        else:
            assert [i["name"] for i in frame["inputs"]] == ["PALWORLD_USER"], tpl.name
            assert frame["inputs"][0]["suggestions"] == [], "the host shell's root is not offered for the guest"


def test_the_install_template_carries_the_days_lessons():
    rb = rt.render(rt.INSTALL_OS_CLOUDINIT, rt.values_for(rt.INSTALL_OS_CLOUDINIT, ADD117, _truth("vm"), ENV))
    body = sr.file_writes(rb)[0]["content"]
    assert 'qm set "$GID" --ide2 local-lvm:cloudinit --ciuser "$USER_NAME" --cipassword "$MASS_PASSWORD"' in body
    assert "--sshkeys \"$PUBKEY\" --ipconfig0 \"ip=dhcp\"" in body and "qm importdisk" in body
    assert 'DISK_SIZE="100G"' in body and 'qm resize "$GID" scsi0 "$DISK_SIZE"' in body
    assert body.index("nmap -sn") < body.index("ip neigh show"), "the kernel-table read (the no-nmap path) comes after a sweep"
    # §17.1295 — with nmap, the address is read from nmap's OWN report, never from `ip neigh`
    assert "/^Nmap scan report for/ { ip=$NF" in body and 'tolower($0) ~ "mac address: " m { print ip; exit }' in body
    assert "| head -n 1 || true)" in body, "every lookup a wait expects to be empty ends in || true"
    assert "set -uo pipefail" in body and "set -e" not in body
    assert sr.runbook_commands(rb) == [f'MASS_PASSWORD="$MASS_PASSWORD" GUEST_USER="<PALWORLD_USER>" bash /tmp/install_os_106.sh {ph}'
                                       for ph in ("prepare", "swap", "boot", "check")], "§17.1290c — four phases, 180 s each"
    assert "curl -fL -C - --retry 2 --max-time 165" in body, "the download is resumable and bounded to the budget"
    assert "cloud-init drive already present" in body, "a re-run after a partial first run does not swap twice"
    assert sr.verify_commands(rb) == ["qm config 106 | grep -E '^(scsi0|ide2|boot):'", "qm status 106"]


def test_the_reach_template_copies_the_key_only_when_none_is_known():
    yes = rt.render(rt.REACH_VM_SSH_AND_RUN, rt.values_for(rt.REACH_VM_SSH_AND_RUN, ADD82, _truth("vm"), ENV, {"REMOTE_COMMANDS": REMOTE}))
    no = rt.render(rt.REACH_VM_SSH_AND_RUN, rt.values_for(rt.REACH_VM_SSH_AND_RUN, ADD82, _truth("vm", key="ADD26"), ENV, {"REMOTE_COMMANDS": REMOTE}))
    assert 'if [ "yes" = "yes" ]' in yes and 'if [ "no" = "yes" ]' in no
    assert "sshpass -e ssh-copy-id" in yes and "sudo -S -p '' bash -s" in yes and REMOTE in yes


def test_selection_follows_the_step_and_the_measured_guest():
    assert rt.select_template(ADD117, _truth("vm")) is rt.INSTALL_OS_CLOUDINIT
    assert rt.select_template(ADD82, _truth("vm")) is rt.REACH_VM_SSH_AND_RUN
    assert rt.select_template(ADD82, _truth("vm", agent=True)) is rt.RUN_IN_VM_VIA_AGENT, "§17.1303 — with the agent up, the agent template owns the shape"
    assert rt.select_template(ADD84, _truth("vm", agent=True)) is rt.RUN_IN_VM_VIA_AGENT
    assert rt.select_template(ADD84, _truth("vm")) is rt.REACH_VM_SSH_AND_RUN, "no agent: ssh"
    assert rt.select_template(ADD94, _truth("vm", agent=True)) is None, "host-side work stays host-side even with an agent"
    assert rt.select_template(ADD100, _truth("ct")) is rt.RUN_IN_CONTAINER
    assert rt.select_template(ADD94, _truth("vm")) is None, "host-side work on a guest is not a guest template"
    assert rt.select_template({"node_key": "X", "title": "Install the NVIDIA driver on the Proxmox host", "description": ""}, None) is None
    assert rt.select_template(ADD82, None) is None, "an unmeasured guest selects nothing"


def test_the_model_fills_one_fence_and_nothing_else():
    assert rt.model_fence("Here:\n```bash\n$ apt-get update\n# comment\napt-get install -y x\n```\nthanks") == "apt-get update\napt-get install -y x"
    assert rt.model_fence("apt-get update") == "apt-get update"


def test_the_drafter_renders_a_template_first_and_the_pause_measures_before_drafting():
    src = pathlib.Path(sr.__file__).read_text(encoding="utf-8")
    i = src.index("async def draft_runbook(")
    assert "truth=None" in src[i:i + 400] and "rt.select_template(node, truth)" in src[i:] and "rt.render(tpl" in src[i:]
    assert src.index("rt.select_template(node, truth)", i) < src.index("prompt = build_base_prompt(node, b, environment)", i)
    from app.modules import execution_agent as ea
    esrc = pathlib.Path(ea.__file__).read_text(encoding="utf-8")
    j = esrc.index("async def _pause_for_decision(")
    ebody = esrc[j:esrc.index("\nasync def ", j + 10)]
    assert ebody.index("machine_truth.read_guest_truth(") < ebody.index("spec=spec, environment=_env, truth=_truth)")


# ───── §17.1290b — the first live pass: the import, and the OS-install step is not blocked by the empty disk

import pytest


@pytest.mark.asyncio
async def test_a_template_without_a_free_parameter_asks_the_model_nothing():
    assert await rt.fill_free_params(rt.INSTALL_OS_CLOUDINIT, ADD117, "brief") == {}


def test_the_free_parameter_draw_imports_the_router_from_where_it_lives():
    src = pathlib.Path(rt.__file__).read_text(encoding="utf-8")
    assert "from app import model_router" in src and "from app.modules import model_router" not in src
    i = src.index("async def fill_free_params(")
    body = src[i:]
    assert body.index("return {}") < body.index("from app import model_router"), "no import before the early return"


@pytest.mark.asyncio
async def test_the_install_template_is_not_refused_for_the_disk_it_exists_to_fill():
    """§17.1288p blocks a step that reaches into a VM with a never-written disk;
    the OS-install step is the one exception, and its script sshes in AFTER."""
    from unittest.mock import MagicMock, patch
    from app.modules import runbook_preconditions as pc
    rb = rt.render(rt.INSTALL_OS_CLOUDINIT, rt.values_for(rt.INSTALL_OS_CLOUDINIT, ADD117, _truth("vm"), ENV))
    cmds, files = sr.runbook_commands(rb), sr.file_writes(rb)
    lvs = (pathlib.Path(__file__).parent / "fixtures" / "pve_lvs_2026_10_02.txt").read_text(encoding="utf-8")
    qm = "      VMID NAME                 STATUS     MEM(MB)    BOOTDISK(GB) PID\n       106 palworld-server      running    8192               0.00 1\n"

    async def fake(spec, tool, args):
        r = MagicMock(); r.structured = None; r.is_error = False
        c = args["command"]; r.text = {"qm list": qm, "pct list": ""}.get(c, "")
        if c.startswith("lvs"): r.text = lvs
        return r
    spec = MagicMock(); spec.name = "pve-runner"
    plan = [{"node_key": "ADD5", "title": "Install Ubuntu Server 22.04 on VM 106", "status": "done"}]
    with patch("app.modules.mcp_client.call_tool", new=fake):
        out = await pc.unmet(cmds, spec, plan=plan, files=files, node=ADD117)
    assert all("has never been written" not in o["why"] for o in out), [o["why"][:100] for o in out]
    assert out == [], [o["why"][:100] for o in out]


# ───── §17.1290d — a rerun never stops a guest whose swap is done; a capture is a read

def test_the_swap_phase_checks_before_it_stops():
    body = rt.render(rt.INSTALL_OS_CLOUDINIT, rt.values_for(rt.INSTALL_OS_CLOUDINIT, ADD117, _truth("vm"), ENV))
    script = sr.file_writes(body)[0]["content"]
    i = script.index("phase_swap() {")
    swap = script[i:script.index("phase_boot() {")]
    assert swap.index("cloud-init drive already present") < swap.index('qm stop "$GID"'), \
        "live: the rerun stopped VM 106 mid-first-boot because the stop came before the check"


def test_a_capture_is_a_read_but_a_capture_file_is_not():
    from app.modules.assist_state_check import read_only_command
    assert read_only_command("timeout 12 tcpdump -nn -i tap106i0 -c 12 port 67 or port 68")
    assert read_only_command("tcpdump -nn -i tap106i0 -c 5")
    assert not read_only_command("tcpdump -nn -i tap106i0 -w /tmp/cap.pcap")
    assert not read_only_command("tcpdump -i tap106i0 -C 10 -w x")


def test_the_waits_fit_the_commands_budget_and_a_pinned_address_becomes_static():
    body = sr.file_writes(rt.render(rt.INSTALL_OS_CLOUDINIT, rt.values_for(rt.INSTALL_OS_CLOUDINIT, ADD117, _truth("vm"), ENV)))[0]["content"]
    assert 'deadline=$(( SECONDS + ${2:-130} ))' in body and 'while [ "$SECONDS" -lt "$deadline" ]' in body, \
        "live: 12 sweeps + 12 × 10 s overran the 180 s budget and the boot phase timed out"
    assert 'wait_for_address "$MAC" 40' in body and 'while [ "$SECONDS" -lt 150 ]' in body
    assert '--ipconfig0 "ip=dhcp"' in body
    pinned = {**ENV, "substitutions": {"PALWORLD_IP": "192.168.1.106"}}
    body2 = sr.file_writes(rt.render(rt.INSTALL_OS_CLOUDINIT, rt.values_for(rt.INSTALL_OS_CLOUDINIT, ADD117, _truth("vm"), pinned)))[0]["content"]
    assert '--ipconfig0 "ip=192.168.1.106/24,gw=192.168.1.1"' in body2 and "ipconfig0: ip=192.168.1.106/24,gw=192.168.1.1" in body2
    frame = _frame(rt.render(rt.INSTALL_OS_CLOUDINIT, rt.values_for(rt.INSTALL_OS_CLOUDINIT, ADD117, _truth("vm"), pinned)), ADD117)
    assert frame["refused"] == [], [r["why"][:100] for r in frame["refused"]]


# ───── §17.1291 — reading a guest's console is a template of its own, not an ssh

ADD118 = {"node_key": "ADD118", "title": "Read VM 106's serial console: why it has no network address",
          "description": "The guest's console is the only place that says why. Read it from the host: the VM has serial0: socket."}


def test_a_console_step_selects_the_console_template_and_passes_every_gate():
    assert rt.select_template(ADD118, _truth("vm")) is rt.READ_GUEST_CONSOLE, "live: the reach template was chosen and would have sshed into a VM with no address"
    assert rt.select_template(ADD82, _truth("vm")) is rt.REACH_VM_SSH_AND_RUN
    rb = rt.render(rt.READ_GUEST_CONSOLE, rt.values_for(rt.READ_GUEST_CONSOLE, ADD118, _truth("vm"), ENV))
    frame = _frame(rb, ADD118)
    assert frame["refused"] == [], [r["why"][:120] for r in frame["refused"]]
    assert frame["commands"] == ["printf '\\n' | timeout 8 socat -t 6 - UNIX-CONNECT:/var/run/qemu-server/106.serial0"]
    assert frame["inputs"] == [] and frame["files"] == [] and "run" in {o["id"] for o in frame["options"]}
    assert frame["verify"] == ["qm config 106 | grep -E '^serial0:'"]


# ───── §17.1292 — the boot, as the console shows it

ADD119 = {"node_key": "ADD119", "title": "Reset VM 106 and capture its serial console during boot (why it has no address)",
          "description": "A newline on the console produced nothing; the boot log says why the guest has no address."}


def test_a_boot_watch_step_selects_its_template_and_passes_every_gate():
    assert rt.select_template(ADD119, _truth("vm")) is rt.WATCH_GUEST_BOOT
    assert rt.select_template(ADD118, _truth("vm")) is rt.READ_GUEST_CONSOLE, "a plain console read is still the peek"
    rb = rt.render(rt.WATCH_GUEST_BOOT, rt.values_for(rt.WATCH_GUEST_BOOT, ADD119, _truth("vm"), ENV))
    frame = _frame(rb, ADD119)
    assert frame["refused"] == [], [r["why"][:120] for r in frame["refused"]]
    body = frame["files"][0]["content"]
    assert "timeout 140 socat -u UNIX-CONNECT:/var/run/qemu-server/$GID.serial0 STDOUT | tee" in body
    assert 'qm reset "$GID"' in body and frame["commands"] == ["bash /tmp/watch_boot_106.sh"] and frame["inputs"] == []


# ───── §17.1293 — a step the engine owns a template for is hands-on, fence or no fence

def test_a_template_shaped_step_is_hands_on_without_a_fenced_command():
    from app.modules.step_classify import step_is_hands_on
    add119 = {"node_key": "ADD119", "title": "Reset VM 106 and capture its serial console during boot (why it has no address)",
              "description": "From the host: capture the serial console for about 140 seconds while the VM is hard-reset, then print the tail.", "tool": "LLM"}
    assert step_is_hands_on(add119, shell_backend=False, mcp_enabled=False) == (True, "template:watch_guest_boot")
    # a step with a command in its text keeps the command's reason (the template intent is the fallback)
    assert step_is_hands_on(ADD82, shell_backend=False, mcp_enabled=False)[0] is True
    assert rt.intent_of(ADD117) == "install_os_cloudinit" and rt.intent_of(ADD118) == "read_guest_console"
    assert rt.intent_of(ADD82) == "guest_work" and rt.intent_of(ADD100) == "guest_work"
    assert rt.intent_of(ADD94) is None, "host-side work on a guest keeps today's classification"
    assert rt.intent_of({"node_key": "X", "title": "Install the NVIDIA driver on the Proxmox host", "description": "On the host."}) is None
    host = step_is_hands_on({"node_key": "X", "title": "Write the project README", "description": "Prose only.", "tool": "LLM"}, shell_backend=False, mcp_enabled=False)
    assert host == (False, "")


def test_an_empty_boot_capture_fails_the_step():
    """§17.1294 — the model's redraft was marked done with an empty log."""
    body = sr.file_writes(rt.render(rt.WATCH_GUEST_BOOT, rt.values_for(rt.WATCH_GUEST_BOOT, ADD119, _truth("vm"), ENV)))[0]["content"]
    assert 'if [ "${BYTES:-0}" -lt 20 ]' in body and "FAILED: nothing arrived on VM $GID's serial console" in body
    frame = _frame(rt.render(rt.WATCH_GUEST_BOOT, rt.values_for(rt.WATCH_GUEST_BOOT, ADD119, _truth("vm"), ENV)), ADD119)
    assert frame["refused"] == [], [r["why"][:100] for r in frame["refused"]]


def test_the_wait_parses_nmaps_report_the_way_the_live_host_prints_it():
    """§17.1295 — the awk in wait_for_address, run on nmap's real output for VM 106."""
    import subprocess
    report = ("Starting Nmap 7.95 ( https://nmap.org ) at 2026-10-02 18:55 HDT\n"
              "Nmap scan report for pve.lan (192.168.1.156)\nHost is up.\n"
              "Nmap scan report for 192.168.1.106\nHost is up (0.00025s latency).\nMAC Address: BC:24:11:E8:9F:7A (Proxmox Server Solutions GmbH)\n"
              "Nmap scan report for AdamsTV.lan (192.168.1.211)\nHost is up (0.12s latency).\nMAC Address: 0C:62:A6:77:7E:11 (Hui Zhou Gaoshengda Technology)\n"
              "Nmap done: 256 IP addresses (21 hosts up) scanned in 3.44 seconds\n")
    awk = r"""/^Nmap scan report for/ { ip=$NF; gsub(/[()]/, "", ip) }
                tolower($0) ~ "mac address: " m { print ip; exit }"""
    out = subprocess.run(["awk", "-v", "m=bc:24:11:e8:9f:7a", awk], input=report, capture_output=True, text=True).stdout.strip()
    assert out == "192.168.1.106", out
    out2 = subprocess.run(["awk", "-v", "m=0c:62:a6:77:7e:11", awk], input=report, capture_output=True, text=True).stdout.strip()
    assert out2 == "192.168.1.211", "a named host's ip is the parenthesised one"


# ───── §17.1297 — the install intent is the OS, not the word

def test_a_step_that_mentions_installing_the_agent_on_ubuntu_is_not_an_os_install():
    add65 = {"node_key": "ADD65", "title": "Verify QEMU Guest Agent responds on VM 106",
             "description": "ADD82 installs qemu-guest-agent inside the Ubuntu guest (VM 106); confirm `qm agent 106 ping` answers from the host."}
    assert rt.intent_of(add65) != "install_os_cloudinit"
    assert rt.select_template(add65, _truth("vm")) is not rt.INSTALL_OS_CLOUDINIT
    assert rt.intent_of(ADD117) == "install_os_cloudinit"
    assert rt.intent_of({"node_key": "X", "title": "Install Ubuntu Server 22.04 on VM 106", "description": ""}) == "install_os_cloudinit"
    assert rt.intent_of({"node_key": "X", "title": "Reinstall the operating system on VM 106", "description": ""}) == "install_os_cloudinit"
    assert rt.intent_of(ADD82) == "guest_work"


# ───── §17.1298 — the console intent is the task, not the word

def test_a_step_whose_history_mentions_the_console_is_not_a_console_read():
    desc = pathlib.Path(__file__).parent.joinpath("fixtures", "add82_description.txt").read_text(encoding="utf-8")
    add82_live = {**ADD82, "description": desc + "\n\nENGINE CORRECTION (2026-10-02): the lines above that say \"done at the console\" are SUPERSEDED … Nothing here is done at a console by hand."}
    assert rt.intent_of(add82_live) == "guest_work", "live: ADD82 was templated as read_guest_console"
    assert rt.select_template(add82_live, _truth("vm")) is rt.REACH_VM_SSH_AND_RUN
    assert rt.intent_of(ADD118) == "read_guest_console" and rt.select_template(ADD118, _truth("vm")) is rt.READ_GUEST_CONSOLE
    assert rt.intent_of(ADD119) == "watch_guest_boot"


# ───── §17.1299 — probe key auth before copying a key; an unattended install counts as a key step

def test_the_reach_template_probes_key_auth_before_any_copy():
    body = sr.file_writes(rt.render(rt.REACH_VM_SSH_AND_RUN, rt.values_for(rt.REACH_VM_SSH_AND_RUN, ADD82, _truth("vm"), ENV, {"REMOTE_COMMANDS": REMOTE})))[0]["content"]
    assert body.index('"$USER_NAME@$IP" true >/dev/null') < body.index("sshpass -e ssh-copy-id"), "the probe comes first"
    assert 'elif [ "yes" = "yes" ]' in body
    frame = _frame(rt.render(rt.REACH_VM_SSH_AND_RUN, rt.values_for(rt.REACH_VM_SSH_AND_RUN, ADD82, _truth("vm"), ENV, {"REMOTE_COMMANDS": REMOTE})), ADD82)
    assert frame["refused"] == [], [r["why"][:100] for r in frame["refused"]]


def test_an_unattended_install_step_counts_as_the_key_step():
    from app.modules.runbook_preconditions import key_known_for
    plan = [{"node_key": "ADD117", "title": "Install Ubuntu 22.04 on VM 106 unattended (cloud image + cloud-init)", "status": "done"}]
    assert key_known_for("106", "palworld-server", plan) and key_known_for("106", "palworld-server", plan).startswith("ADD117")


# ───── §17.1303 — a VM with a working agent is reached through the agent

def test_the_agent_template_reaches_the_guest_without_ssh_account_key_or_address():
    rb = rt.render(rt.RUN_IN_VM_VIA_AGENT, rt.values_for(rt.RUN_IN_VM_VIA_AGENT, ADD84, _truth("vm", agent=True), ENV,
                                                          {"REMOTE_COMMANDS": "growpart /dev/sda 1\nresize2fs /dev/sda1", "VERIFY_INSIDE": "df -h /"}))
    body = next(f["content"] for f in sr.file_writes(rb))
    code = "\n".join(ln for ln in body.split("\n") if not ln.lstrip().startswith("#"))
    assert "ssh" not in code and "@" not in code and "MASS_PASSWORD" not in rb, "no ssh, no account, no key, no password"
    assert "<" not in code.replace("<<'REMOTE'", "").replace("<<'PYJ'", "").replace(" < /tmp/", " "), "no operator placeholder"
    assert 'qm guest exec "$GID" --timeout 60 --pass-stdin 1 -- bash -c "cat > /root/.scaffold_step.sh"' in body, "the script travels on stdin; no quoting of the model's lines (§17.1317)"
    assert "seq 1 12" in body and "sleep 5" in body, "60 s agent wait stays inside the runner's 180 s"
    assert 'systemd-run --unit $UNIT --collect' in body and 'UNIT="scaffold-ADD84"' in body, "§17.1317 — the work runs detached as a transient unit named after the step"
    assert "deadline=$(( SECONDS + 165 ))" in body and "STILL RUNNING" in body and 'exit "${code:-1}"' in body, "each wait fits the budget; the unit's exit code is the step's"
    frame = _frame(rb, ADD84)
    assert frame["commands"] == ["bash /tmp/in_vm_106_agent.sh start"] + ["bash /tmp/in_vm_106_agent.sh wait"] * 7 + ["bash /tmp/in_vm_106_agent.sh last"], frame["commands"]
    assert frame["verify"] == ['qm guest exec 106 -- bash -c "df -h /"'], "the verify runs INSIDE the guest, alone, so §17.1302 can read it (§17.1304)"
    assert sr.verify_commands(rb) == frame["verify"], "both checks are read-only through the wrapper"


@pytest.mark.asyncio
async def test_the_model_fills_each_free_parameter_once_and_the_check_is_one_line(monkeypatch):
    import app.utils.llm_retry as lr
    drawn = []

    async def fake(gen, prompt, params, *, system, **kw):
        drawn.append((system, prompt))
        if "VERIFY_INSIDE" in kw.get("label", ""):
            return type("R", (), {"text": "```bash\ndf -h /\nlsblk\n```"})()
        return type("R", (), {"text": "```bash\n$ growpart /dev/sda 1\nresize2fs /dev/sda1\n```"})()
    monkeypatch.setattr(lr, "generate_until_nonempty", fake)
    vals = await rt.fill_free_params(rt.RUN_IN_VM_VIA_AGENT, ADD84, "brief")
    assert vals == {"REMOTE_COMMANDS": "growpart /dev/sda 1\nresize2fs /dev/sda1", "VERIFY_INSIDE": "df -h /"}
    assert len(drawn) == 2 and drawn[1][0] is rt.FREE_PARAM_SYSTEM_VERIFY and "READ-ONLY" in drawn[1][0]


# ───── §17.1304 — resize is a word, not a task

def test_resize2fs_inside_the_guest_is_not_host_side_work():
    """Live, ADD84's text ("growpart + resize2fs … inside the guest") matched the
    bare `resize` and lost the agent template to the model path."""
    assert not rt._HOST_SIDE_RE.search(ADD84["title"] + " " + ADD84["description"])
    assert rt.select_template(ADD84, _truth("vm", agent=True)) is rt.RUN_IN_VM_VIA_AGENT
    assert rt.intent_of(ADD84) == "guest_work"
    for host_side in ("Resize VM 106's disk to 100G", "qm resize 106 scsi0 +50G on the host", "Grow the VM 106 disk",
                      "Re-attach VM 106's detached disk and grow it to 100G"):
        assert rt._HOST_SIDE_RE.search(host_side), host_side


# ───── §17.1305 — every template passes the MACHINE rules too, with its guest stopped

@pytest.mark.asyncio
async def test_every_template_passes_the_preconditions_with_its_guest_stopped():
    """`frame_run` alone never ran `unmet`; live, run_in_container was refused for
    the `pct push` its own script guards with a start. A template owns the start,
    so a stopped guest must refuse nothing."""
    from app.modules.runbook_preconditions import unmet
    inv = {"cts": {"111": "stopped", "120": "stopped"}, "vms": {"106": "stopped"}, "names": {"106": "palworld-server"},
           "disks": {}, "isos": ["ubuntu-22.04.3-live-server-amd64.iso"]}
    cases = [
        (rt.INSTALL_OS_CLOUDINIT, ADD117, mt.GuestTruth(gid="106", kind="vm", status="stopped"), {}),
        (rt.REACH_VM_SSH_AND_RUN, ADD82, mt.GuestTruth(gid="106", kind="vm", status="stopped", key_known_by="ADD117 · Install Ubuntu 22.04 on VM 106 unattended"), {"REMOTE_COMMANDS": REMOTE}),
        (rt.RUN_IN_VM_VIA_AGENT, ADD84, mt.GuestTruth(gid="106", kind="vm", status="stopped", agent=True), {"REMOTE_COMMANDS": "growpart /dev/sda 1", "VERIFY_INSIDE": "df -h /"}),
        (rt.RUN_IN_CONTAINER, ADD100, mt.GuestTruth(gid="111", kind="ct", status="stopped"), {"REMOTE_COMMANDS": "apt-get update", "VERIFY_INSIDE": "dpkg -l nodejs"}),
    ]
    for tpl, node, truth, model_vals in cases:
        rb = rt.render(tpl, rt.values_for(tpl, node, truth, ENV, model_vals))
        spec = type("S", (), {"name": "pve-runner", "headers": {}})()
        out = await unmet(sr.runbook_commands(rb), spec, plan=[], files=sr.file_writes(rb), node=node, inventory=inv, truth=truth)
        assert out == [], (tpl.name, [o["why"][:140] for o in out])


def test_the_container_template_verifies_inside_like_the_agent_template():
    """§17.1307 — `pct status 120` cannot say whether Caddy is installed; live the
    judge said so (`unknown`) and the step would have reinstalled over a validated
    Caddyfile. The check runs inside, alone -- the shape run_in_vm_via_agent has."""
    rb = rt.render(rt.RUN_IN_CONTAINER, rt.values_for(rt.RUN_IN_CONTAINER, ADD100, _truth("ct"), ENV,
                                                      {"REMOTE_COMMANDS": "apt-get install -y caddy", "VERIFY_INSIDE": "caddy validate --config /etc/caddy/Caddyfile"}))
    frame = _frame(rb, ADD100)
    assert frame["verify"] == ['pct exec 111 -- bash -c "caddy validate --config /etc/caddy/Caddyfile"'], frame["verify"]
    assert sr.verify_commands(rb) == frame["verify"], "read-only through the wrapper"
    assert frame["refused"] == [] and frame["inputs"] == []
    assert [p.name for p in rt.RUN_IN_CONTAINER.params if p.source == "model"] == ["REMOTE_COMMANDS", "VERIFY_INSIDE"]


# ───── §17.1308 — a refused template re-renders with the refusal in the draw

@pytest.mark.asyncio
async def test_a_redraft_keeps_the_template_and_hands_the_refusal_to_the_draw(monkeypatch):
    import app.utils.llm_retry as lr
    seen = []

    async def fake(gen, prompt, params, *, system, **kw):
        seen.append(prompt)
        return type("R", (), {"text": "```bash\napt-get install -y caddy\n```"})()
    monkeypatch.setattr(lr, "generate_until_nonempty", fake)
    note = "- `/tmp/in_ct_120.sh: email aedefruscio@…` — appears nowhere the engine holds … Write `<ACME_EMAIL>`"
    rb = await sr.draft_runbook(ADD100, {"description": "brief"}, "", retry_note=note, spec=None, environment=ENV, truth=_truth("ct"))
    assert rt.template_of(rb) == rt.RUN_IN_CONTAINER.name, "the shape stays the engine's on a redraft"
    assert seen and all("THE PREVIOUS ATTEMPT WAS REFUSED" in s and "<ACME_EMAIL>" in s for s in seen)
    src = pathlib.Path(sr.__file__).read_text(encoding="utf-8")
    i = src.index("async def draft_runbook(")
    assert "if truth is not None and for_channel:" in src[i:i + 3000] and "not retry_note and for_channel" not in src[i:i + 3000]


# ───── §17.1311 — the model wraps what the template already wraps

def test_guest_wrappers_the_template_supplies_are_stripped_from_the_models_lines():
    import json as _json
    fr = _json.loads((pathlib.Path(__file__).parent / "fixtures" / "add88_frame_template3_2026_10_03.json").read_text(encoding="utf-8"))
    assert 'bash -c "pct exec 120 -- caddy validate' in fr["verify"][0], "the live double wrap: inside the container, `pct` does not exist"
    assert rt.strip_guest_wrappers("pct exec 120 -- caddy validate --config /etc/caddy/Caddyfile") == "caddy validate --config /etc/caddy/Caddyfile"
    assert rt.strip_guest_wrappers("qm guest exec 106 --timeout 110 -- bash -c 'df -h /'") == "df -h /"
    assert rt.strip_guest_wrappers("sudo pct exec 120 -- bash -c \"systemctl is-active caddy\"") == "systemctl is-active caddy"
    assert rt.strip_guest_wrappers("pct exec 120 -- apt-get update\napt-get install -y caddy\npct exec 120 -- systemctl enable --now caddy") == "apt-get update\napt-get install -y caddy\nsystemctl enable --now caddy"
    assert rt.strip_guest_wrappers("df -h /") == "df -h /" and rt.strip_guest_wrappers("echo 'pct exec 120 -- x'") == "echo 'pct exec 120 -- x'"
    assert rt.strip_guest_wrappers("lxc-attach -n 120 -- caddy version") == "caddy version"


@pytest.mark.asyncio
async def test_the_draw_strips_the_wrapper_before_the_template_wraps_it(monkeypatch):
    import app.utils.llm_retry as lr

    async def fake(gen, prompt, params, *, system, **kw):
        if "VERIFY_INSIDE" in kw.get("label", ""):
            return type("R", (), {"text": "```bash\npct exec 120 -- caddy validate --config /etc/caddy/Caddyfile\n```"})()
        return type("R", (), {"text": "```bash\npct exec 120 -- apt-get update\npct exec 120 -- apt-get install -y caddy\n```"})()
    monkeypatch.setattr(lr, "generate_until_nonempty", fake)
    node = {"node_key": "ADD88", "title": "Install Caddy and write the Caddyfile inside LXC 120", "description": ""}
    vals = await rt.fill_free_params(rt.RUN_IN_CONTAINER, node, "brief")
    assert vals["REMOTE_COMMANDS"].endswith("apt-get update\napt-get install -y caddy") and vals["VERIFY_INSIDE"] == "caddy validate --config /etc/caddy/Caddyfile"   # §17.1320 prepends the apt repair
    rb = rt.render(rt.RUN_IN_CONTAINER, rt.values_for(rt.RUN_IN_CONTAINER, node, mt.GuestTruth(gid="120", kind="ct", status="running"), ENV, vals))
    frame = _frame(rb, node)
    assert frame["verify"] == ['pct exec 120 -- bash -c "caddy validate --config /etc/caddy/Caddyfile"'], frame["verify"]
    assert frame["refused"] == []


# ───── §17.1314 — a write over an existing file keeps a copy

def test_every_overwrite_in_the_models_commands_is_preceded_by_a_backup():
    remote = "apt-get install -y nodejs\ntee /opt/cp/package.json <<'EOF'\n{ \"a\": 1 }\nEOF\ntee /opt/cp/server.js <<'EOF'\nconst x = 1;\nEOF\ntee -a /etc/caddy/Caddyfile <<'EOF'\nx\nEOF\ncat > /etc/x.conf <<'EOF'\ny\nEOF\ncp /tmp/a /etc/b\nsystemctl restart cp"
    out = rt.keep_a_copy_before_overwrites(remote).split("\n")
    backups = [l for l in out if '.bak.$(date' in l]
    assert backups == ['[ -e "/opt/cp/package.json" ] && cp -a "/opt/cp/package.json" "/opt/cp/package.json.bak.$(date +%Y%m%d%H%M%S)"',
                       '[ -e "/opt/cp/server.js" ] && cp -a "/opt/cp/server.js" "/opt/cp/server.js.bak.$(date +%Y%m%d%H%M%S)"',
                       '[ -e "/etc/x.conf" ] && cp -a "/etc/x.conf" "/etc/x.conf.bak.$(date +%Y%m%d%H%M%S)"',
                       '[ -e "/etc/b" ] && cp -a "/etc/b" "/etc/b.bak.$(date +%Y%m%d%H%M%S)"'], backups
    assert out.index(backups[1]) + 1 == out.index("tee /opt/cp/server.js <<'EOF'"), "the copy is taken immediately before the write"
    assert "const x = 1;" in out and not any(".bak." in l for l in out if l.startswith("const")), "heredoc bodies are never touched"
    assert rt.keep_a_copy_before_overwrites("apt-get update\nsystemctl restart x") == "apt-get update\nsystemctl restart x"


@pytest.mark.asyncio
async def test_the_draw_keeps_copies_and_a_cut_heredoc_counts_as_cut(monkeypatch):
    import app.utils.llm_retry as lr
    node = {"node_key": "ADD100", "title": "Rebuild the control panel in LXC 111", "description": ""}

    async def fake(gen, prompt, params, *, system, **kw):
        if "VERIFY_INSIDE" in kw.get("label", ""):
            return type("R", (), {"text": "```bash\ncurl -s http://localhost:3001/api/capabilities\n```"})()
        return type("R", (), {"text": "```bash\ntee /opt/control-panel-backend/server.js <<'EOF'\nconst a = 1;\nEOF\nsystemctl restart control-panel\n```"})()
    monkeypatch.setattr(lr, "generate_until_nonempty", fake)
    vals = await rt.fill_free_params(rt.RUN_IN_CONTAINER, node, "brief")
    assert vals["REMOTE_COMMANDS"].split("\n")[0] == '[ -e "/opt/control-panel-backend/server.js" ] && cp -a "/opt/control-panel-backend/server.js" "/opt/control-panel-backend/server.js.bak.$(date +%Y%m%d%H%M%S)"'
    assert vals["VERIFY_INSIDE"] == "curl -s http://localhost:3001/api/capabilities", "the one-line check is never prefixed"
    rb = rt.render(rt.RUN_IN_CONTAINER, rt.values_for(rt.RUN_IN_CONTAINER, node, mt.GuestTruth(gid="111", kind="ct", status="running"), ENV, vals))
    frame = _frame(rb, node)
    # §17.1327 — `systemctl restart control-panel` names a unit this block does not write; with the
    # guest's units measured (CT 111 really has it) the frame is clean, without them it is asked about.
    assert [r["why"][:46] for r in frame["refused"]] == ["`control-panel` is not a unit anything the engi"[:46]], [r["why"][:90] for r in frame["refused"]]
    spec = type("S", (), {"name": "pve-runner", "headers": {}})()
    assert sr.frame_run(node, rb, spec, POLICY, env=ENV, units=["control-panel", "cron"])["refused"] == []
    # the ran-live record: the fence closed but the inner heredoc never did
    assert rt.content_is_cut("```bash\ntee /opt/x/server.js <<'EOF'\nconst runRes = await axios.post(URL\n```") is True
    assert rt.content_is_cut("```bash\ntee /opt/x/server.js <<'EOF'\nconst a = 1;\nEOF\n```") is False


# ───── §17.1317 — long in-guest work runs detached; SteamCMD bootstraps first

def test_both_guest_templates_run_detached_and_wait_in_phases():
    for tpl, node, truth, gid in ((rt.RUN_IN_VM_VIA_AGENT, ADD84, _truth("vm", agent=True), "106"), (rt.RUN_IN_CONTAINER, ADD100, _truth("ct"), "111")):
        vals = rt.values_for(tpl, node, truth, ENV, {"REMOTE_COMMANDS": "apt-get install -y curl", "VERIFY_INSIDE": "which curl"})
        assert vals["STEP"] == node["node_key"]
        rb = rt.render(tpl, vals)
        body = next(f["content"] for f in sr.file_writes(rb))
        assert f'UNIT="scaffold-{node["node_key"]}"' in body and "systemd-run --unit $UNIT --collect" in body
        assert "systemctl reset-failed $UNIT" in body, "a rerun does not trip over the last run's failed unit"
        assert body.rstrip().endswith("esac"), "the whole script survives the parser"
        cmds = sr.runbook_commands(rb)
        assert len(cmds) == 9 and cmds[0].endswith(" start") and cmds[-1].endswith(" last") and all(c.endswith(" wait") for c in cmds[1:-1]), cmds
        frame = _frame(rb, node)
        assert frame["refused"] == [], (tpl.name, [r["why"][:120] for r in frame["refused"]])
    ct_body = next(f["content"] for f in sr.file_writes(rt.render(rt.RUN_IN_CONTAINER, rt.values_for(rt.RUN_IN_CONTAINER, ADD100, _truth("ct"), ENV, {"REMOTE_COMMANDS": "true", "VERIFY_INSIDE": "true"}))))
    assert 'pct push "$GID" /tmp/in_ct_111_remote.sh /root/.scaffold_step.sh' in ct_body


def test_steamcmd_bootstraps_once_before_the_first_install():
    remote = "mkdir -p /opt/steamcmd && cd /opt/steamcmd && curl -sqL URL | tar zxvf -\n/opt/steamcmd/steamcmd.sh +force_install_dir /opt/palworld +login anonymous +app_update 2394010 validate +quit\n/opt/steamcmd/steamcmd.sh +force_install_dir /opt/palworld +login anonymous +app_update 2394010 validate +quit\nchown -R aedefruscio:aedefruscio /opt/palworld"
    out = rt.bootstrap_steamcmd_first(remote).split("\n")
    assert out[1].startswith("/opt/steamcmd/steamcmd.sh +quit >/dev/null 2>&1 || true"), out
    assert sum(1 for l in out if "+quit >/dev/null" in l) == 1, "once"
    assert out[2].startswith("/opt/steamcmd/steamcmd.sh +force_install_dir")
    assert rt.bootstrap_steamcmd_first("apt-get update") == "apt-get update"


# ───── §17.1319 — the agent phase script reads the text inside the agent's JSON

def test_the_agent_scripts_reads_unwrap_the_agents_json_in_bash(tmp_path):
    """The first live detached run (T23, 16:20 UTC) compared `systemctl is-active`
    against `{"exitcode":0,"out-data":"active\\n"}` and `exit`ed on a JSON blob.
    Run the script's own `ginfo` under a stubbed `qm` and read what it prints."""
    import subprocess
    rb = rt.render(rt.RUN_IN_VM_VIA_AGENT, rt.values_for(rt.RUN_IN_VM_VIA_AGENT, ADD84, _truth("vm", agent=True), ENV,
                                                         {"REMOTE_COMMANDS": "true", "VERIFY_INSIDE": "true"}))
    body = next(f["content"] for f in sr.file_writes(rb))
    i = body.index("ginfo() {"); j = body.index("\n", body.index("sys.stdout.write", i)) + 1
    ginfo = body[i:j]
    stub = tmp_path / "qm"; stub.write_text("#!/usr/bin/env bash\nprintf \'{\\n \"exitcode\" : 0,\\n \"exited\" : 1,\\n \"out-data\" : \"inactive\\\\n\"\\n}\\n\'\n"); stub.chmod(0o755)
    out = subprocess.run(["bash", "-c", f'GID=106\n{ginfo}\nginfo "systemctl is-active scaffold-ADD84"'],
                         capture_output=True, text=True, env={"PATH": f"{tmp_path}:{__import__('os').environ.get('PATH', '/usr/bin:/bin')}", "GID": "106"})
    assert out.returncode == 0 and out.stdout == "inactive\n", (out.stdout, out.stderr)
    stub.write_text("#!/usr/bin/env bash\necho not-json\n"); stub.chmod(0o755)
    out2 = subprocess.run(["bash", "-c", f'GID=106\n{ginfo}\nginfo "x"'], capture_output=True, text=True, env={"PATH": f"{tmp_path}:{__import__('os').environ.get('PATH', '/usr/bin:/bin')}"})
    assert out2.stdout == "", "a non-JSON answer reads as nothing, not as a blob"
    # the container variant reads plain text and needs no unwrapping
    ct = next(f["content"] for f in sr.file_writes(rt.render(rt.RUN_IN_CONTAINER, rt.values_for(rt.RUN_IN_CONTAINER, ADD100, _truth("ct"), ENV, {"REMOTE_COMMANDS": "true", "VERIFY_INSIDE": "true"}))))
    assert 'ginfo() { pct exec "$GID" -- bash -c "$1" 2>/dev/null; }' in ct


def test_identical_refusals_across_the_phases_are_said_once():
    """§17.1319 — ADD100's nine phase commands each earned the same 'reads secrets from
    its environment' refusal: eleven lines on the frame for two findings."""
    node = {"node_key": "ADD100", "title": "Rebuild the control panel in LXC 111", "description": ""}
    vals = rt.values_for(rt.RUN_IN_CONTAINER, node, _truth("ct"), ENV, {"REMOTE_COMMANDS": "export KEY=$RADARR_API_KEY\ncurl -H \"X-Api-Key: $RADARR_API_KEY\" http://192.168.1.22:7878/api/v3/system/status", "VERIFY_INSIDE": "true"})
    frame = _frame(rt.render(rt.RUN_IN_CONTAINER, vals), node)
    whys = [r["why"] for r in frame["refused"]]
    assert whys, "a script that reads a stored value the command never mentions is refused"
    assert len(whys) == len(set(whys)), f"{len(whys)} refusals, {len(set(whys))} distinct: say each once"



# ───── §17.1318 — the draw reads the last attempt and the research

@pytest.mark.asyncio
async def test_the_draw_carries_the_last_attempt_and_the_research(monkeypatch):
    import app.utils.llm_retry as lr
    seen = []

    async def fake(gen, prompt, params, *, system, **kw):
        seen.append((system, prompt))
        return type("R", (), {"text": "```bash\n/opt/steamcmd/steamcmd.sh +force_install_dir /opt/palworld +login anonymous +app_update 2394010 validate +quit\n```"})()
    monkeypatch.setattr(lr, "generate_until_nonempty", fake)

    async def fake_research(node, environment=None):
        return "RESEARCH (current): Palworld dedicated server = SteamCMD app 2394010; needs lib32gcc-s1."
    monkeypatch.setattr(sr, "research_for_step", fake_research)
    node = {"node_key": "T23", "title": "Install PalWorld server", "description": "",
            "last_verification_reason": "supervised run stopped — `ssh … steamcmd.sh … +app_update 2394010` exited 8",
            "output_text": "## Executed on pve-runner\n$ ssh -o BatchMode=yes aedefruscio@192.168.1.106 \"sudo -S -p '' bash -c '/opt/steamcmd/steamcmd.sh +force_install_dir /opt/palworld +login anonymous +app_update 2394010 validate +quit'\"\nFailed installing AppID 2394010 (Missing configuration)\n"}
    vals = await rt.fill_free_params(rt.RUN_IN_VM_VIA_AGENT, node, "brief", upstream="", environment={"profile": "root@pve", "facts": []})
    assert vals["REMOTE_COMMANDS"].splitlines()[0].startswith("/opt/steamcmd/steamcmd.sh +quit"), "§17.1317 bootstrap still first"
    prompt = seen[0][1]
    assert "exited 8" in prompt and "app_update 2394010" in prompt, "the last attempt, what it ran and why it stopped"
    assert "RESEARCH (current)" in prompt, "the step's research"
    assert "not a service a later step creates" in rt.FREE_PARAM_SYSTEM_VERIFY


@pytest.mark.asyncio
async def test_a_failed_research_or_feedback_leaves_the_draw_as_it_was(monkeypatch):
    import app.utils.llm_retry as lr

    async def fake(gen, prompt, params, *, system, **kw):
        return type("R", (), {"text": "```bash\napt-get install -y curl\n```"})()
    monkeypatch.setattr(lr, "generate_until_nonempty", fake)

    async def boom(node, environment=None):
        raise RuntimeError("search down")
    monkeypatch.setattr(sr, "research_for_step", boom)
    vals = await rt.fill_free_params(rt.RUN_IN_CONTAINER, ADD100, "brief", environment={"profile": "x"})
    assert vals["REMOTE_COMMANDS"].endswith("apt-get install -y curl")   # §17.1320 prepends the apt repair


# ───── §17.1320 — the engine repairs the apt state it left; the bootstrap runs as the install's user

def test_the_bootstrap_runs_as_the_same_user_as_the_install():
    remote = "mkdir -p /opt/steamcmd\nsudo -u palworld /opt/steamcmd/steamcmd.sh +force_install_dir /opt/palworld +login anonymous +app_update 2394010 validate +quit"
    out = rt.bootstrap_steamcmd_first(remote).split("\n")
    assert out[1].startswith("sudo -u palworld /opt/steamcmd/steamcmd.sh +quit >/dev/null 2>&1 || true"), out[1]
    plain = rt.bootstrap_steamcmd_first("/opt/steamcmd/steamcmd.sh +login anonymous +app_update 2394010 +quit").split("\n")
    assert plain[0].startswith("/opt/steamcmd/steamcmd.sh +quit")


def test_apt_work_gets_the_repair_prelude_once_and_other_work_does_not():
    out = rt.repair_apt_state_first("apt-get update\napt-get install -y lib32gcc-s1")
    lines = out.split("\n")
    assert lines[1] == "dpkg --configure -a >/dev/null 2>&1 || true" and "dpkg --purge --force-remove-reinstreq" in lines[2] and lines[3] == "apt-get update"
    assert rt.repair_apt_state_first(out) == out, "once"
    assert rt.repair_apt_state_first("systemctl restart caddy") == "systemctl restart caddy"
    import json as _json
    fr = _json.loads((pathlib.Path(__file__).parent / "fixtures" / "t23_frame_agent_steamcmd_2026_10_03.json").read_text(encoding="utf-8"))
    assert "dpkg --configure -a" not in fr["files"][0]["content"], "the live frame (before this fix) had no repair and would have aborted on steamcmd:i386"


@pytest.mark.asyncio
async def test_the_draw_applies_backup_bootstrap_and_repair_in_that_order(monkeypatch):
    import app.utils.llm_retry as lr

    async def fake(gen, prompt, params, *, system, **kw):
        if "VERIFY_INSIDE" in kw.get("label", ""):
            return type("R", (), {"text": "```bash\nls -la /opt/palworld/PalServer.sh\n```"})()
        return type("R", (), {"text": "```bash\napt-get install -y lib32gcc-s1\nsudo -u palworld /opt/steamcmd/steamcmd.sh +force_install_dir /opt/palworld +login anonymous +app_update 2394010 validate +quit\ntee /etc/systemd/system/palworld.service <<'EOF'\n[Unit]\nEOF\n```"})()
    monkeypatch.setattr(lr, "generate_until_nonempty", fake)
    node = {"node_key": "T23", "title": "Install PalWorld server", "description": ""}
    vals = await rt.fill_free_params(rt.RUN_IN_VM_VIA_AGENT, node, "brief")
    lines = vals["REMOTE_COMMANDS"].split("\n")
    assert lines[1].startswith("dpkg --configure -a"), "repair first"
    assert any(l.startswith("sudo -u palworld /opt/steamcmd/steamcmd.sh +quit") for l in lines), "bootstrap as the install's user"
    assert any(l.startswith('[ -e "/etc/systemd/system/palworld.service" ] && cp -a') for l in lines), "backup before the overwrite"
    rb = rt.render(rt.RUN_IN_VM_VIA_AGENT, rt.values_for(rt.RUN_IN_VM_VIA_AGENT, node, _truth("vm", agent=True), ENV, vals))
    frame = _frame(rb, node)
    assert frame["refused"] == [], [r["why"][:100] for r in frame["refused"]]


# ───── §17.1321 — the bootstrap keeps its quotes; the check looks where the work went

def test_the_bootstrap_under_su_dash_c_is_one_quoted_argument():
    import json as _json, subprocess
    fr = _json.loads((pathlib.Path(__file__).parent / "fixtures" / "t23_frame_agent_su_2026_10_03.json").read_text(encoding="utf-8"))
    bad = next(l for l in fr["files"][0]["content"].split("\n") if "+quit" in l)
    assert bad.count("'") % 2 == 1, "the live line had an unterminated quote"
    remote = "su - palworld -c '/opt/palworld/steamcmd/steamcmd.sh +force_install_dir /opt/palworld/server +login anonymous +app_update 2394010 validate +quit'"
    out = rt.bootstrap_steamcmd_first(remote).split("\n")
    assert out[0] == "su - palworld -c '/opt/palworld/steamcmd/steamcmd.sh +quit >/dev/null 2>&1 || true'   # bootstrap: a fresh SteamCMD updates itself and exits", out[0]
    assert subprocess.run(["bash", "-n"], input="\n".join(out), capture_output=True, text=True).returncode == 0, "the script parses"
    assert rt.bootstrap_steamcmd_first('sudo -u palworld "/opt/steamcmd/steamcmd.sh" +login anonymous +app_update 2394010 +quit').split("\n")[0].startswith("sudo -u palworld /opt/steamcmd/steamcmd.sh +quit")


def test_a_check_that_looks_where_the_work_did_not_go_is_named():
    remote = "mkdir -p /opt/palworld/steamcmd\nsu - palworld -c '/opt/palworld/steamcmd/steamcmd.sh +force_install_dir /opt/palworld/server +app_update 2394010 +quit'\ntee /etc/systemd/system/palworld.service <<'EOF'\nx\nEOF"
    why = rt.check_looks_where_the_work_went(remote, "ls -la /opt/palworld/PalServer.sh")
    assert why and "/opt/palworld/PalServer.sh" in why and "/opt/palworld/server" in why
    assert rt.check_looks_where_the_work_went(remote, "ls -la /opt/palworld/server/PalServer.sh") == ""
    assert rt.check_looks_where_the_work_went(remote, "systemctl is-enabled palworld.service") == "", "no path named: nothing to compare"
    assert rt.check_looks_where_the_work_went(remote, "test -f /etc/systemd/system/palworld.service && echo ok") == ""
    assert rt.check_looks_where_the_work_went("apt-get install -y caddy", "caddy validate --config /etc/caddy/Caddyfile") , "a config the commands never touch"


@pytest.mark.asyncio
async def test_the_check_is_redrawn_once_when_it_looks_elsewhere(monkeypatch):
    import app.utils.llm_retry as lr
    draws = []

    async def fake(gen, prompt, params, *, system, **kw):
        draws.append(kw.get("label"))
        if "redraw" in kw.get("label", ""):
            return type("R", (), {"text": "```bash\nls -la /opt/palworld/server/PalServer.sh\n```"})()
        if "VERIFY_INSIDE" in kw.get("label", ""):
            return type("R", (), {"text": "```bash\nls -la /opt/palworld/PalServer.sh\n```"})()
        return type("R", (), {"text": "```bash\nsu - palworld -c '/opt/palworld/steamcmd/steamcmd.sh +force_install_dir /opt/palworld/server +login anonymous +app_update 2394010 validate +quit'\n```"})()
    monkeypatch.setattr(lr, "generate_until_nonempty", fake)
    node = {"node_key": "T23", "title": "Install PalWorld server", "description": ""}
    vals = await rt.fill_free_params(rt.RUN_IN_VM_VIA_AGENT, node, "brief")
    assert vals["VERIFY_INSIDE"] == "ls -la /opt/palworld/server/PalServer.sh"
    assert sum(1 for d in draws if d and d.endswith("redraw")) == 1
    rb = rt.render(rt.RUN_IN_VM_VIA_AGENT, rt.values_for(rt.RUN_IN_VM_VIA_AGENT, node, _truth("vm", agent=True), ENV, vals))
    frame = _frame(rb, node)
    assert frame["refused"] == [] and frame["verify"] == ['qm guest exec 106 -- bash -c "ls -la /opt/palworld/server/PalServer.sh"']


# ───── §17.1322 — the engine owns the Steam dedicated-server install

T23_NODE = {"node_key": "T23", "title": "Install PalWorld server", "description": "",
            "output_text": "## Executed on pve-runner\n$ ssh … steamcmd.sh +force_install_dir /opt/palworld +login anonymous +app_update 2394010 validate +quit\nFailed installing AppID 2394010 (Missing configuration)\n"}


def test_the_steam_template_selects_for_the_palworld_install_and_derives_its_values():
    truth = _truth("vm", agent=True)
    assert rt.select_template(T23_NODE, truth) is rt.INSTALL_STEAM_SERVER
    assert rt.select_template(ADD84, truth) is rt.RUN_IN_VM_VIA_AGENT, "a non-Steam step keeps the general agent template"
    assert rt.select_template(ADD82, truth) is rt.RUN_IN_VM_VIA_AGENT
    assert rt.derive_param("APP_ID", T23_NODE) == "2394010" and rt.derive_param("GAME", T23_NODE) == "palworld"
    bare = {"node_key": "X", "title": "Install PalWorld server", "description": ""}
    assert rt.derive_param("APP_ID", bare) == "2394010", "a known game names its own server app id"
    assert rt.derive_param("APP_ID", {"node_key": "X", "title": "Install the dedicated server for Foo", "description": ""}) == ""


@pytest.mark.asyncio
async def test_the_steam_template_renders_without_a_model_and_parses_in_bash(monkeypatch):
    import subprocess
    import app.utils.llm_retry as lr

    async def never(*a, **k):
        raise AssertionError("nothing to draw: every value is held")
    monkeypatch.setattr(lr, "generate_until_nonempty", never)
    vals = await rt.fill_free_params(rt.INSTALL_STEAM_SERVER, T23_NODE, "brief")
    assert vals == {}
    vals = rt.values_for(rt.INSTALL_STEAM_SERVER, T23_NODE, _truth("vm", agent=True), ENV, vals)
    assert vals["APP_ID"] == "2394010" and vals["GAME"] == "palworld" and vals["GID"] == "106"
    rb = rt.render(rt.INSTALL_STEAM_SERVER, vals)
    body = next(f["content"] for f in sr.file_writes(rb))
    assert "+app_update 2394010 validate +quit" in body and "/opt/palworld" in body and "{GAME}" not in body and "{INSTALL_DIR}" not in body
    assert "su - steam -c '/opt/steamcmd/steamcmd.sh +quit >/dev/null 2>&1 || true'" in body, "bootstrap as the install's user, one quoted argument"
    assert "systemctl enable palworld.service" in body and "ExecStart=$START" in body
    assert "chown -R steam:steam /opt/palworld" in body, "§17.1323 — the ssh attempt left a root-owned steamapps/ under the install dir"
    assert body.index("install -d -o steam -g steam /opt/steamcmd /opt/palworld") < body.index("chown -R steam:steam /opt/palworld") < body.index("+app_update 2394010")
    assert subprocess.run(["bash", "-n"], input=body, capture_output=True, text=True).returncode == 0
    i = body.find("<<'REMOTE'"); j = body.find("\nREMOTE\n"); remote = body[i + len("<<'REMOTE'\n"):j]
    assert subprocess.run(["bash", "-n"], input=remote, capture_output=True, text=True).returncode == 0
    frame = _frame(rb, T23_NODE)
    assert frame["refused"] == [], [r["why"][:120] for r in frame["refused"]]
    assert frame["inputs"] == [] and len(frame["commands"]) == 9
    assert frame["verify"] == ['qm guest exec 106 -- bash -c "ls -la /opt/palworld/*.sh"']
    from app.modules.runbook_preconditions import unmet
    inv = {"cts": {}, "vms": {"106": "stopped"}, "names": {"106": "palworld-server"}, "disks": {}, "isos": []}
    spec = type("S", (), {"name": "pve-runner", "headers": {}})()
    assert await unmet(sr.runbook_commands(rb), spec, plan=[], files=sr.file_writes(rb), node=T23_NODE, inventory=inv,
                       truth=mt.GuestTruth(gid="106", kind="vm", status="stopped", agent=True)) == []


@pytest.mark.asyncio
async def test_an_unknown_app_id_is_the_one_thing_the_model_is_asked(monkeypatch):
    import app.utils.llm_retry as lr
    asked = []

    async def fake(gen, prompt, params, *, system, **kw):
        asked.append(kw.get("label")); return type("R", (), {"text": "```\n896660\n```"})()
    monkeypatch.setattr(lr, "generate_until_nonempty", fake)
    node = {"node_key": "X", "title": "Install the dedicated server for Foo via SteamCMD", "description": ""}
    vals = await rt.fill_free_params(rt.INSTALL_STEAM_SERVER, node, "brief")
    assert vals == {"APP_ID": "896660"} and len(asked) == 1
    full = rt.values_for(rt.INSTALL_STEAM_SERVER, node, _truth("vm", agent=True), ENV, vals)
    assert full["APP_ID"] == "896660" and full["GAME"] == "steamapp" and full["INSTALL_DIR"] == "/opt/steamapp"
