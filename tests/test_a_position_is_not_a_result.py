"""§17.1439 — two defects from ADD4's router walkthrough (2026-10-09).

1. "yes i am logged in and have gotten to the router page of the app." was routed to submit (the /decide
   model's own rationale: "a partial result on the current step, not yet the completed port-forward rules"),
   judged incomplete, and answered "If it IS done, reply confirm".
2. The guide's blocker query was "Router app: forward TCP ports 80 443 Caddy proxy (192.168.1. Inside LXC
   (jellyfin), apt-get update now succeeds … without error" — a success fact about another machine, matched
   on the word "error".
"""
import pytest

from app.modules import assist_policy as P
from app.modules.assist_decide import _DECIDE_EXTRA
from app.modules.assist_guide import blocker_research_query as bq

ADD4_MSG = "yes i am logged in and have gotten to the router page of the app."
ADD4_TITLE = "Router app: forward TCP ports 80 and 443 to the Caddy proxy (192.168.1.26) -- a guided walkthrough"
ADD4_TASK = ("In the My Spectrum app on the operator's phone, create two port-forwarding rules: TCP 80 -> "
             "192.168.1.26:80 and TCP 443 -> 192.168.1.26:443 so Caddy (container 120) can get its certificate.")
JELLYFIN = ("Inside LXC container 101 (jellyfin), apt-get update now succeeds and fetches from repo.jellyfin.org "
            "(bookworm main) without error.")


# ── 1. a position report is the cue for the next instruction, not a submit ──────────────────────────────
@pytest.mark.parametrize("msg", [
    ADD4_MSG,
    # the other two hits among 214 real operator messages
    "i am in the spectrum app under the router, which is where the DNS server forwarding is but it only "
    "lists Primary DNS Server and Secondary DNS server.",
    "I am logged in to the Console for VM 110",
    "ok I'm on the Advanced Settings screen",
    "got to the port forwarding page",
])
def test_a_position_report(msg):
    assert P.looks_like_position_report(msg) is True


@pytest.mark.parametrize("msg", [
    "I'm on the router page and both rules are saved",   # a result → submit
    "done, both rules created",
    "how do I get to the router page",
    "where is the router page?",
    "i'm on ubuntu 24.04",                               # a machine fact → set_env
    "it's done",
])
def test_not_a_position_report(msg):
    assert P.looks_like_position_report(msg) is False


def test_the_live_submit_is_overridden_to_fix():
    decision = {"action": "submit", "confidence": "medium", "signals": {},
                "evidence": "Operator is logged into the My Spectrum app and has reached the router page",
                "rationale": "a partial result on the current step, not yet the completed port-forward rules."}
    out = P.apply_deterministic_overrides(decision, ADD4_MSG)
    assert out["action"] == "fix" and out["override"] == "position_report"
    assert out["error_text"] == ADD4_MSG and out["confidence"] == "high"


def test_a_real_result_still_submits_and_a_paste_still_wins():
    keep = P.apply_deterministic_overrides({"action": "submit", "signals": {}},
                                           "I'm on the router page and both rules are saved")
    assert keep["action"] == "submit"
    paste = P.apply_deterministic_overrides(
        {"action": "submit", "signals": {"shell_paste": True, "shell_error": False}},
        "root@pve:~# curl -sI https://example.org\nHTTP/2 200")
    assert paste["action"] == "submit", "the shell-result gate is first"


def test_the_decide_prompt_names_the_route():
    assert "WHERE THEY ARE" in _DECIDE_EXTRA and "NEVER submit it" in _DECIDE_EXTRA


# ── 2. the blocker query ───────────────────────────────────────────────────────────────────────────────
def test_a_success_fact_is_not_a_blocker():
    assert bq({"facts": [JELLYFIN]}, [], ADD4_TITLE) == ""
    assert bq({"facts": [JELLYFIN]}, [], ADD4_TITLE, task_text=ADD4_TASK) == ""
    # a real failure in the same sentence still counts
    assert "fails" in bq({"facts": ["Caddy now starts but the certificate request fails with a timeout"]},
                         [], "Configure reverse proxy", task_text="Install Caddy as the reverse proxy.")


def test_a_fact_about_another_machine_does_not_ride_along_when_the_step_text_is_known():
    other = "Inside LXC container 101 (jellyfin), apt-get update fails: repository not signed."
    assert bq({"facts": [other]}, [], ADD4_TITLE, task_text=ADD4_TASK) == ""
    on_step = "Let's Encrypt for Caddy fails: Timeout during connect (likely firewall problem)"
    out = bq({"facts": [other, on_step]}, [], ADD4_TITLE, task_text=ADD4_TASK)
    assert "Timeout during connect" in out and "jellyfin" not in out


def test_the_subject_is_cut_on_a_word_and_drops_a_broken_address():
    out = bq({"facts": ["Let's Encrypt for Caddy fails: Timeout during connect"]}, [], ADD4_TITLE,
             task_text=ADD4_TASK)
    assert "(192.168.1." not in out and "192.168" not in out.split("Let's")[0]
    assert out.startswith("Router app: forward TCP ports 80 443 Caddy proxy")
