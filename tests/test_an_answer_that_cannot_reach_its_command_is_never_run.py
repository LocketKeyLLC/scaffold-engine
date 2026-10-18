"""§17.1649 — a placeholder a stored answer cannot reach is refused; nothing runs with the bare `<NAME>`.

Live (job 5db1b088 PK1, 2026-10-18): `pct exec 111 -- sh -c 'echo "192.168.1.44 ssh-ed25519 <KEY_FROM_STEP_1>" >>
/root/.ssh/known_hosts'`. The name looked like a secret, so its answer was stored and the placeholder rewritten to a
reference -- which cannot be expanded inside a single-quoted guest payload. The rewrite's problem was ignored at the
frame (only HELD names were tested) and at the run (`[0]` dropped it), and CT 111's known_hosts got the literal text.
"""
from __future__ import annotations

import inspect

from app.modules import supervised_runs as sr

PK1 = ["qm guest exec 106 -- cat /etc/ssh/ssh_host_ed25519_key.pub",
       "pct exec 111 -- sh -c 'echo \"192.168.1.44 ssh-ed25519 <KEY_FROM_STEP_1>\" >> /root/.ssh/known_hosts'"]
ASKED = [{"name": "KEY_FROM_STEP_1", "secret": True, "hint": "", "value": "", "suggestions": []}]


def test_the_frame_refuses_a_name_the_answer_cannot_reach():
    _c, _v, kept, resolved, blocked = sr.apply_runner_secrets(PK1, [], [dict(i) for i in ASKED], {"secrets": [], "held": []})
    assert blocked == ["KEY_FROM_STEP_1"] and not any(i["name"] == "KEY_FROM_STEP_1" for i in kept), (kept, blocked)


def test_a_name_the_answer_can_reach_is_still_asked():
    ok = ["curl -s -H \"X-Token: <API_TOKEN>\" http://192.168.1.30/api"]
    _c, _v, kept, _r, blocked = sr.apply_runner_secrets(ok, [], [{"name": "API_TOKEN", "secret": True}],
                                                          {"secrets": [], "held": []})
    assert blocked == [] and [i["name"] for i in kept] == ["API_TOKEN"]


def test_the_run_refuses_too():
    src = inspect.getsource(sr.resolve_run)
    assert "_stuck = [p for _c, p in _rw + _rwv if p]" in src and "_left_ph" in src
    assert "secret_ref_rewrite(c, name)[0]" not in src            # the problem is never dropped again
