"""§17.1447 — `last_assistant_was_fix` knows the native engine's fix replies.

It keyed on the OWUI pipeline's banners (`## 🔧 Troubleshooting`, "something went wrong — let me help"); the
native engine's fix replies are `## 👉 Do this next … ## Diagnosis … ## Fix` (fixture: ADD4 turn 3146, the
fix that asked for `pct exec 120 -- ip a`). Over every session's turns, all 104 clean shell pastes that
followed a fix read False. And the routing rule it fed was wrong for the native surface: of the 60 such
pastes that reached submit anyway, 17 proved the step done and advanced it — so a clean paste after a fix is
judged by the verifier, and only the "reply confirm" offer is dropped when it says not done.
"""
import pathlib

from app.modules import assist_policy as P
from app.modules.assist_runner_lookup import ReplyTail

FIX_3146 = pathlib.Path("tests/fixtures/add4_fix_3146.md").read_text()
PASTE = "root@pve:~# pct exec 120 -- ip a\n2: eth0@if21: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500"


def test_the_native_fix_reply_is_a_fix():
    assert "🔧 Troubleshooting" not in FIX_3146
    assert P.is_fix_reply(FIX_3146)
    hist = [{"role": "operator", "content": "x"}, {"role": "assistant", "content": FIX_3146}]
    assert P._compute_signals(PASTE, hist)["last_assistant_was_fix"] is True


def test_the_kind_alone_says_so_and_a_walkthrough_is_not_one():
    assert P.is_fix_reply("anything", "fix")
    guide = pathlib.Path("tests/ui/fixtures/add4_guide_3141.md").read_text()
    assert not P.is_fix_reply(guide, "guide")
    assert P._compute_signals(PASTE, [{"role": "assistant", "content": guide}])["last_assistant_was_fix"] is False


def test_the_pipeline_banners_still_count():
    assert P.is_fix_reply("## 🔧 Troubleshooting `T1`\n…")
    assert P.is_fix_reply("_🔧 Sounds like something went wrong — let me help…_")


def test_the_reply_tail_carries_its_kind_into_the_reentry():
    t = ReplyTail()
    t.feed("assist_answer", {"kind": "fix", "text": FIX_3146, "node_key": "ADD4"})
    assert t.kind() == "fix" and t.text() == FIX_3146
    src = pathlib.Path("app/modules/assist_turn.py").read_text()
    assert '{"role": "assistant", "kind": tail.kind(), "content": _asked}' in src


def test_the_blocked_submit_drops_the_offer_for_a_paste_after_a_fix():
    src = pathlib.Path("app/modules/assist_turn.py").read_text()
    assert 'assist_policy._compute_signals(text_ or "", history)["last_assistant_was_fix"]' in src


def test_the_spa_sends_the_kind():
    js = pathlib.Path("app/ui/static/views/assist.js").read_text()
    assert "({ role: t.role, content: t.content, kind: t.kind })" in js
