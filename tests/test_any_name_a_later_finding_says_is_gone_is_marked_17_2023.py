"""§17.2023 — an earlier refusal's demand for a name a later refusal says the module no longer has is marked as not
applying, whatever gate said it: not only a patch target (§17.2009).

The history a retry is given says "every one must stay fixed". §17.2009 marked an earlier demand for a patch target the
patch-target gate later called missing; every other "`M` has no `N`" finding -- an import of it, a call of it, a read of
it as a module attribute -- left the earlier demand binding. The live shape: §17.1850's refusal names where a key
lives ("`M.account_snapshot()` returns dicts with it"), the step's next draft follows that hint, and by then the
sibling's module was rewritten without it; the import gate refuses "`M` (step T6D) has no `account_snapshot`" while
the history still asks for it."""
from __future__ import annotations

from app.modules.engine_contracts import unreturned_reads
from app.modules.execution_retry import _format_reviewer_feedback, _superseded
from tests.test_a_step_reads_only_keys_the_module_returns import DATA, FILES, MODULES

MOD = "paper_daily_signal_proposal_batch_data"
WITH_SNAPSHOT = DATA.replace("async def load_batch_context", "async def account_snapshot(*, db=None):\n"
                             '    return {"equity": 1, "cash": 2}\n\n\nasync def load_batch_context')


def _earlier() -> str:
    """The real §17.1850 refusal, from the producer, for the draft whose sibling still had `account_snapshot`."""
    found = unreturned_reads(FILES, {**MODULES, MOD: WITH_SNAPSHOT})
    assert f"`{MOD}.account_snapshot()` returns dicts with it" in found[0]
    return "§17.1749 this step writes code for the engine itself:\n  - " + found[0]


IMPORT = (f"§17.1749 this step writes code for the engine itself:\n  - `from app.modules.{MOD} import account_snapshot`: "
          f"`{MOD}` (step T6D) has no `account_snapshot` (it has load_batch_context)")
CALL = (f"§17.1749 this step writes code for the engine itself:\n  - `data.account_snapshot(…)`: `{MOD}` (step T6D) has "
        "no `account_snapshot`")
READ = (f"§17.1749 this step writes code for the engine itself:\n  - `tests/test_batch.py:12` reads "
        f"`data.account_snapshot`, but `{MOD}` has no top-level `account_snapshot` (it has `load_batch_context`)")


def test_the_import_gate_marks_the_earlier_hint():
    out = _superseded(_earlier(), IMPORT)
    assert (f"`{MOD}.account_snapshot()` (no longer: `{MOD}` has no `account_snapshot` now -- use it nowhere) "
            "returns dicts with it") in out
    # the correct miss: the rest of the earlier demand stands, unmarked
    assert "`AlpacaPaperClient.get_account()`;" in out and out.count("(no longer:") == 1


def test_the_call_and_read_gates_mark_it_too():
    for newest in (CALL, READ):
        assert "(no longer: `" + MOD + "` has no `account_snapshot` now" in _superseded(_earlier(), newest)


def test_every_mention_is_marked_and_a_patch_keeps_its_own_words():
    earlier = (f"Patch `app.modules.{MOD}.values_for` here, and again `app.modules.{MOD}.values_for` there; "
               f"`app.modules.paper_alpaca_paper_client.values_for` stays")
    newest = f"`tests/t.py:3` patches `values_for` on `{MOD}`, which has no such name (it has x)"
    out = _superseded(earlier, newest)
    assert out.count("(no longer: `" + MOD + "` has no `values_for` now -- patch it nowhere)") == 2
    assert "`app.modules.paper_alpaca_paper_client.values_for` stays" in out
    assert _superseded(out, newest) == out                       # read again (each later refusal): marked once


def test_an_unrelated_newest_marks_nothing_and_the_feedback_carries_the_mark():
    assert _superseded(_earlier(), "`tests/t.py:3` patches `x` on `other_mod`, which has no such name") == _earlier()
    fb = _format_reviewer_feedback({"retry_count": 2, "last_verification_reason": IMPORT, "prior_refusals": [_earlier()]})
    assert "has no `account_snapshot` now -- use it nowhere" in fb.split("Earlier attempts", 1)[1]
