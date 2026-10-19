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
