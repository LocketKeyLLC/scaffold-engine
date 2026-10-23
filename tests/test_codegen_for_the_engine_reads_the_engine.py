"""§17.1749 — a CodeGen step writing code for the engine itself is shown the engine, and refused when it reaches for
an engine module, name, setting or package that does not exist.

Live (job 2772acae "Paper-trading research lab in scaffold engine", 2026-10-22): the steps were never shown the engine
and guessed `app.clients.searxng`, `app.utils.searxng`, `settings.SEARXNG_BASE_URL`, `engine.secrets_store`,
`import websocket`, `requests` and `psycopg2` -- each behind try/except, so a wrong guess fell through to a made-up
default. The programs below are cut down from those outputs; names in them are the live ones, the bodies are not.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.modules.execution_agent  # noqa: F401 — load-bearing (real app.database)
from app.modules import engine_source as es

LIVE_BRIEF = {
    "title": "Paper-trading research lab in scaffold engine",
    "description": "Add a paper-trading research lab to the scaffold engine's own codebase so the engine can research, "
                   "propose, and track US stock trades without risking money.",
    "goals": ["Add a paper-trading research lab module to this repository's code that researches, proposes, and "
              "tracks US stock trades using a free Alpaca paper-trading account."],
}

NEWS_CLIENT = '''```python
import requests
from app.config import settings


def _url():
    try:
        return getattr(settings, "SEARXNG_BASE_URL", "http://localhost:8080")
    except Exception:
        return "http://localhost:8080"


def _search(q):
    try:
        from app.clients.searxng import search
        return search(query=q)
    except Exception:
        pass
    try:
        from app.utils.searxng import searxng_search
        return searxng_search(query=q)
    except Exception:
        pass
    return requests.get(_url() + "/search", params={"q": q}).json()
```'''

PAPER_CLIENT = '''```python
import os
from engine.secrets_store import get_secret
PAPER_BASE_URL = os.environ.get("ALPACA_PAPER_BASE_URL", "https://paper-api.alpaca.markets")
```'''

GROUNDED = '''```python
import logging
import httpx
from sqlalchemy import text
from app.config import settings
from app.database import async_session
from app.modules.runner_secrets import values_for
from app.utils.http_clients import get_searxng_client
from alpaca_client import submit_order
from app.modules.paper_lab.risk_controls import check_max_position

logger = logging.getLogger("scaffold")
PAPER_BASE_URL = "https://paper-api.alpaca.markets"


async def news(q):
    r = await get_searxng_client().get(settings.searxng_url + "/search", params={"q": q, "format": "json"})
    return r.json()


async def keys():
    async with async_session() as db:
        return await values_for(db, ["ALPACA_PAPER_KEY_ID"])


def window():
    return settings.lab_proposal_hour
```'''

# what the job's own steps write: T9 `alpaca_client.py`, T11 `risk_controls.py`, and a settings field T2 adds
SIBLINGS = {"alpaca_client", "risk_controls"}
SIBLING_NAMES = {"submit_order", "check_max_position", "lab_proposal_hour"}


# ── which jobs it is for ──────────────────────────────────────────────────────

def test_the_live_brief_targets_the_engine():
    assert es.targets_the_engine(LIVE_BRIEF)
    assert es.targets_the_engine({"description": "Add a dashboard to the scaffold engine itself."})
    assert es.targets_the_engine({"goals": ["Extend the engine's own codebase with a CSV export."]})


@pytest.mark.parametrize("brief", [
    {"title": "VM 110: redeploy engine services", "description": "Redeploy the scaffold-engine on VM 110 at main."},
    {"description": "Build a k3s cluster from old laptops with the AI VM as head, to add compute for the engine."},
    {"description": "Write a Python CLI that renames photos by date.", "goals": ["A tool in its own repository."]},
    {"description": "Configure the VPN kill-switch for CT 105."},
    {}, None,
])
def test_other_jobs_are_left_alone(brief):
    assert not es.targets_the_engine(brief)


# ── the refusal ──────────────────────────────────────────────────────────────

def test_the_live_news_client_is_refused_for_each_guess():
    found = es.unresolved_refs(NEWS_CLIENT, SIBLINGS, SIBLING_NAMES)
    joined = "\n".join(found)
    assert "the engine has no module `app.clients.searxng`" in joined
    assert "the engine has no module `app.utils.searxng`" in joined
    assert "`settings.SEARXNG_BASE_URL`: the engine has no such setting (it has `searxng_url`" in joined
    assert "`import requests`: blocking HTTP" in joined
    assert len(found) == 4, found


def test_a_missing_setting_is_told_what_to_do_and_not_offered_a_stranger():
    """§17.1753 — live (T3's redraft): `getattr(settings, "edgar_user_agent", None) or DEFAULT` was refused, and the
    hint offered `multi_user_enabled` because both contain "user"."""
    found = es.unresolved_refs('```python\nfrom app.config import settings\nua = getattr(settings, "edgar_user_agent", None)\n```')
    assert len(found) == 1 and "`settings.edgar_user_agent`: the engine has no such setting" in found[0]
    assert "multi_user_enabled" not in found[0]
    assert "module constant" in found[0] and "app.config.Settings" in found[0]


def test_an_invented_package_is_refused():
    found = es.unresolved_refs(PAPER_CLIENT, SIBLINGS, SIBLING_NAMES)
    assert found == ["`from engine.secrets_store import …`: `engine` is not installed in the engine"]


def test_a_name_missing_from_a_real_module_is_refused_with_what_it_has():
    found = es.unresolved_refs("```python\nfrom app.modules.runner_secrets import get_secret\n```")
    assert len(found) == 1 and "`app.modules.runner_secrets` has no `get_secret`" in found[0]
    assert "values_for" in found[0]


def test_code_written_against_the_engine_and_its_siblings_passes():
    assert es.unresolved_refs(GROUNDED, SIBLINGS, SIBLING_NAMES) == []


def test_without_the_siblings_their_names_are_refused():
    """The sibling allowance is what makes `from alpaca_client import …` pass -- it is not a blanket pass."""
    found = "\n".join(es.unresolved_refs(GROUNDED))
    assert "`alpaca_client` is not installed" in found
    assert "`settings.lab_proposal_hour`" in found


def test_the_job_own_code_is_read_from_its_steps():
    mods, names = es.job_own_code([
        "Implement Alpaca paper client: Python module `alpaca_client.py` with submit_order",
        "```python\ndef submit_order(symbol, qty):\n    ...\nlab_proposal_hour: int = 8\n```",
    ])
    assert "alpaca_client" in mods and {"submit_order", "lab_proposal_hour"} <= names


def test_non_python_and_unparsable_blocks_are_not_its_business():
    assert es.unresolved_refs("```sql\nSELECT 1;\n```\n```python\ndef broken(:\n```") == []


# ── §17.1754 the engine's capabilities, named ────────────────────────────────

def test_every_name_the_capability_list_promises_exists_in_the_engine():
    """The list is hand-written: when code moves, this fails instead of the list going blind (a gate goes blind when
    code moves)."""
    gone = [(m, n) for c in es.CAPABILITIES for m, names in c["names"].items() for n in names
            if not es._signature_of(m, n)]
    assert gone == []
    assert all(m in es.module_index() for c in es.CAPABILITIES for m in c["names"])


def test_the_live_approval_step_is_shown_the_decision_pause():
    t14 = {"title": "Implement approve-before-run step",
           "prompt_template": "Python module `approval_step.py` with `request_approval(proposal) -> bool`. Reuses the "
                              "engine's existing approve-before-run runner-step pattern."}
    assert [c["name"] for c in es.capabilities_for(t14)][0] == "operator approval (approve-before-run)"
    b = es.block_for(t14)
    assert "async def park_awaiting_decision(db, job_id, node, frame)" in b
    assert "never input()" in b


def test_a_step_that_names_no_capability_is_shown_none():
    assert es.capabilities_for({"title": "Implement FINRA data client",
                                "prompt_template": "Fetch short interest files and parse them."}) == []


def test_input_in_engine_code_is_refused():
    found = es.unresolved_refs("```python\ndef request_approval(p):\n    return input('approve? ') == 'y'\n```")
    assert len(found) == 1 and "decision pause" in found[0]


def test_a_missing_setting_is_never_offered_a_pydantic_method():
    """§17.1754 — live (T17): `backtest_model_name` was offered `model_copy`, `model_dump`."""
    found = es.unresolved_refs("```python\nfrom app.config import settings\nm = settings.backtest_model_name\n```")
    assert len(found) == 1 and "model_copy" not in found[0] and "model_dump" not in found[0]


# ── what the step is shown ───────────────────────────────────────────────────

def test_the_block_shows_the_real_engine():
    b = es.block_for({"title": "Implement news signal client", "prompt_template": "Fetch news via SearXNG."},
                     SIBLINGS)
    assert "async def values_for(db, wanted)" in b                       # real signature, read from the source
    assert "names: engine, async_session" in b                           # a module-level name, not only defs
    assert "def get_searxng_client()" in b
    assert "app.modules: " in b and "runner_secrets" in b                 # the package layout
    assert "alpaca_client" in b and "risk_controls" in b                  # the job's own modules
    assert len(b) <= 9000


def test_settings_are_offered_only_for_a_rare_word_in_the_title():
    assert es.matching_settings({"reddit", "signal", "client"})[:1] == ["reddit_max_posts"]
    assert es.matching_settings({"order", "executor"}) == []              # "order" is common: not offered


# ── the production path ──────────────────────────────────────────────────────

def _session(db):
    f = MagicMock()
    f.return_value.__aenter__ = AsyncMock(return_value=db)
    f.return_value.__aexit__ = AsyncMock(return_value=False)
    return f


async def _run(output: str, brief: dict):
    from app.modules import execution_agent as xa
    db = AsyncMock(); db.commit = AsyncMock()
    rows = MagicMock(); rows.mappings.return_value.all.return_value = [
        {"node_key": "T9", "title": "Implement Alpaca paper client",
         "description": "", "prompt_template": "Python module `alpaca_client.py`", "output_text": ""}]
    db.execute = AsyncMock(return_value=rows)
    job = AsyncMock(return_value={"id": "job-1", "status": "running", "refined_brief": brief})
    node = AsyncMock(return_value={
        "id": "node-7", "node_key": "T7", "title": "Implement news signal client", "tool": "CodeGen",
        "prompt_template": "Python module `news_client.py`: fetch news via the engine's SearXNG.", "domain": None,
        "depends_on": [], "assigned_model": None, "retry_count": 0, "last_verification_reason": None})
    captured: dict = {}

    async def chat(messages, **kw):
        captured["prompt"] = messages[-1]["content"]
        return SimpleNamespace(text=output, success=True, model="m", error=None, tool_calls=None,
                               fallback_used=False, prompt_tokens=1, completion_tokens=1, retries=0)
    set_status = AsyncMock()
    verify = AsyncMock(return_value=("pass", "fine", 0.9))
    with patch.object(xa.settings, "execution_optimize_enabled", False), \
         patch.object(xa.settings, "best_of_n_enabled", False), \
         patch.object(xa.settings, "node_token_streaming_enabled", False), \
         patch.object(xa.settings, "codegen_execution_check_enabled", False), \
         patch("app.modules.execution_agent.async_session", _session(db)), \
         patch("app.modules.execution_agent._get_job", job), \
         patch("app.modules.execution_agent._get_next_node", node), \
         patch("app.modules.execution_agent._fetch_upstream_outputs", new=AsyncMock(return_value={})), \
         patch("app.modules.execution_agent.fetch_upstream_flagged", new=AsyncMock(return_value=set())), \
         patch("app.modules.execution_agent._fetch_rag_context", new=AsyncMock(return_value="")), \
         patch("app.modules.execution_agent.model_router.chat", new=chat), \
         patch("app.modules.execution_agent._verify_codegen_output", new=verify), \
         patch("app.modules.execution_agent._set_node_status", new=set_status), \
         patch("app.modules.execution_agent._log_execution", new=AsyncMock()):
        try:
            await xa.execute_next_node("job-1")
        except Exception as exc:                                  # the persistence tail is not under test
            captured["raised"] = exc
    reasons = [str(c.kwargs.get("verification_reason") or "") for c in set_status.call_args_list]
    return captured, reasons, verify


@pytest.mark.asyncio
async def test_the_executor_shows_the_engine_and_fails_the_live_guess():
    captured, reasons, verify = await _run(NEWS_CLIENT, LIVE_BRIEF)
    assert "## The engine's own code (read from the running engine, §17.1749)" in captured["prompt"]
    hit = [r for r in reasons if "§17.1749" in r]
    assert hit and "app.clients.searxng" in hit[0], reasons
    verify.assert_not_awaited()                                   # refused before the LLM verifier is spent


@pytest.mark.asyncio
async def test_the_executor_leaves_another_jobs_code_alone():
    captured, reasons, verify = await _run(NEWS_CLIENT, {"description": "Write a news CLI.", "goals": ["A CLI."]})
    assert "§17.1749" not in captured["prompt"]
    assert not [r for r in reasons if "§17.1749" in r]
    verify.assert_awaited()
