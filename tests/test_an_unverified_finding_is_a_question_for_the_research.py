"""§17.1928 — an audit line that holds the code to nothing the documentation says is a question for the research, not
a defect: an UNVERIFIED verdict, or a NOT MET whose own text says only that no source states it, opens no fix step and
is sent back to the API's research step (once), which reads again with its audit. An open fix step holds what its
review finds as the review reads NOW (skipped when nothing is left, reopened when a re-read finds something); a step
done before a review's latest reading does not own what it finds; a later rewrite of the module does.

Live (follow-up 5b4880c0, 2026-11-05): 11 of API_ALPACA_AUDIT's 13 NOT MET lines were "No retrieved source states
the REST order-request parameter names ... Unverified." (the research had read three guides, not the reference pages),
one said "matches ... No disagreement"; API_ALPACA_AUDITF1 passed the sandbox four times and the verifier refused each
as a no-op against them -- and three more fix steps waited on the same kind of lines."""
from __future__ import annotations

from datetime import datetime, timezone

from app.modules import engine_plan as ep

MODS = ["paper_alpaca_paper_client", "paper_fill_tracker", "paper_order_executor"]
AUDIT = """Findings:

- NOT MET — paper_alpaca_paper_client (T5): `submit_order` posts `"type"`, `"qty"`. No retrieved source states the REST order-request parameter names. Unverified.
- NOT MET — paper_alpaca_paper_client (T5): `PAPER_STREAM_URL` matches the research's [3] "connect to 'wss://paper-api.alpaca.markets/stream'". No disagreement on the stream URL.
- NOT MET — paper_fill_tracker (T18): `track_fills` sends `{"action": "subscribe"}`. The research's [3] documents `{"action": "listen"}` -- a different action name.
- UNVERIFIED — paper_order_executor (T2): `execute_order` passes `limit_price`; the documentation would have to state the order fields.
- paper_order_executor (T2): submits orders via AlpacaPaperClient.submit_order.
- MET — paper_alpaca_paper_client (T5): the auth headers match the research's [2].
"""


def test_a_line_the_documentation_cannot_settle_is_a_question_and_a_disagreement_a_finding():
    found = ep.review_findings(AUDIT, MODS)
    assert list(found) == ["paper_fill_tracker"] and "a different action name" in found["paper_fill_tracker"][0]
    gaps = ep.review_gaps(AUDIT, MODS)
    assert sorted(gaps) == ["paper_alpaca_paper_client", "paper_order_executor"]
    assert len(gaps["paper_alpaca_paper_client"]) == 1 and "Unverified." in gaps["paper_alpaca_paper_client"][0]
    # a review that writes no verdicts keeps its plain bullets as findings (the T1 inventory audit, §17.1818)
    plain = "- `paper_order_executor` reads `proposal[\"symbol\"]` while the proposer uses `ticker` for it.\n"
    assert list(ep.review_findings(plain, MODS)) == ["paper_order_executor"]


def _fix(fid: str, leaf: str, key: str, lines: list[str], status: str = "pending", **kw) -> dict:
    return {"id": fid, "name": f"Fix {leaf} (review {key} findings)", "tool": "CodeGen", "status": status,
            "depends_on": [key], "notes": (f"Python module `{leaf}.py` -- this job's module: edit it in place. Fix what "
                                           f"review step {key} found in it:\n" + "".join(f"- {ln}\n" for ln in lines)
                                           + "Its tests ..."), **kw}


def test_an_open_fix_step_holds_the_reviews_findings_as_it_reads_now():
    old = "NOT MET — paper_alpaca_paper_client (T5): `submit_order` posts `type`. No retrieved source states it. Unverified."
    tasks = [{"id": "API_ALPACA_AUDIT", "name": "Audit the code against the Alpaca research", "tool": "LLM",
              "status": "done", "depends_on": []},
             _fix("API_ALPACA_AUDITF1", "paper_alpaca_paper_client", "API_ALPACA_AUDIT", [old], status="failed")]
    changes = ep.add_review_fixes(tasks, {"API_ALPACA_AUDIT": AUDIT}, [], own=MODS)
    f1 = tasks[1]
    assert f1["status"] == "skipped" and f1["tool_config"]["dropped_by_review"] == "API_ALPACA_AUDIT"
    assert any(c.startswith("review_fix_dropped: API_ALPACA_AUDITF1") for c in changes)
    # the audit read again finds a real disagreement: the dropped step is reopened with it
    again = AUDIT + ("- NOT MET — paper_alpaca_paper_client (T5): `get_bars` reads `bars[].c`; the research's [4] "
                     "documents the close as `c` under `bars[symbol]`, a different nesting.\n")
    ep.add_review_fixes(tasks, {"API_ALPACA_AUDIT": again}, [], own=MODS)
    assert f1["status"] == "pending" and "dropped_by_review" not in f1["tool_config"]
    assert "a different nesting" in f1["notes"] and "Unverified" not in f1["notes"] and f1["notes"].endswith("Its tests ...")


def test_the_research_is_asked_what_its_audit_could_not_verify_once():
    research = ep._research_step(next(a for a in ep._KNOWN_APIS if a["key"] == "ALPACA"))
    assert "https://docs.alpaca.markets/reference/postorder" in research["tool_config"]["urls"]
    tasks = [research, {"id": "API_ALPACA_AUDIT", "name": "Audit", "tool": "LLM", "status": "done",
                        "depends_on": ["API_ALPACA"]}]
    changes, reset = ep.ask_research_gaps(tasks, {"API_ALPACA_AUDIT": AUDIT}, MODS)
    assert reset == ["API_ALPACA", "API_ALPACA_AUDIT"] and len(changes) == 1
    assert "could not verify these" in research["notes"] and "order-request parameter names" in research["notes"]
    assert research["tool_config"]["gaps_asked"] == "API_ALPACA_AUDIT"
    assert ep.ask_research_gaps(tasks, {"API_ALPACA_AUDIT": AUDIT}, MODS) == ([], [])      # once
    assert "UNVERIFIED" in ep._AUDIT_NOTE


def test_a_fix_done_before_the_reviews_latest_reading_does_not_own_what_it_finds_now():
    t_old = datetime(2026, 11, 5, 4, 0, tzinfo=timezone.utc)
    t_new = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
    audit = "- NOT MET — paper_fill_tracker (T18): sends `subscribe`; the research's [3] documents `listen`, a different action.\n"
    tasks = [{"id": "API_ALPACA_AUDIT", "name": "Audit the code against the Alpaca research", "tool": "LLM",
              "status": "done", "depends_on": [], "_done_at": t_new},
             _fix("API_ALPACA_AUDITF3", "paper_fill_tracker", "API_ALPACA_AUDIT", ["old"], status="done", _done_at=t_old)]
    changes = ep.add_review_fixes(tasks, {"API_ALPACA_AUDIT": audit}, [], own=MODS,
                                  reviewed_at={"API_ALPACA_AUDIT": t_new})
    assert any(c.startswith("review_fix_added: API_ALPACA_AUDITF1 fixes `paper_fill_tracker.py`") for c in changes)
    # read before the fix finished: the fix owns it, nothing is added
    tasks = tasks[:2]
    tasks[1]["_done_at"] = t_new
    assert ep.add_review_fixes(tasks, {"API_ALPACA_AUDIT": audit}, [], own=MODS,
                               reviewed_at={"API_ALPACA_AUDIT": t_old}) == []


def test_a_later_rewrite_of_the_module_owns_an_earlier_reviews_finding():
    t1 = "- `app/modules/paper_order_executor.py` — module constant `URL` is a placeholder vendor URL that does not exist.\n"
    tasks = [{"id": "T1", "name": "Audit existing paper modules", "tool": "LLM", "status": "done", "depends_on": []},
             {"id": "T5U4", "name": "Fix paper_order_executor (found by T5's tests)", "tool": "CodeGen", "status": "done",
              "depends_on": [], "notes": "Python module `paper_order_executor.py` -- edit it in place."},
             _fix("API_ALPACA_AUDITF4", "paper_order_executor", "API_ALPACA_AUDIT", ["a real one"])]
    assert ep.add_review_fixes(tasks, {"T1": t1}, ["paper_order_executor"], own=[]) == []
    assert "Also fix what review step T1" not in tasks[2]["notes"]


def test_an_audit_never_stands_on_its_own_fix_steps_and_a_skipped_step_wrote_nothing():
    tasks = [{"id": "T5U4", "name": "Fix lab_spy (found by T5's tests)", "tool": "CodeGen", "type": "action",
              "status": "done", "depends_on": [], "notes": "Python module `lab_spy.py`."},
             {"id": "API_ALPACA_AUDIT", "name": "Audit the code that calls the Alpaca paper trading API against its research",
              "tool": "LLM", "type": "action", "status": "pending", "depends_on": ["API_ALPACA", "T5U4"], "notes": ""},
             {"id": "API_ALPACA_AUDITF1", "name": "Fix lab_spy (review API_ALPACA_AUDIT findings)", "tool": "CodeGen",
              "type": "action", "status": "done", "depends_on": ["API_ALPACA_AUDIT"], "notes": "Python module `lab_spy.py`."},
             {"id": "T7", "name": "Fix lab_orders", "tool": "CodeGen", "type": "action", "status": "skipped",
              "depends_on": [], "notes": "Python module `lab_orders.py`."}]
    sources = {"lab_spy": 'URL = "https://data.alpaca.markets/v2/stocks/bars"  # alpaca\n',
               "lab_orders": 'URL = "https://paper-api.alpaca.markets/v2/orders"  # alpaca\n'}
    ep.complete_engine_plan(tasks, {"targets_engine": True}, [], sources)
    audit = next(t for t in tasks if t["id"] == "API_ALPACA_AUDIT")
    assert audit["depends_on"] == ["API_ALPACA"]                     # not its own fix (a cycle), not the skipped T7
    assert "`lab_spy` (step API_ALPACA_AUDITF1)" in audit["notes"] and "(step T7)" not in audit["notes"]
