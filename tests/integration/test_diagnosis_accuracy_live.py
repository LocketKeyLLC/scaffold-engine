"""§17.1203 — is the diagnosis TRUE of the machine? Measured, not asserted.

The operator's question was accuracy, and the honest answer at §17.1202 was
"unknown until it deploys" — because the only machine to test against was their
production hypervisor. That is both risky and unrepeatable, so this stands up a
Proxmox-SHAPED one instead: `tests/integration/fixtures/fake_pve/` reproduces
the three behaviours that actually bit, and nothing else.

    qm set 106 --scsi0 local-lvm:100  →  storage 'local-lvm' does not exist
                                         400 Parameter verification failed.  (exit 2)
    pvesm status                      →  local · local-zfs · backup-nas
    (unprivileged)                    →  ipcc_send_rec … Unable to load access control list

The storages are deliberately NOT `local-lvm`. So there is a fact — `local-zfs`
— that a diagnosis can only state **by having looked at the machine**, which
makes accuracy measurable rather than a matter of opinion: the live diagnosis
said "the storage name may be different", and this asserts the engine now says
which.

Its own engine (:8004) and its own helper (:8794) over the real MCP transport,
against $TEST_DB_NAME. Needs Ollama (the fix model) and SearXNG for the
research pre-pass. ~3-6 minutes.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.database import async_session

pytestmark = [pytest.mark.asyncio, pytest.mark.integration, pytest.mark.timeout(900)]

ROOT = Path(__file__).resolve().parents[2]
FAKE_PVE = ROOT / "tests" / "integration" / "fixtures" / "fake_pve"
HELPER_PORT = 8794
RUNNER_NAME = "itest-pve-runner"
TOKEN = "itest-pve-" + uuid.uuid4().hex[:12]

FAILING = "qm set 106 --scsi0 local-lvm:100"


def _port_open(port: int, timeout: float = 1.0) -> bool:
    try:
        socket.create_connection(("127.0.0.1", port), timeout=timeout).close()
        return True
    except OSError:
        return False


@pytest.fixture(scope="module")
def pve_runner():
    """The helper with a Proxmox-shaped machine under it, trusting what is
    approved (`--write-allow ANY`) so the shape of the run matches the
    operator's."""
    assert not _port_open(HELPER_PORT), f"port {HELPER_PORT} is taken"
    env = {**os.environ, "PATH": f"{FAKE_PVE}:{os.environ.get('PATH', '')}"}
    log = open(os.environ.get("ITEST_LOG_DIR", "/tmp") + "/itest_pve_helper.log", "wb")
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "scripts" / "local_runner_mcp.py"), "--host", "127.0.0.1",
         "--port", str(HELPER_PORT), "--token", TOKEN, "--write-allow", "ANY"],
        stdout=log, stderr=subprocess.STDOUT, start_new_session=True, env=env,
    )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not _port_open(HELPER_PORT):
        time.sleep(1)
    assert _port_open(HELPER_PORT), "the helper never listened"
    try:
        yield proc
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()


@pytest_asyncio.fixture
async def registered(pve_runner, monkeypatch):
    from app.config import settings
    from app.modules import assist_supervised as sw
    from app.modules import mcp_client
    from app.modules.mcp_registry import McpServerSpec, delete_server, upsert_server
    monkeypatch.setattr(settings, "assist_local_runner_server", RUNNER_NAME)
    monkeypatch.setattr(settings, "mcp_tool_enabled", True)
    async with async_session() as db:
        await upsert_server(db, McpServerSpec(
            name=RUNNER_NAME, transport="streamable_http",
            endpoint=f"http://127.0.0.1:{HELPER_PORT}/mcp/",
            headers={"X-Runner-Token": TOKEN}, enabled=True,
            description="§17.1203 diagnosis-accuracy fixture — NOT the assist's tagged row"))
        await db.commit()
    mcp_client.clear_tool_cache(RUNNER_NAME)
    sw.clear_policy_cache(RUNNER_NAME)
    yield
    async with async_session() as db:
        await delete_server(db, RUNNER_NAME)
        await db.commit()


@pytest_asyncio.fixture
async def job(registered, tracked_jobs):
    jid = str(uuid.uuid4())
    tracked_jobs.append(jid)
    async with async_session() as db:
        await db.execute(text(
            "INSERT INTO jobs (id, title, status, input_text, job_type, refined_brief) "
            "VALUES (:j, '§17.1203 diagnosis accuracy', 'executing', 'x', 'legacy', CAST(:b AS jsonb))"),
            {"j": jid, "b": json.dumps({"description": "Grow VM 106's disk on a Proxmox host."})})
        await db.execute(text(
            "INSERT INTO dag_nodes (job_id, node_key, title, description, node_type, status, "
            "depends_on, execution_order, tool, prompt_template) "
            "VALUES (:j, 'D1', :t, :d, 'task', 'pending', '{}', 0, 'LLM', :d)"),
            {"j": jid, "t": "Grow VM 106's system disk to 100G",
             "d": f"On the Proxmox host, enlarge VM 106's disk.\n\nRun this:\n\n```bash\n{FAILING}\n```\n\n"
                  "Done when `qm config 106` shows the larger size."})
        await db.commit()
    return jid


async def test_the_runner_can_read_the_machine_under_trusted_mode(job):
    """§17.1202's fix, on a real transport: with `ANY` the READ tool is elevated
    too. Without it every check came back `ipcc_send_rec …` on a machine whose
    runner had full rights, and the engine could not see what it reasoned about."""
    from app.modules import assist_local_runner as lr
    async with async_session() as db:
        spec = await lr.runner_spec(db)
        assert spec is not None and spec.name == RUNNER_NAME
        pasted, _ = await lr.run_probes(spec, [{"id": "S1", "command": "pvesm status"}])
    assert "local-zfs" in pasted, pasted[:400]
    assert "ipcc_send_rec" not in pasted, "the read ran unprivileged"


async def test_the_state_probe_picks_the_right_question_from_the_failure(job):
    """`storage 'local-lvm' does not exist` must send the engine to `pvesm
    status` — the one read that turns the guess into a fact."""
    from app.modules import supervised_runs as sr
    async with async_session() as db:
        state = await sr._failure_state(db, [{
            "command": FAILING, "exit": 2, "ok": False, "informational": False,
            "output": "storage 'local-lvm' does not exist\n400 Parameter verification failed."}])
    assert "local-zfs" in state and "backup-nas" in state, state[:400]
    assert "local-lvm" not in state, "the fixture must not contain the name the plan guessed"


async def test_the_diagnosis_states_what_only_the_machine_could_tell_it(job):
    """THE accuracy measure. `local-zfs` appears nowhere in the plan, the step,
    the error, or the engine's own knowledge of this host — only in what
    `pvesm status` returned. A diagnosis that names it has looked; one that
    still says "the storage name may be different" has not."""
    from app.modules import supervised_runs as sr
    executed = [{"command": FAILING, "exit": 2, "ok": False, "informational": False, "refused": False,
                 "unreachable": False, "approval_id": "a",
                 "output": "storage 'local-lvm' does not exist\n400 Parameter verification failed."}]
    async with async_session() as db:
        out = await sr.diagnose_failure(db, job, "D1", executed, f"`{FAILING}` exited 2")
    assert out, "no diagnosis was produced"
    print("\n----- DIAGNOSIS -----\n" + out[:2500])
    real = [s for s in ("local-zfs", "backup-nas", "local ") if s in out]
    assert real, ("the diagnosis named no storage that exists on the machine — "
                  "it reasoned about a host it did not look at:\n" + out[:1200])
