"""§17.2022 — a finding the gates dedupe per name names every line it is about.

§17.2010 found the shape on the patch-target gate: one finding per (module, name), naming only its first line, and
§17.1994 tells the retry to keep everything its findings do not name -- so each attempt fixed one line and kept the
rest. Three more gates deduped the same way: a stand-in for a finished module (§17.1836, and across files: a second
test file's stand-in was never named at all), a module run into a secret (§17.1857), and a key read from what a
finished function never returns (§17.1850). Each now names "`path:L1` (and lines L2, …)", which §17.2021 drops kept
tests at."""
from __future__ import annotations

from app.modules.engine_contracts import (
    _lines_named,
    internal_fakes,
    runs_secrets_for_real,
    unreturned_keys,
    unreturned_reads,
)
from tests.test_a_step_reads_only_keys_the_module_returns import BATCH, MODULES as KEY_MODULES
from tests.test_a_test_mocks_the_secret_a_module_reads import MODULES as SECRET_MODULES, VIEW
from tests.test_a_test_runs_the_real_engine_module import GENERATOR, MODULES as FAKE_MODULES

FAKE = '''import pytest

async def _fake(timestamp, *, db=None):
    return [{"id": 1, "payload": {}}]

async def test_a(monkeypatch):
    monkeypatch.setattr("app.modules.paper_signal_generator.get_data_as_of", _fake)

async def test_b(monkeypatch):
    monkeypatch.setattr("app.modules.paper_signal_generator.get_data_as_of", _fake)

async def test_c(monkeypatch):
    monkeypatch.setattr("app.modules.paper_signal_generator.get_data_as_of", _fake)
'''


def test_a_stand_in_made_three_times_is_one_finding_naming_all_three():
    found = internal_fakes({"app/modules/paper_signal_generator.py": GENERATOR,
                            "tests/test_paper_signal_generator.py": FAKE}, FAKE_MODULES, {"paper_signal_generator"})
    assert len(found) == 1
    assert found[0].startswith("`tests/test_paper_signal_generator.py:7` (and lines 10, 13) replaces `get_data_as_of`")
    assert _lines_named(found[0]) == {7, 10, 13}


def test_the_same_stand_in_in_a_second_test_file_is_named_there_too():
    found = internal_fakes({"app/modules/paper_signal_generator.py": GENERATOR,
                            "tests/test_paper_signal_generator.py": FAKE, "tests/test_paper_more.py": FAKE},
                           FAKE_MODULES, {"paper_signal_generator"})
    assert sorted(f.split("`")[1] for f in found) == ["tests/test_paper_more.py:7",
                                                      "tests/test_paper_signal_generator.py:7"]


SECRET = '''from app.modules.paper_edgar_form_4_ingestion import ingest_form_4_filings, parse_form_4

async def test_x(db):
    assert parse_form_4("<xml/>") == {}
    result = await ingest_form_4_filings(db=db, cik="0000320193")

async def test_y(db):
    result = await ingest_form_4_filings(db=db, cik="0000789019")
'''


def test_a_secret_reader_run_twice_is_one_finding_naming_both_lines():
    found = runs_secrets_for_real({"app/modules/paper_operator_view.py": VIEW, "tests/test_paper_twelve_modules.py": SECRET},
                                  SECRET_MODULES, {"paper_operator_view"})
    assert len(found) == 1
    assert found[0].startswith("`tests/test_paper_twelve_modules.py:5` (and line 8) runs `ingest_form_4_filings`")
    assert "`SEC_CONTACT_EMAIL`" in found[0]


def test_one_run_keeps_the_plain_form():
    one = SECRET.split("\nasync def test_y")[0] + "\n"
    found = runs_secrets_for_real({"app/modules/paper_operator_view.py": VIEW, "tests/test_paper_twelve_modules.py": one},
                                  SECRET_MODULES, {"paper_operator_view"})
    assert len(found) == 1 and found[0].startswith("`tests/test_paper_twelve_modules.py:5` runs")


def test_a_key_read_twice_from_what_is_never_returned_names_both_lines():
    twice = BATCH.replace("    return len(proposals)\n", '    print(context["equity"])\n    return len(proposals)\n')
    assert [(g["line"], g["also"]) for g in unreturned_keys(twice, KEY_MODULES)] == [(10, [15])]
    found = unreturned_reads({"app/modules/paper_daily_signal_proposal_batch.py": twice}, KEY_MODULES)
    assert len(found) == 1 and found[0].startswith(
        "`app/modules/paper_daily_signal_proposal_batch.py:10` (and line 15) reads `equity`")
