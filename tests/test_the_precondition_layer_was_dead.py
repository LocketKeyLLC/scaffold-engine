r"""§17.1359 — every machine-contradiction gate was dead, and one line said so.

Live, 2026-10-04, from the orchestrator's own log:

    preconditions_failed job=55f68b7f-2ced-4e13-a1bc-812320df56f8 node=ADD132
    err=TypeError("unmet() got an unexpected keyword argument 'services'")

`services=` was added to this call site in §17.1346 and never to `unmet`'s
signature. The call raised before `unmet` ran, `_pre_for`'s `except Exception`
turned that into `[]`, and so **every frame came back with an empty
`preconditions` list** — which is indistinguishable from "the machine
contradicts nothing".

What was off: the stopped guest (§17.1213), `pct` against a VM and `qm` against
a container, a guest with no OS, the missing ssh key (§17.1288g), the measured
address beating a written one (§17.1303), a guest that cannot resolve names
(§17.1313), §17.1343's sectioned-config rule and §17.1353's inert in-place edit
— the last of which was written, tested and merged THAT DAY and never ran once
against the live machines.

It is also why ADD132's fourth frame arrived with `refused: []` and Run
suggested while its *arr half wrote `<DownloadClientConfig>` into a file that
has no such concept (§17.1360).

Two things stop it recurring: the call is bound against the real signature in a
test, and a `TypeError` naming `unmet()` is re-raised instead of being filed as
blindness.
"""
from __future__ import annotations

import ast
import inspect
import pathlib
import re

import pytest

from app.modules import execution_agent as ea
from app.modules.runbook_preconditions import unmet


def _pre_for_source() -> str:
    src = pathlib.Path(ea.__file__).read_text(encoding="utf-8")
    i = src.index("async def _pre_for(")
    return src[i:src.index("frame = supervised_runs.frame_run(", i)]


def test_the_call_site_binds_against_the_real_signature():
    """The ratchet. Every keyword `_pre_for` passes must be one `unmet` accepts —
    checked by binding, so a new argument on either side cannot drift again."""
    body = _pre_for_source()
    call = re.search(r"_args = dict\((?P<kw>.*?)\)\n", body, re.S)
    assert call, body
    names = set(re.findall(r"(\w+)\s*=", call.group("kw")))
    assert names, call.group("kw")
    params = inspect.signature(unmet).parameters
    unknown = sorted(n for n in names if n not in params)
    assert unknown == [], f"_pre_for passes {unknown}, which unmet() does not accept"
    # and the positional pair is still right
    inspect.signature(unmet).bind(["cmd"], object(), **{n: None for n in names})


def test_services_and_verify_are_what_the_caller_passes():
    """Named explicitly, so removing one from `unmet` fails here rather than live."""
    params = inspect.signature(unmet).parameters
    for name in ("plan", "files", "node", "inventory", "truth", "services", "verify"):
        assert name in params, name
        assert params[name].kind is inspect.Parameter.KEYWORD_ONLY, name


def test_a_signature_mismatch_is_re_raised_not_filed_as_blindness():
    body = _pre_for_source()
    assert "except TypeError as exc:" in body
    assert 'if "unmet()" in str(exc):' in body
    assert "preconditions_signature_mismatch" in body
    assert "raise" in body
    # and the broad catch still exists for a genuinely unreadable host
    assert body.count("except Exception as exc:") == 1


def test_the_broad_catch_no_longer_wraps_the_call_arguments():
    """The arguments are built OUTSIDE the try, so a mistake in them is loud."""
    body = _pre_for_source()
    assert body.index("_args = dict(") < body.index("try:")


@pytest.mark.asyncio
async def test_unmet_accepts_the_live_call_and_returns_a_list():
    """With no channel it reads nothing and refuses nothing — but it RUNS."""
    out = await unmet(["pct exec 105 -- systemctl restart qbittorrent-nox"], None,
                      plan=[], files=[], node={"title": "x", "description": ""},
                      inventory=None, truth=None, services=[], verify=[])
    assert out == []


def test_every_gate_the_layer_owns_is_still_wired():
    """A roll-call, so the layer cannot be quietly emptied again."""
    src = inspect.getsource(unmet)
    for marker in ("key_known_for", "a_bare_append_lands_in_the_last_section",
                   "an_in_place_edit_the_file_cannot_match",
                   "writes_where_the_check_does_not_read"):
        assert marker in src, marker


def test_the_log_line_that_was_the_only_trace_is_still_distinct():
    """One warning was the whole signal. It now has a louder sibling."""
    body = _pre_for_source()
    assert "preconditions_failed" in body and "preconditions_signature_mismatch" in body
    tree = ast.parse(pathlib.Path(ea.__file__).read_text(encoding="utf-8"))
    assert tree is not None
