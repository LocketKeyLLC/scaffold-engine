"""§17.1887 — a leaked reasoning draft is not the answer (the text after a bare `</think>` is, and a redraw replaces
the LAST block headed by the path, the one `files_of` reads); `await result.fetchone()` after an awaited execute is
refused; the set-fields hint names every field the fake lacks at once; a scalar alias handed to a function of the same
module that writes it to a column carries `to`; a value returned under a dict key is filled with the caller's reads.

Live (follow-up 5b4880c0 T5, 2026-10-31, the §17.1886 round, 10:38–10:51 UTC; T5U1 DONE at its fourth attempt):
a1's draw was a draft file, "We'll output this as the final answer.</think>```python", the file again -- the gate read
the draft (18 findings), the redraw spliced the first block, nothing moved, the attempt spent. a2: `ingested_item_id =
(await result.fetchone())[0]` in the row helper every test used -- "'Row' object can't be awaited", eight tests. a3:
`handle_approval_answer(payload={"proposal": {"proposal_id": …}})` where the approve path reads `payload["proposal_id"]`
into `approval.proposal_id` -- NOT NULL, in the sandbox; and `submit_order()` returning a fake with `id`, `status` set
where `_record_fill_from_order(order=send_result["order"])` reads `.filled_qty` -- "'<=' not supported between
instances of 'MagicMock' and 'int'"."""
from __future__ import annotations

from app.modules import engine_source as es
from app.modules.engine_contracts import _target_uses, awaited_result_calls, replace_file_block, wrong_fakes
from app.sandbox.engine_tests import after_reasoning, files_of

PATH = "tests/test_paper_twelve_modules.py"

GATE = '''# app/modules/paper_operator_approval_gate.py
from typing import Any
from sqlalchemy import text
from app.database import async_session

async def _record_approval(db, *, proposal_id: int, decision: str) -> None:
    await db.execute(text("INSERT INTO approval (proposal_id, decision) VALUES (:proposal_id, :decision)"),
                     {"proposal_id": proposal_id, "decision": decision})
    await db.commit()

async def handle_approval_answer(job_id: str, answer: dict[str, Any], payload: dict[str, Any]) -> None:
    if not answer.get("approved"):
        proposal = payload.get("proposal", {})
        return
    proposal_id = payload.get("proposal_id")
    async with async_session() as db:
        await _record_approval(db, proposal_id=proposal_id, decision="approved")
'''

ENTRY = '''# app/modules/paper_execution_entry_point.py
from typing import Any
from app.modules.paper_alpaca_paper_client import AlpacaPaperClient

async def _send_order(client, proposal: dict) -> dict:
    order = await client.submit_order(symbol=proposal["symbol"])
    return {"order": order, "broker_order_id": order.id, "status": order.status}

async def _record_fill_from_order(db, *, order: Any) -> int | None:
    if order.filled_qty is None or order.filled_qty <= 0:
        return None
    return int(order.filled_qty)

async def run_execution_entry_point(db) -> dict:
    client = AlpacaPaperClient()
    send_result = await _send_order(client, {"symbol": "SPY"})
    order_obj = send_result["order"]
    fill = await _record_fill_from_order(db, order=order_obj)
    return {"fill": fill}
'''

ALPACA = '''# app/modules/paper_alpaca_paper_client.py
class AlpacaPaperClient:
    async def submit_order(self, *, symbol: str):
        return None
'''

MODULES = {"paper_operator_approval_gate": GATE, "paper_execution_entry_point": ENTRY, "paper_alpaca_paper_client": ALPACA}


def _row(key: str, code: str) -> dict:
    mod = code.split("\n", 1)[0].rsplit("/", 1)[1][:-3]
    return {"node_key": key, "title": mod, "description": "", "prompt_template": f"Python module `{mod}.py`.",
            "output_text": f"```python\n{code}```", "status": "done"}


def test_a_leaked_draft_is_not_the_answer_and_a_redraw_replaces_the_block_the_gate_reads():
    draft = "```python\n# tests/test_paper_twelve_modules.py\ndef test_a():\n    assert 1\n```"
    final = "```python\n# tests/test_paper_twelve_modules.py\ndef test_a():\n    assert 2\n\n\ndef test_b():\n    assert 3\n```"
    output = draft + "\n\nThis test file is extensive. We'll output this as the final answer.</think>" + final
    assert after_reasoning(output) == final
    assert after_reasoning("no reasoning here") == "no reasoning here"
    assert files_of({"output_text": output})[PATH].count("def test_") == 2
    out = replace_file_block(output, PATH, "# tests/test_paper_twelve_modules.py\nX = 1\n")
    assert "</think>" not in out and out.count("```") == 2
    assert files_of({"output_text": out})[PATH].strip() == "# tests/test_paper_twelve_modules.py\nX = 1"
    # two blocks for the path with no reasoning in between: the LAST is replaced, as `files_of` reads the last
    two = draft + "\n\n" + final
    out2 = replace_file_block(two, PATH, "# tests/test_paper_twelve_modules.py\nY = 2\n")
    assert files_of({"output_text": out2})[PATH].strip() == "# tests/test_paper_twelve_modules.py\nY = 2"
    assert out2.startswith(draft)


def test_a_result_method_awaited_after_an_awaited_execute_is_refused():
    test = '''# tests/test_paper_twelve_modules.py
from sqlalchemy import text
from app.database import async_session

async def _insert(db):
    result = await db.execute(text("INSERT INTO ingested_item (source) VALUES ('x') RETURNING id"))
    ingested_item_id = (await result.fetchone())[0]
    row = await (await db.execute(text("SELECT 1"))).scalar_one()
    ok = (await db.execute(text("SELECT 1"))).fetchone()
    return ingested_item_id
'''
    assert awaited_result_calls({PATH: test}) == [
        "`tests/test_paper_twelve_modules.py:7` awaits `result.fetchone()`: `execute` was the coroutine and was awaited "
        "already; a `Result`'s methods are plain (`'Row' object can't be awaited`) -- `result.fetchone()` without `await`",
        "`tests/test_paper_twelve_modules.py:8` awaits `(await db.execute(…)).scalar_one()`: `execute` was the coroutine "
        "and was awaited already; a `Result`'s methods are plain (`'Row' object can't be awaited`) -- "
        "`(await db.execute(…)).scalar_one()` without `await`"]
    assert awaited_result_calls({"app/modules/x.py": test}) == []


def test_the_set_fields_hint_names_every_field_the_fake_lacks_at_once():
    test = '''# tests/test_paper_twelve_modules.py
from unittest.mock import AsyncMock, MagicMock, patch
from app.modules.paper_execution_entry_point import run_execution_entry_point

async def test_run(db):
    fake_order = MagicMock()
    fake_order.id = "o1"
    fake_order.status = "accepted"
    fake_client = MagicMock()
    fake_client.submit_order = AsyncMock(return_value=fake_order)
    with patch("app.modules.paper_execution_entry_point.AlpacaPaperClient", return_value=fake_client):
        await run_execution_entry_point(db)
'''
    assert es._uses_text(_target_uses(ENTRY, "", "AlpacaPaperClient")) == "()->{submit_order()->{filled_qty, id, status}}"
    got = wrong_fakes({PATH: test}, MODULES, set())
    assert got == [
        "`tests/test_paper_twelve_modules.py:11` patches `AlpacaPaperClient` on `paper_execution_entry_point` with a fake "
        "whose call returns a fake whose `submit_order()` returns a fake with `id`, `status` set (line 6), where the module "
        "reads `.filled_qty` -- set `fake.filled_qty = …` too (the index shows what it reads) -- give it what the module "
        "uses: `AlpacaPaperClient()->{submit_order()->{filled_qty, id, status}}`"], got
    assert wrong_fakes({PATH: test.replace('    fake_order.status = "accepted"', '    fake_order.status = "accepted"\n    fake_order.filled_qty = 1')},
                       MODULES, set()) == []


def test_a_scalar_alias_handed_to_a_writer_of_the_same_module_carries_its_column():
    test = '''# tests/test_paper_twelve_modules.py
from app.modules.paper_operator_approval_gate import handle_approval_answer

async def test_it():
    await handle_approval_answer(job_id="j", answer={"approved": True}, payload={"proposal": {"proposal_id": 1}})
'''
    defs = es.sibling_definitions([_row("T16", GATE)])
    assert "payload: dict{proposal, proposal_id}" in es.block_for(
        {"node_key": "T5", "title": "t", "description": "", "prompt_template": "Write tests.", "output_text": None},
        set(), modules=defs)
    fks = {"approval": {"proposal_id": "proposal.id"}}
    got = [f.split(" -- the signature")[0] for f in es.sibling_refs(f"```python\n{test}```", defs, fks=fks)]
    assert got, got
    assert got[0].startswith("`tests/test_paper_twelve_modules.py:5` calls `handle_approval_answer(…)`: `payload` is read for "
                             "`proposal_id` (written to `approval.proposal_id`, which references `proposal`) -- the dict "
                             "given has `proposal` and no `proposal_id`"), got
    assert es.sibling_refs(f"```python\n{test}```", defs, fks=fks) != [] and es.sibling_refs(
        "```python\n" + test.replace('payload={"proposal": {"proposal_id": 1}}', 'payload={"proposal_id": pid, "proposal": {"proposal_id": pid}}')
        .replace("async def test_it():", "async def test_it(pid):") + "```", defs, fks=fks) == []
