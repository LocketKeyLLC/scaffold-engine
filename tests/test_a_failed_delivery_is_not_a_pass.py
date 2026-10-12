"""§17.1496 — a delivery that fails in the sandbox is never a pass, and the other develop seams.

- `_acceptance_refusal` returned [] whenever every acceptance request answered OK -- even
  when `deliver.sh` itself had exited non-zero and the sandbox had started the OLD code,
  whose existing routes answered. A developed step whose delivery dies (live: the
  runner-held credential no sandbox has) was parked as "rehearsed" without the version
  ever running.
- A developed step that produced nothing in every round parked with no commands AND no
  refusal: "Run" greyed out, nothing to read.
- The empty runbook of a developed step was rehearsed before any version existed.
- The staged copy that receives the real key sat with default mode in sticky /tmp.
- Measured paths went into `find`/`cat` unquoted.
"""
from __future__ import annotations

import inspect
import json
import pathlib
import re
from unittest.mock import AsyncMock, patch

import pytest

from app.modules import develop as dv
from app.modules import execution_agent as ea
from app.modules import rehearsal as rh
from app.modules import supervised_runs as sr

OK_ACCEPT = [{"ok": True, "status": 200, "method": "GET", "url": "http://127.0.0.1:3001/api/palworld-settings"}]
DEAD_DELIVERY = {"command": "bash /tmp/scaffold-dev-ADD127--deliver.sh", "exit": 1,
                 "out": "the runner holds no PANEL_PASSWORD -- store it in Settings -> Machines"}


def test_a_delivery_that_exited_is_refused_even_when_the_old_code_answered():
    report = {"accept": OK_ACCEPT, "commands": [DEAD_DELIVERY]}
    ref = rh._acceptance_refusal(report)
    assert len(ref) == 1 and "did not finish" in ref[0]["why"] and "exited 1" in ref[0]["why"]
    assert "the runner holds no PANEL_PASSWORD" in ref[0]["why"]
    assert "not this version" in ref[0]["why"]
    assert rh.score(report) == 1000, "never 0: a pass means the version ran"


def test_a_clean_delivery_with_every_request_answered_is_still_a_pass():
    report = {"accept": OK_ACCEPT, "commands": [dict(DEAD_DELIVERY, exit=0, out="")]}
    assert rh._acceptance_refusal(report) == [] and rh.score(report) == 0


def test_the_steps_own_request_failing_is_still_not_the_versions_fault():
    """The step's own `curl -f` against another machine's API fails in the sandbox by design."""
    report = {"accept": OK_ACCEPT,
              "commands": [{"command": "curl -f -s http://127.0.0.1:3001/api/palworld-settings", "exit": 22, "out": ""}]}
    assert rh._acceptance_refusal(report) == [] and rh.score(report) == 0


def test_eight_empty_rounds_park_with_a_refusal_that_says_why():
    ref = dv.nothing_refusal("- these paths are outside /opt/control-panel-backend and every directory the step names, "
                             "so the engine will not write them: /etc/nginx/nginx.conf")
    assert len(ref) == 1 and ref[0]["command"] == "(develop)"
    assert f"{dv.ROUNDS} rounds" in ref[0]["why"] and "/etc/nginx/nginx.conf" in ref[0]["why"]
    assert "Nothing ran" in ref[0]["why"]
    assert "no files" in dv.nothing_refusal("")[0]["why"]
    src = inspect.getsource(ea._pause_for_decision)
    i = src.index("develop_gave_nothing")
    assert "develop.nothing_refusal(evidence)" in src[i:i + 400], "the executor parks WITH the refusal"


def test_an_empty_runbook_is_not_rehearsed():
    src = inspect.getsource(ea._pause_for_decision)
    i = src.index("async def _rehearse_or_nothing(rb: str, files: list)")
    body = src[i:i + 700]
    assert 'if not (rb or "").strip() and not files:' in body and "return []" in body


FIX = pathlib.Path(__file__).parent / "fixtures"
ADD127 = json.loads((FIX / "add127_node_2026_10_08.json").read_text())
HOST = dv.Host(guest="111", vm=False, workdir="/opt/control-panel-backend", unit="control-panel.service")
MARK = dv.SECRET_MARK.format(name="PANEL_PASSWORD")
CFG = {"/opt/control-panel-backend/config/auth.json": '{"user": "panel", "password": "' + MARK + '"}\n'}


def test_the_staged_copy_is_owner_only_before_the_key_lands_in_it():
    creds = dv.credentials_for([], ["PANEL_PASSWORD"], ADD127)
    rb = dv.render_delivery(ADD127, HOST, CFG, dv.done_checks(ADD127, HOST), dv.acceptance_checks(ADD127), creds)
    sh = {f["path"]: f["content"] for f in sr.file_writes(rb)}["/tmp/scaffold-dev-ADD127--deliver.sh"]
    m = re.search(r"^chmod 600 (\S*auth\.json)$", sh, re.M)
    assert m, sh
    assert m.group(1).startswith("/tmp/"), "the STAGED copy on the host, not only the guest's file"
    assert sh.index("chmod 600 " + m.group(1)) < sh.index("SCAFFOLD_V="), "owner-only BEFORE the fill"


def test_a_measured_path_is_a_shell_word():
    assert rh.listing_command("pct exec 111 --", "/opt/control-panel-backend").startswith(
        "pct exec 111 -- find /opt/control-panel-backend -maxdepth 3"), "a plain path is untouched"
    assert "find '/opt/my app' -maxdepth 3" in rh.listing_command("pct exec 111 --", "/opt/my app")


@pytest.mark.asyncio
async def test_cat_quotes_the_path_it_was_given():
    from app.modules import service_truth as st
    seen = []

    async def probe(spec, cmd):
        seen.append(cmd)
        return True, "body"
    with patch.object(st, "_probe", new=AsyncMock(side_effect=probe)):
        assert await rh._cat(object(), "111", False, "/opt/my app/server.js") == "body"
        assert await rh._cat(object(), "111", False, "/opt/app/server.js") == "body"
    assert seen[0].endswith("cat '/opt/my app/server.js'") and seen[1].endswith("cat /opt/app/server.js")
