"""§17.2051 — the pre-approval "already met?" check is never composed into a run the read path must refuse.

Live (2026-11-26, job 0fb4ea7f, every redeploy): before RD80 was parked, §17.1302's `already_met` sent
`GUEST_USER="<AI_USER>" bash /tmp/in_vm_110.sh check 'curl -s http://localhost:8000/health' # $MASS_PASSWORD`
to the runner. `<AI_USER>` is answered with the approval; `$MASS_PASSWORD` is held by the ENGINE and handed to the
runner only with an approved run (§17.1193). The runner refused ("this command needs $MASS_PASSW..."), and the engine
logged `supervised_run_already_met_unreadable` -- so it could never learn a step was already done.

These drive the PRODUCTION `already_met` -> `run_verify` with only the runner's two tools faked (`run_probes`, the
read path; `run_block`, the supervised path) and the judge: a check only the approval can make runnable is not sent
at all and the outcome is the known "needs approval" one; checks the runner can answer now still run and still prove
a step met.
"""
from __future__ import annotations

import inspect
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules import execution_agent as ea
from app.modules import supervised_runs as sr

RUN_LINE = 'GUEST_USER="<AI_USER>" bash /tmp/in_vm_110.sh start # $MASS_PASSWORD'
LIVE_CHECK = "GUEST_USER=\"<AI_USER>\" bash /tmp/in_vm_110.sh check 'curl -s http://localhost:8000/health' # $MASS_PASSWORD"
NODE = {"node_key": "RD80", "title": "Redeploy the engine in VM 110 at the merged commit",
        "description": "Pull the merged commit and rebuild the orchestrator inside VM 110. "
                       "Done when `curl -s http://localhost:8000/health` answers status ok."}
# the runner holds ARR_KEY itself; the engine holds MASS_PASSWORD and DB_TOKEN (sent only with an approved run)
POLICY = {"allow": ["ANY"], "secrets": ["ARR_KEY"], "held": ["MASS_PASSWORD", "DB_TOKEN"], "helper": "24"}


def _spec():
    s = MagicMock()
    s.name = "lab-runner"
    return s


def _judge(kind="confirmed"):
    async def fake(claim, cmds, pasted, expects=None):
        return [{"id": f"V{i}", "verdict": kind, "reason": "the health answer says ok", "claim": claim}
                for i, _ in enumerate(cmds, 1)]
    return fake


async def _already_met(frame, node=NODE, policy=POLICY):
    probes = AsyncMock(side_effect=lambda spec, ps: (
        "".join(f"== {p['id']} ==\n{{\"status\": \"ok\"}}\n" for p in ps),
        [{"id": p["id"], "command": p["command"], "ok": True, "ran": True} for p in ps]))
    block = AsyncMock(side_effect=lambda spec, cmds, env=None: [
        {"command": c, "output": '{"status": "ok"}', "exit": 0, "ok": True} for c in cmds])
    with patch("app.modules.assist_local_runner.run_probes", probes), \
         patch("app.modules.assist_supervised.run_block", block), \
         patch.object(sr, "_verify_verdicts", _judge()):
        met = await sr.already_met(_spec(), node, frame, {}, policy=policy)
    return met, probes, block


@pytest.mark.asyncio
@pytest.mark.parametrize("verify, needs", [
    ([LIVE_CHECK], "<AI_USER>"),                                                       # the live RD80 check
    (["bash /tmp/in_vm_110.sh check 'cat /opt/scaffold-engine/VERSION' # $MASS_PASSWORD"], "$MASS_PASSWORD"),
    (["curl -s -H \"Authorization: Bearer $DB_TOKEN\" http://localhost:8000/health"], "$DB_TOKEN"),
    (["curl -s http://<ENGINE_HOST>:8000/health"], "<ENGINE_HOST>"),
])
async def test_a_check_only_the_approval_can_run_is_not_sent(caplog, verify, needs):
    frame = {"commands": [RUN_LINE], "verify": verify, "files": []}
    with caplog.at_level(logging.INFO, logger="scaffold"):
        met, probes, block = await _already_met(frame)
    assert met is None
    probes.assert_not_awaited()
    block.assert_not_awaited()          # nothing reached the runner to be refused
    msgs = [r.getMessage() for r in caplog.records]
    hit = [m for m in msgs if "supervised_run_already_met_needs_approval" in m]
    assert len(hit) == 1 and needs in hit[0] and "node=RD80" in hit[0], msgs
    assert not any("already_met_unreadable" in m for m in msgs), msgs


@pytest.mark.asyncio
@pytest.mark.parametrize("verify, path", [
    (["curl -s http://localhost:8000/health"], "probes"),                               # plain: the read path
    (["curl -s -H \"X-Api-Key: $ARR_KEY\" http://localhost:8989/api/v3/health"], "block"),  # the runner holds it
    (["for F in /opt/a /opt/b; do test -s \"$F\" && echo present; done"], "block"),     # a name the check binds
])
async def test_a_check_the_runner_can_answer_now_still_proves_a_step_met(verify, path):
    node = {"node_key": "RD81", "title": "Read the service health", "description": "Restart the service."}
    frame = {"commands": ["systemctl restart arrsvc"], "verify": verify, "files": []}
    met, probes, block = await _already_met(frame, node=node)
    assert met and met["checks"] == 1, met
    assert (probes if path == "probes" else block).await_count == 1


def test_the_rule_reads_the_runner_and_the_approval_correctly():
    assert sr.checks_need_approval(["cat /etc/hostname"], POLICY) == ""
    assert sr.checks_need_approval(['curl -H "X-Api-Key: $ARR_KEY" http://localhost:1/x'], POLICY) == ""
    assert "$MASS_PASSWORD" in sr.checks_need_approval(["true # $MASS_PASSWORD"], POLICY)
    assert "$ARR_KEY" in sr.checks_need_approval(['curl -H "X-Api-Key: $ARR_KEY" http://localhost:1/x'], None), \
        "with no policy nothing is known to be runner-held"
    assert "<AI_USER>" in sr.checks_need_approval([LIVE_CHECK], POLICY)


def test_the_executor_hands_already_met_the_runners_policy():
    src = inspect.getsource(ea)
    assert "supervised_runs.already_met(spec, run_node, frame, _env, policy=policy)" in src
