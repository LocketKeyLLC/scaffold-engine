"""§17.626 — assist_agent natural-language wrappers.

classify_session_turn (grounds a message on the current step, gated by the
master toggle) and list_assist_candidates (assistable jobs for natural start).
AsyncMock DB sessions; the classifier LLM call is patched.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules import assist_agent


def _result(mappings_first=None, mappings_all=None):
    r = MagicMock()
    mappings = MagicMock()
    mappings.first.return_value = mappings_first
    mappings.all.return_value = mappings_all or []
    r.mappings.return_value = mappings
    return r


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_classify_turn_empty_message_is_question():
    db = AsyncMock()
    out = await assist_agent.classify_session_turn(
        session_id="s1", message="   ", db=db,
    )
    assert out["intent"] == "question"
    db.execute.assert_not_called()


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_classify_turn_disabled_toggle_is_question():
    db = AsyncMock()
    # `settings` is a function-local import; patch the singleton's attribute.
    with patch("app.config.settings.assist_nl_turns_enabled", False):
        out = await assist_agent.classify_session_turn(
            session_id="s1", message="I picked ZFS", db=db,
        )
    assert out["intent"] == "question"


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_classify_turn_no_current_step_is_question():
    db = AsyncMock()
    # session exists + active but has no current_node_key and none supplied.
    db.execute = AsyncMock(return_value=_result(mappings_first={
        "id": "s1", "job_id": "j1", "status": "active", "current_node_key": None,
    }))
    with patch("app.config.settings.assist_nl_turns_enabled", True):
        out = await assist_agent.classify_session_turn(
            session_id="s1", message="I picked ZFS", db=db,
        )
    assert out["intent"] == "question"


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_classify_turn_delegates_and_threads_context():
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_result(mappings_first={
        "id": "s1", "job_id": "j1", "status": "active", "current_node_key": "T1",
    }))
    ctx = MagicMock(title="Decide storage", base_prompt="ZFS vs LVM", tool="LLM")
    with patch("app.config.settings.assist_nl_turns_enabled", True), \
         patch.object(assist_agent, "_assemble_ctx_for_node",
                      new=AsyncMock(return_value=({"domain": None}, ctx))), \
         patch("app.modules.assist_guide.classify_turn",
               new=AsyncMock(return_value={"intent": "submit", "evidence": "ZFS",
                                           "error_text": ""})) as classify:
        out = await assist_agent.classify_session_turn(
            session_id="s1", message="going with ZFS", db=db,
        )
    assert out["intent"] == "submit"
    assert out["node_key"] == "T1" and out["title"] == "Decide storage"
    # grounded on the current step's title/prompt/tool.
    _, kwargs = classify.call_args
    assert kwargs["title"] == "Decide storage"
    assert kwargs["task_prompt"] == "ZFS vs LVM"


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_list_candidates_filters_umbrella_and_zero_node():
    rows = [
        {"id": "j1", "title": "Proxmox", "status": "assisted_running",
         "job_type": "legacy", "node_count": 9},
        {"id": "j2", "title": "Umbrella group", "status": "aggregating",
         "job_type": "umbrella", "node_count": 0},
        {"id": "j3", "title": "Empty", "status": "planning",
         "job_type": "legacy", "node_count": 0},
    ]
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_result(mappings_all=rows))
    out = await assist_agent.list_assist_candidates(db=db)
    ids = [c["job_id"] for c in out]
    assert ids == ["j1"]  # umbrella + 0-node dropped
    assert out[0]["node_count"] == 9
