"""§17.1485 — a plan update says where it came from; the inventory says where Proxmox keeps security groups.

Live (2026-10-11): T38's text carried "[Plan update — operator constraint]: Documentation must reflect that
/etc/pve/firewall/groups/ does not yet exist and the security groups still need to be created" — the engine's
reading of an observed fact, labelled as the operator's, written before ADD151 turned every guest's firewall on.
Every write-up repeated it: Proxmox has no groups/ directory; the groups are the [group …] sections of cluster.fw.
"""
import asyncio
import pathlib

from app.modules import assist_replan as ar
from app.modules import inventory_report as ir

FW = pathlib.Path("tests/fixtures/cluster_fw_2026_10_11.txt").read_text()


def test_the_label_names_the_source_and_the_date():
    lab = ar.plan_update_label({"note_kind": "fact", "ts": "2026-10-10T09:12:00Z",
                                "note_text": "/etc/pve/firewall/groups/ does not exist"})
    assert lab == ('[Plan update 2026-10-10 — the engine\'s reading of an observed fact '
                   '"/etc/pve/firewall/groups/ does not exist", approved by the operator]')
    assert ar.plan_update_label({"note_kind": "note", "note_text": "use DHCP"}).startswith(
        '[Plan update — the operator\'s note "use DHCP"')
    assert "operator constraint" not in ar.plan_update_label(None)


def test_the_revision_is_written_with_it():
    import inspect
    src = inspect.getsource(ar.apply_note_replan)
    assert "|| E'\\n\\n' || :label || ': ' || :change" in src and '"label": plan_update_label(source)' in src
    notes = pathlib.Path("app/modules/assist_notes.py").read_text()
    assert 'source={k: pending.get(k) for k in ("note_kind", "note_text", "ts")}' in notes


def test_the_inventory_names_where_the_groups_live(monkeypatch):
    import app.modules.service_truth as st

    async def probe(spec, cmd):
        if cmd == "cat /etc/pve/firewall/cluster.fw":
            return True, FW
        if cmd == "hostname -I":
            return True, "192.168.1.156"
        return True, ""
    monkeypatch.setattr(st, "_probe", probe)
    inv = asyncio.run(ir.measured_inventory(object()))
    g = inv[0]["groups"]
    assert g.startswith("management, media, game, ai, dmz — defined in /etc/pve/firewall/cluster.fw")
    assert "no groups/ directory" in ir.inventory_block(inv)
