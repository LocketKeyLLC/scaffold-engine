"""§17.1052 — start_session finishes a stranded session (all steps terminal,
status still active) instead of handing the last step out forever."""
import pathlib
import re


def _src():
    return (pathlib.Path(__file__).resolve().parents[1] / "app/modules/assist_agent.py").read_text()


def test_start_session_resumes_a_lost_finalization():
    src = _src()
    start = src[src.index("async def start_assist_session("):src.index("assist_session_started session_id=")]
    assert "if total and not pending and not reopening:" in start
    assert "_maybe_finalize_session(session_id=session_id, db=db)" in start
    assert "assist_session_finalize_resumed" in start
    # the finalize happens INSIDE the start transaction (before its commit)
    assert start.index("_maybe_finalize_session(session_id=session_id, db=db)") < start.rindex("await db.commit()")


def test_finalizer_is_idempotent_by_construction():
    src = _src()
    fin = src[src.index("async def _maybe_finalize_session("):]
    fin = fin[:fin.index("\nasync def ", 10)]
    assert re.search(r"status NOT IN \('committed', 'skipped', 'handed_off', 'escalated'\)", fin)
    assert "WHERE id = :sid AND status IN ('active', 'paused')" in fin
