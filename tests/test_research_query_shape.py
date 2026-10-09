"""§17.989 — the shape of a generated research query decides whether Phase 2
grounds the plan or distils nothing.

These are GUARDS, not proofs. An LLM's output can't be pinned deterministically,
so the behavioural evidence lives in the sprint log; what these protect is the
guidance itself, which is invisible at runtime and trivially lost in a refactor.
The one real assertion here is on `relevant_search_results`, which IS
deterministic — and which demonstrably cannot save the leading-term case.
"""
import inspect


def test_the_query_field_asks_for_keywords_not_prose():
    """The description used to say only "Search queries to ground the plan",
    so the model wrote prose: 'best local markdown to pdf libraries for python
    cpu' returned Best Buy's storefront and two dictionary entries for "best"."""
    from app.modules import ideation_workflow as iw

    desc = iw.FEASIBILITY_TOOL.input_schema["properties"][
        "recommended_research_queries"]["description"].lower()
    assert "keyword" in desc
    for banned_shape in ("not a question", "superlative"):
        assert banned_shape in desc, banned_shape


def test_the_query_field_pins_the_leading_term_rule():
    """Measured: same words, different first word —
        'python markdown pdf library'  kept=10 junk=6  (python.org landing pages)
        'markdown pdf library python'  kept=10 junk=0
    The gate cannot fix this: the junk contains a real query token."""
    from app.modules import ideation_workflow as iw

    desc = iw.FEASIBILITY_TOOL.input_schema["properties"][
        "recommended_research_queries"]["description"].lower()
    assert "distinctive" in desc
    assert "last, never first" in desc or "last" in desc
    # The concrete worked example must survive, not just the abstract rule.
    assert "markdown pdf library python" in desc


def test_the_system_prompt_carries_the_rule_too():
    """The field description is what the model reads while FILLING the tool;
    the system prompt is what frames the pass. §17.989 put it in both."""
    from app.modules import ideation_workflow as iw

    s = iw.FEASIBILITY_SYSTEM.lower()
    assert "keyword" in s
    assert "distinctive" in s


def test_the_relevance_gate_now_drops_landing_pages():
    """§17.1436 — this test used to pin that the gate COULD NOT drop leading-term landing pages (python.org
    for 'python markdown pdf library'), and said: "if the gate ever DOES drop these, the generation-side
    rule can be revisited". It does now -- a bare homepage never answers a how-to, and a long query needs
    two of its words, not one. Live, the one-word rule let Spectrum's homepage and billing page outrank
    the support page with the router's port-forwarding steps, and the walkthrough read the wrong pages.
    The generation-side query-shape rule (§17.988) stays: it still keeps junk from being returned at all."""
    from app.modules.research_extractors import relevant_search_results

    landing = [
        {"title": "Welcome to Python.org", "content": "The official home of the Python Programming Language",
         "url": "https://www.python.org/"},
        {"title": "Spectrum Account Sign-In & Bill Pay", "content": "Sign in to your Spectrum account",
         "url": "https://www.spectrum.net/?msockid=abc"},
    ]
    answer = {"title": "Advanced WiFi: Advanced Settings | Spectrum Support",
              "content": "select Router. Scroll down and select Advanced Settings. Select Port Forwarding & IP Reservations",
              "url": "https://www.spectrum.net/support/internet/advanced-wifi-advanced-settings"}
    # a homepage alone is kept (it can be the answer) but ranks behind any page with content
    py_doc = {"title": "Markdown to PDF in Python — library comparison", "content": "python markdown pdf library",
              "url": "https://example.org/guides/md-pdf"}
    assert relevant_search_results("python markdown pdf library", landing[:1] + [py_doc])[0] is py_doc
    # a long query needs two of its words: the sign-in page (only "spectrum") is out, the support page is first
    got = relevant_search_results("Spectrum SAX1V1K port forwarding My Spectrum app steps", landing + [answer])
    assert got == [answer]


# ── §17.994 — drift, not just deletion ──────────────────────────────────

# sha256[:16] of the query-shape guidance as measured. These are REVIEW PINS,
# not correctness checks: the §17.989 guards above catch a rule being deleted,
# but someone can reword this text, keep every asserted keyword, and regress
# search quality with CI green — the guidance is prompt copy, so its behaviour
# lives in measurement, not in an assertion.
#
# If this fails you changed the text. That is allowed. The ask is to re-measure
# before updating the hash, because the numbers behind it were expensive:
#
#   "best local markdown to pdf libraries for python cpu"  10 raw ->  0 usable
#   "markdown pdf libraries python"                        10 raw -> 10 usable
#   "python markdown pdf library"   kept=10 junk=6   (python.org landing pages)
#   "markdown pdf library python"   kept=10 junk=0   (same words, new first word)
#
# Re-measure with: for each query, GET searxng /search with
# engines=_engines_for_category("general"), then relevant_search_results(q, raw)
# and count how many survive. Then paste the new hash here with the new numbers.
_QUERY_GUIDANCE_SHA = "f719c4c52019c00b"
_FEASIBILITY_SYSTEM_SHA = "01d36df61e544faa"


def _sha16(s: str) -> str:
    import hashlib

    return hashlib.sha256(s.encode()).hexdigest()[:16]


def test_the_query_guidance_has_not_drifted_unmeasured():
    from app.modules import ideation_workflow as iw

    desc = iw.FEASIBILITY_TOOL.input_schema["properties"][
        "recommended_research_queries"]["description"]
    assert _sha16(desc) == _QUERY_GUIDANCE_SHA, (
        "the research-query guidance changed. That is allowed — but re-measure "
        "junk-per-query before updating _QUERY_GUIDANCE_SHA (see the note above "
        "this test). A keyword-only guard would have let this through green.")


def test_the_feasibility_system_prompt_has_not_drifted_unmeasured():
    from app.modules import ideation_workflow as iw

    assert _sha16(iw.FEASIBILITY_SYSTEM) == _FEASIBILITY_SYSTEM_SHA, (
        "FEASIBILITY_SYSTEM changed; re-measure before updating the hash.")
