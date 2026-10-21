"""§17.1730 — a step that asks the operator for a credential the engine can read becomes the read.

Live (job 7d30fe4f, 2026-10-21): T2 "Resolve admin credential" was a `decision` node asking the operator for
Jellyfin's admin API key or password; §17.1382/1385 already read (or create) the engine's own Jellyfin key, but
only on a RUN frame. Operator: "It should read the admin key itself."
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.modules import decision_pause, machine_values

_T2 = {
    "node_key": "T2", "title": "Resolve admin credential", "depends_on": ["T1"], "execution_order": 2,
    "description": "Jellyfin's POST /Users/New requires an authenticated admin session or API key. The brief lists "
                   "only the three end-user secrets, so the operator must supply the admin credential (API key or "
                   "admin username/password) once. Do NOT ask for the three end-user passwords.",
    "prompt_template": "",
}


@pytest.mark.smoke
def test_the_live_step_is_recognised_and_rewritten():
    assert machine_values.asks_for_a_readable_credential(_T2) == "jellyfin"
    new = machine_values.read_the_credential_instead("jellyfin", _T2)
    assert "never ask the operator" in new["description"]
    assert "/var/lib/jellyfin/data/jellyfin.db" in new["description"]
    assert "<JELLYFIN_API_KEY>" in new["description"]
    assert "create the engine's own" in new["description"]          # §17.1385 offered when there is none
    assert "Rewritten by the engine" in new["description"] and "Resolve admin credential" in new["description"]


@pytest.mark.smoke
@pytest.mark.parametrize("node", [
    {"title": "Grant library access", "description": "Which libraries should each user see? Options: all, none."},
    {"title": "Rotate MASS_PASSWORD", "description": "The operator should rotate the password; choose when."},
    {"title": "Read the Sonarr API key", "description": "Read Sonarr's API key from its config.xml."},
    {"title": "Supply the key", "description": "The operator must supply the API key for Plex."},   # not readable
    {"title": "Keys", "description": "The operator must supply the API keys for Sonarr and Radarr."},  # two services
])
def test_what_is_not_this(node):
    assert machine_values.asks_for_a_readable_credential(node) is None


async def test_the_pause_turns_it_into_work_instead_of_asking():
    """Production path: `pending_decision` never returns the credential question; it rewrites the node."""
    rows = MagicMock()
    rows.mappings.return_value.all.return_value = [dict(_T2)]
    delegated = MagicMock()
    delegated.scalar.return_value = {}
    writes: list[tuple[str, dict]] = []

    async def _exec(stmt, params=None):
        sql = str(stmt)
        if sql.lstrip().startswith("UPDATE"):
            writes.append((sql, params))
            return MagicMock()
        return delegated if "metadata->'decisions'" in sql else rows
    db = MagicMock()
    db.execute = AsyncMock(side_effect=_exec)
    db.commit = AsyncMock()
    assert await decision_pause.pending_decision(db, "00000000-0000-0000-0000-0000000000aa") is None
    assert len(writes) == 1
    sql, params = writes[0]
    assert "node_type = 'task'" in sql and "tool = 'Shell'" in sql
    assert params["nk"] == "T2" and params["t"] == "Get jellyfin API key"
    assert "never ask the operator" in params["d"]


# ── §17.1731 — the rewritten step runs the engine's own read-or-create ────────────────────────────

def test_the_rewritten_step_is_drafted_from_the_engines_own_command_and_passes_every_gate():
    from app.modules import machine_truth as mt
    from app.modules import runbook_templates as rt
    from app.modules import supervised_runs as sr

    node = {"node_key": "T2", **machine_values.read_the_credential_instead("jellyfin", _T2)}
    truth = mt.GuestTruth(gid="101", kind="ct", status="running", agent=False, key_known_by=None)
    env = {"profile": "root@pve", "system_state": {"host": {"kind": "host", "attrs": {"ip": "192.168.1.156"}},
                                                   "101": {"kind": "ct", "attrs": {"hostname": "jellyfin"}}}}
    tpl = rt.select_template(node, truth)
    assert tpl is rt.GET_SERVICE_KEY
    rb = rt.render(tpl, rt.values_for(tpl, node, truth, env, {}))
    body = sr.file_writes(rb)[0]["content"]
    # the engine's own read and create, not a model's guess at the schema
    assert "SELECT AccessToken FROM ApiKeys WHERE Name=?" in body and "scaffold-engine" in body
    assert "PRAGMA table_info(ApiKeys)" in body and '"systemctl","stop","jellyfin"' in body
    assert "FROM Tokens" not in body
    assert 'X-Emby-Token: $K' in body and 'echo "jellyfin key found (length ${#K})"' in body
    assert "echo $K" not in body and 'echo "$K"' not in body, "the key itself is never printed"
    spec = type("S", (), {"name": "pve-runner", "headers": {}})()
    policy = {"allow": ["ANY"], "sudo": True, "helper": "24", "secrets": [], "can_write_files": True}
    frame = sr.frame_run(node, rb, spec, policy, env=env)
    assert frame["refused"] == [], [r["why"][:160] for r in frame["refused"]]
    assert frame["inputs"] == [], "nothing is asked of the operator"
    assert any("systemctl is-active jellyfin" in v for v in frame["verify"])


@pytest.mark.smoke
def test_an_ordinary_container_step_does_not_take_the_key_template():
    from app.modules import machine_truth as mt
    from app.modules import runbook_templates as rt
    truth = mt.GuestTruth(gid="101", kind="ct", status="running", agent=False, key_known_by=None)
    node = {"node_key": "T3", "title": "Create user mark", "description": "Inside jellyfin's container, POST /Users/New."}
    assert rt.select_template(node, truth) is not rt.GET_SERVICE_KEY
