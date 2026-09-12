"""§17.1021–1023 — the web query must carry the hardware AND the goal.

Traced against the operator's live session after they reported, correctly, that
nothing had changed:

  hint      : "SAX1V1K ES2251 Secure Home Lab & Media/Game/AI Server"   <- §17.1019 worked
  web query : "Spectrum app primary server secondary server"           <- generator dropped it

The model number was handed to `_focus_web_query` as a PROJECT hint and dropped.
Adding the GOAL to that hint was dropped too, twice more. A hint is a request;
these append deterministically.

Then the query still retrieved the wrong thing, because it searched the
operator's SYMPTOM ("I only see two fields") rather than their GOAL. Measured
against the live search backend:

  "SAX1V1K ES2251 Spectrum app only primary secondary server configuration"
      -> Spectrum DNS / general pages
  "SAX1V1K Spectrum router port forwarding"
      -> the r/Spectrum guide, the Askey SAX1V1K manual, Spectrum Support

The goal lives in the step recap's OPEN line, which the engine already writes.
"""
import pytest

from app.modules.assist_agent import _recap_goal_terms
from app.modules.assist_research_lib import _cap_query, _goal_keywords

REAL_RECAP = """GOAL: Validate the entire build end-to-end (firewall, Tailscale, containers/VMs).

DONE:
- Firewall confirmed enabled/running
- Caddy in LXC 120 listening on ports 80 and 443

OPEN:
- Public HTTPS unreachable: router not forwarding ports 80/443 to Caddy (192.168.1.26)
- Operator cannot find port forwarding in the Spectrum app — only Primary/Secondary DNS Server fields are visible
"""


# ── the recap's OPEN line is the goal ────────────────────────────────────
def test_open_outranks_goal():
    """OPEN is what is BLOCKING now; GOAL describes the whole step and would
    eat the budget on work already done."""
    terms = _recap_goal_terms(REAL_RECAP)
    assert terms.startswith("Public HTTPS unreachable"), terms[:70]
    assert "port forwarding" in terms.lower()


def test_done_lines_never_become_query_terms():
    """They describe solved problems and would steer retrieval at them."""
    terms = _recap_goal_terms(REAL_RECAP).lower()
    assert "firewall confirmed" not in terms
    assert "tailscale" not in terms


def test_no_recap_is_silent():
    assert _recap_goal_terms(None) == ""
    assert _recap_goal_terms("   ") == ""
    assert _recap_goal_terms("DONE:\n- everything") == ""


# ── goal → a few query keywords, not a pasted sentence ───────────────────
def test_a_multiword_phrase_leads_and_the_blocker_is_present():
    """§17.1025 — asserting `kw[0] == "port forwarding"` was the answer key in
    test form: it pinned ONE operator's phrase to first place, and it passed
    only because the implementation carried that phrase in a literal list.
    The general property is that a multi-word phrase leads (phrases retrieve
    better than bare nouns) and the blocker's own wording is represented."""
    kw = _goal_keywords(_recap_goal_terms(REAL_RECAP))
    assert " " in kw[0], f"a bare noun leads instead of a phrase: {kw}"
    assert any("forwarding" in k for k in kw), kw
    assert len(kw) <= 4, f"a query is not a sentence: {kw}"


def test_stopwords_and_filler_are_dropped():
    kw = _goal_keywords("Operator cannot find the port forwarding in it")
    assert "operator" not in kw and "cannot" not in kw and "the" not in kw


def test_words_inside_the_phrase_are_not_repeated():
    kw = _goal_keywords("cannot find port forwarding on the router")
    assert "port forwarding" in kw
    assert "port" not in kw and "forwarding" not in kw, kw


# ── the query stays short ────────────────────────────────────────────────
def test_query_is_capped():
    """Live, an unbounded query grew to 'SAX1V1K ES2251 Spectrum app primary
    secondary server only options PM2 Debian 12 LXC' and matched nothing —
    keyword engines degrade with length."""
    long_q = " ".join(f"w{i}" for i in range(40))
    assert len(_cap_query(long_q).split()) == 12
    assert _cap_query("short query").split() == ["short", "query"]


def test_the_cap_keeps_the_front_where_hardware_and_goal_go():
    q = _cap_query("SAX1V1K port forwarding " + " ".join(f"x{i}" for i in range(30)))
    assert q.startswith("SAX1V1K port forwarding")


# ── wiring: a hint is a request, these are enforced ──────────────────────
def test_the_web_query_enforces_both():
    """§17.1027 — the enforcement moved into `assist_evidence` so the fix path
    shares it. research_one must still route the generator's output THROUGH
    it, and the shared builder must still carry all three rules."""
    import inspect
    from app.modules import assist_research_lib as lib
    from app.modules import assist_evidence as ev
    src = inspect.getsource(lib.research_one)
    assert "derive_need(" in src and "finalize_query(" in src, "the need is not enforced"
    # Enforcement must run AFTER the generator, or it is just another hint.
    assert src.index("_focus_web_query(") < src.index("finalize_query(")
    need_src = inspect.getsource(ev.derive_need)
    assert "hardware_for_text(" in need_src, "hardware is only a hint again"
    fin_src = inspect.getsource(ev.finalize_query)
    assert "need.hardware" in fin_src and "need.goal_terms" in fin_src
    assert "_cap(" in fin_src, "the query is unbounded again"


def test_the_ask_path_supplies_the_goal():
    import inspect
    from app.modules import assist_agent
    src = inspect.getsource(assist_agent.run_step_research)
    assert "goal_terms=_recap_goal_terms(_recap)" in src
    assert "progress_recap" in src, "the recap is never read"
