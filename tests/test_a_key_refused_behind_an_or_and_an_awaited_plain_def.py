"""§17.1893 — a key the function refuses without, read behind an `or` (`symbol = p.get("symbol") or p.get("ticker")`)
and a compound `if not a or not b or c is None: raise`; the parent of refused-without sub-keys is required itself;
`await` on a plain `def`, and an `async def` called without `await` whose coroutine is used as the result.

Live (follow-up 5b4880c0 T5, 2026-11-03 21:40 UTC): `execute_order({"approval_id": …, "ticker": "SPY", "side":
"buy", "quantity": …})` -- the proposal's fields at the top level, no `proposal` -- "ValueError: Proposal 1 missing
symbol/side/quantity"; `decision = await evaluate_proposal(…)` on a plain `def` -- "'RiskDecision' object can't be
awaited". Two of five sandbox failures, nothing said before it."""
from __future__ import annotations

from app.modules import engine_source as es

EXECUTOR = '''# app/modules/paper_order_executor.py
from typing import Any

async def execute_order(approval: dict[str, Any]) -> dict[str, Any]:
    proposal_id = approval.get("proposal_id")
    proposal = approval.get("proposal", {})
    symbol = proposal.get("symbol") or proposal.get("ticker")
    side = proposal.get("side")
    quantity = proposal.get("quantity")
    order_type = proposal.get("order_type", "market")
    if not symbol or not side or quantity is None:
        raise ValueError(f"Proposal {proposal_id} missing symbol/side/quantity")
    return {"symbol": symbol, "side": side, "quantity": quantity, "order_type": order_type}
'''

RISK = '''# app/modules/paper_risk_control_module.py
from decimal import Decimal

def evaluate_proposal(*, ticker: str, equity: Decimal) -> dict:
    return {"ticker": ticker, "allowed": equity > 0}

async def kill_switch(reason: str) -> dict:
    return {"reason": reason}
'''


def _row(key: str, code: str) -> dict:
    mod = code.split("\n", 1)[0].rsplit("/", 1)[1][:-3]
    return {"node_key": key, "title": mod, "description": "", "prompt_template": f"Python module `{mod}.py`.",
            "output_text": f"```python\n{code}```", "status": "done"}


def test_a_parent_whose_sub_keys_are_refused_without_is_required_and_an_alternative_key_satisfies():
    test = '''# tests/test_paper_twelve_modules.py
from decimal import Decimal
from app.modules.paper_order_executor import execute_order

async def test_flat():
    approval = {"approval_id": 1, "proposal_id": 1, "ticker": "SPY", "side": "buy", "quantity": Decimal("1")}
    result = await execute_order(approval)

async def test_ticker_instead_of_symbol():
    result = await execute_order({"proposal_id": 1, "proposal": {"ticker": "SPY", "side": "buy", "quantity": 1}})

async def test_no_side():
    result = await execute_order({"proposal_id": 1, "proposal": {"symbol": "SPY", "quantity": 1}})
'''
    defs = es.sibling_definitions([_row("T2", EXECUTOR)])
    got = [f.split(" -- the signature")[0] for f in es.sibling_refs(f"```python\n{test}```", defs)]
    assert got == [
        "`tests/test_paper_twelve_modules.py:7` calls `execute_order(…)`: `approval` is read for `proposal` (its "
        "`quantity`, `side`, `symbol` (or `ticker`) the function refuses without) -- the dict given has `approval_id`, "
        "`proposal_id`, `ticker`, `side`, `quantity` and no `proposal`",
        "`tests/test_paper_twelve_modules.py:13` calls `execute_order(…)`: `approval`[\"proposal\"] is read for `side` "
        "(and refused without it) -- the dict given has `symbol`, `quantity` and no `side`"], got
    # the signature shows the keys the function refuses without, under `proposal`
    sig = es.block_for({"node_key": "T5", "title": "t", "description": "", "prompt_template": "Write tests.",
                        "output_text": None}, set(), modules=defs)
    assert "execute_order(approval: dict{proposal{order_type, quantity, side, symbol, ticker}, proposal_id})" in sig


def test_await_on_a_plain_def_and_a_coroutine_used_as_the_result_are_refused():
    test = '''# tests/test_paper_twelve_modules.py
import asyncio
from decimal import Decimal
from app.modules.paper_risk_control_module import evaluate_proposal, kill_switch

async def test_evaluate():
    decision = await evaluate_proposal(ticker="SPY", equity=Decimal("1"))
    plain = evaluate_proposal(ticker="SPY", equity=Decimal("1"))

async def test_kill():
    result = kill_switch("x")
    assert kill_switch("y")["reason"] == "y"
    awaited = await kill_switch("z")
    task = asyncio.create_task(kill_switch("w"))
    await asyncio.gather(kill_switch("v"))
'''
    defs = es.sibling_definitions([_row("T1F3", RISK)])
    got = sorted((f.split(" -- the signature")[0] for f in es.sibling_refs(f"```python\n{test}```", defs)),
                 key=lambda m: int(m.split(":")[1].split("`")[0]))
    assert got[0] == (
        "`tests/test_paper_twelve_modules.py:7` calls `evaluate_proposal(…)` under `await`: a plain `def`, whose result "
        "cannot be awaited -- drop the `await` (`paper_risk_control_module`, step T1F3)")
    # a bare call on an `async def` is the older gate's (§17.1757): one each for the two bare calls, none for the
    # awaited one, the task or the gather
    assert [m.split("`")[1] for m in got[1:]] == ["tests/test_paper_twelve_modules.py:11", "tests/test_paper_twelve_modules.py:12"]
    assert all("is `async def` in `paper_risk_control_module`: `await` it" in m for m in got[1:]), got


def test_a_compound_refusal_marks_each_name_and_an_or_records_the_other_key():
    import ast
    fn = ast.parse(EXECUTOR).body[-1]
    keys = es._param_keys(fn, {})
    sub = keys["approval"]["proposal"]["sub"]
    assert sub["symbol"] == {"needed": True, "or": ["ticker"]}
    assert sub["side"] == {"needed": True} and sub["quantity"] == {"needed": True}
    assert "needed" not in sub["order_type"] and "needed" not in sub["ticker"]
    assert es._needed_below(sub) == "`quantity`, `side`, `symbol` (or `ticker`)"
    assert es._needed_below({"a": {"to": "t.c"}}) is None


def test_none_given_for_a_key_written_to_a_column_the_database_refuses_null_in():
    """§17.1898 — live (5b4880c0 T5, 2026-11-03 23:46 UTC): `{"approval_id": None, "proposal_id": pid, "proposal":
    {…}}` into `execute_order`, which writes it to `broker_order_id.approval_id` -- the keys gate read missing keys
    and kinds, not an explicit None."""
    module = '''# app/modules/paper_order_executor.py
from typing import Any
from sqlalchemy import text

async def execute_order(approval: dict[str, Any], db) -> dict[str, Any]:
    proposal = approval.get("proposal", {})
    await db.execute(text("INSERT INTO broker_order_id (approval_id, proposal_id, broker_order_id) "
                          "VALUES (:approval_id, :proposal_id, :b)"),
                     {"approval_id": approval.get("approval_id"), "proposal_id": approval.get("proposal_id"), "b": "x"})
    return {"status": "submitted", "note": approval.get("note")}
'''
    test = '''# tests/test_paper_twelve_modules.py
from app.modules.paper_order_executor import execute_order

async def test_none_id(db):
    result = await execute_order({"approval_id": None, "proposal_id": 1, "proposal": {"symbol": "SPY"}, "note": None}, db)
'''
    defs = es.sibling_definitions([_row("T2", module)])
    fks = {"broker_order_id": {"approval_id": "approval.id"}}
    rules = {"broker_order_id": {"json": [], "checks": [], "required": ["proposal_id"], "unique": [],
                                 "not_null": ["approval_id", "proposal_id", "broker_order_id"]}}
    got = [f.split(" -- the signature")[0] for f in es.sibling_refs(f"```python\n{test}```", defs, fks=fks, rules=rules)]
    assert got == [
        "`tests/test_paper_twelve_modules.py:5` calls `execute_order(…)`: `approval`[\"approval_id\"] is written to "
        "`broker_order_id.approval_id`, which references `approval`; it was given None -- the database refuses the row; "
        "pass the id its parent's writer returned"], got           # `note` is read, never written: None is fine
    none_rules = {"broker_order_id": {"json": [], "checks": [], "required": [], "unique": [], "not_null": ["proposal_id"]}}
    test2 = test.replace('"approval_id": None, ', "").replace('{"proposal_id": 1', '{"proposal_id": None')
    got2 = [f.split(" -- the signature")[0] for f in es.sibling_refs(f"```python\n{test2}```", defs, fks={}, rules=none_rules)]
    assert got2 == [
        "`tests/test_paper_twelve_modules.py:5` calls `execute_order(…)`: `approval`[\"proposal_id\"] is written to "
        "`broker_order_id.proposal_id`, which is NOT NULL; it was given None -- the database refuses the row"], got2
