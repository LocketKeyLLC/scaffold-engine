"""§17.802 — depth-scaled per-iteration URL cap resolver."""
from app.config import Settings


def test_default_depth_scaled_caps():
    s = Settings()
    assert s.research_max_urls_shallow == 30
    assert s.research_max_urls_medium == 60
    assert s.research_max_urls_deep == 90


def test_resolver_maps_each_depth():
    s = Settings()
    assert s.research_max_urls_for_depth("shallow") == 30
    assert s.research_max_urls_for_depth("medium") == 60
    assert s.research_max_urls_for_depth("deep") == 90


def test_resolver_unknown_depth_falls_back_to_medium():
    s = Settings()
    assert s.research_max_urls_for_depth("") == 60
    assert s.research_max_urls_for_depth("galaxy-brain") == 60
    assert s.research_max_urls_for_depth(None) == 60  # defensive: never crashes


def test_resolver_honors_overrides(monkeypatch):
    s = Settings()
    monkeypatch.setattr(s, "research_max_urls_deep", 120)
    assert s.research_max_urls_for_depth("deep") == 120
    # increasing degrees invariant holds for the defaults
    assert s.research_max_urls_shallow < s.research_max_urls_medium < 120
