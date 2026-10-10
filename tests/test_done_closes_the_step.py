"""§17.1441 — ✓ Done → next step closes the step; a click path is one action.

Live (ADD4, turn 3140, fixture `add4_guide_3140.md`): the walkthrough shrank the step to "find the
port-forwarding screen" and ended "Then press ✓ Done → next step and I'll give the exact fields for the two
rules." Done closes ADD4 and the plan moves to ADD49 — nothing carries "the rest" anywhere. The engine's own
clip note taught that phrase ("press ✓ Done … and I'll walk you through the rest as the next step").
"""
import pathlib

import pytest
from unittest.mock import AsyncMock, patch

from app.modules import assist_coherence as coh
from app.modules.assist_directives import _SINGLE_ACTION_DIRECTIVE

LIVE = (pathlib.Path(__file__).parent / "fixtures" / "add4_guide_3140.md").read_text()

CLICK_PATH = """📍 On: your phone, in the My Spectrum app
## Steps
**Phase 1 — Find the screen**
### Phase 1
1. Open the My Spectrum app and tap **Services** → **Router** → **Advanced Settings**.
2. Tap **Port Forwarding & IP Reservations**.
### Phase 2
3. Tap **Add Port Assignment**: TCP, 80 → 192.168.1.26:80. Save.
4. Again: TCP, 443 → 192.168.1.26:443. Save.
## ✅ Done when
Both rules are listed.
"""

SHELL_PHASES = """### Phase 1
```bash
apt-get install -y caddy
```
### Phase 2
```bash
systemctl restart caddy
```
"""


def test_the_live_promise_is_found_and_defused():
    hits = coh.premature_done_issue(LIVE)
    assert len(hits) == 1 and "I'll give the exact fields" in hits[0]
    out, n = coh.defuse_premature_done(LIVE)
    assert n == 1 and "I'll give the exact fields" not in out
    assert coh.CONTINUE_SAME_STEP in out
    assert coh.premature_done_issue(out) == [], "the replacement must not trip the gate itself"


@pytest.mark.parametrize("ok", [
    "When both rules are listed, press **✓ Done → next step**.",
    "Press ✓ Done → next step once both rules are saved.",
    "Once it's working, press Done and move on.",
])
def test_pressing_done_at_the_goal_is_fine(ok):
    assert coh.premature_done_issue(ok) == []


def test_a_click_path_with_phases_is_one_action():
    assert coh.multi_action_issue(CLICK_PATH) is None


def test_shell_phases_are_still_split():
    assert coh.multi_action_issue(SHELL_PHASES) is not None


def test_the_engines_own_wording_no_longer_teaches_the_promise():
    for text in (coh._CLIP_NOTE, coh.coherence_directive({"multi_action": {"phases": 2, "contexts": [], "changes": []}})):
        assert coh.premature_done_issue(text) == [], text
    assert "CLOSES" in _SINGLE_ACTION_DIRECTIVE and "CLICK PATH" in _SINGLE_ACTION_DIRECTIVE


@pytest.mark.asyncio
async def test_enforce_coherence_defuses_without_a_model_call():
    from app.modules import assist_guide
    with patch.object(assist_guide, "chat_until_nonempty", new=AsyncMock(side_effect=AssertionError("no model call"))):
        out, meta, block = await assist_guide.enforce_coherence(text_out=LIVE, messages=[], role="r", label="t")
    assert meta["action"] == "defused_done" and "I'll give the exact fields" not in out and block == ""


def test_the_gate_is_registered():
    from app.modules.assist_gates import GATES
    g = {x.name: x for x in GATES}["no_premature_done"]
    assert g.module == "assist_coherence" and g.symbol == "premature_done_issue"


def test_a_saved_walkthrough_with_the_promise_is_not_re_served():
    from app.modules.assist_guide import _cached_sends_to_done_early
    assert _cached_sends_to_done_early({"guidance": LIVE}) is True
    assert _cached_sends_to_done_early({"guidance": CLICK_PATH}) is False
    src = pathlib.Path("app/modules/assist_guide.py").read_text()
    assert src.count("or _cached_sends_to_done_early(cached) or _cached_predates_rules(cached)") == 2, "both cache paths"
