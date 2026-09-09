"""§17.990 — `facts_extracted=0` conflated three different situations.

Chasing the residual empty draws established that most of them are the engine
correctly refusing to invent facts: `prometheus histogram buckets` came back as
the Greek titan, the 2012 Ridley Scott film, IMDb and Rotten Tomatoes, and not
one of 15 results mentioned `histogram` or `buckets`. Pointed at that corpus a
different model produced four accurate-but-off-topic entries ("Prometheus is a
monitoring toolkit") — worse than zero, because it makes a plan LOOK grounded.

So the fix is not to force entries out of the distiller; it is to say which of
the three happened. Unlike the §17.989 prompt guidance, this IS deterministic
and therefore genuinely testable.
"""
import pytest

from app.modules.ideation_workflow import (
    _MIN_ON_TOPIC_FOR_DISTILLER_BLAME, _diagnose_grounding)

TOPIC = "prometheus histogram buckets"


def _r(title, content=""):
    return {"title": title, "content": content}


def test_entries_present_is_grounded():
    out = _diagnose_grounding(TOPIC, [_r("anything")], [{"title": "t"}])
    assert out["status"] == "grounded"
    assert out["facts"] == 1


def test_no_results_blames_search_not_the_distiller():
    out = _diagnose_grounding(TOPIC, [], [])
    assert out["status"] == "ungrounded"
    assert out["reason"] == "search_returned_nothing"


def test_the_live_prometheus_corpus_is_classified_off_topic():
    """The real corpus, as observed: everything matched only the leading term."""
    corpus = [
        _r("Prometheus - Wikipedia", "Greek titan who stole fire"),
        _r("Prometheus (2012 film) - Wikipedia", "Ridley Scott science fiction"),
        _r("Prometheus (2012) - IMDb", "cast and crew"),
        _r("Prometheus | God, Description, Meaning, & Myth", "Britannica"),
        _r("Prometheus (2012) | Rotten Tomatoes", "reviews"),
    ]
    out = _diagnose_grounding(TOPIC, corpus, [])
    assert out["reason"] == "results_off_topic"
    assert "not about this topic" in out["detail"]


def test_a_thin_on_topic_corpus_does_not_blame_the_distiller():
    """First calibration said "defect" at 1-of-20 on topic. That is not a fair
    thing to say about a distiller handed 19 irrelevant results — the same
    misattribution §17.987 spent the day unwinding, in a smaller form."""
    corpus = [_r("Prometheus - Wikipedia", "Greek titan")] * 19
    corpus.append(_r("Prometheus histogram buckets explained",
                     "histogram buckets in prometheus"))
    out = _diagnose_grounding(TOPIC, corpus, [])
    assert out["reason"] == "results_thin_on_topic"
    assert "search engine is the reason" in out["detail"]


def test_an_on_topic_corpus_with_no_entries_IS_called_a_defect():
    """The one case that is genuinely the distiller's fault."""
    good = _r("Prometheus histogram buckets guide",
              "how histogram buckets work in prometheus")
    corpus = [good] * _MIN_ON_TOPIC_FOR_DISTILLER_BLAME
    out = _diagnose_grounding(TOPIC, corpus, [])
    assert out["reason"] == "distiller_returned_nothing"
    assert "IS a defect" in out["detail"]


@pytest.mark.parametrize("topic,results", [
    ("", [{"title": "x"}]),
    (None, [{"title": "x"}]),
    ("t", [{}]),
    ("t", [{"title": None, "content": None}]),
])
def test_it_never_raises(topic, results):
    """Advisory only — it must never be the thing that breaks Phase 2."""
    out = _diagnose_grounding(topic, results, [])
    assert out["status"] in {"grounded", "ungrounded"}


def test_it_is_surfaced_in_the_response():
    import inspect

    from app.modules import ideation_workflow as iw

    src = inspect.getsource(iw.research_and_compile)
    assert '"grounding": _grounding' in src, (
        "the operator reads this from research_summary, not from the log")
