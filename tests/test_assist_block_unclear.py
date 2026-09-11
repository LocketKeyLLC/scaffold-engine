"""§17.1016 — an 'unclear' verdict plus a stated hedge must not commit.

§17.1014 stopped "i believe it is done but am unsure" counting as a completion
CLAIM, which removes the §17.890 exemption. Driving the live API showed that
fix was INERT for the input it was written for: the exemption only ever applied
to a 'failed'/'incomplete' verdict, and an evidence-free hedge produces
'unclear', which was in neither branch of `_blocked`.

Surface evidence (POST /assist/{id}/submit, node T2 "Install PM2 process
manager", 2026-09-11 03:30):

    {"status":"committed",
     "success_verdict":{"outcome":"unclear",
       "reason":"The operator only states \\"I believe it is done but am unsure\\"
                 with no actual command output or confirmation, so there is no
                 evidence to verify PM2 was installed."},
     "captured_facts":["PM2 installation status is UNKNOWN/unverified ..."]}

The engine recorded the work as unverified and marked the step done in the same
response.
"""
import inspect
import re

from app.config import Settings
from app.routers import assist as assist_router


def test_the_valve_exists_and_ships_off():
    s = Settings()
    assert s.assist_block_on_unclear_when_unsure is False, (
        "behavioural valves ship code-default-off, live via compose"
    )


def test_the_block_covers_unclear_only_when_the_operator_is_unsure():
    src = inspect.getsource(assist_router.assist_submit)
    assert "expresses_uncertainty(" in src, "uncertainty is never computed"
    assert "assist_block_on_unclear_when_unsure" in src, "the valve is not read"

    # The gate must be INSIDE the _blocked expression, not merely computed
    # nearby: blocking EVERY 'unclear' would also block when verification is
    # UNAVAILABLE (model down, parse failure), which §17.731 designed to never
    # block a submit. Asserting the literal's presence in the file missed a
    # mutation that deleted exactly this conjunction.
    start = src.index("_blocked = (")
    expr = src[start:src.index("\n        if _blocked", start)]
    assert '_v_outcome == "unclear"' in expr, "the unclear branch is missing"
    # Word-boundary, NOT substring: the valve is named
    # `assist_block_on_unclear_when_unsure`, which ENDS with "_unsure" — so a
    # bare `"_unsure" in expr` passes on the valve name alone and survives
    # deleting the variable. (Caught by mutating this very test.)
    assert re.search(r"\b_unsure\b", expr.replace(
        "assist_block_on_unclear_when_unsure", "")), (
        "the unclear branch does not consult the operator's stated uncertainty "
        "— this blocks every ambiguous verdict, including a verifier outage"
    )


def test_an_unclear_block_is_not_reported_as_a_failure():
    """The verifier did not judge the work bad — it could not tell. Telling an
    operator who just said they were unsure that their work FAILED is a third
    wrong answer in a row."""
    src = inspect.getsource(assist_router.assist_submit)
    assert '"step_unverified"' in src
    i_unclear = src.index('"step_unverified"')
    i_failed = src.index('else "verification_failed"')
    assert i_unclear < i_failed, "unclear must be matched before the failure default"


def test_the_turn_loop_treats_it_as_a_block():
    """Or §17.1014's "here is how to find out" never fires — the check is gated
    on `blocked_reason`, which only gets set for a recognised blocked status."""
    from app.modules import assist_turn
    src = inspect.getsource(assist_turn)
    assert '"step_unverified"' in src


def test_every_surface_renders_the_new_status():
    """A status no surface knows about is an operator staring at silence —
    the (field, surface) rule from the operator field inventory."""
    spa = open("app/ui/static/views/assist.js", encoding="utf-8").read()
    assert "step_unverified" in spa, "the SPA drops the status"
    assert "couldn't verify this step" in spa, "SPA must not call it a failure"
    owui = open("pipelines/_vendor/_assist_handlers.py", encoding="utf-8").read()
    assert "step_unverified" in owui, "the OWUI pipeline drops the status"


def test_confirm_still_outranks_the_new_block():
    """An explicit decision commits; a hedge does not. `operator_affirmed` is
    the §17.951 confirm path and must keep lifting the block."""
    src = inspect.getsource(assist_router.assist_submit)
    i_block = src.index("_blocked = (")
    i_affirm = src.index("_affirmed = bool(")
    assert i_affirm > i_block, "the affirm override must run after the block is computed"
    assert "_blocked = False" in src[i_affirm:]
