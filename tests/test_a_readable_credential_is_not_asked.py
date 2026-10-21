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


# ── §17.1732 / §17.1733 — T3's live command ───────────────────────────────────────────────────────

_T3_LIVE = ("pct exec 101 -- sh -c 'curl -s --fail-with-body -X POST \"http://127.0.0.1:8096/Users/New?Name=mark&Password="
            "'\"$JELLYFIN_MARK_PASSWORD\"'\" -H \"X-Emby-Token: $(sed -n \"s:.*<ApiKey>\\(.*\\)</ApiKey>.*:\\1:p\" "
            "/var/lib/jellyfin/config.xml | head -1)\"'")


@pytest.mark.smoke
def test_a_key_read_from_the_wrong_file_becomes_the_placeholder():
    cmds, _v, notes = machine_values.a_key_read_from_the_wrong_place_is_the_placeholder([_T3_LIVE], [])
    assert '-H "X-Emby-Token: <JELLYFIN_API_KEY>"' in cmds[0] and "config.xml" not in cmds[0]
    assert notes and "§17.1732" in notes[0]["why"]
    right = "pct exec 103 -- sh -c 'curl -H \"X-Api-Key: $(sed -n \"s:.*<ApiKey>\\(.*\\)</ApiKey>.*:\\1:p\" /var/lib/radarr/config.xml)\" x'"
    assert machine_values.a_key_read_from_the_wrong_place_is_the_placeholder([right], [])[0] == [right]


@pytest.mark.smoke
def test_a_secret_spliced_into_a_guest_exec_line_is_refused_and_stdin_passes():
    from app.modules import supervised_runs as sr
    pol = {"held": ["JELLYFIN_MARK_PASSWORD"]}
    out = sr.secret_in_a_guest_exec_command_line([_T3_LIVE], pol)
    assert len(out) == 1 and "is spliced into `pct exec`'s command line" in out[0]["why"]
    assert any(m in out[0]["why"] for m in sr._SHAPE_REFUSALS), "a refusal no redraft recognises is a dead end"
    stdin = ("pct exec 101 -- sh -c 'read -r V; printf \"%s\" \"$V\" | curl -s --data-binary @- http://x' "
             "<<< \"$JELLYFIN_MARK_PASSWORD\"")
    assert sr.secret_in_a_guest_exec_command_line([stdin], pol) == []
    assert sr.secret_in_a_guest_exec_command_line(["pct exec 101 -- systemctl is-active jellyfin"], pol) == []
    assert sr.secret_in_a_guest_exec_command_line(['pct exec 105 -- sh -c "curl -u admin:$MASS_PASSWORD x"'],
                                                   {"held": ["MASS_PASSWORD"]})


def test_frame_run_carries_both_fixes_on_the_live_command():
    from app.modules import supervised_runs as sr
    node = {"node_key": "T3", "title": "Create user mark", "description": "POST /Users/New in jellyfin."}
    rb = f"## Run this\n\n```bash\n{_T3_LIVE}\n```\n\n## Verify\n\n- `pct exec 101 -- systemctl is-active jellyfin`\n"
    spec = type("S", (), {"name": "pve-runner", "headers": {}})()
    policy = {"allow": ["ANY"], "sudo": True, "helper": "24", "held": ["JELLYFIN_MARK_PASSWORD"], "can_write_files": True}
    inv = {"names": {"101": "jellyfin", "104": "sonarr"}}
    frame = sr.frame_run(node, rb, spec, policy, env={}, inventory=inv)
    whys = " | ".join(r["why"] for r in frame["refused"])
    assert "which does not hold one" not in whys                      # §17.1732: the engine fixed the read
    assert "is spliced into `pct exec`'s command line" in whys         # §17.1733: the password goes to stdin


def test_the_rules_worked_example_passes_every_gate():
    """§17.1734 — a rule must not teach what a gate refuses: the CHANNEL_RULES example, as a block, through frame_run."""
    import re
    from app.modules import supervised_runs as sr
    rule = sr.CHANNEL_RULES[sr.CHANNEL_RULES.index("(§17.1734)"):]
    rule = rule[:rule.index("\n")]
    run, check = re.findall(r"`(pct exec 101 -- sh -c '.*?'(?: <<< \"\$JELLYFIN_MARK_PASSWORD\")?)`", rule)
    assert "<<<" in run and "<<<" not in check
    rb = f"## Run this\n\n```bash\n{run}\n```\n\n## Verify\n\n- `{check}`\n"
    node = {"node_key": "T3", "title": "Create user mark", "description": "Create the Jellyfin user mark."}
    spec = type("S", (), {"name": "pve-runner", "headers": {}})()
    policy = {"allow": ["ANY"], "sudo": True, "helper": "24", "held": ["JELLYFIN_MARK_PASSWORD"],
              "secrets": ["JELLYFIN_MARK_PASSWORD"], "can_write_files": True}
    inv = {"names": {"101": "jellyfin", "104": "sonarr"}}
    frame = sr.frame_run(node, rb, spec, policy, env={}, inventory=inv)
    assert frame["refused"] == [], [r["why"][:200] for r in frame["refused"]]
    assert frame["inputs"] == []
    assert "<JELLYFIN_API_KEY>" not in " ".join(frame["commands"]), "the engine filled the key"
    assert frame["verify"], "the check survived the read channel"


@pytest.mark.smoke
def test_a_hand_written_read_of_the_first_key_becomes_the_engines_read():
    """§17.1735 — live T3 after §17.1734: the drafter read `… FROM ApiKeys ORDER BY rowid LIMIT 1` (any service's)."""
    live = ("pct exec 101 -- sh -c 'read -r V; printf \"{\\\"Name\\\":\\\"mark\\\",\\\"Password\\\":\\\"%s\\\"}\" \"$V\" | "
            "curl -s --fail-with-body -X POST -H \"Content-Type: application/json\" -H \"X-Emby-Token: $(python3 -c "
            "'\"'\"'import sqlite3;print(sqlite3.connect(\"file:/var/lib/jellyfin/data/jellyfin.db?mode=ro\",uri=True)"
            ".execute(\"SELECT AccessToken FROM ApiKeys ORDER BY rowid LIMIT 1\").fetchone()[0])'\"'\"')\" "
            "--data-binary @- http://127.0.0.1:8096/Users/New' <<< \"$JELLYFIN_MARK_PASSWORD\"")
    cmds, _v, notes = machine_values.a_key_read_from_the_wrong_place_is_the_placeholder([live], [])
    assert '-H "X-Emby-Token: <JELLYFIN_API_KEY>"' in cmds[0] and "ORDER BY rowid LIMIT 1" not in cmds[0]
    assert cmds[0].endswith('<<< "$JELLYFIN_MARK_PASSWORD"')
    # after the reader fills it, the engine's own read (by name) is what runs -- and it is left alone
    filled, _v2, _n = machine_values.read_on_the_machine(cmds, [], {"names": {"101": "jellyfin"}})
    assert 'WHERE Name=?' in filled[0] and "scaffold-engine" in filled[0]
    again, _v3, notes2 = machine_values.a_key_read_from_the_wrong_place_is_the_placeholder(filled, [])
    assert again == filled and notes2 == []
