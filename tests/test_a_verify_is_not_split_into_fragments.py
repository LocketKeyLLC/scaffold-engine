r"""§17.1568 — a bundled validation splits into CHECKS, never into fragments of one check.

§17.1092's splitter cut a check's text at the first `.` and split on every `/`, `,`, `and`, `&`, and never asked
whether a clause said anything. Live fragments it made: "Verify etc" + "Verify gai" from "Confirm … /etc/gai.conf is
unchanged …" (job 977a8a38 T20/T20v2); "Verify Name" / "Verify Overview" / "Verify Primary image presence" from
"Verify item metadata completeness: Name, Overview, Primary image presence" (job 263e920a T7/T7v2/T7v3); "Verify: api."
+ "X answers at." from one URL check (job c28ddeb0, ten of them). Each fragment drafted to empty or wrong-target
commands, parked dead, and was skipped by hand.
"""
from __future__ import annotations

from app.modules.dag_generator import split_bundled_validation, _check_clauses, _is_a_check


def _node(nid, name, notes, deps=("T1",)):
    return {"id": nid, "name": name, "type": "validation", "depends_on": list(deps), "notes": notes}


def test_a_path_is_one_token_and_a_list_of_values_is_one_check():
    t = _node("T20", "Confirm out-of-scope untouched",
              "Confirm /etc/gai.conf is unchanged on containers 101, 105, 107 and 108 and the host was not touched.")
    assert _check_clauses(f"{t['name']}. {t['notes']}") == []
    out = split_bundled_validation([{"id": "T1", "name": "Fix", "type": "action", "depends_on": []}, t])
    assert [x["id"] for x in out] == ["T1", "T20"] and out[1]["name"] == "Confirm out-of-scope untouched"


def test_attributes_of_one_check_are_not_split_apart():
    t = _node("T7", "Verify item metadata completeness",
              "Verify: Name, Overview, Primary image presence for every series, season and episode.")
    assert _check_clauses(f"{t['name']}. {t['notes']}") == []
    out = split_bundled_validation([{"id": "T1", "name": "Refresh", "type": "action", "depends_on": []}, t])
    assert [x["id"] for x in out] == ["T1", "T7"]


def test_a_url_stays_whole_and_real_checks_still_split():
    t = _node("T22", "Verify the panel",
              "Verify the /api/health endpoint answers at http://127.0.0.1:3001/api/health and the panel serves index.html")
    clauses = _check_clauses(f"{t['name']}. {t['notes']}")
    assert clauses == ["the /api/health endpoint answers at http://127.0.0.1:3001/api/health", "the panel serves index.html"]
    out = split_bundled_validation([{"id": "T1", "name": "Build", "type": "action", "depends_on": []}, t])
    names = [x["name"] for x in out if x["id"].startswith("T22")]
    # the title is capped at 60 chars but cut at a word, never inside the URL; the notes carry the whole clause
    assert len(names) == 2 and names[0] == "Verify the /api/health endpoint answers at"
    notes = [x["notes"] for x in out if x["id"].startswith("T22")]
    assert notes[0].startswith("Verify: the /api/health endpoint answers at http://127.0.0.1:3001/api/health.")
    assert not any(n in ("Verify api", "Verify: api.", "Verify health") for n in names)


def test_the_original_bundled_build_check_still_splits_per_check():
    """§17.1092's own case keeps working: each clause has a predicate."""
    t = _node("T37", "Validate entire build",
              "Check all services reachable, firewall rules correct, VPN works, GPU works, control panel functional. No installs.")
    assert _check_clauses(f"{t['name']}. {t['notes']}") == [
        "all services reachable", "firewall rules correct", "VPN works", "GPU works", "control panel functional"]


def test_what_counts_as_a_check():
    for yes in ("services reachable", "VPN works", "api healthy", "db connected", "the panel serves index.html",
                "getent lists an IPv4 address first", "jellyfin.service is active"):
        assert _is_a_check(yes), yes
    for no in ("Name", "Overview", "Primary image presence", "etc", "gai", "api", "101", "105 107 108"):
        assert not _is_a_check(no), no


def test_a_slash_or_ampersand_with_spaces_around_still_separates():
    clauses = _check_clauses("Check web reachable / api healthy & db connected")
    assert clauses == ["web reachable", "api healthy", "db connected"]
    kept = _check_clauses("Check http://x/a?b=1&c=2 answers and /var/lib/x/y.db exists")
    assert kept == ["http://x/a?b=1&c=2 answers", "/var/lib/x/y.db exists"]
