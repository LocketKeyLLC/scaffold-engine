"""§17.1474 — a check of the whole build is hands-on: only the machines can answer it.

Live (2026-10-11): T37 "Validate entire build" (tool LLM, node_type checkpoint; fixture) ran as a model task once
§17.1473 stopped a single-guest template from fitting it: it answered from the upstream outputs, ran nothing, and
was failed "this step produced INSTRUCTIONS, not work".
"""
import json
import pathlib

from app.modules import step_classify as sc

T37 = json.loads(pathlib.Path("tests/fixtures/t37_node_2026_10_11.json").read_text())


def test_the_live_step_is_hands_on():
    on, why = sc.step_is_hands_on(T37, shell_backend=True, mcp_enabled=False)
    assert on and why == "observe:whole-build"


def test_a_writeup_is_not():
    on, _ = sc.step_is_hands_on({"tool": "LLM", "title": "Document architecture and setup",
                                 "description": "Write the architecture and setup document."},
                                shell_backend=True, mcp_enabled=False)
    assert on is False
