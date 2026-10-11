"""§17.1484 — a document's commands are its content.

Live (2026-10-11): T38 "Document architecture and setup" came back whole (8,203 chars) and was failed "this step
produced INSTRUCTIONS, not work" for the maintenance section's `pct exec 120 -- caddy validate --config …`.
"""
import asyncio

from app.modules import supervised_runs as sr

DOC = ("# Homelab Documentation\n\n## Maintenance\n\n```bash\npct exec 120 -- caddy validate --config /etc/caddy/Caddyfile\n"
       "pct exec 120 -- systemctl reload caddy\nsystemctl is-active pve-firewall\n```\n")


def test_a_document_is_left_alone():
    out = asyncio.run(sr.wrote_instructions_instead_of_doing_it(
        DOC, {"title": "Document architecture and setup", "tool": "LLM"}, None))
    assert out is None


def test_a_step_that_should_have_done_it_is_still_caught(monkeypatch):
    """The rule itself is unchanged for work: its own tests cover the channel; here only the title gate."""
    import inspect
    src = inspect.getsource(sr.wrote_instructions_instead_of_doing_it)
    assert src.index("_DOC_RE.search(") < src.index("runbook_commands(text_value)")
    from app.modules.inventory_report import _DOC_RE
    assert not _DOC_RE.search("Give the media-stack containers working DNS")
    assert not _DOC_RE.search("Validate entire build")


def test_the_list_outranks_the_steps_own_older_notes():
    """§17.1484 — T38's text carried "[Plan update — operator constraint]: Documentation must reflect that
    /etc/pve/firewall/groups/ does not yet exist …", written before ADD151 turned every guest's firewall on."""
    from app.modules import inventory_report as ir
    block = ir.inventory_block([{"kind": "host", "id": "host", "name": "pve", "status": "running", "address": "x"}])
    assert "A NOTE IN THIS STEP'S OWN TEXT" in block and "still to be created what it shows in place" in block
