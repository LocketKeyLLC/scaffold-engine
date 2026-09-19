"""§17.1119 — Phase 1 ledger S-6..S-9, the hygiene set.

S-6 the sim pipeline's status setter refuses every terminal status (not only
cancelled); S-7 a node claim heartbeats jobs.updated_at so a long run is not
reap-eligible between claims; S-8 an assist reopen never resets a node the
executor is running; S-9 the dead 'blocked' node literal is gone from code
and the documented flow matches the code.
"""
from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


# ── S-6 ──────────────────────────────────────────────────────────────────────

def test_design_pipeline_status_setter_uses_the_transition_function():
    src = _src("app/sim/design_pipeline.py")
    assert "from app.modules.job_state import transition" in src
    body = src[src.index("async def _set_job_status"):src.index("async def _job_was_cancelled")]
    assert "await transition(" in body and "UPDATE jobs" not in body


# ── S-7 ──────────────────────────────────────────────────────────────────────

async def test_touch_only_bumps_a_job_that_is_mid_run():
    from app.modules.job_state import touch
    seen = {}

    class Db:
        async def execute(self, stmt, params=None):
            seen["sql"] = " ".join(str(stmt).split()); seen["params"] = params
            r = MagicMock(); r.first.return_value = ("x",); return r

    assert await touch(Db(), "00000000-0000-0000-0000-0000000000aa") is True
    assert "SET updated_at = NOW()" in seen["sql"]
    assert "status IN ('running', 'executing')" in seen["sql"], "a finished job must not be touched"
    assert "status =" not in seen["sql"].replace("status IN", ""), "the heartbeat is not a status write"


def test_both_claim_paths_heartbeat():
    src = _src("app/modules/execution_agent.py")
    assert src.count('await touch(db, job_id, reason="node_claim")') == 2, "serial claim AND parallel frontier"
    assert "touch, transition" in src or "touch," in src.split("from app.modules.job_state import")[1].split("\n")[0]


# ── S-8 ──────────────────────────────────────────────────────────────────────

def test_assist_reopen_never_resets_a_running_node():
    src = _src("app/modules/assist_agent.py")
    assert "WHERE job_id = :jid AND status NOT IN ('pending', 'running')" in src, "session-wide reopen reset"
    assert "WHERE job_id=:jid AND node_key=:nk AND status <> 'running'" in src, "by-key reopen reset"
    assert "WHERE job_id = :jid AND status <> 'pending'\n" not in src


# ── S-9 ──────────────────────────────────────────────────────────────────────

_DEAD = re.compile(r"""\(\s*['"]pending['"]\s*,\s*['"]blocked['"]\s*\)""")


def test_no_code_treats_blocked_as_a_node_status():
    offenders = []
    for rel in ("app/modules/plan_reconcile.py", "app/modules/execution_agent.py", "app/modules/assist_agent.py",
                "app/modules/assist_replan.py", "app/modules/node_editor.py"):
        p = ROOT / rel
        if not p.exists():
            continue
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if _DEAD.search(line) or '_non_terminal = {"failed", "blocked"' in line:
                offenders.append(f"{rel}:{i}")
    assert not offenders, f"dag_nodes.status has no 'blocked' (db/init.sql CHECK): {offenders}"
    from app.modules.job_state import NODE_STATUSES
    assert "blocked" not in NODE_STATUSES


def test_documented_flow_matches_the_code():
    ov = _src("OVERVIEW.md")
    assert "pending → refining" not in ov, "no job is ever written at 'pending'"
    assert "`blocked` is NOT terminal" in ov or "`blocked` re-enterable" in ov
    assert "no node `blocked`" in ov or "nodes have no `blocked`" in ov
