"""§17.1866 — a parent row inserted in the test's session is committed before a writer with its own session; and a
dict a function reads keys from is shown those keys and checked for the ones it must carry.

Live (follow-up 5b4880c0 T5, 2026-10-29, four attempts): attempts 1 and 3 inserted the parent through a helper of
their own (`insert_ingested_item(db, …)`, an INSERT with no commit) and then called `write_signal(ingested_item_id=…)`,
which opens its own `async_session()` -- "Key (ingested_item_id)=(1) is not present in table ingested_item"; attempt
3 also called `write_approval(proposal_id=…)` one line before its `await db.commit()`. Attempts 2 and 4, whose helpers
committed, passed the same tests. The sandbox's §17.1853 cause read every one as a made-up id. And three attempts
called `handle_approval_answer(…, payload={"proposal": {"id": …}})` where the gate reads
`payload["proposal"]["proposal_id"]` into `approval.proposal_id` (a foreign key, not null): the index showed
`payload: dict` and nothing of what it is read for."""
from __future__ import annotations

from app.modules import engine_source as es
from app.modules.engine_contracts import findings_in_tests, uncommitted_parents
from app.sandbox import engine_tests as et

GATE = '''# app/modules/paper_operator_approval_gate.py
from typing import Any
from sqlalchemy import text
from app.database import async_session

async def _record_rejection(db, proposal: dict[str, Any], reason: str) -> None:
    await db.execute(text("INSERT INTO approval (proposal_id, operator_id, decision, reason) "
                          "VALUES (:proposal_id, :operator_id, :decision, :reason)"),
                     {"proposal_id": proposal.get("proposal_id"), "operator_id": "operator",
                      "decision": "rejected", "reason": reason})

async def handle_approval_answer(job_id: str, node_key: str, answer: dict[str, Any], payload: dict[str, Any]) -> None:
    if not answer.get("approved"):
        proposal = payload.get("proposal", {})
        async with async_session() as db:
            await _record_rejection(db, proposal, answer.get("note") or "rejected")
        return
    proposal_id = payload.get("proposal_id")

async def act(payload: dict[str, Any]) -> None:
    proposal_id = payload["proposal_id"]

async def request_approval(proposal: dict[str, Any], *, db=None) -> dict:
    return {"proposal_id": proposal.get("proposal_id")}
'''
WRITER = '''# app/modules/paper_audit_trail_writer.py
from sqlalchemy import text
from app.database import async_session

async def write_signal(*, ingested_item_id: int, ticker: str) -> int:
    async with async_session() as db:
        r = await db.execute(text("INSERT INTO signal (ingested_item_id, ticker) VALUES (:i, :t) RETURNING id"),
                             {"i": ingested_item_id, "t": ticker})
        await db.commit()
        return r.scalar_one()

async def write_proposal(*, signal_id: int, ticker: str) -> int:
    async with async_session() as db:
        r = await db.execute(text("INSERT INTO proposal (signal_id, ticker) VALUES (:s, :t) RETURNING id"),
                             {"s": signal_id, "t": ticker})
        await db.commit()
        return r.scalar_one()

async def write_approval(*, proposal_id: int, decision: str) -> int:
    async with async_session() as db:
        r = await db.execute(text("INSERT INTO approval (proposal_id, decision) VALUES (:p, :d) RETURNING id"),
                             {"p": proposal_id, "d": decision})
        await db.commit()
        return r.scalar_one()
'''
FKS = {"signal": {"ingested_item_id": "ingested_item.id"}, "proposal": {"signal_id": "signal.id"},
       "approval": {"proposal_id": "proposal.id"}}
MODULES = {"paper_audit_trail_writer": WRITER.split("\n", 1)[1], "paper_operator_approval_gate": GATE.split("\n", 1)[1]}
PATH = "tests/test_paper_twelve_modules.py"


def _defs():
    return es.sibling_definitions([{"node_key": "T1", "status": "done", "output_text": "```python\n" + GATE + "```"},
                                   {"node_key": "T2", "status": "done", "output_text": "```python\n" + WRITER + "```"}])


def test_the_index_shows_the_keys_a_dict_parameter_is_read_for_and_who_opens_a_session():
    defs = _defs()
    out = es._render_job_modules(defs, "")
    assert ("handle_approval_answer(job_id: str, node_key: str, answer: dict{approved, note}, "
            "payload: dict{proposal{proposal_id}, proposal_id}) [own session]") in out
    assert "request_approval(proposal: dict{proposal_id}, *, db=…)" in out and "act(payload: dict{proposal_id})" in out
    assert "write_signal(*, ingested_item_id: int, ticker: str)" in out
    assert "`[own session]` means it opens its own `async_session()` and sees only rows the test has committed" in out
    assert "write_signal(*, ingested_item_id: int, ticker: str)" in es._public_api(WRITER) and "[own session]" in es._public_api(WRITER)
    # a function with a `db` parameter is not tagged, whatever it opens
    assert "_record_rejection" not in defs["paper_operator_approval_gate"]["own_session"]
    assert defs["paper_operator_approval_gate"]["own_session"] == {"handle_approval_answer"}


def test_a_dict_without_the_key_a_not_null_foreign_key_column_takes_is_refused():
    test = '''# tests/test_paper_twelve_modules.py
from app.modules import paper_operator_approval_gate

async def test_refuse():
    proposal = {"id": 7, "ticker": "SPY"}
    payload = {"proposal": proposal}
    await paper_operator_approval_gate.handle_approval_answer("j", "n", {"approved": False}, payload)
    await paper_operator_approval_gate.act({"proposal": {"proposal_id": 7}})
'''
    found = es.sibling_refs("```python\n" + test + "```", _defs(), fks=FKS, rules={})
    assert len(found) == 2, found
    assert ('`tests/test_paper_twelve_modules.py:7` calls `handle_approval_answer(…)`: `payload`["proposal"] is read for '
            "`proposal_id` (written to `approval.proposal_id`, which references `proposal`) -- the dict given has `id`, "
            "`ticker` and no `proposal_id`; pass the id its parent's writer returned") in found[0]
    # `payload["proposal_id"]` is read with brackets: it raises without the key
    assert ('`tests/test_paper_twelve_modules.py:8` calls `act(…)`: `payload` is read for `proposal_id` '
            '(as `["proposal_id"]`, which raises without it) -- the dict given has `proposal` and no `proposal_id`') in found[1]
    assert findings_in_tests(found) == found                                   # §17.1859: a redraw, not the attempt
    # the key under `proposal` only reaches a column through `.get`: a dict that carries it (the id a writer returned,
    # never a number -- §17.1869) passes
    ok = test.replace('{"id": 7, "ticker": "SPY"}', '{"proposal_id": pid}').replace('{"proposal": {"proposal_id": 7}}', '{"proposal_id": pid}')
    ok = ok.replace("async def test_refuse():\n", "async def test_refuse():\n    pid = await write_proposal(signal_id=1, ticker='SPY')\n")
    assert es.sibling_refs("```python\n" + ok + "```", _defs(), fks=FKS, rules={}) == []


def test_a_number_under_a_key_written_to_a_foreign_key_is_a_made_up_id():
    """§17.1869 — live (5b4880c0 T5, 2026-10-30): `handle_approval_answer(…, {"proposal": {"proposal_id": 1}, …})`
    reached the sandbox and the scratch database refused `approval.proposal_id`=1."""
    test = '''# tests/test_paper_twelve_modules.py
from app.modules import paper_operator_approval_gate

async def test_refuse():
    await paper_operator_approval_gate.handle_approval_answer("j", "n", {"approved": False}, {"proposal": {"proposal_id": 1}, "proposal_id": 1})
'''
    found = es.sibling_refs("```python\n" + test + "```", _defs(), fks=FKS, rules={})
    assert len(found) == 1 and found[0].startswith(
        '`tests/test_paper_twelve_modules.py:5` calls `handle_approval_answer(…)`: `payload`["proposal"]["proposal_id"] is '
        "written to `approval.proposal_id`, which references `proposal`; it was given the number 1 -- a made-up id the "
        "scratch database refuses: pass the id its parent's writer returned"), found


def _files(test: str) -> dict[str, str]:
    return {PATH: "# " + PATH + "\n" + test}


def test_the_live_uncommitted_parents_are_refused_and_the_committed_ones_pass():
    a1 = '''from sqlalchemy import text
from app.database import async_session
from app.modules import paper_audit_trail_writer

async def insert_ingested_item(db, *, source="news"):
    result = await db.execute(text("INSERT INTO ingested_item (source) VALUES (:s) RETURNING id"), {"s": source})
    row = result.fetchone()
    return int(row[0])

async def test_write_signal():
    async with async_session() as db:
        ingested_id = await insert_ingested_item(db, source="news")
        signal_id = await paper_audit_trail_writer.write_signal(ingested_item_id=ingested_id, ticker="AAPL")
        proposal_id = await paper_audit_trail_writer.write_proposal(signal_id=signal_id, ticker="AAPL")
        await db.commit()
'''
    found = uncommitted_parents(_files(a1), MODULES, FKS)
    assert len(found) == 1, found                     # the writer's own result (`signal_id`) is committed: not refused
    assert found[0].startswith(
        "`tests/test_paper_twelve_modules.py:14` hands `write_signal(…)` `ingested_id`, the id `insert_ingested_item(db, …)` "
        "returned at line 13 (an INSERT with no commit) -- a row in the test's own session, not committed: `write_signal` "
        "opens its own `async_session()` and sees only committed rows, so the scratch database refuses the child "
        "(`signal.ingested_item_id` references `ingested_item`: no such row). `await db.commit()` after inserting the "
        "parent rows, before the call")
    a3 = '''from sqlalchemy import text
from app.database import async_session
from app.modules.paper_audit_trail_writer import write_approval

async def _insert_proposal(db, *, signal_id):
    r = await db.execute(text("INSERT INTO proposal (signal_id) VALUES (:s) RETURNING id"), {"s": signal_id})
    return r.scalar_one()

async def test_write_order():
    async with async_session() as db:
        r = await db.execute(text("INSERT INTO signal (ingested_item_id) VALUES (1) RETURNING id"))
        signal_id = r.scalar_one()
        proposal_id = await _insert_proposal(db, signal_id=signal_id)
        approval_id = await write_approval(proposal_id=proposal_id, decision="approved")
        await db.commit()
'''
    found = uncommitted_parents(_files(a3), MODULES, FKS)
    assert len(found) == 1 and "`tests/test_paper_twelve_modules.py:15` hands `write_approval(…)` `proposal_id`, the id " \
        "`_insert_proposal(db, …)` returned at line 14" in found[0], found
    # the commit one line earlier, or a helper that commits, or a helper that writes through the writer: nothing to say
    committed = a3.replace("        approval_id = await write_approval", "        await db.commit()\n        approval_id = await write_approval")
    assert uncommitted_parents(_files(committed), MODULES, FKS) == []
    helper_commits = a3.replace("    return r.scalar_one()", "    await db.commit()\n    return r.scalar_one()")
    assert [f for f in uncommitted_parents(_files(helper_commits), MODULES, FKS) if "write_approval" in f] == []
    a2 = '''from app.database import async_session
from app.modules import paper_audit_trail_writer

async def _insert_signal(db, *, ingested_item_id):
    return await paper_audit_trail_writer.write_signal(ingested_item_id=ingested_item_id, ticker="SPY")

async def test_it():
    async with async_session() as db:
        signal_id = await _insert_signal(db, ingested_item_id=1)
        await paper_audit_trail_writer.write_proposal(signal_id=signal_id, ticker="SPY")
'''
    assert uncommitted_parents(_files(a2), MODULES, FKS) == []


def test_a_helper_that_forwards_to_the_writer_is_named_with_it():
    test = '''from sqlalchemy import text
from app.database import async_session
from app.modules import paper_audit_trail_writer

async def _insert_ingested_item(db):
    r = await db.execute(text("INSERT INTO ingested_item (source) VALUES ('x') RETURNING id"))
    return r.scalar_one()

async def _insert_signal(db, *, ingested_item_id):
    return await paper_audit_trail_writer.write_signal(ingested_item_id=ingested_item_id, ticker="SPY")

async def test_it():
    async with async_session() as db:
        item_id = await _insert_ingested_item(db)
        signal_id = await _insert_signal(db, ingested_item_id=item_id)
'''
    found = uncommitted_parents(_files(test), MODULES, FKS)
    assert len(found) == 1 and found[0].startswith(
        "`tests/test_paper_twelve_modules.py:16` hands `_insert_signal(…)` -- which calls `write_signal` -- `item_id`, the id "
        "`_insert_ingested_item(db, …)` returned at line 15 (an INSERT with no commit)"), found


def test_the_sandbox_cause_names_the_uncommitted_parent_too():
    cause = et.known_causes('E   asyncpg.exceptions.ForeignKeyViolationError: insert or update on table "signal" violates '
                            'foreign key constraint "signal_ingested_item_id_fkey"\n'
                            'E   DETAIL:  Key (ingested_item_id)=(1) is not present in table "ingested_item".')
    assert "never a made-up one" in cause
    assert ("and when the parent was inserted in the test's own session, `await db.commit()` before calling a writer "
            "that opens its own `async_session()` -- it sees only committed rows") in cause
    assert "`ingested_item_id`=1 has no `ingested_item` row" in cause
