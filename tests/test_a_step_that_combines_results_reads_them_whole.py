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


def test_a_research_step_reads_a_measurement_of_this_size():
    """§17.1687 — T9's dataset (~6.9k) and M1's five shows (~9k) fit the default upstream budget"""
    from app.config import Settings
    assert Settings.model_fields["max_upstream_chars"].default >= 6900 + 9000


def test_a_cut_keeps_what_it_is_given_and_the_middle_too():
    """§17.1688 — truncate_output kept 20% + 20% of its budget; a research step's middle (its verdicts) was lost"""
    from app.modules.prompt_assembly import truncate_output
    text = "HEAD " * 400 + "VERDICT: category sync NOT MET " + "TAIL " * 400
    out = truncate_output(text, 3000)
    assert 2900 <= len(out) <= 3000, len(out)
    assert truncate_output("short", 3000) == "short"


def test_a_synthesis_step_reads_its_own_inputs_first():
    """§17.1688 — T20's research inputs shared the budget with every raw measurement"""
    from app.modules.execution_agent import _format_upstream_block
    ups = {f"R{i}": ("research finding " * 200) for i in range(1, 10)}          # nine ~3.4k research outputs (live: 2.4–5k)
    ups.update({f"M{i}": ("raw measurement " * 1500) for i in range(1, 12)})   # eleven ~24k measurements
    block = _format_upstream_block(ups, "T20", budget=40000, primary=[f"R{i}" for i in range(1, 10)])
    def section(k):
        i = block.index(f"### {k}\n"); j = block.find("\n### ", i + 1)
        return block[i:j if j > 0 else len(block)]
    assert all("truncated" not in section(f"R{i}") for i in range(1, 10))      # the step's own inputs whole
