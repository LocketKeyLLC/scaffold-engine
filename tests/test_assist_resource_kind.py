"""§17.897/898 — copy-paste command format + VM-vs-container resource-kind gate.

Two live defects from the same homelab session, both of the §17.882/893 shape
("prompts are guidance, code is enforcement"):

§17.897 — a research/ask answer told the operator to run
``qm resize 106 scsi0 +60G`` as an INLINE code span. Only fenced blocks get a
⧉ copy button (``util.js`` ``mdToHtml``), so the command could not be copied.
The ask path was applying two of the five output directives; the missing one
(``apply_next_callout``) is where the fenced-block mandate lives.

§17.898 — the engine's own guide prescribed ``pct enter 106`` (an LXC verb) for
a resource its own facts ledger records as ``VM 106``, three times across an
ask and two guides. When it failed, the fix opened with "The error happened
because YOU used `pct enter`" — blaming the operator for the engine's command.
The session's other five resources really are containers, so container-shaped
context out-voted the one fact that mattered.
"""
from __future__ import annotations

import pytest

from app.modules import assist_guide
from app.modules.assist_directives import promote_inline_commands

pytestmark = pytest.mark.asyncio


# The confirmed-facts ledger as it actually stood in the live session.
_LIVE_FACTS = {
    "facts": [
        "Inside container 103, a 'radarr' user already exists.",
        "Sonarr LXC container 104 was created successfully with: hostname sonarr.",
        "Download-client LXC container 105 created: hostname download-client.",
        "Container 105 (download-client) has firewall=1 enabled on net0.",
        "VM 106 (palworld-server) has Ubuntu Server installed.",
        "VM 106 (palworld-server) disk scsi0 has been resized to 100G.",
    ]
}


# ── §17.897 — promote_inline_commands ────────────────────────────────────────

async def test_promotes_whole_line_command_spans():
    """The exact shape the ask path emitted (assist_turns id=1351)."""
    src = (
        "**Run this command:**\n"
        "`ssh root@192.168.1.1`\n"
        "\n"
        "**Run this command:**\n"
        "`qm resize 106 scsi0 +60G`\n"
        "\n"
        "`qm config 106 | grep scsi0`\n"
    )
    out = promote_inline_commands(src)
    assert "```bash\nssh root@192.168.1.1\n```" in out
    assert "```bash\nqm resize 106 scsi0 +60G\n```" in out
    assert "```bash\nqm config 106 | grep scsi0\n```" in out


async def test_leaves_references_and_prose_alone():
    """The narrow rule exists so ordinary prose is never mangled."""
    # Mid-sentence mentions keep their inline span.
    assert promote_inline_commands("- `qm resize`: Tells Proxmox to grow a disk.") == (
        "- `qm resize`: Tells Proxmox to grow a disk."
    )
    assert promote_inline_commands("Inside container 103, a `radarr` user exists.") == (
        "Inside container 103, a `radarr` user exists."
    )
    # A bare identifier or path on its own line is a REFERENCE, not a command.
    for ref in ("`scsi0`", "`/opt/Radarr`", "`radarr.service`", "`106`"):
        assert promote_inline_commands(ref) == ref
    # A shell-prompt paste is operator OUTPUT echoed back, not an instruction.
    assert promote_inline_commands("`root@pve:~# qm start 106`") == (
        "`root@pve:~# qm start 106`"
    )


async def test_never_touches_fenced_content():
    src = "```bash\n`this stays exactly as written`\n```"
    assert promote_inline_commands(src) == src


async def test_promote_is_a_noop_without_backticks():
    assert promote_inline_commands("no code here at all") == "no code here at all"
    assert promote_inline_commands("") == ""
    assert promote_inline_commands(None) is None


async def test_strips_prompt_marker_and_handles_sudo():
    assert "```bash\napt update\n```" in promote_inline_commands("`$ apt update`")
    assert "```bash\nsudo useradd -m steam\n```" in promote_inline_commands(
        "`sudo useradd -m steam`"
    )


# ── §17.898 — resource_kinds_from_facts ──────────────────────────────────────

async def test_reads_vm_and_container_ids_from_facts():
    kinds = assist_guide.resource_kinds_from_facts(_LIVE_FACTS)
    assert kinds == {"103": "ct", "104": "ct", "105": "ct", "106": "vm"}


async def test_ambiguous_id_is_dropped_not_guessed():
    """A half-remembered fact must never become an enforcement rule."""
    kinds = assist_guide.resource_kinds_from_facts(
        {"facts": ["VM 107 was created.", "container 107 was started."]}
    )
    assert "107" not in kinds


async def test_no_facts_is_a_noop():
    assert assist_guide.resource_kinds_from_facts(None) == {}
    assert assist_guide.resource_kinds_from_facts({}) == {}
    assert assist_guide.resource_kinds_from_facts({"facts": []}) == {}


# ── §17.898 — find_resource_kind_violations ──────────────────────────────────

async def test_catches_the_live_pct_on_a_vm_bug():
    """The guide that actually shipped the bug (assist_turns id=1319)."""
    kinds = assist_guide.resource_kinds_from_facts(_LIVE_FACTS)
    bad = "## 👉 Do this next\n\n**Run this now:**\n```bash\npct enter 106\n```\n"
    hits = assist_guide.find_resource_kind_violations(bad, kinds)
    assert hits == [
        {"id": "106", "used": "ct", "correct": "vm", "command": "pct enter 106"}
    ]


async def test_catches_qm_on_a_container_too():
    kinds = assist_guide.resource_kinds_from_facts(_LIVE_FACTS)
    hits = assist_guide.find_resource_kind_violations("```bash\nqm start 104\n```", kinds)
    assert len(hits) == 1 and hits[0]["used"] == "vm" and hits[0]["correct"] == "ct"


async def test_correct_verbs_pass_clean():
    kinds = assist_guide.resource_kinds_from_facts(_LIVE_FACTS)
    good = "```bash\nqm start 106\n```\n```bash\npct enter 105\n```"
    assert assist_guide.find_resource_kind_violations(good, kinds) == []


async def test_explanatory_prose_is_not_flagged():
    """Only fenced blocks are scanned: the sentence that CORRECTS the mistake
    necessarily names both verbs, and flagging it would punish the cure."""
    kinds = assist_guide.resource_kinds_from_facts(_LIVE_FACTS)
    prose = (
        "The error happened because `pct enter` is only for containers. "
        "VM 106 is a Virtual Machine, so it needs `qm` instead."
    )
    assert assist_guide.find_resource_kind_violations(prose, kinds) == []


async def test_unknown_ids_and_empty_inputs_are_noops():
    kinds = assist_guide.resource_kinds_from_facts(_LIVE_FACTS)
    # An id the facts say nothing about is never enforced.
    assert assist_guide.find_resource_kind_violations("```bash\npct enter 999\n```", kinds) == []
    assert assist_guide.find_resource_kind_violations("```bash\npct enter 106\n```", {}) == []
    assert assist_guide.find_resource_kind_violations("", kinds) == []


async def test_warning_names_the_resource_and_the_right_tool():
    hits = [{"id": "106", "used": "ct", "correct": "vm", "command": "pct enter 106"}]
    warn = assist_guide.resource_kind_warning(hits)
    assert "106" in warn and "qm" in warn
    assert assist_guide.resource_kind_warning([]) == ""
