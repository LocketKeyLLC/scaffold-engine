r"""§17.1366 — a step that failed before is drafted with what failed and why.

ADD132 drafted **seven** times on 2026-10-04 and the seventh rediscovered ground
the second had covered. Attempt six did most of the work — `qBittorrent login:
Ok.`, the credentials written into `[Preferences]` and verified on the machine —
then failed loudly (`--fail-with-body`, exit 22) on Radarr's API:

    'Config Contract' must not be empty.
    'Priority' must be between 1 and 50. You entered 0.

The engine diagnosed it correctly: *"it sent an incomplete payload … the correct
move is to update the existing client … sends the full existing client object
back with PUT, so configContract and priority stay intact."*

The next draft was made blind to all of it. From `llm_traces`:

    23:24:45   51,271 chars   "Config Contract" present   <- diagnose_failure
    23:25:21   51,271 chars   "Config Contract" present   <- diagnose_failure
    23:25:46   10,834 chars   absent                      <- the DRAFTER
    23:27:40   17,106 chars   absent                      <- the drafter again

It added `priority: 1` — half of one error — and still omitted `configContract`.

`reset` is the documented way to give a supervised step another attempt
(§17.1284) and it NULLs `output_text`. It audits the pre-image first, so the whole
report survives in `dag_node_edits.before`: **11,504 bytes for ADD132**, holding
the error and the diagnosis, read by nobody. The engine produced the knowledge,
stored it, and threw it away at the moment it was needed — §17.1363's shape one
layer up.
"""
from __future__ import annotations

import pathlib

import pytest

from app.modules.previous_attempt import carry_forward, what_failed

REPORT = (pathlib.Path(__file__).parent / "fixtures"
          / "add132_failed_report_2026_10_04.txt").read_text()


# ------------------------------------------------- what is worth carrying


def test_the_decisive_part_is_carried_and_the_paste_is_not():
    got = what_failed(REPORT)
    assert 1200 < len(got) < 4000, len(got)         # 2,304 of 11,504
    assert "'Config Contract' must not be empty" in got.replace("\\u0027", "'")
    assert "update the existing client" in got
    assert "qBittorrent login: Ok." in got          # what the attempt got RIGHT
    # the operator-facing paste is the bulk of the report and none of its use
    assert "Paste 1 of 3" not in got
    assert "cat > /tmp/fix_qbit_auth.py" not in got


def test_the_sections_it_keeps_are_the_named_ones():
    got = what_failed(REPORT)
    assert "### Executed on pve-runner" in got
    assert "### Diagnosis" in got
    assert "### Fix" in got
    assert "### Rollback" not in got and "### Verify" not in got


def test_a_report_with_nothing_to_learn_carries_nothing():
    assert what_failed("") == ""
    assert what_failed("## Verify\nsome check\n") == ""
    assert what_failed("no headings at all") == ""


def test_each_section_and_the_whole_are_capped():
    """A retry's context must not crowd out the step itself. (The `x` count is
    1401, not 1400: the heading `### Fix` contributes one.)"""
    big = "## Diagnosis\n" + ("x" * 9000) + "\n## Fix\n" + ("y" * 9000)
    got = what_failed(big)
    assert len(got) <= 3600, len(got)
    assert got.count("x") == 1401 and got.count("y") == 1400
    assert "### Diagnosis" in got and "### Fix" in got


# --------------------------------------------------------- where it comes from


class _Res:
    """§17.1371 — the pre-image query reads MANY now (`.scalars().all()`), because
    carrying only the latest failure is what made ADD132 oscillate."""

    def __init__(self, one=None, many=None):
        self._one, self._many = one, many

    def scalar(self):
        return self._one

    def scalars(self):
        return self

    def all(self):
        return self._many or []


class _DB:
    """The node row first, then the reset pre-images."""

    def __init__(self, row=None, pre=None):
        self.row, self.pre, self.asked = row, pre, []

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        if "dag_node_edits" in sql:
            self.asked.append("dag_node_edits")
            return _Res(many=[self.pre] if self.pre else [])
        self.asked.append("dag_nodes")
        return _Res(one=self.row)


@pytest.mark.asyncio
async def test_a_failed_step_still_holding_its_report_is_read_from_the_row():
    db = _DB(row=REPORT)
    got = await carry_forward(db, "j", "ADD132")
    assert "Config Contract" in got.replace("\\u0027", "'")
    # §17.1371 — the audit table is read as well now, always: the row holds the
    # LATEST failure and the earlier ones are what stopped ADD132 converging.
    assert db.asked == ["dag_nodes", "dag_node_edits"], db.asked


@pytest.mark.asyncio
async def test_a_reset_step_is_read_from_the_pre_image():
    """`reset` NULLs the column and audits the old value first."""
    db = _DB(row=None, pre=REPORT)
    got = await carry_forward(db, "j", "ADD132")
    assert "update the existing client" in got
    assert db.asked == ["dag_nodes", "dag_node_edits"]


@pytest.mark.asyncio
async def test_a_step_never_tried_carries_nothing():
    db = _DB(row=None, pre=None)
    assert await carry_forward(db, "j", "ADD9999") == ""


@pytest.mark.asyncio
async def test_the_block_tells_the_drafter_what_to_do_with_it():
    got = await carry_forward(_DB(row=REPORT), "j", "ADD132")
    assert got.startswith("THIS STEP HAS BEEN TRIED BEFORE AND FAILED")
    assert "must differ in the way the diagnosis says" in got
    assert "already done and must not be undone" in got


@pytest.mark.asyncio
async def test_it_joins_the_reading_so_the_scope_names_it():
    from app.modules.measured import Reading
    r = Reading()
    await carry_forward(_DB(row=REPORT), "j", "ADD132", reading=r)
    assert any("previous attempt" in w for w, _d in r.saw), r.saw
    assert r.gaps() == []


@pytest.mark.asyncio
async def test_a_database_that_raises_is_not_fatal():
    class _Boom:
        async def execute(self, *a, **k):
            raise RuntimeError("no db")
    assert await carry_forward(_Boom(), "j", "ADD132") == ""


# ------------------------------------------------------------- the lane


def test_the_pause_carries_it_into_the_drafters_context():
    import inspect

    from app.modules import execution_agent as ea
    src = inspect.getsource(ea._pause_for_decision)
    assert "_pa.carry_forward(" in src
    assert "up_block = (up_block + " in src
    assert "previous_attempt_carried" in src
    # before the draft is made, not after
    assert src.index("_pa.carry_forward(") < src.index("supervised_runs.draft_runbook(")


def test_the_fixture_carries_no_derived_secret():
    r"""The stored report held the real `@ByteArray(salt:key)` — the PBKDF2 hash
    the block computed at run time. `scrub_run_output` masks values the ENGINE
    holds, and it never held this one: the block derived it from
    `$MASS_PASSWORD` inside the run. So a value a block DERIVES from a secret
    reaches the record in clear. Redacted here; noted in §17.1366 as its own
    finding."""
    assert "@ByteArray(<REDACTED" in REPORT
    import re
    assert not re.search(r"@ByteArray\([A-Za-z0-9+/]{8,}", REPORT)
    assert REPORT.count("$MASS_PASSWORD") >= 1, "the password itself is by NAME"
