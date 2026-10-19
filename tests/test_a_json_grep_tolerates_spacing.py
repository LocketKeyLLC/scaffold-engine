"""§17.1658 — a grep for a JSON key/value literal matches with or without the space after the colon (T5, T15)."""
from __future__ import annotations

import subprocess

from app.modules.json_grep import repair, tolerate_json_spacing

COMPACT = '[{"enable":true,"name":"a"},{"enable":true},{"enable":false}]'
SPACED = '[{"status": "importBlocked"}, {"status": "completed"}, {"status": "importBlocked"}]'


def _count(line: str, data: str) -> str:
    return subprocess.run(["bash", "-c", line], input=data, capture_output=True, text=True).stdout.strip()


def test_the_live_t5_count_now_finds_them():
    live = "grep -c '\"enable\": true'"
    (fixed,), n = tolerate_json_spacing([live])
    assert n == 1 and _count(live, COMPACT) == "0" and _count(fixed, COMPACT.replace("},", "},\n")) == "2", fixed


def test_a_compact_pattern_on_spaced_json_and_the_double_quoted_form():
    (fixed,), _ = tolerate_json_spacing(["grep -o '\"status\":\"importBlocked\"'"])
    assert _count(fixed + " | wc -l", SPACED) == "2", fixed
    (dq,), _ = tolerate_json_spacing(['grep -c "\\"enable\\": true"'])
    assert _count(dq, COMPACT.replace("},", "},\n")) == "2", dq


def test_other_patterns_are_left_alone():
    same = ["grep -c importBlocked", "grep -oP '(?<=<ApiKey>)[^<]+' f", "grep -E '\"a\": *1'", "grep -q 'name: x'"]
    assert tolerate_json_spacing(same) == (same, 0)


def test_files_and_checks_are_repaired_too():
    c, v, f, notes = repair([], ["curl -s x | grep -c '\"enable\": true'"],
                            [{"path": "/tmp/a.sh", "content": "curl x | grep -o '\"status\":\"importBlocked\"'"}])
    assert "-E" in v[0] and "-E" in f[0]["content"] and notes
