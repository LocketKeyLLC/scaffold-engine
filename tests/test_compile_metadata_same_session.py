"""§17.1052 — compile-time metadata writes ride the CALLER'S session.

The assist completion path compiles inside the turn's transaction, which
already holds the jobs row; a second session updating that row waited on the
caller forever and took every later /assist/start for the job with it.
"""
import pathlib
import re

import pytest

from app.modules import execution_compile as ec


class _Db:
    def __init__(self):
        self.calls = []

    async def execute(self, stmt, params=None):
        self.calls.append((str(stmt), params))

        class _R:
            def mappings(self_inner):
                class _M:
                    def first(self_m):
                        return None
                return _M()
        return _R()


@pytest.mark.asyncio
async def test_metadata_write_uses_the_caller_session(monkeypatch):
    import app.database as dbmod

    def _boom():
        raise AssertionError("opened a second session while the caller holds the row")
    monkeypatch.setattr(dbmod, "async_session", _boom)
    db = _Db()
    await ec._record_job_metadata("job-1", "compile_evidence", {"checked": True}, db=db)
    assert len(db.calls) == 1
    stmt, params = db.calls[0]
    assert "UPDATE jobs SET metadata" in stmt and params["k"] == "compile_evidence"
    brief, inp = await ec._job_given("job-1", db=db)
    assert (brief, inp) == ({}, "") and len(db.calls) == 2


def test_compile_threads_the_session_to_every_metadata_writer():
    src = (pathlib.Path(ec.__file__)).read_text()
    for call in ("_maybe_compile_value_check(job_id, text_value, nodes",
                 "_maybe_grounding_gate(job_id, synthesized, heuristic",
                 "_record_job_metadata(job_id, \"compile_evidence\", record",
                 "_record_grounding_metadata(job_id, record",
                 "_job_given(job_id"):
        sites = [m.end() for m in re.finditer(re.escape(call), src)
                 if not src[m.start() - 10:m.start()].endswith("def ")]
        assert sites, call
        for end in sites:
            assert src[end:end + 12].startswith(", db=db)"), f"{call} missing db=db"
