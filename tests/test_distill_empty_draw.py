"""§17.986 — an empty tool-call payload is a failed draw, not an answer.

Found verifying §17.985: the ideation phase-2 distill logged
`phase2_distill entry_count=0` with `results_found=30` and NOTHING else — no
`llm_failed`, no `parse_failed`. Both remaining exits from `distill_entries`
were silent, so an entirely inert stage looked identical to "nothing to
distill".

Measured on the live path, same idea and box minutes apart: one run distilled
0 entries in 0.59s, the next 4 in 1.28s. The stage is not inert — the model
intermittently returns `{"entries": []}`, which §17.583's redraw treats as a
successful draw because the tool args are well-formed.
"""
import inspect

import pytest


class _Resp:
    def __init__(self, args, success=True):
        self.success = success
        self.error = None
        self.text = ""
        self.finish_reason = "stop"
        self.tool_calls = [type("TC", (), {"arguments": args})()] if args is not None else []


# ---------------------------------------------------------------------------
# The mechanism: model_router.tool_call(require_nonempty=...)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_empty_payload_is_redrawn_when_required():
    """The exact gap: args are well-formed, the list is empty. §17.583's
    predicate (`read_tool_args(...) is not None`) is True here, so before
    §17.986 this returned on draw 1."""
    from unittest.mock import AsyncMock, patch

    from app import model_router

    draws = [_Resp({"entries": []}), _Resp({"entries": [{"title": "t"}]})]
    with patch.object(model_router, "_tool_call_once",
                      AsyncMock(side_effect=draws)) as once:
        out = await model_router.tool_call(
            messages=[{"role": "user", "content": "x"}],
            tools=[object()], require_nonempty="entries", draws=3)

    assert once.await_count == 2, "an empty payload must be redrawn"
    assert out.tool_calls[0].arguments["entries"] == [{"title": "t"}]


@pytest.mark.asyncio
async def test_a_list_of_non_objects_is_also_an_empty_draw():
    """The documented phase2_distill_shape_drift case: the model emits an array
    of strings, which the caller's dict-filter reduces to nothing."""
    from unittest.mock import AsyncMock, patch

    from app import model_router

    draws = [_Resp({"entries": ["a", "b"]}), _Resp({"entries": [{"title": "t"}]})]
    with patch.object(model_router, "_tool_call_once",
                      AsyncMock(side_effect=draws)) as once:
        await model_router.tool_call(
            messages=[{"role": "user", "content": "x"}],
            tools=[object()], require_nonempty="entries", draws=3)

    assert once.await_count == 2


@pytest.mark.asyncio
async def test_default_behaviour_is_unchanged_without_the_opt_in():
    """Off by default — an empty list is a legitimate answer for callers like
    "no options" / "nothing stale", which must not pay for extra draws."""
    from unittest.mock import AsyncMock, patch

    from app import model_router

    draws = [_Resp({"entries": []}), _Resp({"entries": [{"title": "t"}]})]
    with patch.object(model_router, "_tool_call_once",
                      AsyncMock(side_effect=draws)) as once:
        await model_router.tool_call(
            messages=[{"role": "user", "content": "x"}],
            tools=[object()], draws=3)

    assert once.await_count == 1, "must not redraw when the caller did not ask"


@pytest.mark.asyncio
async def test_a_nonempty_payload_still_returns_on_the_first_draw():
    from unittest.mock import AsyncMock, patch

    from app import model_router

    with patch.object(model_router, "_tool_call_once",
                      AsyncMock(return_value=_Resp({"entries": [{"t": 1}]}))) as once:
        await model_router.tool_call(
            messages=[{"role": "user", "content": "x"}],
            tools=[object()], require_nonempty="entries", draws=3)

    assert once.await_count == 1


@pytest.mark.asyncio
async def test_a_hard_failure_is_not_redrawn():
    """Unchanged §17.583 contract: hard failures return as-is."""
    from unittest.mock import AsyncMock, patch

    from app import model_router

    bad = _Resp(None, success=False)
    with patch.object(model_router, "_tool_call_once",
                      AsyncMock(return_value=bad)) as once:
        out = await model_router.tool_call(
            messages=[{"role": "user", "content": "x"}],
            tools=[object()], require_nonempty="entries", draws=3)

    assert once.await_count == 1
    assert out.success is False


# ---------------------------------------------------------------------------
# The caller + the silence that hid it
# ---------------------------------------------------------------------------

def test_distill_entries_opts_into_the_redraw():
    """Distilling N search results into zero entries is a failed draw."""
    from app.modules import gt_extractor

    src = inspect.getsource(gt_extractor.distill_entries)
    assert 'require_nonempty="entries"' in src


def test_no_silent_exit_remains_in_distill_entries():
    """`entry_count=0` was the ONLY trace: both remaining `return []` exits
    logged nothing, so an inert stage read exactly like an empty topic."""
    from app.modules import gt_extractor

    src = inspect.getsource(gt_extractor.distill_entries)
    body = src[src.index("resp = await model_router.tool_call"):]
    for chunk in body.split("return []")[:-1]:
        assert "logger.warning" in chunk, (
            "every failure exit after the model call must say why")


@pytest.mark.asyncio
async def test_distill_logs_when_the_model_returns_no_usable_objects():
    from unittest.mock import AsyncMock, patch

    from app.modules import gt_extractor

    with patch.object(gt_extractor.model_router, "tool_call",
                      AsyncMock(return_value=_Resp({"entries": ["a string"]}))), \
            patch.object(gt_extractor, "logger") as log:
        out = await gt_extractor.distill_entries(
            [{"title": "t", "url": "u", "content": "c"}], topic="x")

    assert out == []
    assert any("empty_after_filter" in str(c) for c in log.warning.call_args_list)
