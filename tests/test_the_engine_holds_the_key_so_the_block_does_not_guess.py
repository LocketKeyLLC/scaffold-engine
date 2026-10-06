"""§17.1384 — the block does not invent a credential for a key the engine reads.

Live, 2026-10-06, ADD134's fifth draft. §17.1382 put Jellyfin's key in the
registry; §17.1383 taught the read in `FILE_RULES`. This draft did not read the
wrong file — it stopped reading altogether::

    def jellyfin_login():
        url = f"http://{JELLYFIN_IP}:{JELLYFIN_PORT}/Users/AuthenticateByName"
        data = {"Username": "jellyfin", "Pw": MASS_PASSWORD}
        return http_json(url, method="POST", data=data)["AccessToken"]

A guessed username, the operator's mass password, and no `X-Emby-Authorization`
header, which that endpoint requires. Measured: **HTTP 400**, and
`GET /Users/Public` answers `[]`, so there is no user of that name at all. The
step had proved five of its six hops and died on the one the engine could have
answered from a dict.

§17.1383 judges a key read from the wrong path. This judges not reading at all.
"""
from __future__ import annotations

import inspect
import pathlib

import pytest

from app.modules import machine_values as mv
from app.modules import supervised_runs as sr
from app.modules.machine_values import authenticates_instead_of_reading_the_key as GUESSED

FIX = pathlib.Path(__file__).parent / "fixtures"
#: the live draft, saved off the engine
LIVE = (FIX / "add134_invented_a_jellyfin_login_2026_10_06.py").read_text()
LIVE_FILES = [{"path": "/tmp/chain_proof.py", "content": LIVE}]


def test_the_live_invented_login_is_refused():
    out = GUESSED([], LIVE_FILES)
    assert len(out) == 1, out
    why = out[0]["why"]
    assert "jellyfin" in why
    assert "HTTP 400" in why, "the measurement, not an opinion"
    assert "/Users/Public` answers\n`[]`" in why or "answers\n`[]`" in why or "`[]`" in why
    assert "AuthenticateByName" in out[0]["command"]


def test_the_remedy_is_the_read_itself():
    assert mv.readable_for("JELLYFIN_API_KEY").read(None) in GUESSED([], LIVE_FILES)[0]["why"]


def test_qbittorrent_login_is_left_alone():
    """The opposite case, and the reason this gate is keyed on the REGISTRY:
    qBittorrent has no readable key, so a username/password login is correct
    there — and the same draft's qBittorrent login worked live (progress 100%)."""
    assert mv.readable_for("QBITTORRENT_API_KEY") is None
    assert "qbittorrent" not in mv._AUTH_ENDPOINTS
    assert GUESSED(["curl -d 'username=admin&password=$MASS_PASSWORD' "
                    "http://192.168.1.24:8080/api/v2/auth/login"], []) == []
    # the live draft logs in to qBittorrent too, and only Jellyfin is refused
    assert len(GUESSED([], LIVE_FILES)) == 1


@pytest.mark.parametrize("text", [
    'python3 -c "import sqlite3; ... SELECT AccessToken FROM ApiKeys"',
    'curl -H "X-Emby-Token: $KEY" http://192.168.1.20:8096/Items',
    'curl http://192.168.1.20:8096/System/Info/Public',
])
def test_what_it_must_not_claim(text):
    assert GUESSED([text], []) == []


def test_only_a_service_with_a_readable_is_judged():
    for app in mv._AUTH_ENDPOINTS:
        assert mv._SERVICES.get(app) is not None, app


def test_one_refusal_per_service():
    body = LIVE + "\n" + LIVE
    assert len(GUESSED([], [{"path": "/tmp/x.py", "content": body}])) == 1


# ── wired ────────────────────────────────────────────────────────────────────

def test_it_runs_in_frame_run():
    src = inspect.getsource(sr.frame_run)
    assert "_mv.authenticates_instead_of_reading_the_key(cmds, shape_files)" in src


def test_it_drives_a_redraft():
    refused = GUESSED([], LIVE_FILES)
    assert refused
    assert sr.shape_retry_note({"kind": "run", "refused": refused})


def test_the_rules_say_both_halves():
    assert "never LOG IN to a service whose key is on the disk" in sr.FILE_RULES
    assert "qBittorrent is the opposite case" in sr.FILE_RULES


def test_the_fixture_is_the_live_draft():
    assert "AuthenticateByName" in LIVE and "chain_proof" not in LIVE[:50]
    assert "qbit_login" in LIVE, "the draft that got five hops right"
