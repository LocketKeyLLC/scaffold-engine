r"""§17.1501 — an empty-body POST to an *arr `/{resource}/test` is repaired to `/testall`.

Live, job c75d77fa T3v2 "Verify testable" (2026-10-12). The step drafted
`curl … -X POST … -d '{}' …/api/v3/downloadclient/test` to check connectivity. A
`/test` endpoint validates the WHOLE resource, so an empty body fails on the
resource's own required fields (`'Name' must not be empty`, …) — §17.1378b refuses
it. The standard "test everything configured" is `/{resource}/testall`, which takes
no body: it is what the arr UI's "Test All" button calls. The engine repairs the
drafter's own mistake instead of parking the step.
"""
from __future__ import annotations

import subprocess

from app.modules.supervised_runs import (
    repair_empty_body_test_call,
    a_validating_endpoint_sent_an_empty_body,
)

RADARR = ('pct exec 103 -- curl -s --fail-with-body -X POST -H "X-Api-Key: $RADARR_API_KEY" '
          "-H 'Content-Type: application/json' -d '{}' http://127.0.0.1:7878/api/v3/downloadclient/test")


def test_the_live_empty_body_test_becomes_testall_without_a_body():
    cmds, _, notes = repair_empty_body_test_call([RADARR], [])
    out = cmds[0]
    assert "/api/v3/downloadclient/testall" in out
    assert "/downloadclient/test " not in out and out.rstrip().endswith("/testall")
    assert "-d '{}'" not in out and "-d " not in out, "the empty body is removed"
    assert "-X POST" in out, "testall is still a POST"
    assert notes and "§17.1501" in notes[0]["why"]


def test_the_repaired_command_is_valid_shell():
    out = repair_empty_body_test_call([RADARR], [])[0][0]
    r = subprocess.run(["bash", "-n", "-c", out], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_the_empty_body_gate_is_quiet_after_the_repair():
    assert a_validating_endpoint_sent_an_empty_body([RADARR]), "raw empty-body /test must be refused"
    repaired = repair_empty_body_test_call([RADARR], [])[0]
    assert a_validating_endpoint_sent_an_empty_body(repaired) == [], "nothing left to refuse after the repair"


def test_a_test_call_that_sends_a_real_object_is_left_alone():
    real = ("pct exec 103 -- sh -c 'curl -s -X POST -H \"X-Api-Key: $K\" -d @/tmp/client.json "
            "http://127.0.0.1:7878/api/v3/downloadclient/test'")
    out, _, notes = repair_empty_body_test_call([real], [])
    assert out == [real] and not notes, "a real body (file) is not the empty-body defect"


def test_an_unknown_resource_is_left_for_the_refusal():
    # A resource not known to carry /testall is NOT rewritten — inventing an endpoint
    # is worse than the §17.1378b refusal.
    cmd = ("pct exec 103 -- curl -s -X POST -H \"X-Api-Key: $K\" -d '{}' "
           "http://127.0.0.1:7878/api/v3/qualityprofile/test")
    out, _, notes = repair_empty_body_test_call([cmd], [])
    assert out == [cmd] and not notes


def test_checks_are_repaired_too():
    _, verify, notes = repair_empty_body_test_call([], [RADARR])
    assert "/testall" in verify[0] and notes


def test_frame_run_repairs_before_the_refusal_gate():
    import inspect
    from app.modules import supervised_runs as sr
    src = inspect.getsource(sr)
    assert "repair_empty_body_test_call(cmds, verify)" in src
    assert src.index("repair_empty_body_test_call(cmds, verify)") < src.index(
        "a_validating_endpoint_sent_an_empty_body(cmds")
