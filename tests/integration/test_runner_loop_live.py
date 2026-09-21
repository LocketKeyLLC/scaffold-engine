"""§17.1153 — the local-runner loop, end to end, as a test that stays green.

Every proof for §17.1147–1152 was a hand-driven second engine + a runner
container + curl. This turns that recipe into a test: it starts its OWN
engine (a uvicorn subprocess on 127.0.0.1:8001, pinned by env to its own
runner name so it never touches the operator's registered runner) and its
OWN helper (``scripts/local_runner_mcp.py`` on 127.0.0.1:8791), seeds a job
+ assist session, and drives ``/assist/{sid}/message`` over HTTP the way the
SPA does. Nothing here reads the live engine's settings or the operator's
runner row; the job and the registry row are deleted at teardown.

What it asserts (each was a live defect this week):
- the verify step is probed BY THE ENGINE and renders ✅ when the helper is
  reachable and current (§17.1147/1151);
- a token mismatch becomes a REPAIR STEP inserted before the verify step,
  guided deterministically (§17.1149); its commit re-probes and RE-POINTS to
  the verify step (§17.1148/1152), which then commits on ✓ Done;
- a read-only "Run this now" block is run THROUGH THE RUNNER and recorded as
  a ``[local-runner]`` operator turn (§17.1150);
- Verify state runs EVERY batch through the runner and asks for no paste
  (§17.1152);
- the helper's gate refuses a write and a quoted `|` parses (§17.1151/1152).

Needs Postgres (the app's DATABASE_URL) and Ollama (the state-check judge,
the decide layer). ~3–5 minutes.
"""
from __future__ import annotations

import asyncio
import json
import os
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
ENGINE_PORT = 8001
HELPER_PORT = 8791
RUNNER_NAME = "itest-runner"
TOKEN = "itest-" + uuid.uuid4().hex[:12]
AUTH = {"X-API-Key": os.environ.get("SCAFFOLD_API_KEY", "test-key-for-ci")}
BASE = f"http://127.0.0.1:{ENGINE_PORT}"


def _port_open(port: int, timeout: float = 1.0) -> bool:
    try:
        socket.create_connection(("127.0.0.1", port), timeout=timeout).close()
        return True
    except OSError:
        return False


def _wait(pred, *, budget_s: float, what: str) -> None:
    deadline = time.monotonic() + budget_s
    while time.monotonic() < deadline:
        if pred():
            return
        time.sleep(1.0)
    raise AssertionError(f"timed out waiting for {what}")


@pytest.fixture(scope="module")
def helper():
    """The helper script, as the operator would run it (minus the installer)."""
    assert not _port_open(HELPER_PORT), f"port {HELPER_PORT} is already taken"
    log = open(os.environ.get("ITEST_LOG_DIR", "/tmp") + "/itest_helper.log", "wb")
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "scripts" / "local_runner_mcp.py"), "--host", "127.0.0.1",
         "--port", str(HELPER_PORT), "--token", TOKEN],
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


@pytest.fixture(scope="module")
def engine(helper):
    """A SECOND engine, pinned to the test runner by env. Same database as
    the test process; its own settings; nothing shared with the live one."""
    assert not _port_open(ENGINE_PORT), f"port {ENGINE_PORT} is already taken"
    env = {**os.environ,
           "SCAFFOLD_RUN_MIGRATIONS_ON_STARTUP": "false", "LOG_FILE": "", "SCAFFOLD_PREWARM_RERANKER": "false",
           "ASSIST_LOCAL_RUNNER_SERVER": RUNNER_NAME,
           "ASSIST_BLOCK_ON_INCOMPLETE_VERIFY": "true", "ASSIST_BLOCK_ON_FAILED_VERIFY": "true",
           "ASSIST_BLOCK_ON_UNCLEAR_WHEN_UNSURE": "true",
           "ASSIST_STATE_CHECK_MAX_PROBES": "4",       # so an 8-probe check needs TWO batches
           # the live compose turns these on by default (docker-compose.yml); the code defaults are off
           "ASSIST_UNIFIED_MEMORY_ENABLED": "true", "ASSIST_UMEM_CAPTURE": "true",
           "OTEL_SDK_DISABLED": "true"}
    log = open(os.environ.get("ITEST_LOG_DIR", "/tmp") + "/itest_engine.log", "wb")
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


async def _register(endpoint: str, token: str) -> None:
    from app.modules.mcp_registry import McpServerSpec, upsert_server
    async with async_session() as db:
        spec = McpServerSpec(name=RUNNER_NAME, transport="streamable_http", endpoint=endpoint,
                             headers={"X-Runner-Token": token}, enabled=True,
                             description="integration test runner (§17.1153) — NOT the assist's tagged row")
        await upsert_server(db, spec)
        await db.commit()


@pytest_asyncio.fixture
async def runner_row():
    await _register(f"http://127.0.0.1:{HELPER_PORT}/mcp/", TOKEN)
    yield
    from app.modules.mcp_registry import delete_server
    async with async_session() as db:
        await delete_server(db, RUNNER_NAME)
        await db.commit()


COMMITTED = [
    ("T3", "Confirm the OS release", "root@host:~# cat /etc/os-release | head -1\nPRETTY_NAME=\"Debian GNU/Linux 12 (bookworm)\""),
    ("T4", "Confirm /tmp is writable", "root@host:~# ls -ld /tmp\ndrwxrwxrwt 1 root root 4096 Sep 20 22:00 /tmp"),
    ("T5", "Confirm python3 is installed", "root@host:~# python3 --version\nPython 3.14.0"),
    ("T6", "Confirm the helper source is present", f"root@host:~# ls {ROOT}/scripts/local_runner_mcp.py\n{ROOT}/scripts/local_runner_mcp.py"),
    ("T7", "Confirm the helper is listening", f"root@host:~# ss -tlnp | grep {HELPER_PORT}\nLISTEN 0 2048 127.0.0.1:{HELPER_PORT} 0.0.0.0:* users:((\"python\",pid=1,fd=6))"),
    ("T8", "Confirm the hostname resolves", "root@host:~# hostname\nsomehost"),
    ("T9", "Confirm the working directory exists", f"root@host:~# ls -d {ROOT}\n{ROOT}"),
    ("T10", "Confirm the process list is readable", "root@host:~# ps -eo comm | head -1\nCOMMAND"),
]


@pytest_asyncio.fixture
async def session(engine, runner_row, tracked_jobs):
    """A job whose plan holds the recipe's two steps (install committed,
    verify pending), eight committed read-only steps for the state check,
    and one plain pending step after them."""
    from app.modules import assist_agent, engine_setup as es
    jid = str(uuid.uuid4())
    tracked_jobs.append(jid)
    ctx = {**es._UNKNOWN, "target_ip": "127.0.0.1", "target_host": "localhost", "target_user": "root", "token": TOKEN}
    steps = es.recipe_steps(es.BY_ID["local_runner"], ctx=ctx)
    async with async_session() as db:
        await db.execute(text("INSERT INTO jobs (id, title, status, input_text, job_type) VALUES (:j, '§17.1153 runner loop', 'awaiting_assist', 'runner loop', 'legacy')"), {"j": jid})
        nodes = [("ADD1", steps[0]["title"], steps[0]["description"], "done", []),
                 ("ADD2", steps[1]["title"], steps[1]["description"], "pending", ["ADD1"])]
        prev = "ADD2"
        for k, t, _ev in COMMITTED:
            nodes.append((k, t, t, "done", [prev])); prev = k
        nodes.append(("T11", "Make VM 110 (ai-vm) reachable on SSH port 22", "Done when ssh to VM 110 from the host works.", "pending", [prev]))
        for i, (k, t, d, st, deps) in enumerate(nodes):
            await db.execute(text("INSERT INTO dag_nodes (job_id,node_key,title,description,node_type,status,depends_on,execution_order,tool,prompt_template) VALUES (:j,:k,:t,:d,'task',:st,:deps,:o,'shell',:d)"),
                             {"j": jid, "k": k, "t": t, "d": d, "st": st, "deps": deps, "o": i})
        await db.commit()
        sess = await assist_agent.start_assist_session(job_id=jid, db=db)
        sid = sess.get("session_id") or sess.get("id")
        await db.execute(text("INSERT INTO assist_steps (session_id, job_id, node_key, status, committed_at, submitted_at, presented_at, evidence, evidence_kind) VALUES (:s,:j,'ADD1','committed',NOW(),NOW(),NOW(),'OK: local runner active','text') ON CONFLICT DO NOTHING"), {"s": sid, "j": jid})
        for k, _t, ev in COMMITTED:
            await db.execute(text("INSERT INTO assist_steps (session_id, job_id, node_key, status, committed_at, submitted_at, presented_at, evidence, evidence_kind) VALUES (:s,:j,:k,'committed',NOW(),NOW(),NOW(),:e,'text') ON CONFLICT DO NOTHING"), {"s": sid, "j": jid, "k": k, "e": ev})
        await db.execute(text("UPDATE assist_sessions SET current_node_key='ADD2', metadata = COALESCE(metadata,'{}'::jsonb) || CAST(:m AS jsonb) WHERE id=:s"),
                         {"s": sid, "m": json.dumps({"environment": {"profile": "Operator runs commands as root@localhost in ONE interactive shell.",
                                                                     "system_state": {"host": {"attrs": {"ip": "127.0.0.1"}},
                                                                                      # §17.1154 — a guest the plan names, for the reachability check
                                                                                      "110": {"kind": "vm", "attrs": {"name": "ai-vm", "boot": "order=scsi0;ide2", "status": "running"},
                                                                                              "devices": {"net0": "virtio=BC:24:11:B4:AF:15,bridge=vmbr0",
                                                                                                          "ide2": "local:iso/ubuntu-22.04.3-live-server-amd64.iso,media=cdrom"}}}}})})
        await db.commit()
    return {"job_id": jid, "session_id": sid}


async def _turn(sid: str, body: dict, *, budget_s: float = 420) -> list[tuple[str, dict]]:
    """POST /assist/{sid}/message and collect the SSE frames as (event, data)."""
    frames: list[tuple[str, dict]] = []
    ev = None
    async with httpx.AsyncClient(timeout=httpx.Timeout(budget_s, connect=10)) as client:
        async with client.stream("POST", f"{BASE}/assist/{sid}/message", headers=AUTH, json=body) as resp:
            assert resp.status_code == 200, await resp.aread()
            async for line in resp.aiter_lines():
                if line.startswith("event:"):
                    ev = line[6:].strip()
                elif line.startswith("data:") and ev:
                    try:
                        frames.append((ev, json.loads(line[5:])))
                    except json.JSONDecodeError:
                        pass
    return frames


def _guide_text(frames) -> str:
    return "".join(d.get("text", "") for e, d in frames if e == "assist_guide_delta")


def _done_nodes(frames) -> list[str | None]:
    return [d.get("node_key") for e, d in frames if e == "assist_guide_done"]


async def _steps(sid: str) -> dict[str, str]:
    async with async_session() as db:
        rows = (await db.execute(text("SELECT node_key, status FROM assist_steps WHERE session_id = :s"), {"s": sid})).all()
        return {r[0]: r[1] for r in rows}


async def _pointer(sid: str) -> str | None:
    async with async_session() as db:
        return (await db.execute(text("SELECT current_node_key FROM assist_sessions WHERE id = :s"), {"s": sid})).scalar()


async def test_runner_loop_end_to_end(session, helper):
    sid = session["session_id"]
    from app.modules import engine_setup as es

    # ── 1. the verify step is checked BY THE ENGINE; a reachable, current helper → ✅ ──
    frames = await _turn(sid, {"command": "guide"})
    assert _done_nodes(frames)[-1] == "ADD2", frames[-3:]
    txt = _guide_text(frames)
    assert "## ✅ The engine reached your runner" in txt and "run_readonly" in txt
    meta = next(d["guidance_meta"] for e, d in frames if e == "assist_guide_done")
    assert meta.get("probe", {}).get("ok") is True and meta["probe"]["class"] == "ok"
    assert (await _steps(sid))["ADD2"] == "presented" and (await _pointer(sid)) == "ADD2"
    assert "ADD3" not in await _steps(sid)                       # nothing to repair → nothing inserted

    # ── 2. a token mismatch → a REPAIR STEP before the verify step, guided deterministically ──
    await _register(f"http://127.0.0.1:{HELPER_PORT}/mcp/", "wrong-token")
    frames = await _turn(sid, {"command": "guide"})
    done = _done_nodes(frames)
    assert done[-1] == "ADD3", done                                # the guide DIVERTED to the repair step
    txt = _guide_text(frames)
    assert "🔧 I checked the runner from the engine host" in txt and "rejected the token" in txt
    assert "## 👉 Do this next" in txt and "--install" in txt and "--token" in txt
    meta = next(d["guidance_meta"] for e, d in frames if e == "assist_guide_done" and d.get("node_key") == "ADD3")
    assert meta.get("deterministic") is True and meta.get("repair") == "token"
    st = await _steps(sid)
    assert st["ADD3"] == "presented" and st["ADD2"] == "pending" and (await _pointer(sid)) == "ADD3"
    # a second Guide press finds the OPEN repair step — never a duplicate
    frames = await _turn(sid, {"command": "guide"})
    assert _done_nodes(frames)[-1] == "ADD3" and "ADD4" not in await _steps(sid)
    # the install line is NOT a look-up (curl -o to disk writes) — the runner ran nothing for it
    assert not any(e == "assist_answer" and d.get("kind") == "note" and "ran that look-up" in d.get("text", "") for e, d in frames)

    # ── 3. the operator "re-runs the install": the token matches again; ✓ Done on the repair step
    #       re-probes → commits → RE-POINTS to the verify step (not the earliest claimable step) ──
    await _register(f"http://127.0.0.1:{HELPER_PORT}/mcp/", TOKEN)
    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(f"{BASE}/assist/{sid}/submit", headers=AUTH,
                              json={"node_key": "ADD3", "output": "OK: local runner active on 0.0.0.0:8791/mcp/", "action": "submit", "history": []})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "committed" and body["next_node_key"] == "ADD2", body
    assert body["success_verdict"]["recipe"] == "local_runner" and body["success_verdict"]["probe_class"] == "ok"
    st = await _steps(sid)
    assert st["ADD3"] == "committed" and st["ADD2"] == "presented" and (await _pointer(sid)) == "ADD2"

    # ── 4. the verify step renders ✅ again and commits on ✓ Done (recipe verdict, no model) ──
    frames = await _turn(sid, {"command": "guide"})
    assert _done_nodes(frames)[-1] == "ADD2" and "## ✅ The engine reached your runner" in _guide_text(frames)
    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(f"{BASE}/assist/{sid}/submit", headers=AUTH,
                              json={"node_key": "ADD2", "output": "done", "action": "submit", "history": []})
    assert r.status_code == 200 and r.json()["status"] == "committed", r.text
    assert (await _steps(sid))["ADD2"] == "committed"

    # ── 5. Verify state runs EVERY batch through the runner and asks for no paste ──
    helper_log_before = Path(os.environ.get("ITEST_LOG_DIR", "/tmp") + "/itest_helper.log").read_text(errors="replace").count("RUN:")
    frames = await _turn(sid, {"command": "verify_state"}, budget_s=600)
    statuses = [d.get("text", "") for e, d in frames if e == "assist_turn_status"]
    assert any("read-only checks through your local runner" in s for s in statuses), statuses[:6]
    assert any(s.startswith("🩺 Batch 2:") for s in statuses), statuses                     # 8 probes, cap 4 → two batches
    answers = [d["text"] for e, d in frames if e == "assist_answer" and d.get("kind") == "ask"]
    assert answers and "State check result" in answers[-1], answers[-1][:200]
    assert 'echo "== ' not in answers[-1] and "Paste the output back" not in answers[-1]   # no script handed to the operator
    ran = Path(os.environ.get("ITEST_LOG_DIR", "/tmp") + "/itest_helper.log").read_text(errors="replace").count("RUN:") - helper_log_before
    assert ran >= 8, ran
    async with async_session() as db:
        pend = (await db.execute(text("SELECT metadata ? 'pending_state_check' FROM assist_sessions WHERE id = :s"), {"s": sid})).scalar()
    assert pend is False                                                                     # nothing left pending

    # ── 6. a read-only "Run this now" block is run THROUGH THE RUNNER and recorded ──
    #      (a port_closed repair step carries one: systemctl status…; journalctl…; ss -tlnp | grep <port>)
    from app.modules import assist_notes
    diag = {"class": "port_closed", "detail": "refused", "checks": {"host": "127.0.0.1", "port": HELPER_PORT}}
    step = es.repair_step(es.BY_ID["local_runner"], {"ok": False, **diag, "repair": es.runner_repair_block(diag)})
    async with async_session() as db:
        res = await assist_notes.add_step(session_id=sid, request=step["title"], before_node_key="T11", steps=[step], db=db)
        await es._present(db, sid, res["node_key"])
    rk = res["node_key"]
    helper_log_before = Path(os.environ.get("ITEST_LOG_DIR", "/tmp") + "/itest_helper.log").read_text(errors="replace").count("RUN:")
    frames = await _turn(sid, {"command": "guide"})
    assert _done_nodes(frames)[0] == rk
    notes = [d["text"] for e, d in frames if e == "assist_answer" and d.get("kind") == "note"]
    assert any(n.startswith("🔁 Your local runner is connected, so I ran that look-up myself") for n in notes), [n[:80] for n in notes]
    assert any(e == "assist_turn_status" and "running that read-only look-up myself" in d.get("text", "") for e, d in frames)
    assert Path(os.environ.get("ITEST_LOG_DIR", "/tmp") + "/itest_helper.log").read_text(errors="replace").count("RUN:") >= helper_log_before + 1   # the block is ONE line (`a; b; c | d`) → one RUN
    async with async_session() as db:
        n_rec = (await db.execute(text("SELECT count(*) FROM assist_turns WHERE session_id = :s AND role = 'operator' AND content LIKE '[local-runner] ran the walkthrough%'"), {"s": sid})).scalar()
    assert n_rec >= 1
    handled = next(d for e, d in frames if e == "assist_turn_done")["handled"]
    assert "+lookup" in handled, handled
    # the runner is reachable, so the re-probe on that paste passes: the repair commits and the pointer returns to T11
    # (only when the decide layer routed the record as a submit — a model call; assert the deterministic outcome if it did)
    st = await _steps(sid)
    if st[rk] == "committed":
        assert (await _pointer(sid)) == "T11"

    # ── 7. the helper's gate: a quoted `|` parses, a write is refused, what runs inside a guest is judged ──
    from app.modules.mcp_registry import get_server
    from app.modules import mcp_client
    async with async_session() as db:
        spec = await get_server(db, RUNNER_NAME)
    out = await mcp_client.call_tool(spec, "run_readonly", {"command": "cat /etc/os-release | grep -E 'NAME|VERSION' | head -1", "timeout_s": 10})
    assert "NAME" in (out.structured or {}).get("result", out.text)
    out = await mcp_client.call_tool(spec, "run_readonly", {"command": "sh -c 'ls /tmp && rm -rf /tmp/nope'", "timeout_s": 10})
    assert "refused by the local runner" in (out.structured or {}).get("result", out.text)
    tools = await mcp_client.list_tools(spec, use_cache=False)
    assert f"(helper v{es.expected_helper_version()})" in tools[0]["description"]

    # ── 8. an ssh failure to a guest is answered FROM THE HOST through the runner, before any model fix (§17.1154) ──
    #      (this box has no `qm`, so the honest verdict is `unknown` — the wiring and the probes are what is proven)
    helper_log_before = Path(os.environ.get("ITEST_LOG_DIR", "/tmp") + "/itest_helper.log").read_text(errors="replace").count("RUN:")
    paste = "root@localhost:~# ssh aedefruscio@192.168.1.127 nvidia-smi\nssh: connect to host 192.168.1.127 port 22: No route to host\nroot@localhost:~#"
    async with httpx.AsyncClient(timeout=300) as client:
        r = await client.post(f"{BASE}/assist/{sid}/fix", headers=AUTH, json={"error": paste, "node_key": "T11", "history": []})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["guidance_meta"].get("deterministic") is True, body.get("guidance_meta")
    assert body["fix"].startswith("## 🔎 Can the host reach VM 110 (ai-vm)?") and "`qm status 110` →" in body["fix"]
    assert body["guidance_meta"]["guest_check"]["class"] in ("unknown", "no_ip")
    ran = Path(os.environ.get("ITEST_LOG_DIR", "/tmp") + "/itest_helper.log").read_text(errors="replace").count("RUN:") - helper_log_before
    assert ran >= 5, ran                                                                      # status, agent, neigh, fdb, ping, port
    async with async_session() as db:
        n_rec = (await db.execute(text("SELECT count(*) FROM assist_turns WHERE session_id = :s AND content LIKE '[local-runner] the engine checked whether VM 110%'"), {"s": sid})).scalar()
    assert n_rec >= 1


async def test_lookup_skips_a_block_meant_for_another_machine(session, helper):
    """§17.1152 — a reply whose `📍 On:` line names a VM console never runs on
    the host through the runner (live: `ip a` ran on the host and the model
    reasoned over the host's interfaces as the VM's)."""
    from app.modules import assist_turn
    sid = session["session_id"]
    console = ("## 👉 Do this next\n\n**Open the web console for VM 110 and type this:**\n\n```\nip a\n```\n\n"
               "📍 On: web UI console for VM 110 — you're leaving the root@localhost shell")
    shell = "📍 On: the host shell (root@localhost)\n\n**Run this now:**\n\n```bash\nhostname\n```"
    async with async_session() as db:
        ev = [e async for e in assist_turn._auto_lookup(sid, console, db, set())]
        assert ev == []
        ran: set = set()
        ev = [e async for e in assist_turn._auto_lookup(sid, shell, db, ran)]
        assert any(e[0] == "_record" for e in ev) and ran == {("hostname",)}
        ev = [e async for e in assist_turn._auto_lookup(sid, shell, db, ran)]
        assert ev == []                                                                      # the repeat guard
