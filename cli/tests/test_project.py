"""Tests for cli/scaffold_cli/project.py — nickname store + slug + status
explainer (Sprint U.4).
"""
from __future__ import annotations


import pytest

from scaffold_cli import project as p


@pytest.fixture(autouse=True)
def _isolate_store(monkeypatch, tmp_path):
    """Force the nickname store to a per-test temp dir so we don't
    pollute the user's real ~/.scaffold/nicknames.json."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))


# ---------------------------------------------------------------------------
# slugify
# ---------------------------------------------------------------------------


def test_slugify_lowercases_and_hyphenates():
    assert p.slugify("Build a Markdown Linter") == "build-a-markdown-linter"


def test_slugify_collapses_whitespace_and_punctuation():
    assert p.slugify("  Make me, a script!! ") == "make-me-a-script"


def test_slugify_truncates_to_max_chars():
    long_idea = "a " * 100  # very long
    out = p.slugify(long_idea, max_chars=20)
    assert len(out) <= 20
    assert not out.startswith("-") and not out.endswith("-")


def test_slugify_falls_back_to_project_when_empty():
    assert p.slugify("") == "project"
    assert p.slugify("!@#$%") == "project"


# ---------------------------------------------------------------------------
# UUID detection
# ---------------------------------------------------------------------------


def test_looks_like_uuid_accepts_canonical():
    assert p.looks_like_uuid("481010cd-9542-4b27-9af3-7c80f468af89")


def test_looks_like_uuid_is_case_insensitive():
    assert p.looks_like_uuid("ABC12345-1234-ABCD-1234-ABCDEF123456")


def test_looks_like_uuid_rejects_short_and_malformed():
    assert not p.looks_like_uuid("not-a-uuid")
    assert not p.looks_like_uuid("481010cd")
    assert not p.looks_like_uuid("")
    assert not p.looks_like_uuid("markdown-linter-a4f2")


# ---------------------------------------------------------------------------
# nickname generation
# ---------------------------------------------------------------------------


def test_make_nickname_combines_slug_and_short_hash():
    uid = "481010cd-9542-4b27-9af3-7c80f468af89"
    nick = p.make_nickname("Build a markdown linter", uid)
    assert nick.startswith("build-a-markdown-linter-")
    assert len(nick.split("-")[-1]) == 4  # 4-char hash suffix


def test_make_nickname_is_deterministic_for_same_inputs():
    uid = "481010cd-9542-4b27-9af3-7c80f468af89"
    a = p.make_nickname("Build X", uid)
    b = p.make_nickname("Build X", uid)
    assert a == b


def test_make_nickname_distinguishes_collisions_via_hash():
    """Same idea text + different UUIDs → different nicknames (different hash)."""
    a = p.make_nickname("Build X", "11111111-1111-1111-1111-111111111111")
    b = p.make_nickname("Build X", "22222222-2222-2222-2222-222222222222")
    assert a != b
    assert a.startswith("build-x-") and b.startswith("build-x-")


# ---------------------------------------------------------------------------
# store roundtrip
# ---------------------------------------------------------------------------


def test_load_store_returns_empty_dict_on_fresh_install():
    assert p.load_store() == {}


def test_save_then_load_roundtrips():
    p.save_store({"foo": "uuid-1", "bar": "uuid-2"})
    assert p.load_store() == {"foo": "uuid-1", "bar": "uuid-2"}


def test_add_nickname_appends_to_existing_store():
    p.add_nickname("first", "uuid-1")
    p.add_nickname("second", "uuid-2")
    store = p.load_store()
    assert store == {"first": "uuid-1", "second": "uuid-2"}


def test_add_nickname_overwrites_existing():
    p.add_nickname("project", "uuid-old")
    p.add_nickname("project", "uuid-new")
    assert p.load_store() == {"project": "uuid-new"}


def test_load_store_tolerates_corrupt_file(tmp_path):
    """If the file is unreadable JSON, return {} instead of crashing."""
    path = p._store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json at all", encoding="utf-8")
    assert p.load_store() == {}


# ---------------------------------------------------------------------------
# resolve / reverse_lookup
# ---------------------------------------------------------------------------


def test_resolve_passes_uuids_through_unchanged():
    uid = "481010cd-9542-4b27-9af3-7c80f468af89"
    assert p.resolve(uid) == uid


def test_resolve_looks_up_nickname():
    p.add_nickname("markdown-linter-a4f2", "the-job-uuid")
    assert p.resolve("markdown-linter-a4f2") == "the-job-uuid"


def test_resolve_returns_none_for_unknown_nickname():
    assert p.resolve("never-registered") is None


def test_resolve_handles_empty_input():
    assert p.resolve("") is None


def test_reverse_lookup_finds_nickname_for_uuid():
    p.add_nickname("foo", "uuid-1")
    p.add_nickname("bar", "uuid-2")
    assert p.reverse_lookup("uuid-1") == "foo"
    assert p.reverse_lookup("uuid-2") == "bar"


def test_reverse_lookup_returns_none_for_unknown_uuid():
    assert p.reverse_lookup("never-stored") is None


# ---------------------------------------------------------------------------
# status explanations
# ---------------------------------------------------------------------------


def test_status_explain_covers_every_known_status():
    """Mirror of NEXT_ACTIONS coverage, kept in lockstep with JobStatus."""
    expected = {
        "pending", "refining", "awaiting_confirmation", "researching",
        "planning", "executing", "running", "completed", "failed",
        "cancelled", "blocked", "assisted_executing", "assisted_running",
        "assisted_paused",
    }
    assert set(p.STATUS_EXPLAIN.keys()) == expected


def test_each_status_has_required_fields():
    for status, info in p.STATUS_EXPLAIN.items():
        assert "headline" in info, f"{status} missing headline"
        assert "what_happens" in info, f"{status} missing what_happens"
        assert "valid_actions" in info, f"{status} missing valid_actions"
        assert isinstance(info["valid_actions"], list), f"{status} valid_actions not a list"
