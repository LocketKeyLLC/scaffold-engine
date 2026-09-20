"""§17.1081 — the engine walks the operator through its own optional setup.

The recipes are the engine's; a page beside the engine is not. These pin:
the registry's shape (unique ids, prerequisites that exist, briefs that carry
the facts a plan needs), detection against live settings, the in-progress
override from an open job, the start path (same door as an idea, tagged in
jobs.metadata), the router's status codes, and the state-check nudge that
appears exactly when a paste is being asked for and no runner is set.
"""
from __future__ import annotations

import pathlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import settings
from app.modules import engine_setup as es

ROOT = pathlib.Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.asyncio


def _db(rows=None):
    db = MagicMock()
    r = MagicMock(); r.mappings.return_value.all.return_value = rows or []
    db.execute = AsyncMock(return_value=r); db.commit = AsyncMock(); return db


def test_registry_shape_and_briefs_carry_facts():
    ids = [r.id for r in es.RECIPES]
    assert len(ids) == len(set(ids)) and set(ids) == set(es.BY_ID)
    for r in es.RECIPES:
        assert r.detect is not None and r.title and r.summary and r.why_off and r.effort
        assert all(dep in es.BY_ID for dep in r.requires)
        assert len(r.brief) > 300
    # the facts the plan must stand on, per recipe — not "install it", the exact names
    b = es.BY_ID["local_runner"].brief
    for fact in ("scripts/local_runner_mcp.py", "--port 8790", "X-Runner-Token", "POST http://localhost:8000/mcp/servers",
                 "streamable_http", "run_readonly", "ASSIST_LOCAL_RUNNER_SERVER=pve-runner", "through your local runner"):
        assert fact in b, fact
    assert "--sudo-allow" in es.BY_ID["runner_sudo"].brief and "NOPASSWD" in es.BY_ID["runner_sudo"].brief
    assert es.BY_ID["runner_sudo"].requires == ("local_runner",)
    q = es.BY_ID["queue_worker"].brief
    assert "make queue-schema" in q and "QUEUE_ENABLED=true" in q and "--profile queue up -d queue" in q and "delegated_to_queue" in q
    rr = es.BY_ID["reranker_sidecar"].brief
    assert "RERANKER_BACKEND=http" in rr and "RERANKER_URL=http://scaffold-reranker:80" in rr and "--profile reranker" in rr
    f = es.BY_ID["step_fsm_strict"].brief
    assert "step_fsm_violation" in f and "ASSIST_STEP_FSM_STRICT=true" in f and "STOP" in f


def test_every_brief_fact_names_something_that_exists():
    """A brief that names a make target, env key, script or compose service that
    does not exist would send the operator down a hole; check the names."""
    mk = (ROOT / "Makefile").read_text(encoding="utf-8")
    env = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert (ROOT / "scripts" / "local_runner_mcp.py").exists()
    for target in ("queue-schema:", "queue-once:"):
        assert target in mk, target
    for key in ("ASSIST_LOCAL_RUNNER_SERVER=", "QUEUE_ENABLED=", "RERANKER_BACKEND=", "RERANKER_URL=", "ASSIST_STEP_FSM_STRICT"):
        assert key in env or key.rstrip("=").lower() in (ROOT / "app" / "config.py").read_text(encoding="utf-8"), key
    assert "delegated_to_queue" in (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    assert "step_fsm_violation" in (ROOT / "app" / "modules" / "assist_step_fsm.py").read_text(encoding="utf-8")
    assert "--sudo-allow" in (ROOT / "scripts" / "local_runner_mcp.py").read_text(encoding="utf-8")


def test_brief_compose_services_exist():
    """The compose file is not mounted into the CI test container (three-list
    mount parity keeps it out on purpose); check it wherever it is present."""
    compose_path = ROOT / "docker-compose.yml"
    if not compose_path.exists():
        pytest.skip("docker-compose.yml not mounted here (CI container)")
    compose = compose_path.read_text(encoding="utf-8")
    for svc in ("container_name: scaffold-reranker", "container_name: scaffold-queue", 'profiles: ["queue"]', 'profiles: ["reranker"]'):
        assert svc in compose, svc


async def test_detection_reads_live_settings(monkeypatch):
    monkeypatch.setattr(settings, "assist_local_runner_server", "")
    monkeypatch.setattr(settings, "queue_enabled", False)
    monkeypatch.setattr(settings, "reranker_backend", "local")
    monkeypatch.setattr(settings, "assist_step_fsm_strict", False)
    out = {r["id"]: r for r in await es.list_recipes(_db())}
    assert {k: v["status"] for k, v in out.items()} == {
        "local_runner": "off", "runner_sudo": "blocked", "queue_worker": "off", "reranker_sidecar": "off", "step_fsm_strict": "off"}
    assert out["runner_sudo"]["status_detail"] == "Turn on the local runner first."
    monkeypatch.setattr(settings, "assist_local_runner_server", "pve-runner")
    spec = MagicMock(); spec.enabled = True; spec.endpoint = "http://10.0.0.5:8790/mcp/"; spec.command = None
    with patch("app.modules.mcp_registry.get_server", new=AsyncMock(return_value=spec)):
        monkeypatch.setattr(settings, "queue_enabled", True)
        monkeypatch.setattr(settings, "reranker_backend", "http")
        monkeypatch.setattr(settings, "assist_step_fsm_strict", True)
        out = {r["id"]: r for r in await es.list_recipes(_db())}
    assert {k: v["status"] for k, v in out.items()} == {
        "local_runner": "on", "runner_sudo": "manual", "queue_worker": "on", "reranker_sidecar": "on", "step_fsm_strict": "on"}
    assert "10.0.0.5:8790" in out["local_runner"]["status_detail"]
    # a name set but not registered is OFF with the reason, not "on"
    with patch("app.modules.mcp_registry.get_server", new=AsyncMock(return_value=None)):
        st, detail = await es._detect_local_runner(_db())
    assert st == "off" and "no enabled MCP server" in detail


async def test_open_job_overrides_status_and_rendered_fields_are_complete(monkeypatch):
    monkeypatch.setattr(settings, "queue_enabled", False)
    db = _db([{"rid": "queue_worker", "job_id": "abcdef12-0000", "status": "assisted_executing"},
              {"rid": "reranker_sidecar", "job_id": "ffff0000-1111", "status": "completed"}])
    out = {r["id"]: r for r in await es.list_recipes(db)}
    assert out["queue_worker"]["status"] == "in_progress" and out["queue_worker"]["job_id"] == "abcdef12-0000"
    assert "abcdef12" in out["queue_worker"]["status_detail"]
    assert out["reranker_sidecar"]["status"] == "off" and out["reranker_sidecar"]["job_id"] == "ffff0000-1111"   # last attempt, not in progress
    # every field the API emits is read by the console (field-inventory rule, §17.1007c)
    js = (ROOT / "app" / "ui" / "static" / "views" / "capabilities.js").read_text(encoding="utf-8")
    for k in out["queue_worker"]:
        assert f"r.{k}" in js or f'"{k}"' in js or "[id]" in js and k == "id", k


async def test_start_goes_through_the_idea_door_and_tags_the_job(monkeypatch):
    db = _db()
    created = AsyncMock(return_value="job-123")
    spawned = MagicMock()
    with patch("app.modules.idea_refinement.create_ideation_job", new=created), \
         patch("app.modules.ideation_workflow.spawn_phase1_background", new=spawned):
        out = await es.start_recipe(db, "queue_worker", owner="op@x")
    assert out == {"job_id": "job-123", "status": "refining", "recipe": "queue_worker"}
    assert created.await_args.args[0] == es.BY_ID["queue_worker"].brief and created.await_args.kwargs == {"owner": "op@x"}
    stmt, params = db.execute.await_args.args
    assert "setup_recipe" in params["m"] and params["jid"] == "job-123" and "UPDATE jobs SET metadata" in str(stmt)
    assert spawned.call_args.args == ("job-123", es.BY_ID["queue_worker"].brief)
    db.commit.assert_awaited()
    # prerequisite not on → refused before any job exists
    monkeypatch.setattr(settings, "assist_local_runner_server", "")
    with pytest.raises(ValueError, match="must be on first"):
        await es.start_recipe(_db(), "runner_sudo", owner=None)
    with pytest.raises(KeyError):
        await es.start_recipe(_db(), "nope", owner=None)


async def test_router_status_codes():
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from app.routers import setup as setup_router
    from app.database import get_db
    from app.authz import get_principal, require_admin
    app = FastAPI(); app.include_router(setup_router.router)
    app.dependency_overrides[get_db] = lambda: _db()
    app.dependency_overrides[get_principal] = lambda: MagicMock(identity="op")
    app.dependency_overrides[require_admin] = lambda: None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/setup/recipes")
        assert r.status_code == 200 and [x["id"] for x in r.json()["recipes"]] == [x.id for x in es.RECIPES]
        assert (await c.post("/setup/recipes/nope/start")).status_code == 404
        with patch("app.modules.engine_setup.start_recipe", new=AsyncMock(side_effect=ValueError("'X' must be on first"))):
            r = await c.post("/setup/recipes/runner_sudo/start")
        assert r.status_code == 409 and "must be on first" in r.json()["detail"]
        with patch("app.modules.engine_setup.start_recipe", new=AsyncMock(return_value={"job_id": "j", "status": "refining", "recipe": "queue_worker"})):
            r = await c.post("/setup/recipes/queue_worker/start")
        assert r.status_code == 200 and r.json()["job_id"] == "j"


def test_nudge_only_without_a_runner_and_only_on_a_paste_request(monkeypatch):
    monkeypatch.setattr(settings, "assist_local_runner_server", "")
    assert "Capabilities" in es.runner_nudge() and "#/" not in es.runner_nudge()
    monkeypatch.setattr(settings, "assist_local_runner_server", "pve-runner")
    assert es.runner_nudge() == ""
    src = (ROOT / "app" / "modules" / "assist_turn.py").read_text(encoding="utf-8")
    block = src[src.index("§17.1081"):src.index("state_check_capture_failed")]
    assert 'if res.get("probes"):' in block and "runner_nudge()" in block     # not on the "nothing to verify" reply
    assert block.index("_msg = _msg + runner_nudge()") < block.index("ASSIST_ANSWER")
    assert 'content=_msg' in block                                            # the transcript carries what was shown


def test_console_route_nav_and_view_are_wired():
    app_js = (ROOT / "app" / "ui" / "static" / "app.js").read_text(encoding="utf-8")
    nav = (ROOT / "app" / "ui" / "static" / "nav.js").read_text(encoding="utf-8")
    assert 'router.route("/capabilities"' in app_js and 'capabilities: lazy("capabilities"' in app_js
    assert 'path: "/capabilities"' in nav and "adminOnly: true" in nav.split('path: "/capabilities"')[1].split("\n")[0]
    js = (ROOT / "app" / "ui" / "static" / "views" / "capabilities.js").read_text(encoding="utf-8")
    assert 'api.get("/setup/recipes")' in js and "/setup/recipes/${r.id}/start" in js and "router.navigate(" in js


# ── §17.1081b — prescriptive briefs: no decision node, no options, steps as listed ──

async def test_start_stamps_prescriptive_and_lookup_reads_it():
    db = _db()
    with patch("app.modules.idea_refinement.create_ideation_job", new=AsyncMock(return_value="j1")), \
         patch("app.modules.ideation_workflow.spawn_phase1_background", new=MagicMock()):
        await es.start_recipe(db, "reranker_sidecar", owner=None)
    import json
    assert json.loads(db.execute.await_args.args[1]["m"]) == {"setup_recipe": "reranker_sidecar", "prescriptive": True}
    for meta, want in (({"prescriptive": True}, True), ('{"prescriptive": true}', True), ({"setup_recipe": "x"}, False), (None, False)):
        d = MagicMock(); r = MagicMock(); r.scalar.return_value = meta; d.execute = AsyncMock(return_value=r)
        assert await es.job_is_prescriptive(d, "j") is want, meta
    boom = MagicMock(); boom.execute = AsyncMock(side_effect=RuntimeError("x"))
    assert await es.job_is_prescriptive(boom, "j") is False


def test_planner_and_research_honour_the_flag():
    dag = (ROOT / "app" / "modules" / "dag_generator.py").read_text(encoding="utf-8")
    assert "COALESCE(j.metadata->>'prescriptive', '') = 'true' AS prescriptive" in dag
    block = dag[dag.index("§17.1081b"):dag.index("current_hash = _compute_dag_input_hash")]
    assert "if prescriptive:" in block and '"prescribed": True' in block and "_brief_with_operator_decision" in block.split("else:")[1]
    assert "PRESCRIBED_BLOCK + prompt" in dag
    assert "decision nodes" in es.PRESCRIBED_BLOCK and "already exist" in es.PRESCRIBED_BLOCK and "in the brief's order" in es.PRESCRIBED_BLOCK
    iw = (ROOT / "app" / "modules" / "ideation_workflow.py").read_text(encoding="utf-8")
    assert "and not _is_prescriptive:" in iw and "phase2_options_skipped_prescriptive" in iw


# ── §17.1090 — the web UI carries this week's components ──

def test_env_endpoint_carries_the_system_map_and_the_console_renders_it():
    """§17.1007c's rule: a field the server emits for the operator must be read by
    the surface that declares it. `system_map` on GET /assist/{sid}/env → the
    environment card; `assist_gates.crashed` on /health → the dashboard dot's title;
    a background-staged `pending_replan` → the idle poll."""
    router = (ROOT / "app" / "routers" / "assist.py").read_text(encoding="utf-8")
    assert '"system_map": system_map' in router and "render_system_map(env)" in router
    js = (ROOT / "app" / "ui" / "static" / "views" / "assist.js").read_text(encoding="utf-8")
    assert "r.system_map" in js and 'class: "side-map"' in js and "conflicts to settle" in js
    assert "idlePoll = setInterval" in js and "renderReplanProposal(s.pending_replan, { open: true" in js and "clearInterval(idlePoll)" in js
    dash = (ROOT / "app" / "ui" / "static" / "views" / "dashboard.js").read_text(encoding="utf-8")
    assert "crashed gates:" in dash and "c.crashed" in dash
    css = (ROOT / "app" / "ui" / "static" / "app.css").read_text(encoding="utf-8")
    assert ".side-map-pre" in css


@pytest.mark.asyncio
async def test_env_endpoint_returns_the_map_text():
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from app.routers import assist as assist_router
    from app.database import get_db
    from app.modules import assist_agent
    env = {"system_state": {"120": {"kind": "ct", "attrs": {"hostname": "caddy-proxy", "ip": "192.168.1.26"}, "devices": {}, "source": "pct config 120"}},
           "facts": ["Container listeners: CT 120 on *:443 and *:80."]}
    app = FastAPI(); app.include_router(assist_router.router)
    app.dependency_overrides[get_db] = lambda: MagicMock()
    with patch.object(assist_agent, "get_environment", new=AsyncMock(return_value=env)):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get("/assist/9c9c9c9c-0000-0000-0000-000000000000/env")
    assert r.status_code == 200
    body = r.json()
    assert body["environment"] == env and "CT 120 (caddy-proxy) · IP 192.168.1.26" in body["system_map"]


# ── §17.1144 — the assist knows the engine's own capabilities ──────────────
# Live: "please assist me in setting up the local runner on the proxmox server"
# went to the web and came back as a plan to build a CI-runner VM.


@pytest.mark.parametrize("msg,rid", [
    ("please assist me in setting up the local runner on the proxmox server to assist with this.", "local_runner"),
    ("How do I set up the local runner?", "local_runner"),
    ("can the state check run its own checks instead of me pasting", "local_runner"),
    ("stop asking me to paste, run it yourself", "local_runner"),
    ("how do i give the runner sudo for pct config", "runner_sudo"),
    ("move the reranker sidecar to its own container?", "reranker_sidecar"),
    ("what is the queue worker for", "queue_worker"),
])
def test_engine_capability_questions_match_their_recipe(msg, rid):
    assert es.match_recipe(msg).id == rid


@pytest.mark.parametrize("msg", [
    # real operator turns from the live session — none of them is about the engine
    "i found that mac address but it wouldn't allow me to port forward. An the Proxmox b4:af:15's ip is 192.168.1.129",
    "connection timed out",
    "in the spectrum app, under the port forwarding i don't see 192.168.1..26, i see what appear to be MAC addresses.",
    "root@pve:~# qm status 100\nstatus: running\nroot@pve:~#",
    # the world's 'runner' is a CI runner; only OUR phrases match
    "set up a github actions runner on the vm",
    "install the gitlab runner",
    # a paste that merely contains the phrase is evidence, not a question
    "root@pve:~# systemctl status local-runner\n● local-runner.service - x\n   Active: active (running)\nroot@pve:~#",
    "",
])
def test_non_engine_messages_do_not_match(msg):
    assert es.match_recipe(msg) is None


def test_capability_answer_offers_the_steps_in_this_plan_never_a_dashboard():
    """§17.1145 — the first cut opened a separate walkthrough job and sent the
    operator to the dashboard: "It should just assist within the plan"."""
    r = es.BY_ID["local_runner"]
    off = es.capability_answer(r, status="off", detail="ASSIST_LOCAL_RUNNER_SERVER is empty — the state check asks you to paste.",
                               current_step="T6")
    for must in (r.title, r.summary, r.why_off, "ASSIST_LOCAL_RUNNER_SERVER is empty", "Reply **yes**",
                 f"its {len(r.steps)} steps to this plan", "right before **T6**", "walk you through the first one now"):
        assert must in off, must
    for never in ("dashboard", "separate", "Capabilities", "its own job"):
        assert never not in off, never
    on = es.capability_answer(r, status="on", detail="probes run through 'pve-runner' at http://x:8790/mcp/.")
    assert "already on" in on and "pve-runner" in on and "Reply **yes**" not in on
    inplan = es.capability_answer(r, status="off", detail="x", in_plan={"node_key": "ADD61", "title": r.steps[0][0], "status": "pending"})
    assert "already in this plan" in inplan and "ADD61" in inplan and "Reply **yes**" not in inplan
    blocked = es.capability_answer(es.BY_ID["runner_sudo"], status="blocked", detail="Turn on the local runner first.", current_step="T6")
    assert "Not available yet" in blocked and es.BY_ID["local_runner"].title in blocked and "steps in all" in blocked


def test_every_recipe_carries_plan_steps_that_carry_the_facts():
    for r in es.RECIPES:
        assert r.steps, r.id
        titles = [t for t, _ in r.steps]
        assert len(set(titles)) == len(titles), r.id
        for _, what in r.steps:
            assert "Done when" in what, (r.id, what[:60])      # every step says what finishes it
    lr = " ".join(w for _, w in es.BY_ID["local_runner"].steps)
    for fact in ("8790", "X-Runner-Token", "run_readonly", "ASSIST_LOCAL_RUNNER_SERVER=pve-runner", "/mcp/servers", "Verify state"):
        assert fact in lr, fact
    # prerequisites chain first, and every inserted description carries the recipe marker
    steps = es.recipe_steps(es.BY_ID["runner_sudo"])
    assert len(steps) == len(es.BY_ID["local_runner"].steps) + len(es.BY_ID["runner_sudo"].steps)
    assert steps[0]["title"] == es.BY_ID["local_runner"].steps[0][0]
    assert all(f"{es.RECIPE_STEP_MARK} " in st["description"] for st in steps)
    assert steps[-1]["description"].endswith("runner_sudo_")


async def test_setup_offer_round_trip_and_in_plan_insertion(monkeypatch):
    db = MagicMock(); db.execute = AsyncMock(); db.commit = AsyncMock()
    await es.stage_setup_offer(session_id="s1", recipe_id="local_runner", db=db)
    sql = " ".join(str(db.execute.await_args[0][0]).split())
    assert "COALESCE(metadata, '{}'::jsonb) || CAST(:patch AS jsonb)" in sql
    assert es.SETUP_OFFER_KEY in db.execute.await_args[0][1]["patch"]
    row = MagicMock(); row.mappings.return_value.first.return_value = {"metadata": {es.SETUP_OFFER_KEY: {"recipe_id": "nope"}}}
    db.execute = AsyncMock(return_value=row)
    assert await es.get_pending_setup_offer(session_id="s1", db=db) is None
    row.mappings.return_value.first.return_value = {"metadata": {es.SETUP_OFFER_KEY: {"recipe_id": "local_runner"}}}
    assert (await es.get_pending_setup_offer(session_id="s1", db=db))["recipe_id"] == "local_runner"
    db.execute = AsyncMock()
    await es.clear_pending_setup_offer(session_id="s1", db=db)
    assert f"- '{es.SETUP_OFFER_KEY}'" in " ".join(str(db.execute.await_args[0][0]).split())
    # insertion goes through add_step with PRE-DRAFTED steps — no model draft, before the current step
    from app.modules import assist_notes
    seen = {}
    async def fake_add_step(*, session_id, request, before_node_key=None, proposal=None, steps=None, db):
        seen.update(request=request, before=before_node_key, steps=steps)
        return {"node_key": "ADD61", "steps": [{"node_key": "ADD61", "title": steps[0]["title"]}]}
    monkeypatch.setattr(assist_notes, "add_step", fake_add_step)
    res = await es.add_recipe_to_plan(db, "s1", es.BY_ID["local_runner"], before_node_key="T6")
    assert res["node_key"] == "ADD61" and seen["before"] == "T6"
    assert [st["title"] for st in seen["steps"]] == [t for t, _ in es.BY_ID["local_runner"].steps]
    # plan_has_recipe: the first still-open recipe step, committed ones ignored
    rows = MagicMock(); rows.mappings.return_value.all.return_value = [
        {"node_key": "ADD61", "title": "a", "status": "committed"}, {"node_key": "ADD62", "title": "b", "status": "pending"}]
    db.execute = AsyncMock(return_value=rows)
    assert (await es.plan_has_recipe(db, "s1", "local_runner"))["node_key"] == "ADD62"
    assert f"{es.RECIPE_STEP_MARK} local_runner_" in db.execute.await_args[0][1]["mark"]


def test_add_step_inserts_pre_drafted_steps_without_a_model_draft():
    import inspect
    from app.modules import assist_notes
    src = inspect.getsource(assist_notes.add_step)
    assert "steps: list[dict] | None = None" in src
    assert "if steps:" in src and src.index("if steps:") < src.index("drafted = await assist_guide.draft_steps(")


def test_turn_loop_answers_engine_questions_before_the_decision_layer():
    """The classifier has no action for 'about the engine'; the bridge must run
    before decide_turn, and a staged offer must resolve on yes / no / supersede.
    §17.1145 — a yes adds the steps to THIS plan and guides the first; nothing
    opens a separate job or points at the dashboard."""
    import inspect
    from app.modules import assist_turn
    src = inspect.getsource(assist_turn._run_turn_inner)
    assert src.index("match_recipe(") < src.index("decide_turn(")
    assert src.index("get_pending_setup_offer") < src.index("match_recipe(")
    assert src.count("clear_pending_setup_offer") >= 3          # yes, no, supersede
    assert "add_recipe_to_plan" in src and "capability_answer" in src and "plan_has_recipe" in src
    assert src.index("add_recipe_to_plan") < src.index("_claim_and_guide(session_id, (_added or {}).get(\"node_key\")")
    assert "start_recipe" not in src.replace("start_recipe_for_session", "") or "start_recipe_for_session" not in src
    assert "dashboard" not in src.split("# 2a.")[1].split("# 2b.")[0].replace("never sent to the dashboard", "")
    assert 'handled["v"] = "engine_capability"' in src and 'handled["v"] = "setup_recipe_added"' in src


def test_every_recipe_has_keywords_and_none_is_a_bare_common_word():
    for r in es.RECIPES:
        assert r.keywords, r.id
        for k in r.keywords:
            assert " " in k or "_" in k or "-" in k, (r.id, k)   # a phrase, never a bare word
