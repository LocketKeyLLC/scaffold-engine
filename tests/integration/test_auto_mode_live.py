"""§17.1189 — Auto mode's own loop, end to end, as a test that stays green.

§17.1183–1188 shipped as five separate slices, each with unit tests. Nothing
drove the whole loop: a run that meets a decision node ASKS, a run that meets
a step which changes a machine ASKS, and an approved block is really carried
out ON a runner and verified. This does exactly that, against its OWN engine
(uvicorn on 127.0.0.1:8002) and its OWN helper (``scripts/local_runner_mcp.py``
on 127.0.0.1:8792, installed with a ``--write-allow`` list confined to
``/tmp/scaffold-auto-itest/``) — never the live engine, never the operator's
registered runner, and every write lands in this container's /tmp.

What it asserts (each is a claim the arc makes to the operator):
- a run that reaches a ``decision`` node parks in ``awaiting_decision``
  (kind ``decision``) instead of letting the model choose (§17.1184);
- ``POST /jobs/{id}/decide`` records the operator's words as that node's
  output and the run continues (§17.1184);
- the next step, which writes on a machine, parks with kind ``run`` carrying
  the runbook's commands, its checks, and the gate's verdict (§17.1186);
- choosing ``run`` really runs the commands THROUGH the runner, verifies, and
  marks the node done with an ``## Executed on`` report — the marker the
  commands create exists on disk afterwards (§17.1185/1186);
- a hands-on step whose commands are NOT on the runner's allow-list is not
  offered ``run`` — it is handed back as a runbook (the honest fallback).

Needs Postgres (the app's DATABASE_URL) and Ollama (the runbook drafter and
the decision framer). ~3–6 minutes.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text

from app.database import async_session

pytestmark = [pytest.mark.asyncio, pytest.mark.integration, pytest.mark.timeout(900)]

ROOT = Path(__file__).resolve().parents[2]
ENGINE_PORT = 8002
HELPER_PORT = 8792
RUNNER_NAME = "itest-auto-runner"
TOKEN = "itest-auto-" + uuid.uuid4().hex[:12]
AUTH = {"X-API-Key": os.environ.get("SCAFFOLD_API_KEY", "test-key-for-ci")}
BASE = f"http://127.0.0.1:{ENGINE_PORT}"

# Every write this test can make is under here, and the runner's allow-list
# says so. The steps name the path; the gate is what makes it true.
MARKER_DIR = "/tmp/scaffold-auto-itest"
WRITE_ALLOW = [f"mkdir -p {MARKER_DIR}/", f"tee {MARKER_DIR}/", f"touch {MARKER_DIR}/"]


def _port_open(port: int, timeout: float = 1.0) -> bool:
    try:
        socket.create_connection(("127.0.0.1", port), timeout=timeout).close()
        return True
    except OSError:
        return False


def _wait(pred, *, budget_s: float, what: str):
    deadline = time.monotonic() + budget_s
    last = None
    while time.monotonic() < deadline:
        last = pred()
        if last:
            return last
        time.sleep(2.0)
    raise AssertionError(f"timed out waiting for {what} (last={last!r})")


@pytest.fixture(scope="module")
def helper():
    """The helper as the operator would run it, WITH the write channel open."""
    assert not _port_open(HELPER_PORT), f"port {HELPER_PORT} is already taken"
    shutil.rmtree(MARKER_DIR, ignore_errors=True)
    log = open(os.environ.get("ITEST_LOG_DIR", "/tmp") + "/itest_auto_helper.log", "wb")
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "scripts" / "local_runner_mcp.py"), "--host", "127.0.0.1",
         "--port", str(HELPER_PORT), "--token", TOKEN, "--write-allow", *WRITE_ALLOW],
        stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
    )
    try:
        _wait(lambda: _port_open(HELPER_PORT), budget_s=30, what="the helper to listen")
        yield proc
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
        shutil.rmtree(MARKER_DIR, ignore_errors=True)


@pytest.fixture(scope="module")
def engine(helper):
    """A SECOND engine, pinned to this test's runner, with Auto mode's gates on."""
    assert not _port_open(ENGINE_PORT), f"port {ENGINE_PORT} is already taken"
    env = {**os.environ,
           "SCAFFOLD_RUN_MIGRATIONS_ON_STARTUP": "false", "LOG_FILE": "", "SCAFFOLD_PREWARM_RERANKER": "false",
           "ASSIST_LOCAL_RUNNER_SERVER": RUNNER_NAME,
           "MCP_TOOL_ENABLED": "true",
           "DECISION_PAUSE_ENABLED": "true",
           "EXECUTION_SUPERVISED_RUNS_ENABLED": "true",
           "SHELL_TOOL_ENABLED": "false",
           "PROMPT_OPTIMIZER_ENABLED": "false",       # the run must show the step's own words
           "OTEL_SDK_DISABLED": "true"}
    log = open(os.environ.get("ITEST_LOG_DIR", "/tmp") + "/itest_auto_engine.log", "wb")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(ENGINE_PORT),
         "--log-level", "warning"],
        cwd=str(ROOT), env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
    )

    def _healthy() -> bool:
        try:
            r = httpx.get(f"{BASE}/health", timeout=3.0)
            return r.status_code == 200 and r.json().get("status") == "healthy"
        except Exception:
            return False
    try:
        _wait(_healthy, budget_s=120, what="the test engine's /health")
        yield proc
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()


@pytest_asyncio.fixture
async def runner_row():
    from app.modules.mcp_registry import McpServerSpec, delete_server, upsert_server
    async with async_session() as db:
        await upsert_server(db, McpServerSpec(
            name=RUNNER_NAME, transport="streamable_http", endpoint=f"http://127.0.0.1:{HELPER_PORT}/mcp/",
            headers={"X-Runner-Token": TOKEN}, enabled=True,
            description="auto-mode integration test runner (§17.1189) — NOT the assist's tagged row"))
        await db.commit()
    yield
    async with async_session() as db:
        await delete_server(db, RUNNER_NAME)
        await db.commit()


# The plan: one decision, one step the engine may run, one it may not.
NODES = [
    ("D1", "decision", "Choose where the media library lives",
     "Decide whether the media library lives on the local NVMe pool or on the NAS share. "
     "Both are available; the choice changes the mount the later steps use.", []),
    ("H1", "task", f"Create the media library staging directory at {MARKER_DIR}/stage1",
     f"On the host, create the staging directory the library will use.\n\n"
     f"Run this:\n\n```bash\nmkdir -p {MARKER_DIR}/stage1\n```\n\n"
     f"Done when `ls -ld {MARKER_DIR}/stage1` prints the directory.", ["D1"]),
    ("H2", "task", "Restart the media server service so it picks up the new mount",
     "On the host, restart the media service.\n\n"
     "Run this:\n\n```bash\nsystemctl restart scaffold-itest-media.service\n```\n\n"
     "Done when `systemctl is-active scaffold-itest-media.service` reports active.", ["H1"]),
]


@pytest_asyncio.fixture
async def job(engine, runner_row, tracked_jobs):
    jid = str(uuid.uuid4())
    tracked_jobs.append(jid)
    async with async_session() as db:
        await db.execute(text(
            "INSERT INTO jobs (id, title, status, input_text, job_type, refined_brief) "
            "VALUES (:j, '§17.1189 auto mode loop', 'executing', :i, 'legacy', CAST(:b AS jsonb))"),
            {"j": jid, "i": "auto mode loop", "b": json.dumps(
                {"description": f"A small home-lab build whose writes all land under {MARKER_DIR}."})})
        for i, (k, kind, title, desc, deps) in enumerate(NODES):
            await db.execute(text(
                "INSERT INTO dag_nodes (job_id, node_key, title, description, node_type, status, depends_on, "
                "execution_order, tool, prompt_template) "
                "VALUES (:j, :k, :t, :d, :nt, 'pending', :deps, :o, 'LLM', :d)"),
                {"j": jid, "k": k, "t": title, "d": desc, "nt": kind, "deps": deps, "o": i})
        await db.commit()
    return jid


async def _start_run(jid: str) -> None:
    """Fire /execute/all and drop the subscription — the run is detached."""
    async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10)) as c:
        try:
            async with c.stream("POST", f"{BASE}/execute/all", headers=AUTH, json={"job_id": jid}) as r:
                assert r.status_code == 200, await r.aread()
                async for _ in r.aiter_lines():
                    break
        except (httpx.ReadTimeout, httpx.RemoteProtocolError):
            pass


async def _job(jid: str) -> dict:
    async with httpx.AsyncClient(timeout=20.0) as c:
        r = await c.get(f"{BASE}/jobs/{jid}", headers=AUTH)
        assert r.status_code == 200, r.text
        return r.json()


async def _await_pause(jid: str, node_key: str, *, budget_s: float = 300) -> dict:
    """The job's awaiting_decision frame for this node."""
    deadline = time.monotonic() + budget_s
    seen = None
    while time.monotonic() < deadline:
        j = await _job(jid)
        seen = j.get("status")
        asked = ((j.get("metadata") or {}).get("awaiting_decision") or {})
        if seen == "awaiting_decision" and asked.get("node_key") == node_key:
            return asked
        if seen in ("failed", "completed", "cancelled"):
            raise AssertionError(f"job reached {seen} without asking about {node_key}")
        await asyncio.sleep(2.0)
    raise AssertionError(f"no pause on {node_key} within {budget_s}s (status={seen})")


async def _decide(jid: str, body: dict) -> httpx.Response:
    async with httpx.AsyncClient(timeout=300.0) as c:
        return await c.post(f"{BASE}/jobs/{jid}/decide", headers=AUTH, json=body)


async def _node(jid: str, key: str) -> dict:
    async with async_session() as db:
        row = (await db.execute(text(
            "SELECT status, output_text, last_verification_reason FROM dag_nodes WHERE job_id=:j AND node_key=:k"),
            {"j": jid, "k": key})).mappings().first()
    return dict(row or {})


async def test_auto_mode_asks_then_runs(job):
    jid = job

    # ── 1. a decision node stops the run and asks ───────────────────────
    await _start_run(jid)
    asked = await _await_pause(jid, "D1")
    assert asked.get("kind") != "run", f"D1 is a decision node, not a run pause: {asked.get('kind')}"
    assert asked.get("question"), "the pause carried no question"
    assert len(asked.get("options") or []) >= 2, f"a decision needs options: {asked.get('options')}"

    # ── 2. the operator's words become the node's output ────────────────
    r = await _decide(jid, {"node_key": "D1", "choice": "The local NVMe pool",
                            "note": "the NAS is for backups only"})
    assert r.status_code == 200, r.text
    assert r.json()["resolved"] == "operator"
    d1 = await _node(jid, "D1")
    assert d1["status"] == "done", d1
    assert "Decision (operator): The local NVMe pool" in (d1["output_text"] or ""), d1["output_text"]

    # ── 3. the next step writes on a machine → a run pause with a block ─
    run_ask = await _await_pause(jid, "H1")
    assert run_ask.get("kind") == "run", run_ask
    assert run_ask.get("runner") == RUNNER_NAME
    assert run_ask.get("commands"), "the run pause carried no commands"
    assert not run_ask.get("refused"), f"the gate refused an allow-listed block: {run_ask.get('refused')}"
    assert "run" in [o["id"] for o in run_ask["options"]], run_ask["options"]
    assert run_ask["suggested"] == "run", run_ask["suggested"]
    for i in run_ask.get("inputs") or []:          # §17.1188 — asked values carry what the engine knows
        assert "suggestions" in i, i

    # ── 4. approving it RUNS it on the runner and verifies ──────────────
    values = {i["name"]: (i.get("value") or "stage1") for i in (run_ask.get("inputs") or [])}
    r = await _decide(jid, {"node_key": "H1", "choice": "run", "inputs": values or None})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["outcome"] == "ran", body
    h1 = await _node(jid, "H1")
    assert h1["status"] == "done", h1
    assert "## Executed on" in (h1["output_text"] or ""), h1["output_text"][:400]
    assert RUNNER_NAME in (h1["last_verification_reason"] or ""), h1["last_verification_reason"]
    assert os.path.isdir(f"{MARKER_DIR}/stage1"), \
        f"the approved command did not actually run: {MARKER_DIR}/stage1 is missing"

    # ── 5. a step outside the allow-list is NOT offered 'run' ───────────
    ask2 = await _await_pause(jid, "H2")
    assert ask2.get("kind") == "run", ask2
    assert ask2["suggested"] == "myself", ask2["suggested"]
    assert "run" not in [o["id"] for o in ask2["options"]], ask2["options"]
    assert ask2.get("refused"), "the gate passed a command that is not on the allow-list"
    r = await _decide(jid, {"node_key": "H2", "choice": "run"})
    assert r.status_code == 409, r.text          # the engine refuses to run what it refused to offer
    r = await _decide(jid, {"node_key": "H2", "choice": "myself"})
    assert r.status_code == 200 and r.json()["outcome"] == "runbook", r.text
    h2 = await _node(jid, "H2")
    assert h2["status"] == "done" and "systemctl restart" in (h2["output_text"] or ""), h2
