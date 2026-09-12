"""§17.1039 — the evidence layer on the executor (app/modules/execution_evidence.py).

Autonomous execution searched the web for a node's bare title, was handed
snippets, and never checked what the node wrote. These pin: the need derived
from a task; page-content sources ranked and dated for SearXNG nodes; the
output check with one regeneration, its report kept OUT of the output text;
CodeGen reported but never redrawn; upstream-flagged values not credited by
the upstream text; and the executor's wiring (structural).
"""
from __future__ import annotations

import pathlib

import pytest

import app.modules.execution_agent  # noqa: F401 — load-bearing: binds the REAL app.database before any test module stubs it (test_status_logs.py setdefault()s a fake at collection time)
from app.modules import execution_evidence as xe
from app.modules import assist_evidence as ev

pytestmark = pytest.mark.asyncio

BRIEF = {"title": "Uptime Kuma behind Nginx", "description": "Deploy Uptime Kuma in Docker on the Ubuntu 24.04 server.",
         "goals": ["status pages"], "constraints": ["reachable at https://status.hamlet-labs.net"]}
NODE = {"node_key": "T4", "title": "Create Uptime Kuma container",
        "prompt_template": "docker run the louislam/uptime-kuma image bound to localhost. Does NOT configure Nginx."}


@pytest.fixture(autouse=True)
def _valves(monkeypatch):
    monkeypatch.setattr(ev.settings, "assist_answer_verification_enabled", True)
    monkeypatch.setattr(ev.settings, "assist_answer_verification_regenerate", True)
    monkeypatch.setattr(ev.settings, "assist_answer_min_citation_score", 0.6)
    monkeypatch.setattr(xe.settings, "execution_answer_verification_enabled", True)
    monkeypatch.setattr(xe.settings, "execution_evidence_retrieval_enabled", True)


def _sync(coro):
    import asyncio
    return asyncio.get_event_loop().run_until_complete(coro)


def test_a_task_becomes_a_goal_need_with_its_own_terms():
    need = xe.node_need(NODE, BRIEF)
    assert need.kind == "goal" and need.researchable
    q = need.query.lower()
    assert "uptime" in q and "kuma" in q and "container" in q


def test_a_node_with_no_notes_still_has_a_need_from_its_title():
    need = xe.node_need({"title": "Verify DNS record exists"}, None)
    assert need.researchable and "dns" in need.query.lower()


async def test_web_sources_fetch_pages_for_the_need_and_keep_only_on_topic_ones(monkeypatch):
    import app.modules.assist_research_lib as rl
    seen = {}

    async def fake_confirm(query, *, node_key, domain, deep=False, **kw):
        seen["query"], seen["deep"] = query, deep
        return [
            {"query": query, "kind": "web", "url": "https://blog.example.org/kuma",
             "text": "Uptime Kuma container docker run guide with the data volume", "date": "2026-03-01"},
            {"query": query, "kind": "web", "url": "https://other.example.org/cats",
             "text": "cats are lovely pets and purr", "date": "2026-05-01"},
        ]
    monkeypatch.setattr(rl, "_confirm_query", fake_confirm)
    need = xe.node_need(NODE, BRIEF)
    srcs = await xe.web_sources(need, node_key="T4", domain=None)
    assert seen["deep"] is True and "kuma" in seen["query"].lower()
    assert [s["url"] for s in srcs] == ["https://blog.example.org/kuma"]
    block = xe.render_node_sources(srcs)
    assert "published 2026-03-01" in block and "CURRENCY" in block


async def test_with_the_valve_off_the_snippet_path_is_used(monkeypatch):
    monkeypatch.setattr(xe.settings, "execution_evidence_retrieval_enabled", False)
    import app.modules.execution_agent as xa

    async def fake_search(query, max_results=5):
        return "[1] Uptime Kuma docs\n    snippet about the container\n    https://x.example.org"
    monkeypatch.setattr(xa, "_searxng_search", fake_search)
    srcs = await xe.web_sources(xe.node_need(NODE, BRIEF), node_key="T4", domain=None)
    assert srcs and srcs[0]["kind"] == "searxng"


def _kw(**over):
    base = dict(need=xe.node_need(NODE, BRIEF), sources=[], brief=BRIEF, input_text="my server is 10.20.0.5",
                task_text=xe.node_task_text(NODE), upstream_text="", upstream_flagged=set(),
                tool="LLM", node_key="T4")
    base.update(over)
    return base


async def test_an_unsupported_value_is_regenerated_and_the_text_carries_no_footer():
    calls = []

    async def regen(notice):
        calls.append(notice)
        return "Run the container bound to 127.0.0.1 on the port the brief names; confirm with docker ps."
    out, rep = await xe.verify_node_output("Bind the container to 10.99.0.7:3001 and open port 3001.",
                                           regenerate=regen, **_kw())
    assert "10.99.0.7" in calls[0] and "3001" in calls[0]
    assert rep["regenerated"] is True and rep["unsupported"] == []
    assert out.startswith("Run the container") and "Unverified" not in out and rep["footer"] == ""


async def test_a_still_unsupported_value_is_reported_not_appended():
    async def regen(notice):
        return "Still bind it to 10.99.0.7."
    text = "Bind the container to 10.99.0.7."
    out, rep = await xe.verify_node_output(text, regenerate=regen, **_kw())
    assert out in (text, "Still bind it to 10.99.0.7.") and "⚠️" not in out
    assert [u["value"] for u in rep["unsupported"]] == ["10.99.0.7"]
    assert "Unverified specifics" in rep["footer"] and "`10.99.0.7` (ip)" in rep["footer"]
    summary = xe.evidence_summary(rep)
    assert summary["checked"] and summary["unsupported"] == ["10.99.0.7"] and summary["footer"] == rep["footer"]


async def test_values_the_operator_gave_or_the_sources_state_are_credited():
    async def regen(notice):
        raise AssertionError("no regeneration needed")
    srcs = [{"kind": "web", "query": "q", "url": "https://docs.example.org/kuma",
             "text": "Uptime Kuma listens on port 3001 by default; run docker with the container image version 1.23.16"}]
    out, rep = await xe.verify_node_output(
        "Server 10.20.0.5: run image 1.23.16 and publish port 3001 at https://status.hamlet-labs.net/admin",
        regenerate=regen, **_kw(sources=srcs))
    assert rep["unsupported"] == [] and rep["regenerated"] is False
    assert {it["value"] for it in rep["sourced_now"]} >= {"1.23.16", "3001"}


async def test_a_value_only_the_task_text_states_is_the_plan_only_tier():
    async def regen(notice):
        raise AssertionError("plan-only is not a failure")
    node = dict(NODE, prompt_template="Bind to 192.168.7.2 (from the plan).")
    out, rep = await xe.verify_node_output("Bind the container to 192.168.7.2.", regenerate=regen,
                                           **_kw(task_text=xe.node_task_text(node)))
    assert rep["unsupported"] == [] and [u["value"] for u in rep["plan_only"]] == ["192.168.7.2"]
    assert "From the plan" in rep["footer"] and "⚠️" not in out


async def test_an_upstream_flagged_value_is_not_credited_by_the_upstream_text():
    """§17.1028 one layer down: T3's report flagged 10.99.0.7; T3's output
    still contains it; T4 repeating it is still unsupported."""
    async def regen(notice):
        return ""
    out, rep = await xe.verify_node_output(
        "Use the gateway 10.99.0.7 as T3 established.", regenerate=regen,
        **_kw(upstream_text="T3 output: the gateway is 10.99.0.7", upstream_flagged={"10.99.0.7"}))
    assert [u["value"] for u in rep["unsupported"]] == ["10.99.0.7"]


async def test_the_same_value_unflagged_upstream_is_credited():
    async def regen(notice):
        raise AssertionError("credited by upstream")
    out, rep = await xe.verify_node_output(
        "Use the gateway 10.99.0.7 as T3 established.", regenerate=regen,
        **_kw(upstream_text="T3 output: the gateway is 10.99.0.7"))
    assert rep["unsupported"] == []


async def test_codegen_is_reported_but_never_regenerated():
    calls = []

    async def regen(notice):
        calls.append(notice)
        return "x"
    code = "FROM python:3.12.4-slim\nEXPOSE 3005\n"
    out, rep = await xe.verify_node_output(code, regenerate=regen, **_kw(tool="CodeGen"))
    assert calls == [] and out == code
    assert {u["value"] for u in rep["unsupported"]} == {"3.12.4"}


async def test_the_valve_turns_the_check_off(monkeypatch):
    monkeypatch.setattr(xe.settings, "execution_answer_verification_enabled", False)

    async def regen(notice):
        raise AssertionError("off")
    out, rep = await xe.verify_node_output("Bind to 10.99.0.7", regenerate=regen, **_kw())
    assert rep == {"checked": False} and xe.evidence_summary(rep) == {"checked": False}


async def test_upstream_flagged_values_are_read_from_the_evidence_log_rows():
    class _Rows:
        def fetchall(self):
            return [({"unsupported": ["10.99.0.7", " 8211 "], "plan_only": []},),
                    ('{"unsupported": ["v9.9.9"]}',), (None,)]

    class _Db:
        async def execute(self, *a, **k):
            return _Rows()
    assert await xe.fetch_upstream_flagged(_Db(), "j", ["T3"]) == {"10.99.0.7", "8211", "v9.9.9"}
    assert await xe.fetch_upstream_flagged(_Db(), "j", []) == set()


def test_the_executor_verifies_before_persisting_and_searches_by_need():
    src = (pathlib.Path(__file__).resolve().parents[1] / "app/modules/execution_agent.py").read_text()
    body = src[src.index("async def execute_next_node("):]
    verify_at = body.index("verify_node_output(")
    assert verify_at < body.index('verify_status: Literal["pass", "fail", "skipped"]')
    assert verify_at < body.index("expected_status=\"running\"")
    searx = body[body.index('elif tool_lower == "searxng":'):body.index("else:\n            rag_context")]
    assert "web_sources(" in searx and "_searxng_search(title)" not in searx
    assert "fetch_upstream_flagged(db, job_id, depends_on)" in body
    assert '{"evidence": _evidence_summary}' in body and '"evidence": _evidence_summary,' in body
    # §17.1040 — durable on the node row and carried on both node_done emitters.
    assert "UPDATE dag_nodes SET evidence = CAST(:ev AS jsonb)" in body
    assert src.count('"evidence": res.get("evidence")') + src.count('"evidence": result.get("evidence")') == 2


def test_the_status_payload_and_both_operator_surfaces_carry_the_report():
    """§17.1040 — /exec/status projects dag_nodes.evidence, the SPA Run tab
    renders it (evidenceLines) and the CLI prints it."""
    root = pathlib.Path(__file__).resolve().parents[1]
    handler = (root / "app/modules/execution_handler.py").read_text()
    assert "completed_at, evidence" in handler and '"evidence": r.evidence' in handler
    spa = (root / "app/ui/static/views/theater.js").read_text()
    assert "function evidenceLines" in spa and "Evidence check" in spa and "evidence: data.evidence" in spa
    cli = (root / "cli/scaffold_cli/main.py").read_text()
    assert 'n.get("evidence")' in cli and "unverified:" in cli
    # The CLI node table reads the LOGS payload (NodeLog), not /exec/status —
    # the field the CLI prints must exist on the model it actually receives.
    logs = (root / "app/routers/status.py").read_text()
    assert "evidence: Optional[dict] = None" in logs and "evidence=_evidence_dict(getattr(row, \"evidence\"" in logs
    assert "status, domain, evidence," in logs
    mig = (root / "db/migrations/075_dag_nodes_evidence.sql").read_text()
    assert mig.count(";") == 0 and "ADD COLUMN IF NOT EXISTS evidence JSONB" in mig
