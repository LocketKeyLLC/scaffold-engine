r"""§17.1373 — a command that spans lines is one command.

Live, 2026-10-05. The ADD132 draft that §17.1371 finally got right in substance —
it used the `fields` array the *arr APIs actually take, kept `priority`, reached
each app at its measured address — was accepted with **one** check:

    pct exec 105 -- systemctl is-active qbittorrent-nox.service

§17.1362 exists to refuse exactly that: a block that changes something through an
API must have a check that reads that API back. It returned nothing, because the
drafter writes a long `curl` the way anyone does:

    curl -s --fail-with-body -X PUT \
        -H "X-Api-Key: $RADARR_KEY" \
        -d '{"id":1,…}' \
        http://192.168.1.22:7878/api/v3/downloadclient/1

The method is on line 1 and the URL on line 4. Scanning line by line,
`api_ports_changed` saw a write with no URL and a URL with no write:

    ports changed: []        1362 refusals: 0

§17.1367 and §17.1368 learned that a judgment has to span the whole BLOCK. This
is the line-level half of the same lesson, and I had left it in place.

And the second half: the settings the \*arr APIs take live in a `fields` array as
`{"name":"username","value":"admin"}` — the setting's name is a VALUE, not a key —
so `fields_sent_to_an_api` saw only the object's own `enable` and `priority`, and
`username`, `password`, `host` and `port` were invisible to §17.1370.
"""
from __future__ import annotations

import json
import pathlib

from app.modules.supervised_runs import (api_ports_changed,
                                         changes_an_api_without_reading_it,
                                         fields_sent_to_an_api, logical_lines)

FRAME = json.loads((pathlib.Path(__file__).parent / "fixtures"
                    / "add132_multiline_curl_2026_10_05.json").read_text())
TEXTS = FRAME["commands"] + [f["content"] for f in FRAME["files"]]


# ----------------------------------------------------- joining continuations


def test_a_continued_command_becomes_one_line():
    text = ("curl -s -X PUT \\\n"
            "    -H 'X-Api-Key: k' \\\n"
            "    -d '{\"id\":1}' \\\n"
            "    http://192.168.1.22:7878/api/v3/downloadclient/1\n")
    got = logical_lines(text)
    assert len(got) == 2, got            # the joined command, then the trailing ""
    assert "-X PUT" in got[0] and "7878" in got[0]


def test_lines_that_do_not_continue_are_untouched():
    assert logical_lines("a\nb\nc") == ["a", "b", "c"]
    assert logical_lines("") == [""]
    assert logical_lines(None) == [""]


def test_a_trailing_continuation_still_yields_its_text():
    got = logical_lines("curl -X PUT \\")
    assert got and "-X PUT" in got[0]


def test_the_real_draft_is_written_with_continuations():
    assert any(ln.rstrip().endswith("\\") for t in TEXTS for ln in t.split("\n"))


# ------------------------------------------------- what the gates now see


def test_the_ports_the_block_changes_are_found():
    assert sorted(api_ports_changed(TEXTS)) == ["7878", "8989"]


def test_the_block_is_refused_for_the_check_that_proves_nothing():
    out = changes_an_api_without_reading_it(FRAME["commands"], FRAME["verify"], FRAME["files"])
    assert len(out) == 1, out
    assert "no check reads that API back" in out[0]["why"]
    assert FRAME["verify"] == ["pct exec 105 -- systemctl is-active qbittorrent-nox.service"]


def test_a_check_on_the_same_port_satisfies_it():
    verify = ["pct exec 103 -- sh -c 'curl -s http://192.168.1.22:7878/api/v3/downloadclient'",
              "pct exec 104 -- sh -c 'curl -s http://192.168.1.23:8989/api/v3/downloadclient'"]
    assert changes_an_api_without_reading_it(
        FRAME["commands"], verify, FRAME["files"]) == []


# ------------------------------------------- the *arr `fields` array shape


def test_every_setting_in_the_fields_array_is_seen():
    got = fields_sent_to_an_api(FRAME["commands"], FRAME["files"])
    for field in ("host", "port", "username", "password"):
        assert field in got, (field, got)
    # and the object's own settings, which the key form already found
    assert "enable" in got and "priority" in got


def test_the_name_value_pair_is_read_at_any_quoting_depth():
    for line in ('curl -d \'{"fields":[{"name":"username","value":"admin"}]}\' http://h:7878/x',
                 'sh -c "curl -d \'{\\"fields\\":[{\\"name\\":\\"password\\",\\"value\\":\\"p\\"}]}\' http://h:7878/x"'):
        got = fields_sent_to_an_api([line])
        assert got and got[0] in ("username", "password"), (line, got)


def test_the_structural_keys_are_still_excluded():
    """`name` and `value` are the array's own plumbing, not settings."""
    got = fields_sent_to_an_api(
        ['curl -d \'{"name":"qBittorrent","fields":[{"name":"host","value":"x"}]}\' http://h:7878/y'])
    assert got == ["host"], got


def test_a_get_still_sends_nothing():
    assert fields_sent_to_an_api(
        ["curl -s -H 'X-Api-Key: k' http://192.168.1.22:7878/api/v3/downloadclient"]) == []
