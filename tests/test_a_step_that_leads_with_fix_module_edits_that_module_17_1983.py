"""§17.1983 — a step that leads with "Fix <module>" edits that module in place.

Live (lab job 5b4880c0 F13F1, 2026-11-21): an operator-added CodeGen step titled "Fix paper_13f_holdings_ingestion
(holdings are in the information-table XML, not the cover page)", its text "Fix paper_13f_holdings_ingestion: it
fetches …", named no `x.py`. Every attempt that edited the module in place was refused -- "that module is step
API_SEC_EDGAR_AUDITF1's; this step names no module of its own" -- and the one that wrote `…_fix.py` importing it was
refused for that import. The renamed attempt also moved its 11 tests to `tests/test_…_fix.py`, and the next attempt,
which kept all 11 in the first file, was refused for "dropping" them from the `_fix` one."""
from __future__ import annotations

import inspect

from app.modules import engine_source as es
from app.modules import execution_agent as xa
from app.modules.engine_contracts import dropped_tests

LIVE = {"node_key": "F13F1", "status": "failed",
        "title": "Fix paper_13f_holdings_ingestion (holdings are in the information-table XML, not the cover page)",
        "prompt_template": "Fix paper_13f_holdings_ingestion: it fetches each 13F-HR filing's primaryDocument (the "
                           "cover page, primary_doc.xml) and looks for infoTable elements there, but a 13F-HR's "
                           "holdings are in a SEPARATE information-table XML document (e.g. 60471.xml).",
        "description": ""}
# the engine's own fix step that last wrote it (text line + tool_config), as on the live job
EARLIER_FIX = {"node_key": "API_SEC_EDGAR_AUDITF1", "status": "done",
               "title": "Fix paper_13f_holdings_ingestion (review API_SEC_EDGAR_AUDIT findings)",
               "prompt_template": "Python module `paper_13f_holdings_ingestion.py` -- the earlier job's module: edit "
                                  "it in place (the whole file, its public names kept). Fix what review found.",
               "output_text": "```python\n# app/modules/paper_13f_holdings_ingestion.py\nasync def ingest(db):\n"
                              "    return 0\n```"}
SIGNALS = {"node_key": "T9", "status": "done", "title": "Signal generator",
           "prompt_template": "Python module `paper_signal_generator.py`. Turn items into signals.",
           "output_text": "```python\n# app/modules/paper_signal_generator.py\ndef generate(items):\n    return []\n```"}

EDIT_IN_PLACE = ("```python\n# app/modules/paper_13f_holdings_ingestion.py\nasync def ingest(db):\n    return 1\n```\n"
                 "```python\n# tests/test_paper_13f_holdings_ingestion.py\ndef test_reads_the_information_table():\n"
                 "    assert True\n```")


def test_the_live_fix_step_owns_the_module_it_leads_with():
    # the delivered lab is left out of the engine under test (conftest): the module is known as a sibling step's
    assert es.step_module(LIVE, {"paper_13f_holdings_ingestion"}) == "paper_13f_holdings_ingestion"
    assert es.writes_a_siblings_module(EDIT_IN_PLACE, {**LIVE, "output_text": EDIT_IN_PLACE},
                                       [EARLIER_FIX, SIGNALS]) == []


def test_a_fix_step_still_may_not_write_a_module_it_does_not_name():
    other = "```python\n# app/modules/paper_signal_generator.py\ndef generate(items):\n    return [1]\n```"
    found = es.writes_a_siblings_module(other, {**LIVE, "output_text": other}, [EARLIER_FIX, SIGNALS])
    assert found and "step T9's" in found[0] and "this step writes `paper_13f_holdings_ingestion.py`" in found[0]
    # a step that names no module at all is held as before
    plain = {"node_key": "X1", "title": "Write a holdings report", "prompt_template": "Summarise the 13F holdings."}
    found = es.writes_a_siblings_module(EDIT_IN_PLACE, {**plain, "output_text": EDIT_IN_PLACE}, [EARLIER_FIX])
    assert found and "names no module of its own" in found[0]


def test_only_an_existing_module_written_like_one_is_claimed():
    # a module only a sibling step writes (not in app/modules) is claimed through `known`
    row = {"title": "Repair `quarry_ledger_sync` (the totals drift)", "prompt_template": "It sums twice."}
    assert es.fixed_module(row) == ""
    assert es.fixed_module(row, {"quarry_ledger_sync"}) == "quarry_ledger_sync"
    assert es.fixed_module({"title": "Update quarry_ledger_sync.py: round the totals"},
                           {"quarry_ledger_sync"}) == "quarry_ledger_sync"
    # plain words, a function name, a dotted reference and a test file are not a module claim
    assert es.fixed_module({"title": "Fix ticker field consistency"}, {"ticker"}) == ""
    assert es.fixed_module({"title": "Fix SPY benchmark tracker"}) == ""
    assert es.fixed_module({"title": "Fix write_ingested_item in the writer"}) == ""
    assert es.fixed_module({"title": "Fix quarry_ledger_sync.totals"}, {"quarry_ledger_sync"}) == ""
    assert es.fixed_module({"title": "Fix test_quarry_ledger_sync"}, {"test_quarry_ledger_sync"}) == ""
    # not leading: a step that only mentions a fix of it later does not claim it
    assert es.fixed_module({"title": "Add a report", "prompt_template": "After we fix quarry_ledger_sync …"},
                           {"quarry_ledger_sync"}) == ""


def test_the_engines_fixes_module_is_read_as_the_steps_module():
    row = {"title": "Tidy the ledger", "tool_config": '{"fixes_module": "quarry_ledger_sync", "owner": "T4"}'}
    assert es.fixed_module(row) == "quarry_ledger_sync" == es.step_module(row)


def test_the_fix_step_is_shown_the_file_it_edits():
    src = inspect.getsource(xa)
    assert "_es.fixed_module({**node_snapshot, \"tool_config\": node.get(\"tool_config\")}" in src
    block = es.edited_module_block([EARLIER_FIX], es.fixed_module(LIVE, {"paper_13f_holdings_ingestion"}))
    assert "paper_13f_holdings_ingestion.py" in block and "keep every public function" in block


def test_a_test_kept_in_another_file_is_not_dropped():
    names = ["test_reads_the_information_table", "test_follows_pagination"]
    prior = {"tests/test_paper_13f_holdings_ingestion.py": names,
             "tests/test_paper_13f_holdings_ingestion_fix.py": names}
    back = ("```python\n# tests/test_paper_13f_holdings_ingestion.py\ndef test_reads_the_information_table():\n"
            "    assert True\n\n\ndef test_follows_pagination():\n    assert True\n```")
    assert dropped_tests(back, prior) == []
    # none written: reported once, not once per file that held them
    found = dropped_tests("```python\n# app/modules/paper_13f_holdings_ingestion.py\nX = 1\n```", prior)
    assert len(found) == 1 and "dropped 2 of the 2" in found[0]
    # one really gone is still refused
    half = back.split("\n\n\ndef test_follows_pagination")[0] + "\n```"
    found = dropped_tests(half, prior)
    assert len(found) == 1 and "`test_follows_pagination`" in found[0]
