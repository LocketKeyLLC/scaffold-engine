"""§17.1014 — "I'm unsure" is a cue to CHECK, never a cue to advance.

Live evidence, session 613dd1df, 2026-09-11 02:03–02:11. The operator wrote:

    ADD3: "i believe it is done but am unsure."
    T35 : "all i could do was add a security group i am unsure where dmz came
           from but i added a security group and the firewall is activated."

The first scored as a completion CLAIM, which under §17.890 exempts the submit
from the §17.731 incomplete-block — so ADD3 ("Rewrite the Caddyfile in pieces")
committed with the evidence field reading, verbatim, "i believe it is done but
am unsure." No work had been done. Every step after it planned against a
machine state that did not exist, which is what the operator reported as
"incomplete and out of order instructions".

Both messages were then answered with *"If it IS done, reply `confirm`… You
know your machine; I only see what you paste"* — handing the question back to
the one person who had just said they could not answer it. That is what they
reported as "an inability to assist in checking".
"""
import asyncio

import pytest

from app.modules import assist_guide, assist_policy

REAL_ADD3 = "i believe it is done but am unsure."
REAL_T35 = ("all i could do was add a security group i am unsure where dmz came "
            "from but i added a security group and the firewall is activated.")


# ── the detector ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("msg", [
    REAL_ADD3,
    REAL_T35,
    "i think it worked but i am not sure",
    "maybe it is done",
    "hopefully that did it",
    "i have no idea if that worked",
    "i cant tell if it applied",
    "should be done i guess",
])
def test_stated_uncertainty_is_detected(msg):
    assert assist_policy.expresses_uncertainty(msg) is True


@pytest.mark.parametrize("msg", [
    "done",
    "it is done",
    "I did that already",
    "it is installed",
    "it appears to be done",
    "the firewall is enabled and dmz is listed",
])
def test_confident_reports_are_not_uncertainty(msg):
    assert assist_policy.expresses_uncertainty(msg) is False


# ── the consequence: no false commit ─────────────────────────────────────
def test_the_live_message_is_no_longer_a_completion_claim():
    """This is the one that committed a Caddyfile rewrite that never happened."""
    assert assist_policy.looks_like_completion_claim(REAL_ADD3) is False


def test_hedged_assertions_still_count_as_claims():
    """§17.971 widened the matcher to accept hedging, on the reasoning that
    hedging is how people assert what they cannot fully verify. §17.1014 must
    not undo that — it only separates 'softening an assertion' from 'declining
    to make one'."""
    assert assist_policy.looks_like_completion_claim("it appears to be done") is True
    assert assist_policy.looks_like_completion_claim("it seems to be installed") is True


@pytest.mark.parametrize("msg", ["done", "it is done", "I did that already",
                                 "it is installed", "already did that"])
def test_confident_claims_are_untouched(msg):
    assert assist_policy.looks_like_completion_claim(msg) is True


# ── the improvement: answer with a check ─────────────────────────────────
class _DB:
    def __init__(self, guidance):
        self._g = guidance

    async def execute(self, *_a, **_k):
        g = self._g

        class _R:
            def mappings(self):
                class _M:
                    def first(self_inner):
                        return {"guidance": g}
                return _M()
        return _R()


REAL_GUIDANCE = """📍 In: the Proxmox web UI

## 👉 Do this next
**Open this URL now:**

## Run this
1. Click **container 120 (caddy-proxy)**.

## Verify
- Container 120's Firewall tab shows **Firewall: Yes** and **dmz** listed.

## Rollback
- Tell me the exact error message.
"""


def test_the_check_comes_from_the_steps_own_verify_section():
    out = asyncio.run(assist_guide.how_to_check_block(
        session_id="s", node_key="T35", db=_DB(REAL_GUIDANCE)))
    assert "here is how to find out" in out.lower()
    assert "Firewall: Yes" in out
    # Only the Verify section — not the whole walkthrough back at them.
    assert "Open this URL now" not in out
    assert "Rollback" not in out
    assert "Tell me the exact error message" not in out


def test_a_heading_inside_a_fence_is_not_a_section_break():
    """The §17.1011 lesson, server-side: a `##` in a heredoc is content."""
    md = ("## Verify\n- run it\n```bash\ncat <<EOF\n## Rollback\nnot a heading\n"
          "EOF\n```\n- and check the output\n\n## Rollback\n- undo it\n")
    out = asyncio.run(assist_guide.how_to_check_block(
        session_id="s", node_key="T", db=_DB(md)))
    assert "not a heading" in out, "content inside the fence must survive"
    assert "undo it" not in out, "the real Rollback heading must still end Verify"


def test_no_verify_section_falls_through_silently():
    out = asyncio.run(assist_guide.how_to_check_block(
        session_id="s", node_key="T", db=_DB("## Run this\n1. do it\n")))
    assert out == ""


def test_no_stored_guidance_falls_through_silently():
    for g in ("", None):
        out = asyncio.run(assist_guide.how_to_check_block(
            session_id="s", node_key="T", db=_DB(g)))
        assert out == ""


def test_blocked_path_leads_with_the_check_when_unsure():
    """The offer must not open by asking an uncertain operator to decide."""
    import inspect
    from app.modules import assist_turn
    src = inspect.getsource(assist_turn)
    assert "expresses_uncertainty(" in src, (
        "the blocked path does not consult uncertainty, so it still answers "
        "'reply confirm' to an operator who said they cannot tell (§17.1014)"
    )
    assert "how_to_check_block(" in src
