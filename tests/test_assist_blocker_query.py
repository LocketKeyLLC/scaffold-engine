"""§17.1012 — the blocker research query: recent, on-topic, and a real symptom.

§17.918 gives each walkthrough ONE deterministic query about what is blocking
the step. On the live homelab job that slot was being spent on noise, and the
mechanism was three separate choices compounding:

  1. Candidates were drawn from the session's ENTIRE history (50 notes).
  2. Among them the LONGEST won, on the theory that length proxies specificity.
  3. The result was cut with a hard ``[:180]``, mid-token.

So the operator's most rambling sentence — a resolved VM-110 reboot hang —
won permanently and was searched on the reverse-proxy, VPN, validation and
documentation steps alike, ending "... plus it app".

The strings below are the operator's ACTUAL notes from that session. Inventing
tidy fixtures here would test a parser against prose written to suit it.
"""
import inspect

from app.modules import assist_guide
from app.modules.assist_guide import blocker_research_query as q

# Verbatim from the live session.
REAL_RAMBLE = ("now its hung up on rebooting, I viewed the full logs and it "
               "appeared to have something to do with the os being out of date. "
               "But its the one we know works, plus it appears to be the only "
               "one available for this")
REAL_REQUEST = ("can we switch to ssh so i can copy and paste? and there seems "
                "to be an error to long for me to sit here and type out")
REAL_DECISION = ("Operator has decided to switch from the Proxmox noVNC console "
                 "to SSH for VM 110 (ai-vm) to enable copy-paste functionality.")

STEP = "Configure reverse proxy"


def _notes(*texts):
    return [{"text": t} for t in texts]


# ── what must be rejected ────────────────────────────────────────────────
def test_a_question_about_tooling_is_not_a_symptom():
    """It passed the symptom filter on the word 'error' and won on length."""
    assert q(None, _notes(REAL_REQUEST), STEP) == ""


def test_an_off_topic_blocker_does_not_ride_along():
    # A real symptom, but about the Ubuntu VM — not about the reverse proxy.
    assert q(None, _notes(REAL_RAMBLE), STEP) == ""


def test_operator_decisions_are_not_blockers():
    assert q(None, _notes(REAL_DECISION), STEP) == ""


def test_no_blocker_recorded_yields_no_query():
    assert q(None, [], STEP) == ""
    assert q(None, _notes("Operator is creating LXC 120"), STEP) == ""


# ── what must still get through ──────────────────────────────────────────
def test_a_distilled_fact_is_trusted_without_title_overlap():
    """Facts are curated system observations: 'Caddy' need not appear in the
    title 'Configure reverse proxy' for the fact to be about this step."""
    env = {"facts": ["Caddy fails to start: permission denied binding port 443"]}
    out = q(env, [], STEP)
    assert "Caddy fails to start" in out
    assert out.startswith("reverse proxy")  # the step subject is prepended


def test_an_on_topic_note_survives():
    out = q(None, _notes("the caddy reverse proxy hangs when reloading config"), STEP)
    assert "hangs when reloading" in out


def test_the_most_specific_on_topic_blocker_wins():
    """Length still breaks ties AMONG relevant candidates (the §17.918 intent)."""
    out = q(None, _notes(
        "proxy fails",
        "the proxy fails to start because port 443 is already bound by nginx",
    ), STEP)
    assert "already bound by nginx" in out


# ── the three mechanisms, each pinned ────────────────────────────────────
def test_recency_window_bounds_the_search():
    """An old blocker must not be researched forever. It is on-topic and would
    win on length; being buried under newer notes is what disqualifies it."""
    old = "the proxy fails to start because port 443 is already bound by nginx"
    buried = _notes(old, *[f"routine note {i}" for i in range(12)])
    assert q(None, buried, STEP) == ""
    # Same note, still inside the window → back in play.
    assert "already bound" in q(None, _notes(old, "routine note"), STEP)


def test_query_is_cut_on_a_word_boundary():
    """The old hard ``[:180]`` ended queries mid-token ("... plus it app"), and
    a dangling fragment is a term the search engine matches on."""
    long_note = "the proxy " + ("fails under sustained load and " * 20)
    out = q(None, _notes(long_note), STEP)
    assert len(out) <= assist_guide._BLOCKER_QUERY_MAX
    assert out == out.strip()
    # It must be long enough that truncation actually happened...
    assert len(out) > assist_guide._BLOCKER_QUERY_MAX - 40, "no truncation exercised"
    # ...and every token, the last one included, must be whole.
    source_tokens = set((STEP + " " + long_note).split())
    assert set(out.split()) <= source_tokens, (
        f"truncation produced a token not present in the input: "
        f"{set(out.split()) - source_tokens}"
    )


def test_instance_identifiers_are_stripped():
    env = {"facts": ["VM 110 fails to boot after the kernel upgrade"]}
    out = q(env, [], "Validate entire build")
    assert "VM 110" not in out and "110" not in out


# ── both guide paths, or it is not fixed for the operator ────────────────
def test_both_guide_paths_run_blocker_research():
    """§17.1012 — it lived inline in the NON-stream generator only, so the SPA
    path (`generate_guidance_stream`) never ran blocker research at all. That is
    the same divergence as §17.854/§17.975/§17.976; assert the shared helper is
    reached from both rather than counting occurrences of a literal.
    """
    for fn in ("generate_guidance", "generate_guidance_stream"):
        src = inspect.getsource(getattr(assist_guide, fn))
        assert "_add_blocker_research(" in src, (
            f"{fn} does not run blocker research — a step the operator is stuck "
            f"on gets no stuck-ness researched on that path (§17.918/§17.1012)"
        )
    assert inspect.iscoroutinefunction(assist_guide._add_blocker_research)
