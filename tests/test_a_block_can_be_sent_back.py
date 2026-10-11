"""§17.1472 — the operator can send a drafted block back with what is wrong with it.

Live (2026-10-11): ADR1's block pinned `cat /etc/ssh/ssh_host_ed25519_key.pub` read INSIDE the panel's LXC 111 as
VM 106's host key. A run pause took only run / myself / skip, so the only way to correct the draft was to edit the
step's text by hand and ask again.
"""
import pathlib

from app.modules import decision_pause as dp

ADR1_TAIL = ("…replace it with 192.168.1.44 and restart whatever reads it.\n\nDone when nothing those steps set up "
             "still names the old address — each of these prints nothing: `grep -rIl x /etc/hosts`")
NOTE = ("VM 106's ssh host key did not change, only its address; a key read inside LXC 111 is the PANEL's own key, "
        "never VM 106's.")


def test_the_review_goes_before_done_when():
    out = dp.with_review(ADR1_TAIL, NOTE, "2026-10-11T13:30:00+00:00")
    head, done = out.split("\n\nDone when", 1)
    assert head.endswith(f"Operator review 2026-10-11 of the drafted block: {NOTE}")
    assert done.startswith(" nothing those steps set up")
    assert dp.with_review("No done-condition here.", "x", "2026-10-11").endswith("of the drafted block: x")


def test_it_is_offered_and_wired():
    sr = pathlib.Path("app/modules/supervised_runs.py").read_text()
    assert '{"id": "redraft", "label": "Redraft it with my correction"' in sr
    src = pathlib.Path("app/modules/decision_pause.py").read_text()
    assert 'waiting.get("kind") == "run" and not delegate and (choice or "").strip().lower() == "redraft"' in src
    assert "redraft needs a note" in src and "await add_review(db, job_id, node_key, note, now)" in src
    ui = pathlib.Path("app/ui/static/views/theater.js").read_text()
    assert 'send("redraft", "Sending back…")' in ui and "runBtn, selfBtn, skipBtn, redraftBtn," in ui
