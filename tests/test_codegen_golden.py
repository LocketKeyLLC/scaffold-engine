"""§17.428 — offline codegen golden tests.

Runs the deterministic structural checkers (tests/_codegen_golden_checks.py)
against the committed good_output for each golden in
tests/fixtures/codegen_goldens.json. No LLM, no services — part of the
default suite + smoke tier.

This tier proves the CHECKERS and the goldens are mutually consistent. The
deferred live tier (logged in OVERVIEW §17.428) reuses the same checkers
against real model output for each brief.
"""
import json
from pathlib import Path

import pytest

from tests._codegen_golden_checks import check_golden, defines_symbol

pytestmark = pytest.mark.smoke

_FIXTURE = Path(__file__).parent / "fixtures" / "codegen_goldens.json"
_GOLDENS = json.loads(_FIXTURE.read_text())["goldens"]


def _ids(goldens):
    return [g["id"] for g in goldens]


@pytest.mark.parametrize("golden", _GOLDENS, ids=_ids(_GOLDENS))
def test_golden_good_output_passes_all_checks(golden):
    """Each golden's committed good_output satisfies its own assertions."""
    failures = check_golden(golden, golden["good_output"])
    assert failures == [], f"{golden['id']}: {failures}"


def test_every_golden_has_id_brief_and_output():
    for g in _GOLDENS:
        assert g.get("id"), "golden missing id"
        assert g.get("brief"), f"{g['id']}: missing brief"
        assert g.get("good_output"), f"{g['id']}: missing good_output"


def test_at_least_one_assertion_per_golden():
    keys = ("must_parse", "must_define", "must_not_contain", "must_contain")
    for g in _GOLDENS:
        assert any(g.get(k) for k in keys), f"{g['id']}: no structural assertions"


# ---------------------------------------------------------------------------
# Negative cases — the checkers must actually fail on bad output, otherwise
# the offline tier is a rubber stamp.
# ---------------------------------------------------------------------------

def test_unparseable_output_fails_must_parse():
    golden = {"must_parse": True}
    failures = check_golden(golden, "```python\ndef broken(:\n```")
    assert any("must_parse" in f for f in failures)


def test_missing_symbol_fails_must_define():
    golden = {"must_define": ["expected_fn"]}
    failures = check_golden(golden, "```python\ndef other_fn():\n    return 1\n```")
    assert any("must_define" in f for f in failures)


def test_banned_construct_fails_must_not_contain():
    golden = {"must_not_contain": ["argparse"]}
    failures = check_golden(golden, "```python\nimport argparse\n```")
    assert any("must_not_contain" in f for f in failures)


def test_module_node_with_main_is_caught():
    # §17.374 regression shape: a "write the generator" node that smuggles in
    # a full CLI instead of a module would trip must_not_contain.
    golden = {"must_not_contain": ["def main(", "__main__"]}
    bad = "```python\ndef gen():\n    return 1\n\n\ndef main():\n    print(gen())\n\n\nif __name__ == \"__main__\":\n    main()\n```"
    failures = check_golden(golden, bad)
    assert len(failures) >= 1


def test_defines_symbol_handles_unparseable():
    assert defines_symbol("```python\ndef broken(:\n```", "broken") is False
