"""§17.1452 — the operator sees what the engine is doing, and each message once.

Live (ADD128, 2026-10-10): the note path recorded every correction a second time as an operator `note` row,
so it showed twice (3166/3167, 3169/3170). The claim-repair and re-guide halves are in tests/test_assist_turn.py.
"""
from app.modules.assist_turns import collapse_double_records


def test_a_message_and_its_note_record_are_one_bubble():
    rows = [
        {"id": 3169, "role": "operator", "kind": "message", "content": "caddy (ct 120) is at 192.168.1.127, not 192.168.1.26."},
        {"id": 3170, "role": "operator", "kind": "note", "content": "caddy (ct 120) is at 192.168.1.127, not 192.168.1.26."},
        {"id": 3171, "role": "assistant", "kind": "note", "content": "🔁 **Plan updated from your note**"},
    ]
    out = collapse_double_records(rows)
    assert [(r["id"], r["kind"]) for r in out] == [(3169, "note"), (3171, "note")]


def test_a_note_with_different_words_stays():
    rows = [{"id": 1, "role": "operator", "kind": "message", "content": "a"},
            {"id": 2, "role": "operator", "kind": "note", "content": "b"}]
    assert len(collapse_double_records(rows)) == 2
