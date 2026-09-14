"""§17.1072 — the instructor decide path: schema from the live vocabulary, fallback on failure."""
from unittest.mock import AsyncMock, patch

import pytest

from app.config import settings
from app.modules import assist_decide
from app.modules.assist_decide_instructor import _decision_model


def test_decision_schema_mirrors_the_live_action_vocabulary():
    Decision = _decision_model(assist_decide.DECIDE_ACTIONS)
    d = Decision(action="add_step", confidence="high")
    assert d.plan_impact == "none" and d.note_kind == "note" and d.suggestion is None
    with pytest.raises(Exception):
        Decision(action="not_an_action", confidence="high")
    with pytest.raises(Exception):
        Decision(action="fix", confidence="certain")


def test_instructor_backend_falls_back_to_router_on_failure():
    src = open("app/modules/assist_decide.py", encoding="utf-8").read()
    block = src[src.index("§17.1072 — instructor path"):src.index("    resp = None\n    for attempt in range(2):")]
    assert "decide_via_instructor" in block and "except Exception" in block and "router fallback" in block
    assert src.index("def _finalize(") < src.index("§17.1072 — instructor path")   # overrides apply to BOTH paths
    assert settings.assist_decide_backend == "router"                           # default stays off
