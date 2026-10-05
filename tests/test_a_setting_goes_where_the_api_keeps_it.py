"""§17.1378 — a secret assigned to a key the API drops, and a mask written back.

Live, 2026-10-05. ADD132 finally RAN: §17.1377 let it through because the body
reached the guest (`< /tmp/radarr_client.json` into `pct exec … -d @-`) and every
heredoc imported what it used. The block exited 22 on its own last step, and then
the machines were read:

    fields.username : 'admin'                 (untouched)
    fields.password : MASKED - the literal asterisks
    the top-level username/password the draft set : ABSENT
    downloadclient/test : HTTP 400 "Authentication Failure" on Username

The draft had done:

    c = json.load(sys.stdin)[0]       # GET -- password comes back "********"
    c["username"] = "admin"           # DROPPED: the API keeps settings in `fields`
    c["password"] = …                  # DROPPED
    … PUT c back                       # STORES THE MASK as the password

So the stored password was left WORSE than before the run. And the last step posted
`-d '{}'` to `…/downloadclient/test`, an endpoint that validates the whole resource,
which answered with four errors about the empty body — reporting the step `failed`
for a malformed CHECK while its PUTs had already landed.
"""
import json
import pathlib

import app.modules.supervised_runs as sr
from app.modules.supervised_runs import (FILE_RULES,
                                         a_credential_set_where_the_api_does_not_keep_it,
                                         a_validating_endpoint_sent_an_empty_body)

FIX = pathlib.Path(__file__).parent / "fixtures"
LIVE = json.loads((FIX / "add132_credential_on_the_object_2026_10_05.json").read_text())


def _block(text):
    return [{"path": "/tmp/block.sh", "content": text}]


# ------------------------------------------------------------ what actually ran

def test_the_draft_ran_and_reported_failed():
    """Recorded from the live run so this file cannot silently pass."""
    assert LIVE["ran_with"] == {"suggested": "run", "refused": 0, "exit": 22,
                                "outcome": "failed", "node_status": "failed"}


def test_the_machines_were_read_after_the_run():
    """The measurement that identified both defects — not a guess from the source."""
    for app in ("radarr", "sonarr"):
        m = LIVE["measured_after_the_run"][app]
        assert m["top_level_username"] == "absent"
        assert m["fields.password"] == "the literal asterisks"
        assert m["priority"] == 1 and m["enable"] is True
    assert "Authentication Failure" in LIVE["measured_after_the_run"]["downloadclient_test"]


# --------------------------------------- a setting goes where the API keeps it

def test_the_credential_on_the_object_is_refused():
    out = a_credential_set_where_the_api_does_not_keep_it(LIVE["commands"], LIVE["files"])
    assert len(out) == LIVE["defects"]["credential_on_the_object"] == 2, out
    assert {m["command"].split("[")[1].split("]")[0].strip("\"'") for m in out} == \
        {"username", "password"}


def test_the_refusal_carries_the_measurement_and_the_remedy():
    why = a_credential_set_where_the_api_does_not_keep_it(LIVE["commands"], LIVE["files"])[0]["why"]
    assert "`fields` array" in why
    assert "literal asterisks" in why          # what was measured
    assert 'f["name"]' in why                  # where to put it instead
    assert "Never PUT back a secret field you only read" in why


def test_the_named_field_shape_is_accepted():
    """§17.1370's shape: the name is a VALUE in the `fields` array, and a block
    that sets it there is doing the right thing."""
    assert a_credential_set_where_the_api_does_not_keep_it([], _block(
        "obj = json.load(sys.stdin)\n"
        "for f in obj['fields']:\n"
        "    if f['name'] == 'username':\n"
        "        f['value'] = 'admin'\n"
        "    elif f['name'] == 'password':\n"
        "        f['value'] = os.environ['MASS_PASSWORD']\n"
        "r = urllib.request.Request(url, method='PUT')   # /api/v3/downloadclient/1\n")) == []


def test_a_non_credential_key_on_the_object_is_fine():
    """`priority` and `enable` really ARE keys of the object — only credentials
    live in `fields`, so a gate that refused these would refuse correct work."""
    assert a_credential_set_where_the_api_does_not_keep_it([], _block(
        "c = json.load(sys.stdin)[0]\n"
        "c['priority'] = 1\n"
        "c['enable'] = True\n"
        "curl -X PUT -d @- http://127.0.0.1:7878/api/v3/downloadclient/1\n")) == []


def test_a_block_that_sends_nothing_back_is_not_judged():
    """Setting a key on an object the block only reads changes nothing anywhere."""
    assert a_credential_set_where_the_api_does_not_keep_it([], _block(
        "c = json.load(sys.stdin)[0]\n"
        "c['password'] = 'x'\n"
        "print(c)   # read from /api/v3/downloadclient, never sent\n")) == []


def test_an_api_without_a_version_path_is_not_judged():
    """The `fields` shape is what these versioned APIs do; a different API is not
    this engine's to assume about."""
    assert a_credential_set_where_the_api_does_not_keep_it([], _block(
        "c['password'] = 'x'\n"
        "curl -X PUT -d @- http://127.0.0.1:9000/settings\n")) == []


# ----------------------------------- a /test endpoint validates the resource

def test_the_empty_body_to_the_test_endpoint_is_refused():
    out = a_validating_endpoint_sent_an_empty_body(LIVE["commands"], LIVE["files"])
    assert len(out) == LIVE["defects"]["empty_body_to_a_validating_test"] == 2, out
    assert "7878" in out[0]["command"] or "8989" in out[0]["command"]


def test_the_refusal_quotes_what_the_api_actually_answered():
    why = a_validating_endpoint_sent_an_empty_body(LIVE["commands"], LIVE["files"])[0]["why"]
    for err in LIVE["radarr_test_errors"]:
        assert err.strip(".").split(" must")[0].strip("'") in why, err
    assert "malformed CHECK" in why


def test_an_id_only_body_is_refused_too():
    assert len(a_validating_endpoint_sent_an_empty_body([], _block(
        'curl -X POST -d \'{"id":1}\' http://127.0.0.1:7878/api/v3/downloadclient/test\n'))) == 1


def test_the_object_sent_to_the_test_endpoint_is_accepted():
    assert a_validating_endpoint_sent_an_empty_body([], _block(
        "curl -s -o /tmp/c.json http://127.0.0.1:7878/api/v3/downloadclient/1\n"
        "curl -X POST -d @/tmp/c.json http://127.0.0.1:7878/api/v3/downloadclient/test\n")) == []


def test_a_body_from_stdin_is_accepted():
    """`-d @-` is §17.1377's own recommended shape, so it must not read as empty."""
    assert a_validating_endpoint_sent_an_empty_body([], _block(
        "cat /tmp/c.json | curl -X POST -d @- "
        "http://127.0.0.1:7878/api/v3/downloadclient/test\n")) == []


def test_a_body_passed_as_a_function_argument_is_not_judged():
    """Measured: treating "no -d flag" as "no body" produced 158 corpus flags,
    nearly all false — this call passes the real object."""
    assert a_validating_endpoint_sent_an_empty_body([], _block(
        "test_raw = api(ct, key, port, 'POST', '/api/v3/downloadclient/test', client)\n")) == []


def test_a_real_body_is_accepted():
    assert a_validating_endpoint_sent_an_empty_body([], _block(
        'curl -X POST -d \'{"name":"qBittorrent","implementation":"QBittorrent",'
        '"configContract":"QBittorrentSettings","priority":1,"fields":[]}\' '
        "http://127.0.0.1:7878/api/v3/downloadclient/test\n")) == []


# --------------------------------------------- taught, wired, and redraftable

def test_the_rules_teach_the_fields_shape_with_its_measurement():
    assert "A SETTING GOES WHERE THE API KEEPS IT" in FILE_RULES
    assert "literal asterisks" in FILE_RULES
    assert 'if f["name"] == "password"' in FILE_RULES


def test_the_rules_teach_what_a_test_endpoint_wants():
    assert "VALIDATES THE WHOLE RESOURCE" in FILE_RULES
    assert "must be between 1 and 50" in FILE_RULES


def test_the_rules_own_python_example_passes_both_gates():
    """The engine must not teach a shape its own gates refuse — §17.1377's lesson."""
    import re
    for block in re.findall(r"```python\n(.*?)```", FILE_RULES, re.S):
        assert a_credential_set_where_the_api_does_not_keep_it([], _block(block)) == [], block


def test_both_gates_run_in_frame_run():
    import inspect
    src = inspect.getsource(sr.frame_run)
    assert "a_credential_set_where_the_api_does_not_keep_it(cmds, shape_files)" in src
    assert "a_validating_endpoint_sent_an_empty_body(cmds, shape_files)" in src


def test_both_refusals_drive_a_redraft():
    """§17.1269 — unregistered, each would park the frame with a greyed-out Run."""
    for fn in (a_credential_set_where_the_api_does_not_keep_it,
               a_validating_endpoint_sent_an_empty_body):
        refused = fn(LIVE["commands"], LIVE["files"])
        assert refused
        assert sr.shape_retry_note({"kind": "run", "refused": refused})
