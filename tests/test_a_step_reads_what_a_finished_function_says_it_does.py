"""§17.1851 — a code step reads what the finished functions it runs through say they do.

Live (follow-up 5b4880c0 T6, 2026-10-28, the round after §17.1850): the proposer reached the step as one line -- its
signature and returned keys. Its docstring, "Proposals that fail risk checks are recorded in the audit trail as
rejected proposals with a reason, and are not returned", never did, and the step's test asserted one proposal back for
a risk-rejected trade. The job-modules list was cut at its budget alphabetically, past the proposer, the risk module
and the signal generator. And a retry clears the attempt's output, so the engine did not know the step ran through the
proposer at all. Now: a refused attempt's imports are recorded with its refusal; the modules the step names or ran
through lead the job-modules list; and each of their public functions is shown with its whole docstring.
"""
from __future__ import annotations

import json
import uuid

import pytest

from app.modules import execution_agent as xa
from app.modules import engine_source as es
from app.modules.engine_contracts import contracts_block, module_imports, step_targets

PROPOSER = '''
from app.modules.paper_risk_control_module import evaluate_proposal


async def propose_trades(signals, *, equity, day_start_equity, orders_today, db=None):
    """Propose trades from signals, sized deterministically at 5% of equity.

    Each returned dict has keys: signal_id, ticker, side, quantity,
    limit_price, rationale, sources. Only rationale and sources are
    LLM-generated.

    Proposals that fail risk checks are recorded in the audit trail as
    rejected proposals with a reason, and are not returned.
    """
    return []


def _compute_quantity(*, equity, limit_price):
    """Private: not shown."""
    return 0
'''
RISK = '''
class RiskDecision:
    """A risk verdict."""

    def allowed(self):
        """True when every limit passed."""
        return True


def evaluate_proposal(*, ticker, equity, proposed_notional):
    """Evaluate one proposal against the deterministic limits: allow-list, 5% position size, daily loss."""
    return RiskDecision()


def no_doc():
    return 1
'''
DATA = '''
from app.modules.paper_signal_generator import generate_signals


async def load_batch_context(*, timestamp, trading_day, db=None):
    """Load the equity and order-count context for a daily batch run.

    Returns a dict with ``day_start_equity`` (Decimal or None) and ``orders_today`` (int).
    """
    return {}
'''
GENERATOR = '''
async def generate_signals(timestamp, *, db=None):
    """Signals as of a timestamp, one per ingested item."""
    return []
'''
MODULES = {"paper_trade_proposer": PROPOSER, "paper_risk_control_module": RISK,
           "paper_daily_signal_proposal_batch_data": DATA, "paper_signal_generator": GENERATOR}
STEP = ("Python module `paper_daily_signal_proposal_batch.py`: import every data function "
        "`paper_daily_signal_proposal_batch_data.py` (step T6D) has.")
OWN = {"paper_daily_signal_proposal_batch"}
NOT_RETURNED = ("- `paper_trade_proposer.propose_trades(signals, *, equity, day_start_equity, orders_today, db=None)`: "
                "Propose trades from signals, sized deterministically at 5% of equity. Each returned dict has keys: "
                "signal_id, ticker, side, quantity, limit_price, rationale, sources. Only rationale and sources are "
                "LLM-generated. Proposals that fail risk checks are recorded in the audit trail as rejected proposals "
                "with a reason, and are not returned.")


def test_the_modules_a_step_runs_through_are_the_named_the_ran_and_what_those_import():
    assert step_targets(STEP, OWN, MODULES) == {"paper_daily_signal_proposal_batch_data", "paper_signal_generator"}
    assert step_targets(STEP, OWN, MODULES, {"paper_trade_proposer"}) == {
        "paper_daily_signal_proposal_batch_data", "paper_signal_generator", "paper_trade_proposer",
        "paper_risk_control_module"}
    assert step_targets("Python module `x.py`.", OWN, MODULES) == set()
    assert step_targets(STEP, OWN, MODULES, {"paper_daily_signal_proposal_batch", "not_a_job_module"}) == {
        "paper_daily_signal_proposal_batch_data", "paper_signal_generator"}            # never its own, never unknown


def test_the_live_step_is_shown_the_proposers_own_contract_once_it_ran_through_it():
    first = contracts_block(STEP, OWN, MODULES)
    assert first.startswith("\n\n## What the finished functions this step runs through say they do (§17.1851)\n")
    assert "a test asserts what the function says it does, never the opposite" in first
    assert "- `paper_daily_signal_proposal_batch_data.load_batch_context(*, timestamp, trading_day, db=None)`: Load " \
           "the equity and order-count context for a daily batch run. Returns a dict with ``day_start_equity`` " \
           "(Decimal or None) and ``orders_today`` (int)." in first
    assert "- `paper_signal_generator.generate_signals(timestamp, *, db=None)`: Signals as of a timestamp" in first
    assert "propose_trades" not in first                                   # not named, not yet ran through
    retry = contracts_block(STEP, OWN, MODULES, {"paper_trade_proposer"})
    assert NOT_RETURNED in retry
    assert "- `paper_risk_control_module.evaluate_proposal(*, ticker, equity, proposed_notional)`: Evaluate one " \
           "proposal against the deterministic limits" in retry                  # what the proposer imports
    assert "- `paper_risk_control_module.RiskDecision.allowed()`: True when every limit passed." in retry
    assert "_compute_quantity" not in retry and "no_doc" not in retry
    lines = [ln for ln in retry.splitlines() if ln.startswith("- `")]
    assert lines[0].startswith("- `paper_daily_signal_proposal_batch_data.") and \
        lines[1].startswith("- `paper_trade_proposer.")                           # direct first, imports after
    many = "\n".join(f'def m{i}():\n    """Method {i}."""\n    return {i}\n' for i in range(40))
    wide = contracts_block(STEP, OWN, {**MODULES, "paper_alpaca_paper_client": many},
                           {"paper_alpaca_paper_client", "paper_trade_proposer"}, budget=1800)
    assert NOT_RETURNED in wide and "m39" not in wide                  # breadth-first: one function each, then the next


def test_nothing_to_say_is_no_block_and_a_long_contract_is_cut_at_a_word():
    assert contracts_block("Python module `x.py`.", OWN, MODULES) == ""
    assert contracts_block(STEP, OWN, {"paper_daily_signal_proposal_batch_data": "def load_batch_context():\n"
                                       "    return {}\n"}) == ""
    long = PROPOSER.replace("and are not returned.", "and are not returned. " + "More words here. " * 60)
    out = contracts_block(STEP, OWN, {**MODULES, "paper_trade_proposer": long}, {"paper_trade_proposer"})
    line = next(ln for ln in out.splitlines() if "propose_trades" in ln)
    assert line.endswith(" …") and len(line) < 820
    tiny = contracts_block(STEP, OWN, MODULES, {"paper_trade_proposer"}, budget=650)
    assert tiny.rstrip().endswith("- …") and "paper_daily_signal_proposal_batch_data" in tiny \
        and "propose_trades" not in tiny                                           # one line fits, the next is cut


def test_a_refused_attempts_imports_are_read_from_its_output():
    out = ("```python\n# app/modules/paper_daily_signal_proposal_batch.py\nfrom app.modules.paper_trade_proposer "
           "import propose_trades\nfrom app.modules.paper_daily_signal_proposal_batch_data import (\n    "
           "load_batch_context,\n)\nimport app.modules.paper_risk_control_module as risk\nfrom app.database import "
           "async_session\n```\n\n```python\n# tests/test_paper_daily_signal_proposal_batch.py\nfrom "
           "app.modules.paper_alpaca_paper_client import AlpacaAccount\n```")
    assert module_imports(out) == ["paper_alpaca_paper_client", "paper_daily_signal_proposal_batch_data",
                                   "paper_risk_control_module", "paper_trade_proposer"]
    assert module_imports("") == [] and module_imports("```python\nthis is not python\n```") == []


def test_the_modules_the_step_runs_through_lead_the_job_modules_list_past_the_cut():
    defs = {f"paper_{c}_module": {"node": "T1", "names": set(), "defined": set(),
                                 "funcs": {f"run_{c}": {"required": [], "optional": [], "kwonly": [], "varargs": False,
                                                        "varkw": False, "coroutine": True}}, "classes": {}, "returns": {}}
            for c in "abcdefghijklmnopqrstuvwxyz"}
    # §17.1876 -- the legend is not budgeted: 26 lines of ~36 chars against 600 leaves the first 16 or so
    plain = es._render_job_modules(defs, "paper_batch", budget=600)
    assert "paper_a_module" in plain and "paper_z_module" not in plain and plain.rstrip().endswith("…")
    led = es._render_job_modules(defs, "paper_batch", budget=600, first={"paper_z_module", "paper_y_module"})
    lines = [ln for ln in led.splitlines() if ln.startswith("    paper_")]
    assert [ln.split(":")[0].strip() for ln in lines[:2]] == ["paper_y_module", "paper_z_module"]
    assert "paper_a_module" in led
    assert es._render_job_modules(defs, "paper_batch", budget=600, first=set()) == plain


@pytest.mark.integration
async def test_prior_imports_are_read_from_the_run_log_over_the_same_window_as_refusals():
    from app.database import async_session
    text = xa.text
    jid, nid = str(uuid.uuid4()), None
    try:
        async with async_session() as db:
            await db.execute(text("INSERT INTO jobs (id, title, status) VALUES (:id, 'imports history', 'running')"),
                             {"id": jid})
            nid = (await db.execute(text(
                "INSERT INTO dag_nodes (job_id, node_key, title, status, execution_order) "
                "VALUES (:j, 'T6', 'Build daily batch', 'pending', 0) RETURNING id"), {"j": jid})).scalar()
            for msg, details in [
                    ("Node 'Build daily batch' -> failed", {"reason": "r1", "imports": ["paper_trade_proposer"]}),
                    ("Node 'Build daily batch' evidence: unsupported=0", {"imports": ["paper_not_counted"]}),
                    ("Node 'Build daily batch' -> failed", {"reason": "r2", "imports": [
                        "paper_daily_signal_proposal_batch_data", "paper_trade_proposer"]}),
                    ("Node 'Build daily batch' -> failed", {"reason": "r3"})]:
                await db.execute(text("INSERT INTO execution_logs (job_id, node_id, log_level, message, details) "
                                      "VALUES (:j, :n, 'warning', :m, CAST(:d AS jsonb))"),
                                 {"j": jid, "n": nid, "m": msg, "d": json.dumps(details)})
            await db.commit()
        async with async_session() as db:
            got = await xa.prior_imports(db, jid, str(nid))
        assert got == ["paper_daily_signal_proposal_batch_data", "paper_trade_proposer"]
        async with async_session() as db:          # the step's prompt edited: earlier attempts no longer count
            await db.execute(text("INSERT INTO dag_node_edits (job_id, node_key, op, after, edited_by) VALUES "
                                  "(:j, 'T6', 'edit', CAST(:a AS jsonb), 'operator')"),
                             {"j": jid, "a": json.dumps({"prompt_template": "new"})})
            await db.commit()
        async with async_session() as db:
            assert await xa.prior_imports(db, jid, str(nid)) == []
    finally:
        async with async_session() as db:
            for tbl in ("execution_logs", "dag_node_edits"):
                await db.execute(text(f"DELETE FROM {tbl} WHERE job_id = :j"), {"j": jid})
            await db.execute(text("DELETE FROM jobs WHERE id = :j"), {"j": jid})
            await db.commit()
