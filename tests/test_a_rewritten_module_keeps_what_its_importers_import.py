"""§17.1931 — a step that rewrites a module keeps every name another finished module of the job imports from it.

Live (follow-up 5b4880c0, 2026-11-05): API_ALPACA_AUDITF6 rewrote `paper_trade_proposer` ("its public names kept") and
dropped `MAX_PROPOSALS_PER_BATCH = 3`; its own tests passed and the step was done, while `paper_walk_forward_backtester`
-- and the lab's wiring module, which imports it -- no longer imported. Found only when the delivered branch's tests
were collected."""
from __future__ import annotations

from app.modules.engine_source import importers_left_without

OLD = "MAX_PROPOSALS_PER_BATCH = 3\n\n\nasync def propose(db):\n    return []\n"
NEW = "async def propose(db):\n    return []\n"
CURRENT = {
    "paper_trade_proposer": ("app/modules/paper_trade_proposer.py", OLD, "T1F2"),
    "paper_walk_forward_backtester": ("app/modules/paper_walk_forward_backtester.py",
                                      "from app.modules.paper_trade_proposer import (\n    MAX_PROPOSALS_PER_BATCH,\n"
                                      "    propose,\n)\n", "f00ea97f:T20"),
}


def test_a_name_an_importer_takes_is_kept_and_its_old_definition_named():
    got = importers_left_without({"app/modules/paper_trade_proposer.py": NEW}, CURRENT)
    assert got == ["`app/modules/paper_walk_forward_backtester.py:1` (step f00ea97f:T20) imports `MAX_PROPOSALS_PER_BATCH` "
                   "from `paper_trade_proposer`, which this rewrite no longer defines -- keep it (it was "
                   "`MAX_PROPOSALS_PER_BATCH = 3`): every finished module that imports from it must still import"]


def test_a_rewrite_that_keeps_it_or_rewrites_the_importer_too_is_no_finding():
    assert importers_left_without({"app/modules/paper_trade_proposer.py": OLD}, CURRENT) == []
    assert importers_left_without({"app/modules/paper_trade_proposer.py": "if True:\n    MAX_PROPOSALS_PER_BATCH = 3\n"
                                   "propose = None\n"}, CURRENT) == []
    both = {"app/modules/paper_trade_proposer.py": NEW,
            "app/modules/paper_walk_forward_backtester.py": "from app.modules.paper_trade_proposer import propose\n"}
    assert importers_left_without(both, CURRENT) == []
    assert importers_left_without({"tests/test_paper_trade_proposer.py": NEW}, CURRENT) == []
