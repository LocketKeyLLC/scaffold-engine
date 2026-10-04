r"""§17.1362 — a change made through an API that no check reads back.

Measured over every recorded step of the live job: **4** make a state-changing
HTTP call, **3** never read that API in their checks — and those three are
exactly the steps whose record is false or that failed:

    records with a state-changing API call=4   check reads the same API=1   FLAGGED=3
      FLAG ADD97 : changes ['7878'] via api, checks read no api
      FLAG ADD98 : changes ['7878'] via api, checks read no api
      FLAG ADD134: changes ['7878'] via api, checks read no api

ADD97 is *"point Radarr and Sonarr at the download client"* — recorded done,
never done, and the reason the download client answered `403` for weeks. ADD98 is
*"prove it end to end"* — recorded done, proved nothing. The one step that does
read its API back is the one that worked.

`curl -s` without `--fail` exits 0 on a 400 or a 403, so a rejected `PUT` leaves
no trace: only a check that asks the API what it now holds can tell.

Live, 2026-10-04: ADD132's redraft (the first to target the right config file,
§17.1361) `PUT`s the download client into Radarr and Sonarr and checks
`systemctl is-active qbittorrent-nox.service` — the service runs either way. Its
own previous draft had read both clients back, which is the shape this asks for.
"""
from __future__ import annotations

import json
import pathlib

from app.modules.supervised_runs import (api_ports_changed,
                                         changes_an_api_without_reading_it,
                                         refusal_kinds)

FRAME = json.loads((pathlib.Path(__file__).parent / "fixtures"
                    / "add132_check_misses_the_api_2026_10_04.json").read_text())
TEXTS = FRAME["commands"] + [f["content"] for f in FRAME["files"]]
#: what the draft BEFORE this one checked, and what this rule asks for
READS_BACK = [
    "pct exec 103 -- sh -c 'curl -s -H \"X-Api-Key: $K\" http://127.0.0.1:7878/api/v3/downloadclient'",
    "pct exec 104 -- sh -c 'curl -s -H \"X-Api-Key: $K\" http://127.0.0.1:8989/api/v3/downloadclient'",
    "curl -s -d 'username=admin&password=x' http://192.168.1.24:8080/api/v2/auth/login",
]


# ------------------------------------------------- which calls change state


def test_the_live_block_changes_the_two_arr_apis():
    """8080 appears in the block too, but only as `…/api/v2/auth/login`, which
    changes no configuration (§17.1362b)."""
    assert sorted(api_ports_changed(TEXTS)) == ["7878", "8989"]
    assert "8080/api/v2/auth/login" in "".join(TEXTS)


def test_an_explicit_method_and_a_body_both_count():
    assert sorted(api_ports_changed(["curl -X PUT http://h:7878/api/v3/x"])) == ["7878"]
    assert sorted(api_ports_changed(["curl -d 'a=b' http://h:8080/api/v2/app/setPreferences"])) == ["8080"]
    assert sorted(api_ports_changed(["curl --json '{}' http://h:9117/api/x"])) == ["9117"]


def test_a_read_is_not_a_change():
    assert api_ports_changed(["curl -s http://h:7878/api/v3/downloadclient"]) == {}
    assert api_ports_changed(["curl -s -X GET -d x http://h:7878/api/v3/x"]) == {}
    assert api_ports_changed(["# curl -X PUT http://h:7878/api/v3/x"]) == {}


def test_a_call_with_no_port_is_not_judged():
    assert api_ports_changed(["curl -X POST https://api.example.com/v1/x"]) == {}


# ------------------------------------------------------------- the rule


def test_the_live_frame_is_refused():
    out = changes_an_api_without_reading_it(FRAME["commands"], FRAME["verify"], FRAME["files"])
    assert len(out) == 1, out
    why = out[0]["why"]
    assert "no check reads that API back" in why
    assert "exits 0 on a 400 or a 403" in why
    assert "ADD97" in why and "403 for weeks" in why
    assert "not the\nservice's state" in why or "not the service's state" in why


def test_the_only_check_it_had_proves_nothing_either_way():
    assert FRAME["verify"] == ["pct exec 105 -- systemctl is-active qbittorrent-nox.service"]


def test_the_previous_drafts_checks_satisfy_it():
    """The shape asked for, taken from the draft that had it."""
    assert changes_an_api_without_reading_it(
        FRAME["commands"], READS_BACK, FRAME["files"]) == []


def test_one_missing_port_is_enough_to_refuse():
    partial = READS_BACK[:1]          # radarr read back, sonarr and qbit not
    out = changes_an_api_without_reading_it(FRAME["commands"], partial, FRAME["files"])
    assert len(out) == 1
    assert "port 8080" in out[0]["why"] or "port 8989" in out[0]["why"]


def test_a_block_that_changes_no_api_is_not_judged():
    cmds = ["pct exec 105 -- systemctl restart qbittorrent-nox.service"]
    assert changes_an_api_without_reading_it(cmds, [], None) == []


def test_the_files_are_read_too():
    """The live change lives inside the WRITTEN SCRIPT, not in the commands: the
    only command is `MASS_PASSWORD="$MASS_PASSWORD" bash /tmp/set_qbit_auth.sh`.
    A rule that read `commands` alone would see no API call at all."""
    assert FRAME["commands"] == ['MASS_PASSWORD="$MASS_PASSWORD" bash /tmp/set_qbit_auth.sh']
    assert api_ports_changed(FRAME["commands"]) == {}
    assert sorted(api_ports_changed([f["content"] for f in FRAME["files"]])) == \
        ["7878", "8989"]
    # so without the files there is nothing to judge, and with them there is
    assert changes_an_api_without_reading_it(FRAME["commands"], FRAME["verify"], None) == []
    assert changes_an_api_without_reading_it(FRAME["commands"], FRAME["verify"], FRAME["files"])


def test_the_refusal_asks_the_drafter_again():
    out = changes_an_api_without_reading_it(FRAME["commands"], FRAME["verify"], FRAME["files"])
    assert refusal_kinds({"refused": out}) == {"no check reads that API back"}


def test_the_framer_runs_it():
    import inspect

    from app.modules import supervised_runs as sr
    src = inspect.getsource(sr.frame_run)
    assert "changes_an_api_without_reading_it(cmds, verify, shape_files)" in src


# ------------------------------- §17.1362b — a login changes no configuration


def test_a_login_is_not_a_change():
    r"""Live, the frame after §17.1362 shipped. The redraft read BOTH \*arrs back
    (7878 and 8989) and was still refused — for port 8080, where the only call is

        curl -s -d "username=admin&password=…" http://192.168.1.24:8080/api/v2/auth/login

    followed by `if [ "$LOGIN_RESULT" != "Ok." ]; then exit 1`. That login changes
    no configuration: qBittorrent's password was set by editing its config, and the
    login is how the block PROVES it. Asking for a check that "reads back" a change
    the API never made is asking for nothing.
    """
    login = ('curl -s -d "username=admin&password=$MASS_PASSWORD" '
             'http://192.168.1.24:8080/api/v2/auth/login')
    assert api_ports_changed([login]) == {}


def test_every_authentication_verb_is_exempt():
    for verb in ("login", "logout", "signin", "signout", "token", "session",
                 "authenticate", "auth"):
        assert api_ports_changed([f"curl -d x http://h:8080/api/v2/{verb}"]) == {}, verb


def test_configuring_authentication_is_still_a_change():
    """`/api/v3/authentication` sets up auth; only the VERB at the end is exempt."""
    assert sorted(api_ports_changed(
        ["curl -X POST -d '{}' http://127.0.0.1:7878/api/v3/authentication"])) == ["7878"]
    assert sorted(api_ports_changed(
        ["curl -X PUT -d '{}' http://127.0.0.1:7878/api/v3/config/host"])) == ["7878"]


def test_the_corpus_only_post_endpoint_is_still_a_change():
    """Measured: `/api/v3/rootfolder` (ADD131) is the one POST-shaped endpoint in
    the whole recorded history, and it is a real configuration change."""
    assert sorted(api_ports_changed(
        ["curl -X POST -d '{}' http://127.0.0.1:7878/api/v3/rootfolder"])) == ["7878"]


def test_a_url_with_no_path_still_counts():
    assert sorted(api_ports_changed(["curl -X POST -d x http://h:9091"])) == ["9091"]


def test_the_live_frame_after_the_amendment_passes():
    r"""The frame that was refused for 8080 alone: with both \*arrs read back and
    the login exempt, nothing is left to refuse."""
    arrs_read_back = [
        "pct exec 103 -- sh -c 'curl -s -H \"X-Api-Key: $K\" http://127.0.0.1:7878/api/v3/downloadclient'",
        "pct exec 104 -- sh -c 'curl -s -H \"X-Api-Key: $K\" http://127.0.0.1:8989/api/v3/downloadclient'",
    ]
    assert changes_an_api_without_reading_it(
        FRAME["commands"], arrs_read_back, FRAME["files"]) == []
