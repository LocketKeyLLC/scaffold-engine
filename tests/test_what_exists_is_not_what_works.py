"""§17.1262b/1263 — a catalogue says what EXISTS; the web says what still works.

Two halves of one defect the operator named: *"this is the exact reason for the
researcher component to be wired up to fixing issues. It should find the most
current up to date and available indexers."*

  §17.1262b  the step-drafting lookup must carry the operator's SYSTEM into the
             query, not just the step's own words (the §17.1020 rule, which the
             first cut of §17.1262 broke on arrival);
  §17.1263   a rejected REQUEST and an unreachable THING are not the same
             failure. §17.1258 stops a block that repeats its own mistake; a
             list of dead third parties is not that, and stopping on the first
             corpse adds none of the ones that work.
"""
import re

import pytest

from app.modules import supervised_runs as sr

_ADD115 = {
    "node_key": "ADD115",
    "title": "Add the most current working public indexers to Prowlarr",
    "description": "Find which public trackers are actually available right now and add those.",
}


# ───────────────────────────────── §17.1263 — rejected vs unreachable

def _lines(ok: int, fail: int, err: str) -> str:
    return "\n".join([f"added: Tracker{i}" for i in range(ok)]
                     + [f"failed: Thing{i} - HTTP Error {err}" for i in range(fail)])


def test_a_repeated_rejection_still_fails_the_step():
    """§17.1258's case, unchanged: 88 identical 400s is one diagnosis thrown away."""
    why = sr.repeated_identical_failures(_lines(0, 88, "400: Bad Request"))
    assert why and "88 times" in why and "RESPONSE BODY" in why


def test_a_rejection_fails_even_when_some_landed():
    """A malformed body is OURS to fix whether or not anything else worked."""
    assert sr.repeated_identical_failures(_lines(3, 10, "400: Bad Request"))


def test_dead_third_parties_do_not_fail_a_step_that_landed_things():
    """54 dead trackers after 35 were added is a finished step, not a stuck one."""
    assert sr.repeated_identical_failures(_lines(35, 54, "502: Bad Gateway")) is None


@pytest.mark.parametrize("err", ["502: Bad Gateway", "503: Service Unavailable",
                                 "504: Gateway Timeout"])
def test_every_unreachable_status_is_treated_the_same(err):
    assert sr.repeated_identical_failures(_lines(5, 9, err)) is None


def test_everything_unreachable_is_still_reported():
    """Nothing landed and all 54 timed out — that is a shared cause (DNS, live
    ADD116), and the operator must be told."""
    why = sr.repeated_identical_failures(_lines(0, 54, "502: Bad Gateway"))
    assert why and "54 times" in why


def test_the_rule_reaches_the_drafter():
    """A detector that fails a step must have told the drafter the rule first."""
    rules = sr.CHANNEL_RULES
    assert "WHAT EXISTS, NOT WHAT WORKS" in rules
    low = rules.lower()
    assert "skip it" in low and "keep going" in low


# ───────────────────────────────── §17.1262b — the query carries the system

def test_no_currency_wording_means_no_lookup():
    node = {"node_key": "ADD50", "title": "Start container 111",
            "description": "`pct start 111`."}
    assert sr.currency_question(node) == ""


@pytest.mark.asyncio
async def test_the_step_lookup_grounds_in_the_operators_machine(monkeypatch):
    """The query must name the operator's hardware, not just the step's words.

    §17.1021 measured an LLM generator dropping a model number three times when
    merely asked for it; `finalize_query` is where the request becomes a rule.
    """
    seen = {}

    async def _fake_research_one(**kw):
        seen.update(kw)
        return {"sources": [{"url": "https://example.test/a", "title": "t",
                             "snippet": "s", "authority": 0.9}]}

    import app.modules.assist_research_lib as lib
    monkeypatch.setattr(lib, "research_one", _fake_research_one)
    monkeypatch.setattr(lib, "_render_research_block", lambda s: "## Current sources\n- a")

    env = {"profile": "The Proxmox host has an RTX 3090 graphics card.\n"
                      "Prowlarr runs in container 103."}
    out = await sr.research_for_step(_ADD115, env)

    assert "Current sources" in out
    q = seen["question"]
    assert q, "nothing was searched"
    # the step's own subject survives …
    assert "indexer" in q.lower()
    # … and the environment reached the retrieval call rather than being dropped
    assert seen["prerequisite_env"] == env
    assert "Proxmox" in (seen["context_hint"] or "")


@pytest.mark.asyncio
async def test_a_failed_lookup_leaves_the_prompt_alone(monkeypatch):
    async def _boom(**kw):
        raise RuntimeError("searxng down")

    import app.modules.assist_research_lib as lib
    monkeypatch.setattr(lib, "research_one", _boom)
    assert await sr.research_for_step(_ADD115, {"profile": "x"}) == ""


def test_the_lookup_is_grounded_by_an_approved_builder():
    """The §17.1020 inventory gate enumerates the builders; this names WHICH one
    this path uses, so a refactor that drops it fails here too."""
    import inspect
    src = inspect.getsource(sr.research_for_step)
    assert "derive_need(" in src and "finalize_query(" in src
    assert re.search(r"research_one\(question=query", src), \
        "the grounded query must be what is actually searched"
