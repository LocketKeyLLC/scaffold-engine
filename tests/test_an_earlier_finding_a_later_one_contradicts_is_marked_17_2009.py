"""§17.2009 — an earlier refusal that asked for a patch target a later refusal says the module does not have is marked
as not applying, in the reviewer feedback a retry is given.

Live (69fe61c4 NOKEY2, 2026-11-24): one draft's module read `values_for` itself, so the view gate asked for a test
patching `app.modules.paper_wire_engine_entry_module.values_for`; the next drafts dropped that read, the patch-target
gate refused the patch ("which has no such name"), and the history said "every one must stay fixed" -- the step kept
the patch through six attempts and the job blocked."""
from __future__ import annotations

from app.modules.execution_retry import _format_reviewer_feedback

MOD = "paper_wire_engine_entry_module"
#: the view gate's refusal for the draft whose module read `values_for` (live text, trimmed)
VIEW = ("§17.1749 this step writes code for the engine itself, and it reaches for things the engine does not have:\n"
        f"  - `app/modules/{MOD}.py:355` the view `GET /dashboard` (`_make_router.dashboard`) reaches the operator "
        "secrets `ALPACA_API_KEY_ID`, `ALPACA_API_SECRET_KEY` through `get_spy_benchmark` (read by "
        f"`paper_alpaca_paper_client`, `{MOD}`) -- … Add a test that requests the route with "
        f"`app.modules.paper_alpaca_paper_client.values_for`, `app.modules.{MOD}.values_for` patched to return `{{}}`")


def _no_such(line: int) -> str:
    return ("§17.1749 this step writes code for the engine itself, and it reaches for things the engine does not have:\n"
            f"  - `tests/test_{MOD}.py:{line}` patches `values_for` on `{MOD}`, which has no such name (it has logger, "
            "LAB_SLUG) -- the sandbox raises AttributeError.")


def _feedback(prior: list[str], newest: str, retry: int = 3) -> str:
    return _format_reviewer_feedback({"retry_count": retry, "last_verification_reason": newest,
                                      "prior_refusals": prior})


def _history(fb: str) -> str:
    return fb.split("Earlier attempts of this step were refused for these too", 1)[1]


def test_the_newest_refusal_marks_the_earlier_demand_for_the_patch():
    hist = _history(_feedback([VIEW], _no_such(799)))
    assert f"`app.modules.{MOD}.values_for` (no longer: `{MOD}` has no `values_for` now -- patch it nowhere)" in hist
    # the rest of the demand stands
    assert "`app.modules.paper_alpaca_paper_client.values_for`, " in hist
    assert "`app.modules.paper_alpaca_paper_client.values_for` (no longer" not in hist


def test_a_later_earlier_refusal_marks_it_too_when_the_newest_is_about_something_else():
    # the step dropped the patch; the newest refusal no longer names it -- the demand must not come back
    hist = _history(_feedback([VIEW, _no_such(763)], "§17.1765 engine-test sandbox: its tests failed"))
    assert f"`app.modules.{MOD}.values_for` (no longer:" in hist


def test_a_refusal_only_older_ones_contradict_is_kept_as_it_was():
    # the correct miss: a demand nothing later contradicts is untouched, and order matters
    hist = _history(_feedback([_no_such(763), VIEW], "§17.1765 engine-test sandbox: its tests failed"))
    assert "(no longer:" not in hist
    hist = _history(_feedback([VIEW], "§17.1765 engine-test sandbox: its tests failed"))
    assert "(no longer:" not in hist


def test_the_history_says_the_reason_read_against_this_draft_wins():
    fb = _feedback([VIEW], _no_such(799))
    assert "every one must stay fixed, except where the reason above, read against this draft, says otherwise" in fb
