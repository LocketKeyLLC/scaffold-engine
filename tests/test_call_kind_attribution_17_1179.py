"""§17.1179 (audit M17/M18) — the cost of a turn must be attributable.

Measured on the live database before this change:

    bucket                 calls    inference hours
    call_kind IS NULL     19,171               34.7
    model_ab               2,974                3.9
    assist:<status line>     668                0.6

`call_kind` had exactly ONE real call site in the codebase
(`execution_compile`'s "synthesis"). Everything else fell through to
`turn_timing.default_call_kind()`, which slugifies the OPERATOR-FACING STATUS
LINE — so `assist:preparing_the_walkthrough_for_t6` and `…_for_add49` are the
same code in two buckets, and every background pass is NULL by construction
because it runs outside a turn timer.

M17 (one submit → six background passes) and M18 (a state check → ~15 calls)
are both cost findings, and neither could be measured. These tests pin the
labels so the measurement stays possible.
"""
from __future__ import annotations

import pytest

from app.utils.cost_tracking import current_call_kind, tagged_calls

#: the eight passes M17 and M18 name, and the label each must carry.
EXPECTED = {
    ("app.modules.assist_guide", "distill_facts"): "assist.memory.distill_facts",
    ("app.modules.assist_guide", "distill_turn_memory"): "assist.memory.distill_turn_memory",
    ("app.modules.assist_guide", "consolidate_facts"): "assist.memory.consolidate_facts",
    ("app.modules.assist_guide", "classify_superseded_facts"): "assist.memory.classify_superseded_facts",
    ("app.modules.assist_guide", "check_grounding"): "assist.verify.check_grounding",
    ("app.modules.assist_memory", "reconcile_on_commit"): "assist.reconcile.on_commit",
    ("app.modules.assist_state_check", "plan_probes"): "assist.state_check.plan_probes",
    ("app.modules.assist_state_check", "judge_outputs"): "assist.state_check.judge_outputs",
}


@pytest.mark.parametrize("ref,kind", sorted(EXPECTED.items()), ids=lambda v: v if isinstance(v, str) else v[1])
def test_every_background_pass_carries_a_stable_label(ref, kind):
    import importlib
    mod = importlib.import_module(ref[0])
    fn = getattr(mod, ref[1])
    got = getattr(fn, "__scaffold_call_kind__", None)
    assert got == kind, (
        f"{ref[0]}.{ref[1]} is untagged, so its cost lands in the NULL bucket "
        f"(34.7 hours of inference already does). Expected {kind!r}, got {got!r}."
    )


@pytest.mark.asyncio
async def test_the_tag_is_visible_to_the_recorder_during_the_call():
    """A decorator that sets nothing is the dead-fix shape. The label must be
    readable from inside the awaited body — that is where the cost recorder
    looks it up."""
    seen = {}

    @tagged_calls("assist.test.probe")
    async def pass_():
        seen["inside"] = current_call_kind.get()
        return 1

    assert current_call_kind.get() is None
    assert await pass_() == 1
    assert seen["inside"] == "assist.test.probe"
    assert current_call_kind.get() is None, "the ContextVar must be reset on exit"


@pytest.mark.asyncio
async def test_the_label_survives_an_exception():
    @tagged_calls("assist.test.boom")
    async def pass_():
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await pass_()
    assert current_call_kind.get() is None, "a raising pass must not leak its tag"


def test_labels_are_a_closed_vocabulary_not_status_lines():
    """The defect being fixed was unbounded cardinality: a label derived from
    the operator-facing progress string. Every label here must be a stable
    dotted call-site path with no step keys or prose in it."""
    import re
    for (mod, fn), kind in EXPECTED.items():
        assert re.fullmatch(r"[a-z_]+(?:\.[a-z_]+)+", kind), f"{kind!r} is not a call-site path"
        assert "add" not in kind.split(".")[-1] or fn.startswith("add"), kind
