"""§17.1684 — a step that aggregates/combines/synthesizes/merges earlier results gets the report budget (live, job
c2518102 T9: the measurements it was handed were cut to nothing and it wrote "payload truncated")."""
from app.modules.execution_agent import formats_what_came_before


def _n(title, prompt, tool="LLM"):
    return {"title": title, "prompt_template": prompt, "tool": tool, "depends_on": ["T2", "T3"]}


def test_the_live_combining_steps_are_report_steps():
    assert formats_what_came_before(_n("Aggregate missing media dataset",
                                       "Combine all fetched data into one structured dataset. No changes."))
    assert formats_what_came_before(_n("Synthesize root cause findings",
                                       "Merge all research into a single root cause analysis. No changes."))


def test_others_are_not():
    assert not formats_what_came_before(_n("Research season pack gaps", "Investigate why season pack searches return empty."))
    assert not formats_what_came_before(_n("Aggregate the logs", "x", tool="Shell"))        # a Shell step runs, not formats
    assert not formats_what_came_before({"title": "Combine", "prompt_template": "x", "tool": "LLM", "depends_on": []})


def test_a_short_result_is_never_cut_because_another_is_long():
    """§17.1684b — live, T4's 150-char answer was cut to "Total mis" while T8's 40k JSON kept most of the budget"""
    from app.modules.execution_agent import fair_shares
    sizes = {"T2": 5200, "T3": 60, "T4": 150, "T5": 900, "T7": 2700, "T8": 40000}
    sh = fair_shares(sizes, 40000, {"T8": 1.0, "T2": 0.5, "T3": 0.5, "T4": 0.5, "T5": 0.5, "T7": 0.5})
    for k in ("T2", "T3", "T4", "T5", "T7"):
        assert sh[k] == sizes[k], (k, sh)                         # every short result whole
    assert sh["T8"] == 40000 - sum(sizes[k] for k in ("T2", "T3", "T4", "T5", "T7"))   # the long one takes the rest
    assert sum(sh.values()) <= 40000
    even = fair_shares({"a": 30000, "b": 30000}, 40000)
    assert even == {"a": 20000, "b": 20000}
