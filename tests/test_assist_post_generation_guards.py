"""§17.1013 — the post-generation guards must run on BOTH guide paths.

The live homelab job is the evidence. Node ADD3, titled "Rewrite the Caddyfile
in pieces to match the intended reverse-proxy config", was told:

    "This step has been removed from the project plan. There is nothing to run."

across six generations over eleven days. The node was live the whole time,
`claims_plan_mutation` fires on that exact sentence, and the correction banner
exists — but the guards lived only inside `ensure_guidance`, which
`generate_guidance_stream` (the SPA path the operator actually drives) does not
call. The operator pressed Done on a Caddyfile rewrite that never happened.

Sixth instance of the same stream-vs-non-stream divergence after §17.854,
§17.975, §17.976, §17.984 and §17.1012.
"""
import inspect
import re as _re

import pytest

from app.modules import assist_guide

REAL_FALSE_CLAIM = ("## 👉 Do this next\n**No action needed:** This step has "
                    "been removed from the project plan. There is nothing to run.")


class _DB:
    """Minimal async db double: job lookup, then node status."""

    def __init__(self, node_status):
        self._node_status = node_status
        self._calls = 0

    async def execute(self, *_a, **_k):
        self._calls += 1
        outer = self

        class _R:
            def mappings(self):
                class _M:
                    def first(self_inner):
                        return {"job_id": "00000000-0000-0000-0000-000000000001"}
                return _M()

            def scalar(self):
                return outer._node_status
        return _R()


# ── the divergence itself ────────────────────────────────────────────────
def test_both_guide_paths_run_the_guards():
    for fn in ("ensure_guidance", "generate_guidance_stream"):
        src = inspect.getsource(getattr(assist_guide, fn))
        assert "apply_post_generation_guards(" in src, (
            f"{fn} does not run the post-generation guards — a false plan "
            f"claim reaches the operator uncorrected there (§17.937/§17.1013)"
        )


def test_the_guards_live_in_one_place():
    """One implementation, or the next directive drifts again."""
    src = (assist_guide.__file__)
    with open(src, encoding="utf-8") as fh:
        text = fh.read()
    assert text.count("async def apply_post_generation_guards(") == 1
    # The enforcement body must not have been copied back inline: exactly one
    # CALL site (the definition of the banner helper is not a call).
    calls = len(_re.findall(r"(?<!def )false_plan_claim_banner\(", text))
    assert calls == 1, f"expected one banner call site, found {calls}"


# ── the correction itself ────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_false_plan_claim_on_a_live_node_is_corrected():
    out, meta = await assist_guide.apply_post_generation_guards(
        REAL_FALSE_CLAIM, session_id="s", node_key="ADD3",
        title="Rewrite the Caddyfile", db=_DB("pending"),
    )
    assert meta.get("false_plan_claim") is True
    assert "the plan has NOT changed" in out
    assert out.index("NOT changed") < out.index("removed from the project plan"), \
        "prepend position must put the correction ABOVE the false claim"


@pytest.mark.asyncio
async def test_a_done_node_was_not_removed_from_the_plan():
    """§17.1013 — the guard skipped both 'skipped' AND 'done'. A COMPLETED step
    was not "removed from the plan", and that conflation is what shielded the
    claim on ADD3 once the operator pressed Done on it."""
    out, meta = await assist_guide.apply_post_generation_guards(
        REAL_FALSE_CLAIM, session_id="s", node_key="ADD3",
        title="Rewrite the Caddyfile", db=_DB("done"),
    )
    assert meta.get("false_plan_claim") is True, "'done' must not shield the claim"


@pytest.mark.asyncio
async def test_a_skipped_node_really_was_retired():
    """'skipped' means the claim is TRUE — correcting it would be the lie."""
    out, meta = await assist_guide.apply_post_generation_guards(
        REAL_FALSE_CLAIM, session_id="s", node_key="ADD3",
        title="Rewrite the Caddyfile", db=_DB("skipped"),
    )
    assert "false_plan_claim" not in meta
    assert "the plan has NOT changed" not in out


@pytest.mark.asyncio
async def test_streamed_corrections_append_below_what_was_read():
    """The operator has already watched the text go past; a correction cannot
    be inserted above text that is on screen."""
    out, meta = await assist_guide.apply_post_generation_guards(
        REAL_FALSE_CLAIM, session_id="s", node_key="ADD3",
        title="Rewrite the Caddyfile", db=_DB("pending"),
        banner_position="append",
    )
    assert meta.get("false_plan_claim") is True
    assert out.startswith("## 👉 Do this next")
    assert out.index("removed from the project plan") < out.index("NOT changed")


@pytest.mark.asyncio
async def test_guards_are_a_noop_on_empty_output():
    out, meta = await assist_guide.apply_post_generation_guards(
        "", session_id="s", node_key="T1", title="t", db=_DB("pending"))
    assert out == "" and meta == {}


@pytest.mark.asyncio
async def test_ordinary_completion_language_is_not_corrected():
    """Precision matters: the banner shouts, so it must not fire on a step that
    merely finished."""
    for benign in ("This step is complete. Press Done to advance.",
                   "You have finished configuring the reverse proxy.",
                   "Nothing to run here — the service is already active."):
        out, meta = await assist_guide.apply_post_generation_guards(
            benign, session_id="s", node_key="T1", title="t", db=_DB("pending"))
        assert "false_plan_claim" not in meta, f"false positive on: {benign}"


# ── §17.1013 — note capture dedupes by default ───────────────────────────
def test_record_note_dedupes_by_default():
    """The live session held "wants to build a markdown linter" FOUR times,
    and every note rides every subsequent prompt. `assemble_generation_memory`
    already deduped in Python against the known set, so in-process bookkeeping
    is not the guarantee — the write itself has to be."""
    import inspect
    from app.modules import assist_notes
    sig = inspect.signature(assist_notes.record_note)
    assert sig.parameters["dedupe"].default is True, (
        "record_note must dedupe unless a caller opts out (§17.1013)"
    )
