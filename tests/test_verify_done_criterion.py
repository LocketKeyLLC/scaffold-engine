"""§17.1100 — the completion verifier judges against the walkthrough's own
'Done when' bar, so a paste that meets the stated finish line auto-commits."""
from __future__ import annotations

import pathlib
import re

from app.modules.assist_guide import extract_done_criterion

ROOT = pathlib.Path(__file__).resolve().parents[1]

_WALK = (
    "## 👉 Do this next\n📍 On: the Proxmox host shell\n```bash\nqm set 106 --scsi0 local-lvm:40\n```\n\n"
    "---\n## ✅ Done when\n"
    "This step is finished when `qm config 106 | grep scsi0` shows `size=40G`.\n\n"
    "Press ✓ Done → next step.\n\n"
    "## If that fails\n- paste the error.\n"
)


def test_extracts_the_done_when_block_verbatim():
    dc = extract_done_criterion(_WALK)
    assert "Done when" in dc
    assert "size=40G" in dc
    assert "Do this next" not in dc      # doesn't reach back above the header
    assert "If that fails" not in dc     # stops at the next section

def test_no_done_block_returns_empty():
    assert extract_done_criterion("## 👉 Do this next\n```\nqm start 106\n```\n") == ""
    assert extract_done_criterion("") == ""

def test_verifier_prompt_uses_the_criterion_and_wiring_is_present():
    guide = (ROOT / "app" / "modules" / "assist_guide.py").read_text(encoding="utf-8")
    # the criterion is threaded into the verify prompt
    assert "done_criteria" in guide and "DONE WHEN" in guide
    # verify_submit_outcome fetches the cached guidance and passes it
    agent = (ROOT / "app" / "modules" / "assist_agent.py").read_text(encoding="utf-8")
    assert "extract_done_criterion(" in agent and "done_criteria=done_criteria" in agent
