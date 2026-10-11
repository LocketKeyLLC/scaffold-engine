"""§17.1466 — a run that moves a machine tells the plan where it went.

Live (2026-10-11): ADD154 moved VM 106 (palworld-server) from a fixed 192.168.1.106 to a router lease, and it came
back as 192.168.1.44. ADD152's walkthrough still said "reserve 192.168.1.106" and the control panel (ADD122) still
reached its Palworld settings at 192.168.1.106: §17.1449 only carried changes the SESSION saw.
"""
import asyncio
import json
import pathlib

from app.modules import address_watch as aw

FIX = pathlib.Path("tests/fixtures")
BLOCK = json.loads((FIX / "add154_block_2026_10_11.json").read_text())
BEFORE = (FIX / "vm106_ip_addr_before_2026_10_11.txt").read_text()
AFTER = (FIX / "vm106_ip_addr_2026_10_11.txt").read_text()


def test_the_guest_a_helper_script_runs_in_is_found():
    assert aw.guests_touched(BLOCK["commands"], [{"path": "/tmp/in_vm_106_agent.sh", "content": "GID=106"}]) == {"106": True}
    assert aw.guests_touched(["pct exec 111 -- systemctl restart panel", "qm set 106 --net0 x"]) == {"111": False, "106": True}
    assert aw.guests_touched(["curl -s http://192.168.1.106:8211"]) == {}


def test_the_move_is_read_off_the_machine():
    assert aw.parse_addresses(BEFORE) == ["192.168.1.106"] and aw.parse_addresses(AFTER) == ["192.168.1.44"]
    assert aw.moves({"106": ["192.168.1.106"]}, {"106": ["192.168.1.44"]}) == [{"id": "106", "old": "192.168.1.106", "new": "192.168.1.44"}]


def test_no_move_without_both_readings():
    assert aw.moves({"106": ["192.168.1.106"]}, {}) == []                    # could not read after: not a move
    assert aw.moves({"106": ["192.168.1.106"]}, {"106": ["192.168.1.106"]}) == []
    assert aw.moves({"106": ["192.168.1.106"]}, {"106": ["192.168.1.106", "10.0.0.5"]}) == []   # an address added


def test_measure_reads_inside_the_guest(monkeypatch):
    seen = []

    async def probe(spec, cmd):
        seen.append(cmd)
        return True, AFTER
    import app.modules.service_truth as st
    monkeypatch.setattr(st, "_probe", probe)
    assert asyncio.run(aw.measure(object(), {"106": True})) == {"106": ["192.168.1.44"]}
    assert seen == ["qm guest exec 106 -- ip -4 addr show scope global"]


def test_done_work_naming_the_old_address_gets_a_step(monkeypatch):
    plan = [{"node_key": "ADD122", "title": "Control panel: edit Palworld settings", "status": "done",
             "output_text": "const PALWORLD_HOST = { address: '192.168.1.106', user: 'aedefruscio' };"},
            {"node_key": "ADD130", "title": "Start VM 106 (palworld-server)", "status": "done",
             "output_text": "inet 192.168.1.1069 nothing"}]
    got = {}

    async def ins(job_id, spec, *, edited_by=None, db=None):
        got.update(spec)
        return {"status": "ok", "node_key": spec["node_key"]}
    import app.modules.node_editor as ne
    monkeypatch.setattr(ne, "insert_node", ins)
    stale = [(plan[0], {"id": "106", "old": "192.168.1.106", "new": "192.168.1.44"}, "VM 106 (palworld-server)")]
    assert aw._addr_re("192.168.1.106").search(plan[1]["output_text"]) is None
    key = asyncio.run(aw._add_repoint_step(None, "j", "ADD154", plan, stale))
    assert key == "ADR1" and got["depends_on"] == ["ADD154"]
    assert "ADD122 (Control panel: edit Palworld settings)" in got["description"]
    assert "192.168.1.106 to 192.168.1.44" in got["description"]


def test_it_is_wired_around_the_run():
    src = pathlib.Path("app/modules/supervised_runs.py").read_text()
    a = src.index("_aw_guests, _aw_before = await _aw.around_run(")
    assert a < src.index("UPDATE dag_nodes SET status = 'running'", a) < src.index("await _aw.after_run(db, spec, job_id, node_key")


def test_what_builds_on_the_repointed_work_waits_for_it():
    """§17.1468 — T37 (validation) was drafted ahead of ADR1."""
    plan = [{"node_key": "ADD151", "status": "done", "depends_on": []},
            {"node_key": "ADD152", "status": "pending", "depends_on": ["ADD154"]},
            {"node_key": "ADD154", "status": "done", "depends_on": []},
            {"node_key": "T37", "status": "pending", "depends_on": ["ADD152", "ADD151"]},
            {"node_key": "T38", "status": "pending", "depends_on": ["T37"]}]
    assert aw.waits_for_repoint(plan, {"ADD151"}) == ["T37", "T38"]
    assert aw.waits_for_repoint(plan, {"ADD122"}) == []


def test_the_repoint_names_a_check_for_each_place():
    """§17.1468 — ADR1 was refused: "names no command the engine can run"."""
    stale = [(json.loads(ln), {"id": "106", "old": "192.168.1.106", "new": "192.168.1.44"}, "VM 106 (palworld-server)")
             for ln in (FIX / "adr1_stale_steps_2026_10_11.jsonl").read_text().splitlines()]
    cs = aw.stale_checks(stale)
    assert any(c.startswith("qm guest exec 106 -- sh -c \"grep -rIl") for c in cs), cs
    assert any(c.startswith("pct exec 111 -- sh -c \"grep -rIl") for c in cs), cs
    assert "grep -rIl -e '192.168.1.106' /etc/pve/firewall /etc/hosts 2>/dev/null; true" in cs
    assert all("--exclude='*.bak*'" in c for c in cs if "exec" in c)
    from app.modules import operator_steps as ops
    desc = "Done when nothing still names it — each of these prints nothing: " + ", ".join(f"`{c}`" for c in cs)
    assert ops.checks({"description": desc}) == cs
