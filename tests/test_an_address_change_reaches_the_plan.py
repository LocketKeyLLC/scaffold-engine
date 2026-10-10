"""§17.1449 — a machine's new address reaches the steps ahead, including steps whose text is their description.

Live (ADD4 → ADD128, 2026-10-10): to reserve it in the router app, CT 120 went from a fixed 192.168.1.26 to
DHCP and got 192.168.1.127. The engine's record of CT 120 said .127 at once; ADD128 ("Caddy in CT 120
(caddy-proxy, 192.168.1.26)", done-check `--resolve …:443:192.168.1.26`) did not, and the reply curled .26.
Two gaps: nothing reacted to the address change, and every plan trigger read and rewrote `prompt_template`
only — ADD128 (and ADD100) keep their text in `description`, so no correction could ever reach them.
"""
import asyncio
import json
import pathlib

from app.modules import plan_reconcile as pr

ADD128 = json.loads(pathlib.Path("tests/fixtures/add128_node_2026_10_08.json").read_text())
CT120_BEFORE = {"kind": "ct", "attrs": {"ip": "192.168.1.26", "hostname": "caddy-proxy"}, "source": "pct config 120"}
CT120_AFTER = {"kind": "ct", "attrs": {"ip": "192.168.1.127", "hostname": "caddy-proxy"}, "source": "pct config 120"}
CT111 = {"kind": "ct", "attrs": {"ip": "192.168.1.25", "hostname": "control-panel"}}


def test_the_live_move_is_an_address_change():
    ch = pr.address_changes({"120": CT120_BEFORE, "111": CT111}, {"120": CT120_AFTER, "111": CT111})
    assert ch == [{"id": "120", "label": "CT 120 (caddy-proxy)", "old": "192.168.1.26", "new": "192.168.1.127"}]


def test_an_ambiguous_move_is_left_alone():
    other_has_old = {"120": CT120_AFTER, "130": {"kind": "ct", "attrs": {"ip": "192.168.1.26"}}}
    assert pr.address_changes({"120": CT120_BEFORE}, other_has_old) == []
    other_had_new = {"120": CT120_BEFORE, "110": {"kind": "vm", "attrs": {"ip": "192.168.1.127"}}}
    assert pr.address_changes(other_had_new, {"120": CT120_AFTER}) == []
    assert pr.address_changes({"120": CT120_AFTER}, {"120": CT120_AFTER}) == []
    assert pr.address_changes({}, {"120": CT120_AFTER}) == [], "a first sighting is not a move"


def test_add128s_description_is_corrected_everywhere_it_names_the_old_address():
    assert "192.168.1.26" in ADD128["description"]
    node = {"node_key": "ADD128", "status": "pending", "prompt_template": ADD128["description"]}  # STEP_TEXT_SQL's shape
    corr = [{"kind": "ip", "old": "192.168.1.26", "new": "192.168.1.127"}]
    out = pr.plan_changes([node], [], corr, source_node_key="env", source_label="the measured address of CT 120")
    new = out["node_updates"][0]["prompt_template"]
    body = "\n".join(ln for ln in new.splitlines() if not ln.lstrip().startswith(pr.PROVENANCE_PREFIX))
    import re
    assert not re.search(r"192\.168\.1\.26(?!\d)", body), "no old address left in the step's own text"
    assert "--resolve defrusciohomelab.duckdns.org:443:192.168.1.127" in body
    assert "(caddy-proxy, 192.168.1.127)" in body
    assert "192.168.1.25:3001" in body, "another machine's address is untouched"


def test_every_trigger_reads_and_writes_the_steps_text():
    src = pathlib.Path("app/modules/plan_reconcile.py").read_text()
    assert "UPDATE dag_nodes SET prompt_template = :pt" not in src
    assert "status, prompt_template FROM dag_nodes" not in src and "title, prompt_template FROM dag_nodes" not in src
    assert src.count("{STEP_TEXT_SET}") == 7 and "AND {STEP_TEXT_EXPR} = :after" in src


class _DB:
    def __init__(self, nodes):
        self.nodes, self.sql = nodes, []

    async def execute(self, stmt, params=None):
        q = str(stmt)
        self.sql.append((q, params or {}))
        rows = self.nodes if "FROM dag_nodes" in q else []

        class R:
            def mappings(self_inner):
                class M:
                    def all(self2):
                        return rows

                    def first(self2):
                        return rows[0] if rows else None
                return M()
        return R()

    async def commit(self):
        pass


def test_the_trigger_rewrites_the_description_held_step_and_ledgers_it(monkeypatch):
    monkeypatch.setattr(pr.settings, "plan_reconcile_enabled", True, raising=False)
    db = _DB([{"node_key": "ADD128", "status": "pending", "prompt_template": ADD128["description"]}])
    rec = asyncio.run(pr.reconcile_after_address_change(
        db=db, session_id="613dd1df-4c92-43f7-a35f-c9519add5701", job_id="55f68b7f-2ced-4e13-a1bc-812320df56f8",
        changes=[{"id": "120", "label": "CT 120 (caddy-proxy)", "old": "192.168.1.26", "new": "192.168.1.127"}]))
    assert rec and [u["node_key"] for u in rec["node_updates"]] == ["ADD128"]
    upd = [p for q, p in db.sql if "UPDATE dag_nodes" in q]
    assert upd and "192.168.1.127" in upd[0]["pt"]
    q_upd = [q for q, _ in db.sql if "UPDATE dag_nodes" in q][0]
    assert "description = CASE WHEN COALESCE(prompt_template, '') = ''" in q_upd
    assert any("'{reconciliation}'" in q for q, _ in db.sql)
    note = pr.render_note(rec)
    assert note.startswith("🔁 **Plan updated: CT 120 (caddy-proxy) has a new address**")
    assert "`192.168.1.26` → `192.168.1.127` (ip) in ADD128" in note


def test_set_environment_runs_it_after_the_write():
    src = pathlib.Path("app/modules/assist_environment.py").read_text()
    i = src.index("async def set_environment(")
    body = src[i:src.index("async def _propagate_address_changes")]
    assert "_state_before = json.loads(json.dumps(current.get(\"system_state\") or {}))" in body
    assert body.index("await db.commit()") < body.index("await _propagate_address_changes(")


def test_a_typed_correction_with_both_addresses_is_one_correction():
    corr, _ = pr.note_corrections("Caddy (CT 120) is at 192.168.1.127, not 192.168.1.26.", ADD128["description"])
    assert corr == [{"kind": "ip", "old": "192.168.1.26", "new": "192.168.1.127"}]
