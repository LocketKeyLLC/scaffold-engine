"""§17.1725–1727 — the planner carries the operator's answers and the engine's own feasibility, is told what it
must not restart on a preserved machine, and the engine can spread its Ollama calls over a pool.

Live pre-image (job f9ca3ee1, 2026-10-21): the operator answered "VM 110 and the laptops should work together as
one pool"; the plan tainted VM 110 so AI work ran on the laptops only. Phase 1's feasibility said "must use k3s,
not full k8s"; the plan installed kubeadm (which rewrites and restarts the containerd VM 110's Docker stack runs
on). And the cluster's load-balanced Service could never carry the engine's work: the engine had one Ollama.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import httpx
import pytest

from app.config import settings
from app.modules import dag_generator, ollama_pool

PRIMARY = "http://172.18.0.1:11434"
LAP1 = "http://192.0.2.11:11434"
LAP2 = "http://192.0.2.12:11434"


@pytest.fixture(autouse=True)
def _pool(monkeypatch):
    monkeypatch.setattr(settings, "ollama_base_url", PRIMARY)
    monkeypatch.setattr(settings, "ollama_pool_urls", "")
    ollama_pool._reset_for_tests()
    yield
    ollama_pool._reset_for_tests()


class _FakeClient:
    """Answers /api/tags per server, records every POST, and refuses connections to ``dead`` servers."""

    def __init__(self, tags: dict[str, list[str]], dead: set[str] = frozenset()):
        self.tags, self.dead, self.posts, self.gets = tags, set(dead), [], []

    def _base(self, url: str) -> str:
        return url.split("/api/")[0]

    async def get(self, url, timeout=None):
        self.gets.append(url)
        base = self._base(url)
        if base in self.dead:
            raise httpx.ConnectError("refused")
        r = MagicMock()
        r.raise_for_status = MagicMock()
        r.json = MagicMock(return_value={"models": [{"name": n} for n in self.tags.get(base, [])]})
        return r

    async def post(self, url, json=None, timeout=None):
        self.posts.append(url)
        if self._base(url) in self.dead:
            raise httpx.ConnectError("refused")
        r = MagicMock()
        r.status_code = 200
        r.json = MagicMock(return_value={"response": f"from {self._base(url)}", "message": {"content": "ok"}})
        return r


def _client(monkeypatch, fake):
    from app import model_router
    monkeypatch.setattr(model_router, "_get_client", lambda: fake)
    return fake


# ---------------------------------------------------------------------------
# §17.1727 — the pool
# ---------------------------------------------------------------------------

async def test_no_pool_is_exactly_the_old_single_server(monkeypatch):
    fake = _client(monkeypatch, _FakeClient({}))
    from app import model_router
    resp = await model_router._call_ollama("/api/generate", {"model": "qwen3:4b"}, "qwen3:4b", 30)
    assert resp.success and fake.posts == [f"{PRIMARY}/api/generate"]
    assert fake.gets == []                                   # no tags read when there is no pool


async def test_a_call_goes_to_a_member_that_holds_its_model(monkeypatch):
    monkeypatch.setattr(settings, "ollama_pool_urls", f"{LAP1}, {LAP2}/")
    fake = _client(monkeypatch, _FakeClient({LAP1: ["nomic-embed-text:latest"], LAP2: ["qwen3:4b"]}))
    from app import model_router
    from app.modules import ollama_pool as pool
    # the primary is busy, so the least-busy HOLDER wins — and only LAP2 holds qwen3:4b
    pool._member(PRIMARY).in_flight = 1
    resp = await model_router._call_ollama("/api/generate", {"model": "qwen3:4b"}, "qwen3:4b", 30)
    assert resp.success and fake.posts == [f"{LAP2}/api/generate"]
    # a bare name matches its :latest tag
    assert LAP1 in await pool.candidates("nomic-embed-text")
    # a model no laptop holds never goes to a laptop
    assert await pool.candidates("cyber-coder-35b") == [PRIMARY]


async def test_a_cloud_tag_stays_on_the_primary(monkeypatch):
    monkeypatch.setattr(settings, "ollama_pool_urls", LAP1)
    fake = _client(monkeypatch, _FakeClient({LAP1: ["deepseek-v4-pro:cloud"]}))
    assert await ollama_pool.candidates("deepseek-v4-pro:cloud") == [PRIMARY]
    assert fake.gets == []


async def test_ties_go_to_the_primary_then_the_least_busy(monkeypatch):
    monkeypatch.setattr(settings, "ollama_pool_urls", f"{LAP1},{LAP2}")
    _client(monkeypatch, _FakeClient({LAP1: ["qwen3:4b"], LAP2: ["qwen3:4b"]}))
    assert await ollama_pool.candidates("qwen3:4b") == [PRIMARY, LAP1, LAP2]
    ollama_pool._member(PRIMARY).in_flight = 2
    ollama_pool._member(LAP1).in_flight = 1
    assert await ollama_pool.candidates("qwen3:4b") == [LAP2, LAP1, PRIMARY]


async def test_an_unplugged_member_is_skipped_and_the_call_still_succeeds(monkeypatch):
    """Operator answer 4: the work keeps going when one laptop dies."""
    monkeypatch.setattr(settings, "ollama_pool_urls", f"{LAP1},{LAP2}")
    fake = _client(monkeypatch, _FakeClient({LAP1: ["qwen3:4b"], LAP2: ["qwen3:4b"]}))
    ollama_pool._member(PRIMARY).in_flight = 5
    await ollama_pool.candidates("qwen3:4b")                 # tags read while both answer
    fake.dead.add(LAP1)                                      # then LAP1 is unplugged
    from app import model_router
    resp = await model_router._call_ollama("/api/generate", {"model": "qwen3:4b"}, "qwen3:4b", 30)
    assert resp.success
    assert fake.posts == [f"{LAP1}/api/generate", f"{LAP2}/api/generate"]
    assert LAP1 not in await ollama_pool.candidates("qwen3:4b")   # resting, not retried on every call
    st = await ollama_pool.status()
    assert {m["url"]: m["status"] for m in st["members"]} == {LAP1: "down", LAP2: "up"}


async def test_a_timeout_is_not_resent_to_another_member(monkeypatch):
    """A request a member already received must not run twice."""
    monkeypatch.setattr(settings, "ollama_pool_urls", LAP1)
    fake = _client(monkeypatch, _FakeClient({LAP1: ["qwen3:4b"]}))
    ollama_pool._member(PRIMARY).in_flight = 5
    fake.post = AsyncMock(side_effect=httpx.ReadTimeout("slow"))
    from app import model_router
    resp = await model_router._call_ollama("/api/generate", {"model": "qwen3:4b"}, "qwen3:4b", 30)
    assert not resp.success and fake.post.await_count == 1


async def test_tool_calls_take_the_same_pool(monkeypatch):
    """Sibling call sites: the provider's tool path must not keep its own copy of the address."""
    monkeypatch.setattr(settings, "ollama_pool_urls", LAP1)
    fake = _client(monkeypatch, _FakeClient({LAP1: ["qwen3:4b"]}))
    ollama_pool._member(PRIMARY).in_flight = 3
    from app.providers.ollama import OllamaProvider
    resp = await OllamaProvider().tool_call("qwen3:4b", [{"role": "user", "content": "hi"}], tools=[])
    assert resp.success and fake.posts == [f"{LAP1}/api/chat"]


async def test_streaming_moves_on_only_before_the_first_chunk(monkeypatch):
    monkeypatch.setattr(settings, "ollama_pool_urls", LAP1)
    fake = _FakeClient({LAP1: ["qwen3:4b"]})
    tried: list[str] = []

    class _Stream:
        def __init__(self, url):
            self.url = url

        async def __aenter__(self):
            tried.append(self.url)
            if self.url.startswith(LAP1):
                raise httpx.ConnectError("refused")
            r = MagicMock()
            r.status_code = 200

            async def _lines():
                yield json.dumps({"message": {"content": "hello"}, "done": True})
            r.aiter_lines = _lines
            return r

        async def __aexit__(self, *a):
            return False

    fake.stream = lambda method, url, json=None, timeout=None: _Stream(url)
    _client(monkeypatch, fake)
    ollama_pool._member(PRIMARY).in_flight = 3
    from app.providers.ollama import OllamaProvider
    out = [c async for c in OllamaProvider().stream_chat("qwen3:4b", [{"role": "user", "content": "x"}])]
    assert out == ["hello"]
    assert tried == [f"{LAP1}/api/chat", f"{PRIMARY}/api/chat"]


@pytest.mark.smoke
def test_every_ollama_model_call_site_goes_through_the_pool():
    """Ratchet: a model call that builds its URL from ollama_base_url again bypasses the pool."""
    import inspect
    from app import model_router
    from app.providers import ollama as prov
    assert "post_to_pool(" in inspect.getsource(model_router._call_ollama)
    src = inspect.getsource(prov.OllamaProvider)
    assert "ollama_base_url" not in src
    assert "post_to_pool(" in src and "ollama_pool.candidates(" in src


@pytest.mark.smoke
def test_the_setting_reaches_the_container():
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1]
    compose = (root / "docker-compose.yml").read_text() if (root / "docker-compose.yml").exists() else None
    if compose is None:
        pytest.skip("compose file not mounted in this lane")
    assert "OLLAMA_POOL_URLS: ${OLLAMA_POOL_URLS:-}" in compose


# ---------------------------------------------------------------------------
# §17.1727 — the planner is told the engine can use added compute
# ---------------------------------------------------------------------------

_K8S_BRIEF = {
    "title": "Kubernetes cluster from old laptops for AI compute",
    "description": "Build a Kubernetes cluster that adds AI compute capacity to the server engine, the AI VM as "
                   "control plane and the old laptops as worker nodes. The engine and Ollama run on the AI VM.",
}
_MOVE_BRIEF = {
    "title": "Move scaffold engine onto Proxmox server",
    "description": "Move the engine and its Ollama inference onto the server VM; keep the models.",
}


@pytest.mark.smoke
def test_planner_note_hits_added_compute_and_misses_a_move():
    assert ollama_pool.planner_note_applies(_K8S_BRIEF)
    assert not ollama_pool.planner_note_applies(_MOVE_BRIEF)
    assert not ollama_pool.planner_note_applies({"title": "Deploy bitmagnet", "description": "torrent search"})


def _resp(text):
    r = MagicMock()
    r.success, r.text, r.error, r.model, r.total_duration_ms = True, text, None, "fake", 0
    return r


_DAG = json.dumps({"strategy": "s", "tasks": [
    {"id": "T1", "name": "Do it", "type": "action", "inputs": [], "outputs": ["x"], "depends_on": [],
     "tool": "Shell", "domain": None, "assigned_model": None, "notes": "n", "is_deliverable": True}]})


async def _planner_prompt(brief, monkeypatch) -> str:
    from app import model_router as _mr
    monkeypatch.setattr(settings, "dag_validator_enabled", False)
    seen: list[str] = []

    async def _gen(prompt, *a, **k):
        seen.append(prompt)
        return _resp(_DAG)
    with patch.object(_mr, "generate", new=AsyncMock(side_effect=_gen)), \
         patch("app.modules.flywheel.retrieve_exemplars", new=AsyncMock(return_value="")):
        await dag_generator._generate_dag_with_validator(brief, {"role": "model_general"})
    return seen[0]


async def test_the_planner_prompt_carries_the_capability_only_for_added_compute(monkeypatch):
    assert "OLLAMA_POOL_URLS" in await _planner_prompt(_K8S_BRIEF, monkeypatch)
    assert "OLLAMA_POOL_URLS" not in await _planner_prompt(_MOVE_BRIEF, monkeypatch)


# ---------------------------------------------------------------------------
# §17.1725 — the operator's answers and the engine's feasibility reach the planner
# ---------------------------------------------------------------------------

_AMBIG = [
    "What should the extra compute be used for first?",
    "How much is the operator willing to spend?",
    "Can the laptops be dedicated?",
    "Must the cluster keep working if one laptop dies?",
    "What happens if the laptops are unavailable — fall back to the VM, or wait?",
    "Is there a target model?",
]
_FEEDBACK = (
    "Operator's answers to the open questions (2026-10-21):\n"
    "1. Use of the extra compute: additional research and programming (coding) work when needed.\n"
    "2. Budget: use the free old laptops; no hardware purchases.\n"
    "3. The laptops' sole purpose is this cluster; they can be fully dedicated.\n"
    "4. The cluster must keep working if one laptop dies or is unplugged.\n"
    "5. The laptops will always be on; the VM and the laptops should work together as one pool.\n"
    "6. unsure"
)
_RESEARCH = {"feasibility": {
    "summary": "Kubernetes on old laptops should use k3s (not full k8s) to keep overhead low.",
    "risks": ["Kubernetes overhead on low-RAM laptops — must use k3s, not full k8s"],
    "recommended_research_queries": ["k3s agent join"],
}}


@pytest.mark.smoke
def test_findings_are_added_as_their_own_keys():
    out = dag_generator._brief_with_engine_findings(
        {**_K8S_BRIEF, "ambiguities": _AMBIG, "user_feedback": _FEEDBACK}, json.dumps(_RESEARCH))
    answers = {a["question"]: a["answer"] for a in out["operator_answers"]}
    assert "work together as one pool" in answers[_AMBIG[4]]
    assert _AMBIG[5] not in answers                          # "unsure" stays an open question (§17.1218)
    assert out["engine_feasibility"]["risks"] == _RESEARCH["feasibility"]["risks"]
    assert "recommended_research_queries" not in out["engine_feasibility"]
    # fail-soft: nothing to add → nothing added
    assert dag_generator._brief_with_engine_findings({"title": "x"}, "not json{") == {"title": "x"}


async def test_generate_dag_hands_the_planner_the_answers_and_the_feasibility():
    """Production path: generate_dag → the brief the planner is prompted with."""
    seen = {}

    async def _stub(brief_data, route_kwargs):
        seen["brief"] = brief_data
        return {"dag_data": None, "raw_text": "", "model": "m", "duration_ms": 0, "warnings": [],
                "error": "stop here", "attempts": 1, "validator_calls": 0}

    row = MagicMock()
    row.first.return_value = ("planning", {**_K8S_BRIEF, "ambiguities": _AMBIG, "user_feedback": _FEEDBACK},
                              None, json.dumps(_RESEARCH), None, 0, False)
    db = AsyncMock()
    db.execute = AsyncMock(return_value=row)
    with patch.object(dag_generator, "_generate_dag_with_validator", _stub), \
         patch.object(dag_generator, "_fail_job", new_callable=AsyncMock):
        await dag_generator.generate_dag(str(uuid4()), db)
    rendered = dag_generator.DAG_PROMPT.format(brief=json.dumps(seen["brief"], indent=2), max_nodes=40)
    assert '"operator_answers"' in rendered and "work together as one pool" in rendered
    assert '"engine_feasibility"' in rendered and "must use k3s" in rendered
    assert "§17.1725" in rendered and "§17.1726" in rendered


# ---------------------------------------------------------------------------
# §17.1729 — a count nobody gave is not planned as numbered copies
# ---------------------------------------------------------------------------

_LIVE_TITLES = [  # job f9ca3ee1's re-plan, 2026-10-21 (titles only)
    ("T11", "Install k3s control plane"), ("T13", "Install k3s agent laptop1"),
    ("T14", "Install k3s agent laptop2"), ("T15", "Install k3s agent laptop3"),
    ("T16", "Install k3s agent laptop4"), ("T18", "Deploy Ollama on laptop1"),
    ("T19", "Deploy Ollama on laptop2"), ("T20", "Deploy Ollama on laptop3"),
    ("T21", "Deploy Ollama on laptop4"), ("T22", "Configure engine pool URLs"),
]


@pytest.mark.smoke
def test_numbered_copies_for_a_count_nobody_gave_are_found():
    tasks = [{"id": i, "name": n} for i, n in _LIVE_TITLES]
    got = dag_generator.detect_invented_counts(tasks, json.dumps(_K8S_BRIEF))
    assert [g["ids"] for g in got] == [["T13", "T14", "T15", "T16"], ["T18", "T19", "T20", "T21"]]
    # the same steps are fine when the operator SAID how many
    assert dag_generator.detect_invented_counts(tasks, "I have four old laptops") == []
    assert dag_generator.detect_invented_counts(tasks, "the 4 Dell laptops") == []
    # numbered things that are names, not a count: VM 106 / VM 110, net0
    named = [{"id": "A", "name": "Start VM 106"}, {"id": "B", "name": "Start VM 110"},
             {"id": "C", "name": "Attach veth105i0 to vmbr0"}]
    assert dag_generator.detect_invented_counts(named, "") == []


async def test_the_planner_is_sent_back_to_write_one_node(monkeypatch):
    """Production loop: an invented count drives a redraft carrying the correction."""
    from app import model_router as _mr
    monkeypatch.setattr(settings, "dag_validator_enabled", True)
    monkeypatch.setattr(settings, "dag_validator_max_retries", 1)
    copies = json.dumps({"strategy": "s", "tasks": [
        {"id": f"T{i}", "name": f"Install k3s agent laptop{i}", "type": "action", "inputs": [], "outputs": [f"o{i}"],
         "depends_on": [], "tool": "Shell", "domain": None, "assigned_model": None, "notes": "n",
         "is_deliverable": i == 2} for i in (1, 2)]})
    prompts: list[str] = []
    replies = iter([_resp(copies), _resp(json.dumps({"issues": []})), _resp(_DAG), _resp(json.dumps({"issues": []}))])

    async def _gen(prompt, *a, **k):
        prompts.append(prompt)
        return next(replies)
    with patch.object(_mr, "generate", new=AsyncMock(side_effect=_gen)), \
         patch("app.modules.flywheel.retrieve_exemplars", new=AsyncMock(return_value="")):
        result = await dag_generator._generate_dag_with_validator(_K8S_BRIEF, {"role": "model_general"})
    assert result["attempts"] == 2
    redraft = [p for p in prompts if "Decompose this refined brief" in p][1]
    assert "Invented counts" in redraft and "T1, T2" in redraft
    assert any(w.startswith("invented_count_found_1_attempt_1") for w in result["warnings"])


@pytest.mark.smoke
def test_copies_the_redrafts_left_are_merged_into_one_node():
    """Live: after three drafts T14–T17 'Install k3s agent laptop N' were still four copies."""
    tasks = [
        {"id": "T13", "name": "Retrieve k3s join token", "depends_on": ["T12"]},
        *[{"id": f"T{13 + i}", "name": f"Install k3s agent laptop {i}", "depends_on": ["T13"],
           "notes": f"SSH to laptop {i}, install k3s agent."} for i in (1, 2, 3, 4)],
        {"id": "T18", "name": "Verify cluster node readiness", "depends_on": ["T14", "T15", "T16", "T17"]},
        {"id": "T19", "name": "Deploy Ollama on laptop1", "depends_on": ["T18"]},
        {"id": "T20", "name": "Deploy Ollama on laptop2", "depends_on": ["T18"]},
        {"id": "T21", "name": "Configure engine pool", "depends_on": ["T19", "T20"]},
    ]
    merged = dag_generator.collapse_invented_counts(tasks, json.dumps(_K8S_BRIEF))
    assert [g["ids"] for g in merged] == [["T14", "T15", "T16", "T17"], ["T19", "T20"]]
    by = {t["id"]: t for t in tasks}
    assert set(by) == {"T13", "T14", "T18", "T19", "T21"}
    assert by["T14"]["name"] == "Install k3s agent, every laptop"
    assert by["T19"]["name"] == "Deploy Ollama on every laptop"
    assert by["T14"]["depends_on"] == ["T13"]
    assert by["T18"]["depends_on"] == ["T14"] and by["T21"]["depends_on"] == ["T19"]
    assert "EVERY laptop confirmed earlier" in by["T14"]["notes"]
    assert dag_generator.collapse_invented_counts(tasks, json.dumps(_K8S_BRIEF)) == []    # idempotent


async def test_generate_dag_merges_what_the_redrafts_left(monkeypatch):
    """Production path: generate_dag persists ONE node, not the copies."""
    copies = {"strategy": "s", "tasks": [
        {"id": "T1", "name": "Confirm the laptops", "type": "action", "inputs": [], "outputs": ["list"],
         "depends_on": [], "tool": "LLM", "domain": None, "assigned_model": None, "notes": "n"},
        *[{"id": f"T{i + 1}", "name": f"Install k3s agent laptop{i}", "type": "action", "inputs": ["list"],
           "outputs": [f"a{i}"], "depends_on": ["T1"], "tool": "Shell", "domain": None, "assigned_model": None,
           "notes": "n"} for i in (1, 2, 3)],
        {"id": "T5", "name": "Verify nodes", "type": "validation", "inputs": ["a1"], "outputs": ["ok"],
         "depends_on": ["T2", "T3", "T4"], "tool": "Shell", "domain": None, "assigned_model": None, "notes": "n",
         "is_deliverable": True}]}

    async def _stub(brief_data, route_kwargs):
        return {"dag_data": json.loads(json.dumps(copies)), "raw_text": "", "model": "m", "duration_ms": 0,
                "warnings": [], "error": None, "attempts": 3, "validator_calls": 3}

    inserted: list[str] = []
    row = MagicMock()
    row.first.return_value = ("planning", dict(_K8S_BRIEF), None, None, None, 0, False)

    async def _exec(stmt, params=None):
        sql = str(stmt)
        if "INSERT INTO dag_nodes" in sql and params:
            for p in (params if isinstance(params, list) else [params]):
                inserted.append(p.get("title") or p.get("name") or "")
        return row
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=_exec)
    with patch.object(dag_generator, "_generate_dag_with_validator", _stub):
        result = await dag_generator.generate_dag(str(uuid4()), db)
    assert any("invented_counts_merged" in w for w in (result.get("warnings") or [])), result
    titles = " | ".join(inserted)
    assert "Install k3s agent, every laptop" in titles
    assert "laptop2" not in titles and "laptop3" not in titles
