"""§17.1955 — a `$` after one backslash is a literal dollar, not a variable read.

Live (job 0fb4ea7f LM15, 2026-11-20): a read-only step's `sed -n "/== ab3 start/,\\$p" /root/lm_bench.log` was
refused "`$p` is read here and nothing sets it", and the frame offered no way to run it.
"""
from app.modules.supervised_runs import variables_nothing_sets

HELD = {"secrets": ["MASS_PASSWORD"], "held": []}


def _names(cmds):
    return [r["why"].split("`")[1] for r in variables_nothing_sets(cmds, [], HELD)]


def test_the_live_sed_range_is_not_refused():
    assert _names(['sed -n "/== ab3 start/,\\$p" /root/lm_bench.log | tail -n 60']) == []


def test_an_escaped_brace_is_not_refused():
    assert _names(['echo "cost: \\${PRICE}"']) == []


def test_an_unset_read_is_still_refused():
    # the miss and the hit side by side: the check still bites
    assert _names(['curl -H "K: $NOPE" http://x']) == ["$NOPE"]


def test_after_an_escaped_backslash_the_dollar_expands():
    assert _names(['echo "\\\\$NOPE"']) == ["$NOPE"]
