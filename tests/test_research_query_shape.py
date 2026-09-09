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


def test_the_relevance_gate_cannot_catch_leading_term_junk():
    """Why this had to be fixed at GENERATION and not with another filter.

    §17.988's gate drops results sharing no distinctive token with the query.
    A python.org landing page returned for 'python markdown pdf library' shares
    'python' — so it is legitimately kept, and no amount of gating removes it.
    """
    from app.modules.research_extractors import relevant_search_results

    junk = [
        {"title": "Welcome to Python.org",
         "content": "The official home of the Python Programming Language"},
        {"title": "Download Python | Python.org",
         "content": "Download the latest version of Python"},
    ]
    kept = relevant_search_results("python markdown pdf library", junk)
    assert len(kept) == 2, (
        "if the gate ever DOES drop these, the generation-side rule can be "
        "revisited — until then it is the only thing standing between the "
        "distiller and a corpus of landing pages")

    # Move the broad term off the front and the same pages stop being returned
    # at all — which is the fix, and is not something a filter can do.
    assert len(relevant_search_results("markdown pdf library python", junk)) == 2
