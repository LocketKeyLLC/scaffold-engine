"""§17.1098 — the coherence detectors: multi-action, self-contradiction, clip."""
from __future__ import annotations

from app.modules.assist_coherence import (
    coherence_directive,
    coherence_issues,
    contradiction_warning,
    count_phases,
    execution_contexts,
    first_action_only,
    multi_action_issue,
    self_contradictions,
)

T35 = (
    "## 👉 Do this next\n\n"
    "### Phase 1: Create the Proxy Container\n📍 On: the Proxmox host shell (root@pve)\n```bash\npct create 120\n```\n\n"
    "### Phase 2: Install Caddy\n📍 On: the Proxmox host shell (root@pve)\n```bash\npct exec 120 -- apt install caddy\n```\n\n"
    "### Phase 3: Configure Reverse Proxy\n📍 On: the caddy container console (pct enter 120)\n```bash\nnano /etc/caddy/Caddyfile\n```\n"
)
ONE = (
    "## 👉 Do this next\n\n📍 On: the Proxmox host shell (root@pve)\n\n"
    "**Run this now:**\n```bash\nqm start 106\n```\nThen tell me what it shows."
)
STOP_THEN_EXEC = (
    "## Fix\n\n1. Stop the container:\n```bash\npct stop 120\n```\n"
    "2. Now open its console:\n```bash\npct exec 120 -- nano /etc/caddy/Caddyfile\n```\n"
)
EXEC_THEN_STOP = (  # ADD11 (real): correct order — use, then stop
    "## Fix\n\n1. Empty it:\n```bash\npct exec 120 -- truncate -s 0 /etc/caddy/Caddyfile\n```\n"
    "2. Confirm 0 bytes:\n```bash\npct exec 120 -- wc -c /etc/caddy/Caddyfile\n```\n"
    "3. Stop the container:\n```bash\npct stop 120\n```\n4. Confirm stopped:\n```bash\npct status 120\n```\n"
)
REBOOT_THEN_USE = (  # reboot RECOVERS — must NOT flag
    "## Fix\n\n1. Reboot the container:\n```bash\npct reboot 120\n```\n"
    "2. After it comes back, check inside it:\n```bash\npct exec 120 -- ip a\n```\n"
)


# ── multi-action ──
def test_phases_make_a_step_multi_action():
    iss = multi_action_issue(T35)
    assert iss and iss["phases"] == 3
    assert len(execution_contexts(T35)) == 2   # two distinct 'On:' targets

def test_single_action_step_is_not_flagged():
    assert multi_action_issue(ONE) is None
    assert count_phases(ONE) == 0

def test_several_commands_one_context_is_one_action():
    # ADD11: four commands, one context, no phases — NOT multi-action.
    assert multi_action_issue(EXEC_THEN_STOP) is None


# ── self-contradiction ──
def test_stop_then_use_is_a_contradiction():
    cs = self_contradictions(STOP_THEN_EXEC)
    assert len(cs) == 1 and cs[0]["resource"] == "120"
    assert cs[0]["down_at"] < cs[0]["use_at"]

def test_use_then_stop_is_fine():
    assert self_contradictions(EXEC_THEN_STOP) == []

def test_reboot_then_use_is_fine():
    # reboot/restart recover, so 'reboot then run inside' is coherent.
    assert self_contradictions(REBOOT_THEN_USE) == []

def test_different_resources_do_not_cross():
    txt = "Stop it:\n```\npct stop 120\n```\nThen use another:\n```\npct exec 106 -- ls\n```\n"
    assert self_contradictions(txt) == []


# ── combined + clip + copy ──
def test_coherence_issues_combines():
    assert "multi_action" in coherence_issues(T35)
    assert "contradictions" in coherence_issues(STOP_THEN_EXEC)
    assert coherence_issues(ONE) == {}

def test_first_action_only_clips_at_the_second_phase():
    clipped, did = first_action_only(T35)
    assert did
    assert "Phase 1" in clipped and "Phase 2" not in clipped and "Phase 3" not in clipped
    assert "next step" in clipped.lower()

def test_first_action_only_noop_on_single_action():
    clipped, did = first_action_only(ONE)
    assert not did and clipped == ONE

def test_directive_names_the_problem():
    d = coherence_directive(coherence_issues(T35))
    assert "phases" in d.lower() and "one action" in d.lower()
    w = contradiction_warning(coherence_issues(STOP_THEN_EXEC))
    assert "120" in w and "stopped" in w.lower()


# ── regression: the false-positive classes found by replaying 596 real turns ──
def test_restart_idiom_is_not_a_contradiction():
    # `qm stop N && qm start N` is a restart; the console after it is fine.
    txt = "```bash\nqm stop 110 && qm start 110\n```\nThen open the **Console** tab for VM 110.\n```\nqm terminal 110\n```"
    assert self_contradictions(txt) == []

def test_stopping_a_service_inside_a_container_is_not_stopping_the_container():
    txt = "## Steps\n1. Stop the service inside container 111:\n```bash\npct exec 111 -- systemctl stop control-panel.service\n```"
    assert self_contradictions(txt) == []

def test_use_in_a_trailing_troubleshooting_section_is_not_a_contradiction():
    txt = ("1. Stop it:\n```bash\npct stop 120\n```\n"
           "## If that fails\n- If `pct exec 120 -- truncate ...` printed an error, paste it.\n")
    assert self_contradictions(txt) == []

def test_use_in_done_when_verification_is_not_a_contradiction():
    txt = ("1. Stop it:\n```bash\npct stop 120\n```\n"
           "## ✅ Done when\n`pct exec 120 -- systemctl is-active caddy` returns active.\n")
    assert self_contradictions(txt) == []
