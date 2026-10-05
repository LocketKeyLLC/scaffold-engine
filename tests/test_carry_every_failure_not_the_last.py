r"""§17.1371 — carry EVERY failure, not just the last one.

§17.1366 put the previous attempt's report in front of the drafter and it worked:
the next draft of ADD132 added the `configContract` the machine had asked for.
It also **dropped `priority`**, which the draft before it had got right. Two
consecutive runs of the same step, live on 2026-10-05:

    run A   'Config Contract' must not be empty.          (that draft HAD priority)
    run B   'Priority' must be between 1 and 50. You entered 0.   (that draft HAD configContract)

Each draft read one failure, fixed exactly that, and lost the other. A fix loop
that trades one defect for another is not converging — and the engine had every
attempt on disk the whole time: `reset` NULLs `output_text` only after auditing
it, so every failure survives in `dag_node_edits.before`.

So the drafter now gets **every distinct complaint the machine has made about
this step**, with the instruction that its draft must satisfy all of them at
once.
"""
from __future__ import annotations

import pathlib

import pytest

from app.modules.previous_attempt import (carry_forward, every_attempt,
                                          failures_in, what_failed)

HISTORY = (pathlib.Path(__file__).parent / "fixtures"
           / "add132_every_attempt_2026_10_05.txt").read_text()
#: the four reports as they sit in the audit table, newest first
REPORTS = HISTORY.split("## Inputs needed")


# --------------------------------------------------- the machine's complaints


def test_both_complaints_are_found_in_the_real_history():
    got = failures_in(REPORTS)
    assert any("Config Contract" in g and "must not be empty" in g for g in got), got
    assert any("Priority" in g and "between 1 and 50" in g for g in got), got


def test_each_complaint_appears_once_however_many_attempts_made_it():
    got = failures_in(REPORTS + REPORTS)
    assert len(got) == len(failures_in(REPORTS))


def test_a_report_with_no_complaint_yields_none():
    assert failures_in(["## Executed on x\n$ true\nok\n"]) == []
    assert failures_in([]) == []
    assert failures_in(None) == []


def test_the_shapes_the_arr_apis_use():
    """`\\u0027` is how the engine's own record stores the quotes."""
    assert failures_in(["\\u0027Config Contract\\u0027 must not be empty."])
    assert failures_in(["'Priority' must be between 1 and 50. You entered 0."])
    assert failures_in(['"Root Folder Path" is required'])


# ------------------------------------------------- every attempt, not the last


class _Res:
    def __init__(self, one=None, many=None):
        self._one, self._many = one, many

    def scalar(self):
        return self._one

    def scalars(self):
        return self

    def all(self):
        return self._many or []


class _DB:
    def __init__(self, row=None, pres=None):
        self.row, self.pres, self.limits = row, pres or [], []

    async def execute(self, stmt, params=None):
        if "dag_node_edits" in str(stmt):
            self.limits.append((params or {}).get("n"))
            return _Res(many=self.pres)
        return _Res(one=self.row)


@pytest.mark.asyncio
async def test_every_attempt_returns_the_row_then_the_pre_images():
    db = _DB(row="A", pres=["B", "C"])
    assert await every_attempt(db, "j", "ADD132") == ["A", "B", "C"]
    assert db.limits == [6], "the pre-image query is bounded"


@pytest.mark.asyncio
async def test_a_reset_step_has_no_row_and_reads_only_pre_images():
    db = _DB(row=None, pres=["B", "C"])
    assert await every_attempt(db, "j", "ADD132") == ["B", "C"]


@pytest.mark.asyncio
async def test_the_number_of_reports_is_capped():
    db = _DB(row="A", pres=[str(i) for i in range(20)])
    assert len(await every_attempt(db, "j", "ADD132", limit=3)) == 3


@pytest.mark.asyncio
async def test_a_database_that_raises_is_not_fatal():
    class _Boom:
        async def execute(self, *a, **k):
            raise RuntimeError("no db")
    assert await every_attempt(_Boom(), "j", "ADD132") == []


# ------------------------------------------------------- what the drafter gets


@pytest.mark.asyncio
async def test_the_block_lists_every_complaint_and_says_to_satisfy_all():
    db = _DB(row=REPORTS[0], pres=REPORTS[1:])
    got = await carry_forward(db, "j", "ADD132")
    assert "EVERY COMPLAINT THE MACHINE HAS MADE ABOUT THIS STEP" in got
    assert "must satisfy ALL of them at once" in got
    assert "Config Contract" in got and "Priority" in got
    assert "dropping what an\nearlier draft already got right" in got \
        or "dropping what an earlier draft already got right" in got


@pytest.mark.asyncio
async def test_one_complaint_is_stated_plainly_without_the_list():
    db = _DB(row="## Executed on x\n'Priority' must be between 1 and 50. You entered 0.\n")
    got = await carry_forward(db, "j", "ADD9")
    assert "What the machine complained about:" in got
    assert "EVERY COMPLAINT" not in got


@pytest.mark.asyncio
async def test_a_step_never_tried_still_carries_nothing():
    assert await carry_forward(_DB(), "j", "ADD9999") == ""


@pytest.mark.asyncio
async def test_the_reading_names_how_many_attempts_and_complaints():
    from app.modules.measured import Reading
    r = Reading()
    db = _DB(row=REPORTS[0], pres=REPORTS[1:])
    await carry_forward(db, "j", "ADD132", reading=r)
    assert any("previous attempts" in w and "complaint" in d for w, d in r.saw), r.saw


@pytest.mark.asyncio
async def test_the_executed_sections_still_come_through():
    """§17.1366's half is unchanged: what ran and the diagnosis, from the newest
    report."""
    db = _DB(row=REPORTS[0], pres=REPORTS[1:])
    got = await carry_forward(db, "j", "ADD132")
    assert what_failed(REPORTS[0])[:200] in got


def test_the_fixture_carries_no_derived_secret():
    import re
    assert not re.search(r"@ByteArray\([A-Za-z0-9+/]{8,}", HISTORY)
