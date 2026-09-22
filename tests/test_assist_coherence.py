"""§17.1098 — the coherence detectors: multi-action, self-contradiction, clip."""
from __future__ import annotations

from app.modules.assist_coherence import (
    changing_commands,
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
# ── §17.1165 — one machine, several CHANGES is multi-action too ──
JELLYFIN_LIST = (  # the live ADD63 shape, minus the accidental second context
    "## 👉 Do this next\n📍 On: the Proxmox host shell (root@pve)\n\n**Run this now:**\n"
    "```bash\npct exec 101 -- ls /etc/apt/sources.list.d/\n```\n\n## Steps\n"
    "2. Install gnupg:\n```bash\npct exec 101 -- apt-get install -y gnupg\n```\n"
    "3. Download the key:\n```bash\npct exec 101 -- curl -fsSL https://repo/key | gpg --dearmor -o /etc/apt/keyrings/j.gpg\n```\n"
    "4. Write the repo file:\n```bash\npct exec 101 -- bash -c 'echo deb ... > /etc/apt/sources.list.d/j.list'\n```\n"
    "5. Refresh apt:\n```bash\npct exec 101 -- apt-get update\n```\n"
)
LOOKUPS_ONLY = (
    "📍 On: the Proxmox host shell (root@pve)\n```bash\nqm status 110\nqm config 110\nip neigh show\nping -c 1 192.168.1.5\n```\n"
)
SAME_MACHINE_TWICE = (  # the live false positive: one machine, two spellings
    "## 👉 Do this next\n📍 On: the Proxmox host shell (root@pve)\n```bash\nls /etc/apt\n```\n"
    "## Steps\n📍 On: the Proxmox host shell (root@pve) — all commands below run here.\n1. Run it.\n"
)


def test_several_changes_in_one_place_is_multi_action():
    """The operator's report: 'a long list of commands without doing one at a
    time'. One context, no phases — the old rule saw nothing."""
    iss = multi_action_issue(JELLYFIN_LIST)
    assert iss, "a five-command install list must be flagged"
    assert len(iss["contexts"]) == 1 and iss["phases"] == 0     # not contexts, not phases
    assert len(iss["changes"]) >= 3
    assert any("apt-get install" in c for c in iss["changes"])
    assert all("ls /etc/apt" not in c for c in iss["changes"])  # the read-only look-up is not a change


def test_reads_are_never_changes_and_do_then_verify_stays_one_action():
    assert multi_action_issue(LOOKUPS_ONLY) is None             # four commands, zero changes
    assert changing_commands(LOOKUPS_ONLY) == []
    assert multi_action_issue(EXEC_THEN_STOP) is None           # two changes + their checks
    assert len(changing_commands(EXEC_THEN_STOP)) == 2
    assert multi_action_issue(ONE) is None


def test_the_same_machine_spelled_twice_is_one_context():
    """The live draft was rewritten for the WRONG reason: a trailing clause made
    one machine read as two contexts."""
    assert execution_contexts(SAME_MACHINE_TWICE) == ["the proxmox host shell (root@pve)"]
    assert multi_action_issue(SAME_MACHINE_TWICE) is None       # one context, one change → clean
    # a genuinely different machine still counts
    two = SAME_MACHINE_TWICE + "\n📍 On: the caddy container console (pct enter 120)\n```bash\nnano /etc/caddy/Caddyfile\n```\n"
    assert len(execution_contexts(two)) == 2 and multi_action_issue(two)


def test_the_directive_names_the_changes():
    d = coherence_directive({"multi_action": multi_action_issue(JELLYFIN_LIST)})
    assert "separate CHANGES in one step" in d and "apt-get install" in d
    assert "see what it printed" in d


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


# ---------------------------------------------------------------------------
# §17.1165 — a corrected walkthrough REPLACES the streamed draft.
# Live: the single-action gate rewrote a five-command Jellyfin repo draft, but
# the correction was APPENDED, so the operator read the rejected list ("a long
# list of commands without doing one at a time") and the transcript turn stored
# BOTH (2,925 chars) while the step's own guidance held the correct 927.
# ---------------------------------------------------------------------------
import inspect

import pytest


def test_the_stream_replaces_the_draft_instead_of_appending_it():
    from pathlib import Path
    whole = (Path(__file__).resolve().parents[1] / "app" / "modules" / "assist_guide.py").read_text(encoding="utf-8")
    src = whole[whole.index("async def generate_guidance_stream("):]
    coh = src[src.index("§17.1098 — single-action"):src.index("§17.887(#8)")]
    assert '"type": "replace"' in coh, "the coherence correction must replace the streamed draft"
    assert coh.index('"type": "replace"') < coh.index("elif _coh_block:"), "replace is the primary path; the append block is the fallback"
    assert "Rewritten as ONE action" in coh
    # the banned-value redraw on the stream path replaces too
    assert '"type": "replace"' in src[src.index("§17.893 — banned-value"):src.index("§17.887(#8)")]


def test_the_transcript_keeps_only_the_correction():
    """The tee that feeds the durable transcript must DROP the rejected draft —
    the live bug: the step's guidance was right (927 chars) and the transcript
    turn held both (2,925). Read as TEXT: this file runs in the host lane too,
    where assist_agent's driver imports are unavailable."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "app" / "modules" / "assist_agent.py").read_text(encoding="utf-8")
    i = src.index('ev.get("type") == "replace"')
    assert "_buf.clear()" in src[i:i + 200], "a replace must reset the captured buffer"


def test_every_surface_renders_the_replace_frame():
    """§17.1133 — a new SSE event needs a renderer on every consumer."""
    from pathlib import Path
    from app import sse_events
    root = Path(__file__).resolve().parents[1]
    assert sse_events.ASSIST_GUIDE_REPLACE == "assist_guide_replace"
    assert sse_events.ASSIST_GUIDE_REPLACE in sse_events.ALL_EVENT_NAMES
    turn = (root / "app" / "modules" / "assist_turn.py").read_text(encoding="utf-8")
    assert "ASSIST_GUIDE_REPLACE" in turn
    router = (root / "app" / "routers" / "assist.py").read_text(encoding="utf-8")
    assert "ASSIST_GUIDE_REPLACE" in router                      # /assist/{sid}/guide/stream
    spa = (root / "app" / "ui" / "static" / "views" / "assist.js").read_text(encoding="utf-8")
    assert 'case "assist_guide_replace"' in spa and "acc = " in spa.split('case "assist_guide_replace"')[1][:400]
    owui = (root / "pipelines" / "_vendor" / "_assist_handlers.py").read_text(encoding="utf-8")
    assert "ASSIST_GUIDE_REPLACE" in owui and "Ignore the draft above" in owui
    vendored = (root / "pipelines" / "_vendor" / "_sse_events.py").read_text(encoding="utf-8")
    assert "ASSIST_GUIDE_REPLACE" in vendored                     # byte-equal vendor carries it


@pytest.mark.asyncio
async def test_enforce_coherence_returns_only_the_rewrite_as_durable():
    """The durable text a caller persists is the CORRECTED one — never the two
    concatenated (the live transcript's 2,925-char turn was the append)."""
    from unittest.mock import AsyncMock, patch
    from app.modules import assist_guide
    multi = T35                                   # the flagged fixture: three phases, two contexts
    one = ONE                                     # one context, one command
    resp = type("R", (), {"text": one, "success": True})()
    with patch.object(assist_guide, "chat_until_nonempty", new=AsyncMock(return_value=resp)):
        durable, meta, block = await assist_guide.enforce_coherence(
            text_out=multi, messages=[{"role": "user", "content": "x"}], role="model_general", label="t")
    assert durable == one, "the durable copy is the rewrite alone"
    assert "Phase 2" not in durable and "apt install caddy" not in durable
    assert meta.get("action") == "regenerated" and meta["issues"].get("multi_action")
    assert block.startswith("\n\n---\n♻️") and one in block      # the legacy append block still exists for non-stream callers
