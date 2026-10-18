"""§17.1637 — a step that says it changes nothing is drafted with reads only.

Live (job 0fb4ea7f MC1, 2026-10-18): "On the Proxmox host: check CPU1 memory channel C … change nothing, read only"
came back with `apt-get install -y ipmitool` -- on a host whose `ipmitool sel elist` an earlier step had read -- and
every BMC read over the network with an address, a login and the password on the command line. The only refusal was
§17.1285's, for missing guest 110 (named in the step's "Why"), whose remedy is to carry the write INTO the VM.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from app.modules import supervised_runs as sr

MC1 = {"node_key": "MC1", "title": "On the Proxmox host: check CPU1 memory channel C", "tool": "Shell",
       "description": "Read only: the operator asked the engine to check the CPU1 memory channel (2026-10-18).",
       "prompt_template": ("On the Proxmox host (not inside the VM): check CPU1's memory channel C; change nothing, "
                           "read only.\nWhy: VS1 read the BMC event log and found every host crash since VM 110's "
                           "P40 passthrough lined up with \"Correctable ECC @DIMMC1(CPU1)\".\nDone when the step "
                           "reports the count of ECC events on channel C of CPU1.")}
#: the drafted block, abridged from the live frame
DRAFT = """## Run this

```bash
apt-get install -y ipmitool
```

```bash
ipmitool -I lanplus -H <BMC_IP> -U <BMC_USER> -P "$MASS_PASSWORD" sel elist
```

```bash
dmidecode -t memory
```

```bash
who -b
```
"""
READS = """## Run this

```bash
ipmitool sel elist
```

```bash
dmidecode -t memory
```

```bash
for mc in /sys/devices/system/edac/mc/mc*; do echo "== $mc =="; cat "$mc/ce_count"; done
```
"""
POLICY = {"allow": ["ANY"], "sudo": True, "helper": "23"}
INV = {"vms": {"110": "running"}, "cts": {}, "names": {"110": "ai-vm"}}


def _frame(node, runbook):
    spec = MagicMock(); spec.name = "pve-runner"; spec.headers = {"X-Runner-Token": "t"}
    return sr.frame_run(node, runbook, spec, POLICY, inventory=INV)


def test_the_live_draft_is_refused_for_writing_not_for_missing_the_guest():
    frame = _frame(MC1, DRAFT)
    whys = [r["why"] for r in frame["refused"]]
    assert any("this step says it is READ ONLY" in w and "apt-get install" in w for w in whys), whys
    assert not any("instead of guest 110" in w for w in whys), whys        # the wrong remedy is gone
    assert sr.shape_retry_note(frame)                             # registered: it redrafts, not parks


def test_the_reads_it_should_have_drafted_pass():
    frame = _frame(MC1, READS)
    assert not any("READ ONLY" in r["why"] for r in frame["refused"]), frame["refused"]


def test_a_read_over_the_network_with_a_login_is_refused_too():
    assert sr.writes_in_a_read_only_step(
        ['ipmitool -I lanplus -H <BMC_IP> -U <BMC_USER> -P "$MASS_PASSWORD" sel elist'], MC1)


def test_what_declares_a_step_read_only():
    for text in ("Read only: the operator asked", "Read only, inside VM 110: show", "Read only.",
                 "print its first lines; change nothing.", "read (change nothing): the board",
                 "test the fallback end to end; change nothing (one short model call)"):
        assert sr.declares_read_only({"description": text}), text
    for text in ("Set the save path; change nothing else.", "mounts the shared storage read-only at /media",
                 "give it the PVEAuditor role on / (read-only), and create its API token",
                 "activity, read-only, with the Pi-hole address read from config",
                 "implement the network-view capability (read-only Pi-hole API)",
                 "Measured 2026-10-08 (read-only): Caddy has never obtained a certificate",
                 "Done when the view is read only: it changes nothing in Pi-hole."):
        assert not sr.declares_read_only({"description": text}), text


def test_a_write_step_is_not_judged_and_still_meets_the_guest_gate():
    node = {"node_key": "W", "title": "Install the agent in VM 110", "description": "Change nothing else."}
    assert sr.writes_in_a_read_only_step(["apt-get install -y qemu-guest-agent"], node) == []
    assert sr.commands_never_reach_the_guest(["apt-get install -y qemu-guest-agent"], node, [], INV)


def test_a_template_frame_is_the_engines_own_shape():
    from app.modules import runbook_templates as rt
    body = rt.render(rt.REACH_VM_SSH_AND_RUN, {"GID": "110", "STEP": "PLR1", "REMOTE_COMMANDS": "cat -n /etc/hostname",
                                              "GUEST_USER_NAME": "AI_USER", "NEEDS_KEY": "yes"})
    node = {"node_key": "PLR1", "title": "VM 110: read the placement record", "description": "Read only."}
    assert sr.writes_in_a_read_only_step(["bash /tmp/in_vm_110.sh start"], node, body) == []


#: §17.1637b — the redraft after §17.1637, verbatim in shape: the install was gone, every BMC read still went over lanplus
REDRAFT = ["command -v ipmitool || echo 'ipmitool: not installed'",
           'ipmitool -I lanplus -H <BMC_IP> -U <BMC_USER> -P "$MASS_PASSWORD" sel list',
           "dmidecode -t memory"]


def test_the_refusal_names_the_local_read():
    found = sr.writes_in_a_read_only_step(REDRAFT, MC1)
    assert len(found) == 1 and "Here that is `ipmitool sel list`" in found[0]["why"], found


def test_only_a_read_gets_a_local_form():
    assert sr.local_form_of_a_read('ipmitool -I lanplus -H <BMC_IP> -U u -P "$P" sel elist') == "ipmitool sel elist"
    assert sr.local_form_of_a_read("ipmitool -I lanplus -H x -U y -E sdr elist") == "ipmitool sdr elist"
    for c in ("ipmitool -I lanplus -H x -U y -P p sel clear", "ipmitool -I lanplus -H x -U y -P p mc reset cold",
              "ipmitool sel elist", "apt-get install -y ipmitool", "ipmitool -I lanplus -H 'x"):
        assert sr.local_form_of_a_read(c) == "", c


#: §17.1637c — the third live draft, after §17.1637b named the local form: still lanplus, now behind pipes too
THIRD = ["command -v ipmitool || echo 'ipmitool: not installed'",
         'ipmitool -I lanplus -H <BMC_IP> -U <BMC_USER> -P "$MASS_PASSWORD" sel list',
         "ipmitool -I lanplus -H <BMC_IP> -U <BMC_USER> -P \"$MASS_PASSWORD\" sel list | grep -c 'DIMMC1(CPU1)'",
         'ipmitool -I lanplus -H <BMC_IP> -U <BMC_USER> -P "$MASS_PASSWORD" sensor list',
         "dmidecode -t memory"]
THIRD_RUNBOOK = "## Run this\n\n" + "\n\n".join(f"```bash\n{c}\n```" for c in THIRD) + "\n"


def test_the_engine_writes_the_local_read_itself_and_asks_nothing():
    frame = _frame(MC1, THIRD_RUNBOOK)
    assert frame["commands"][1:4] == ["ipmitool sel list", "ipmitool sel list | grep -c 'DIMMC1(CPU1)'",
                                      "ipmitool sensor list"], frame["commands"]
    assert frame["inputs"] == [] and not frame["refused"], (frame["inputs"], frame["refused"])
    assert any("the engine wrote them in the local form" in str(f) for f in frame["engine_fixed"]), frame["engine_fixed"]


def test_a_literal_address_or_a_write_step_is_left_alone():
    lit = ['ipmitool -I lanplus -H 10.0.0.9 -U u -P "$P" sel list']
    assert sr.local_reads_in_a_read_only_step(MC1, lit, [])[0] == lit           # a machine the drafter chose
    write_step = {"node_key": "W", "title": "Clear the BMC log", "description": "Change nothing else."}
    assert sr.local_reads_in_a_read_only_step(write_step, THIRD, [])[0] == THIRD
    clear = ['ipmitool -I lanplus -H <BMC_IP> -U u -P "$P" sel clear']          # no local READ form: stays refused
    assert sr.local_reads_in_a_read_only_step(MC1, clear, [])[0] == clear
    assert sr.writes_in_a_read_only_step(clear, MC1)
