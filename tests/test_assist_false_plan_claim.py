"""§17.937 — the engine may not assert a plan change it did not make.

Live (session 613dd1df, node ADD3 "Implement a Markdown linter"): the operator
asked for the step to go away. Asked to write its walkthrough, the model
answered *"The project plan has been updated to remove this step. No further
action is required."* — FOUR times across five days, 2026-08-31 → 09-04, while
the node sat `pending` in dag_nodes the entire time.

§17.927 dutifully appended "reply `skip` to retire this step" underneath. That
made it worse, not better: the operator had just been told in plain language
that the step was already gone, so the offer read as noise. The step stayed in
their plan for a week, and they eventually removed it by hand.

The claim is the bug. `skip` is what retires a step; saying so is not.
"""
import pytest

from app.modules import assist_guide
from app.modules.assist_directives import apply_plan_authority
from app.modules.assist_guide import (
    claims_plan_mutation,
    concludes_no_action_required,
    false_plan_claim_banner,
)


# ── detecting the claim ───────────────────────────────────────────────────


@pytest.mark.parametrize("text_out", [
    # verbatim from the live transcript
    "The project plan has been updated to remove this step. No further action "
    "is required for the Markdown linter implementation.",
    "This step has been removed from the project plan per the operator's "
    "request. No actions are required.",
    # the same assertion, other phrasings
    "I have removed this step.",
    "The plan was revised.",
    "This step is now retired.",
    "It has been removed from the plan.",
])
def test_plan_mutation_claims_are_detected(text_out):
    assert claims_plan_mutation(text_out) is True


@pytest.mark.parametrize("text_out", [
    # restating the REQUEST is not claiming it happened
    "The operator has explicitly requested to remove this step from the "
    "project plan.",
    # offering the real action is exactly right
    "If you want this step removed, reply `skip`.",
    # ordinary guidance that merely contains the word "plan"
    "Update the plan file at /etc/netplan/00-installer.yaml and reboot.",
    "Run `qm set 110 --delete hostpci0` and tell me what it shows.",
    "",
])
def test_ordinary_guidance_is_not_flagged(text_out):
    """Over-firing would staple a scary correction onto correct walkthroughs."""
    assert claims_plan_mutation(text_out) is False


# ── the correction ────────────────────────────────────────────────────────


def test_banner_states_the_plan_did_not_change_and_names_the_real_action():
    b = false_plan_claim_banner("Implement a Markdown linter")
    assert "the plan has NOT changed" in b
    assert "Implement a Markdown linter" in b
    assert "`skip`" in b
    # it must own the error rather than blaming the operator
    assert "I said this step was removed — it was not." in b


def test_banner_leads_rather_than_trails():
    """Same reason as §17.882's integrity warning: the operator reads the top
    of the reply and acts on it. A correction underneath a confident false
    statement is a correction they never see."""
    guidance = "The project plan has been updated to remove this step."
    corrected = false_plan_claim_banner("Some step") + guidance
    assert corrected.index("Correction") < corrected.index("has been updated")
    assert corrected.rstrip().endswith(guidance)


def test_the_live_text_trips_both_gates():
    """§17.937 and §17.927 fire on the same string, which is why ordering
    matters: the correction must come first so "the plan has NOT changed —
    reply `skip`" reads as one coherent instruction."""
    live = ("The project plan has been updated to remove this step. No further "
            "action is required for the Markdown linter implementation.")
    assert claims_plan_mutation(live) is True
    assert concludes_no_action_required(live) is True


# ── the prevention half ───────────────────────────────────────────────────


def test_directive_forbids_narrating_plan_changes():
    out = apply_plan_authority("SYSTEM")
    assert out.startswith("SYSTEM")
    assert "CANNOT CHANGE THE PLAN BY SAYING SO" in out
    assert "`skip`" in out


def test_directive_can_be_disabled():
    assert apply_plan_authority("SYSTEM", enabled=False) == "SYSTEM"


# ── wiring ────────────────────────────────────────────────────────────────


def test_wired_into_both_guide_paths_and_fail_soft():
    """§17.1013 — the guards moved out of `ensure_guidance` into the shared
    `apply_post_generation_guards`, because the STREAM path (the SPA path the
    operator drives) does not call `ensure_guidance` and so never ran them at
    all. Assert the guard body plus the delegation from both generators."""
    import inspect

    from app.modules import assist_guide

    guards = inspect.getsource(assist_guide.apply_post_generation_guards)
    assert "claims_plan_mutation" in guards
    assert "false_plan_claim_banner" in guards
    # §17.1013 — 'skipped' means the claim is TRUE. 'done' does NOT: a
    # COMPLETED step was not "removed from the plan", and that conflation
    # shielded the false claim on the live ADD3 once Done was pressed.
    assert '!= "skipped"' in guards
    assert '"skipped", "done"' not in guards, (
        "a done node was never 'removed from the plan' (§17.1013)"
    )
    # a correction must never break a guide
    assert "assist_false_plan_claim_check_failed" in guards

    for fn in ("ensure_guidance", "generate_guidance_stream"):
        src = inspect.getsource(getattr(assist_guide, fn))
        assert "apply_post_generation_guards(" in src, f"{fn} skips the guards"


def test_correction_runs_before_the_no_action_offer():
    """Ordering is load-bearing. §17.927 appends "reply `skip` to retire this
    step"; §17.937 prepends "the plan has NOT changed". If the offer ran first
    the operator would read a skip offer stapled under a confident false
    statement — which is precisely the shape that failed live."""
    import inspect

    from app.modules import assist_guide

    src = inspect.getsource(assist_guide.apply_post_generation_guards)
    assert src.index("claims_plan_mutation") < src.index(
        "concludes_no_action_required")


def test_directive_is_applied_wherever_guidance_is_generated():
    """Both generation sites, or the model keeps making the claim on one path.

    §17.1011 — this used to assert ``src.count("apply_plan_authority(system)")
    == 2``, one per generator. The two directive stacks were byte-identical and
    have been collapsed into ``_build_guide_system``, so the literal now appears
    ONCE and the count said "regression" about a refactor that made the drift
    impossible. Counting occurrences was always the weaker check anyway: it
    passes just as happily when both copies are wrong. Assert the property the
    count was standing in for — that each generator routes through the shared
    builder, and that the builder actually emits the directive.
    """
    import inspect
    import pathlib

    src = pathlib.Path("app/modules/assist_guide.py").read_text()
    assert src.count("apply_plan_authority(system)") == 1, (
        "expected exactly one application, in the shared builder"
    )
    for fn in ("generate_guidance", "generate_guidance_stream"):
        fsrc = inspect.getsource(getattr(assist_guide, fn))
        assert "_build_guide_system(" in fsrc, (
            f"{fn} no longer routes through the shared directive builder — it "
            f"can now silently miss a directive (§17.1011)"
        )

    class _Ctx:
        tool = "shell"

    system = assist_guide._build_guide_system(_Ctx(), "normal", is_decision=False)
    assert "YOU CANNOT CHANGE THE PLAN BY SAYING SO" in system
