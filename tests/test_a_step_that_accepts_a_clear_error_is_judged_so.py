r"""§17.1593 — a done-condition that says "real data or a clear JSON error" is met by a clear JSON error.

Live (c2792d4b T7): Byparr's own /health answers in 8.3 s; the panel's route answered
`{"error":"Byparr service is not available","detail":"Request timeout"}` — exactly the clear JSON error the step
asked for — and the verify judge, reading a claim built from the step's EMPTY description (its text lives in
prompt_template), failed the step as work that did not land.
"""
from __future__ import annotations

import asyncio
import inspect

from app.modules import assist_state_check as asc
from app.modules import decision_pause as dp

CLAIM = ("Fix remaining endpoint backends: For any endpoint not covered by Radarr/Sonarr/Scaffold, fix its handler on container 111 "
         "to return real data or a clear JSON error. Preserve existing features.")
CHK = "pct exec 111 -- sh -c 'cpw=$(cat); curl -u \"panel:$cpw\" -s --fail-with-body --max-time 20 http://127.0.0.1:3001/api/byparr/health' <<< \"$PANEL_PASSWORD\""
ERR = '{"error":"Byparr service is not available","detail":"Request timeout"}'


def test_a_clear_json_error_meets_a_step_that_asked_for_one(monkeypatch):
    from app import model_router
    called = []

    async def _no_judge(*a, **k):
        called.append(1); raise RuntimeError("the judge must not be asked")
    monkeypatch.setattr(model_router, "tool_call", _no_judge)
    probes = [{"id": "V:1", "kind": "state", "claim": CLAIM, "command": CHK, "expect": ""}]
    out = asyncio.run(asc.judge_outputs(probes, "== V:1 ==\n" + ERR + "\n"))
    assert out[0]["verdict"] == "confirmed" and "clear JSON error" in out[0]["reason"] and not called
    assert asc._accepts_a_clear_error("return JSON data or a clear JSON error") and asc._accepts_a_clear_error("real data or an error message")
    assert not asc._accepts_a_clear_error("Fix the Byparr handler so it returns the service status")
    assert asc._json_error_body('{"status": "ok"}') == "" and asc._json_error_body("not json {") == "" and asc._json_error_body(ERR) == ERR


def test_a_step_that_did_not_ask_for_an_error_is_not_handed_one(monkeypatch):
    from app import model_router

    async def _judge(*a, **k):
        return {"verdicts": [{"id": "V:1", "verdict": "contradicted", "reason": "an error is not the status"}]}
    monkeypatch.setattr(model_router, "tool_call", _judge)
    probes = [{"id": "V:1", "kind": "state", "claim": "Fix the Byparr handler so it returns the service status", "command": CHK, "expect": ""}]
    out = asyncio.run(asc.judge_outputs(probes, "== V:1 ==\n" + ERR + "\n"))
    assert out[0]["verdict"] != "confirmed" or "clear JSON error" not in str(out[0].get("reason"))


def test_the_frame_carries_the_steps_text_where_the_description_is_empty():
    src = inspect.getsource(dp.park_awaiting_decision)
    assert '"description": node.get("description") or node.get("prompt_template") or ""' in src


def test_a_redirect_misread_as_down_is_not_a_clear_error_the_step_accepts(monkeypatch):
    """§17.1597 — live: `{"error":"Byparr service is not available","detail":"HTTP 301: "}` — Byparr answered; the handler misread it."""
    from app import model_router
    called = []

    async def _no_judge(*a, **k):
        called.append(1); raise RuntimeError("the judge must not be asked")
    monkeypatch.setattr(model_router, "tool_call", _no_judge)
    body = '{"error":"Byparr service is not available","detail":"HTTP 301: "}'
    why = asc.negative_evidence(CHK, body, CLAIM)
    assert why and "3xx" in why and "redirect" in why
    probes = [{"id": "V:1", "kind": "state", "claim": CLAIM, "command": CHK, "expect": ""}]
    out = asyncio.run(asc.judge_outputs(probes, "== V:1 ==\n" + body + "\n"))
    assert out[0]["verdict"] == "contradicted" and "redirect" in out[0]["reason"] and not called      # §17.1593 does not accept it
    assert asc.negative_evidence(CHK, ERR, CLAIM) == ""                                               # a timeout is a real outage: §17.1593's case
