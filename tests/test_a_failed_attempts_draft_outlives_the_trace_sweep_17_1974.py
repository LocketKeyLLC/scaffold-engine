"""§17.1974 — a failed attempt's draft outlives the trace sweep: its `-> failed` log (execution_logs, never swept)
carries the draft as the checks judged it, bounded, beside the failure's reason.

Live (2026-11-19, §17.1951): three investigations -- the helper-as-fixture hit, the 11-03 spy-test regression
(5b4880c0 T5 a2), the replay's first prompts -- found the draft gone: the node row holds the NEXT attempt's output
once it runs, and the only other copy was the llm_traces row, deleted at `trace_retention_days` (14). T5's failed
logs carried model, reason, confidence, imports -- never the draft."""
from __future__ import annotations

import inspect
import uuid

import pytest

from app.modules import execution_agent as ea

DRAFT = '''```python
# app/modules/paper_benchmark.py
def _buy_and_hold_return(prices):
    return (prices[-1] - prices[0]) / prices[0]
```

```python
# tests/test_paper_benchmark.py
from app.modules.paper_benchmark import _buy_and_hold_return

def test_return():
    assert _buy_and_hold_return([1.0, 2.0]) == 1.0
```
'''


def test_a_draft_is_kept_whole_and_a_longer_one_keeps_its_head_and_its_tests():
    assert ea.failed_draft(DRAFT) == DRAFT
    assert ea.failed_draft("") == "" and ea.failed_draft(None) == ""          # type: ignore[arg-type]
    long = "# app/modules/big.py\n" + "x = 1\n" * 30_000 + "# tests/test_big.py\ndef test_last():\n    pass\n"
    kept = ea.failed_draft(long)
    assert len(kept) < ea._DRAFT_KEEP + 100
    assert kept.startswith("# app/modules/big.py") and kept.endswith("def test_last():\n    pass\n")
    cut = len(long) - ea._DRAFT_KEEP
    assert f"[§17.1974: {cut} chars cut]" in kept


def test_the_failed_log_carries_the_draft_and_a_passing_one_does_not():
    src = inspect.getsource(ea.execute_next_node)
    at = src.index("f\"Node '{title}' -> {final_status}\"")
    write = src[at:src.index("draft_of_a_failure", at) + 200]
    assert "**draft_of_a_failure(final_status, output)" in write
    assert ea.draft_of_a_failure("failed", DRAFT) == {"draft": DRAFT}
    assert ea.draft_of_a_failure("done", DRAFT) == {} and ea.draft_of_a_failure("failed", "") == {}
    # the reason it failed sits beside it, in the same row
    assert '"reason": reason' in write


@pytest.mark.integration
@pytest.mark.timeout(900)
async def test_the_draft_is_still_there_after_the_trace_sweep(monkeypatch):
    from app.config import settings
    from app.database import async_session
    from app.modules import cleanup
    text = ea.text
    monkeypatch.setattr(settings, "trace_retention_days", 14)
    jid, nid = str(uuid.uuid4()), str(uuid.uuid4())
    try:
        async with async_session() as db:
            await db.execute(text("INSERT INTO jobs (id, title, status) VALUES (:id, 'x', 'running')"), {"id": jid})
            await db.execute(text("INSERT INTO dag_nodes (id, job_id, node_key, title, status) "
                                  "VALUES (:n, :j, 'T5', 'spy tests', 'failed')"), {"n": nid, "j": jid})
            # the attempt's trace, 20 days old -- the copy the sweep takes
            await db.execute(text(
                "INSERT INTO llm_traces (job_id, node_id, request_kind, provider, model, response_content, created_at) "
                "VALUES (:j, :n, 'chat', 'ollama', 'm', :d, NOW() - make_interval(days => 20))"),
                {"j": jid, "n": nid, "d": DRAFT})
            await ea._log_execution(db, jid, nid, "warning", "Node 'spy tests' -> failed",
                                    {"reason": "3 failed: Decimal - float", "draft": ea.failed_draft(DRAFT)})
            # the log is as old as the trace
            await db.execute(text("UPDATE execution_logs SET created_at = NOW() - make_interval(days => 20) "
                                  "WHERE job_id = :j"), {"j": jid})
            await db.commit()
        async with async_session() as db:
            assert await cleanup.sweep_llm_traces(db) >= 1
        async with async_session() as db:
            traces = (await db.execute(text("SELECT count(*) FROM llm_traces WHERE job_id = :j"),
                                       {"j": jid})).scalar()
            row = (await db.execute(text(
                "SELECT details->>'draft', details->>'reason' FROM execution_logs WHERE job_id = :j AND node_id = :n "
                "AND message LIKE :f"), {"j": jid, "n": nid, "f": "%-> failed"})).one()
        assert traces == 0                                                     # the sweep ran
        assert row[0] == DRAFT and row[1] == "3 failed: Decimal - float"       # the draft and its cause did not go
    finally:
        async with async_session() as db:
            await db.execute(text("DELETE FROM jobs WHERE id = :id"), {"id": jid})
            await db.commit()
