r"""§17.1360 — the work wrote into a config the file has no concept of.

Live, 2026-10-04. ADD132's fourth frame arrived with `refused: []` and Run
suggested (because §17.1359 had the whole precondition layer throwing). Its
qBittorrent half was right. Its *arr half, for each app:

    pct exec 103 -- systemctl stop radarr.service
    pct exec 103 -- python3 …   # insert <DownloadClientConfig>… into /var/lib/radarr/config.xml
    pct exec 103 -- systemctl start radarr.service

while its own check read `http://127.0.0.1:7878/api/v3/downloadclient`.

Measured on the machine:

    grep -c -i downloadclient /var/lib/radarr/config.xml   ->  0
    elements in that file: ApiKey AuthenticationMethod AuthenticationRequired
      BindAddress Branch Config EnableSsl InstanceName LaunchBrowser LogLevel
      Port SslCertPassword SslCertPath SslPort UrlBase
    /var/lib/radarr/radarr.db                              ->  CREATE TABLE "DownloadClients"
    /api/v3/downloadclient                                 ->  200

Download clients live in the database, behind the API. The block would have
bounced both services, polluted two configs, registered nothing — and failed its
own check. What pushed it there is the engine's own fact line, *"config … (the
service rewrites it: stop it before editing)"*, which reads as "this is where you
configure it".

The check is the authority: §17.1345 already forces it to read the real result.
"""
from __future__ import annotations

import json
import pathlib

import pytest

from app.modules import runbook_preconditions as rp
from app.modules.service_truth import ServiceTruth

FRAME = json.loads((pathlib.Path(__file__).parent / "fixtures"
                    / "add132_wrong_surface_2026_10_04.json").read_text())
RADARR = ServiceTruth(guest="103", unit="radarr.service", name="radarr", state="active",
                      ports=("7878",), data_dir="/var/lib/radarr",
                      configs=("/var/lib/radarr/config.xml",))
QBIT = ServiceTruth(guest="105", unit="qbittorrent-nox.service", name="qbittorrent-nox",
                    state="active", ports=("61661", "8080"),
                    data_dir="/var/lib/qbittorrent-nox",
                    configs=("/var/lib/qbittorrent-nox/.config/qBittorrent/qBittorrent-data.conf",))


class _Spec:
    endpoint = "http://192.168.1.156:8790/mcp/"


class _Res:
    def __init__(self, text, is_error=False):
        self.structured, self.text, self.is_error = None, text, is_error


def _transport(monkeypatch, answer):
    seen: list[str] = []

    async def fake_call_tool(spec, tool, args, **kw):
        seen.append(args["command"])
        return _Res(answer(args["command"]))

    import app.modules.mcp_client as mc
    monkeypatch.setattr(mc, "call_tool", fake_call_tool)
    return seen


# ------------------------------------------------- what the checks name


def test_the_api_name_comes_off_the_checks():
    got = rp._api_names_the_checks_read(FRAME["verify"])
    assert got == {"7878": {"downloadclient"}, "8989": {"downloadclient"}}


def test_the_api_and_version_prefix_are_not_the_name():
    got = rp._api_names_the_checks_read(
        ["curl http://127.0.0.1:8080/api/v2/app/preferences"])
    assert got == {"8080": {"preferences"}}


def test_a_check_with_no_url_names_nothing():
    assert rp._api_names_the_checks_read(["pct exec 105 -- systemctl is-active x"]) == {}
    assert rp._api_names_the_checks_read([]) == {}


# ----------------------------------------------------------- the rule


@pytest.mark.asyncio
async def test_the_live_frame_is_refused_against_the_measured_config(monkeypatch):
    """0 matches of `downloadclient` in the file the block writes."""
    seen = _transport(monkeypatch, lambda cmd: "0\n")
    texts = [f["content"] for f in FRAME["files"]]
    out = await rp.writes_where_the_check_does_not_read(
        _Spec(), texts, FRAME["verify"], [RADARR, QBIT])
    assert len(out) == 1, out
    why = out[0]["why"]
    assert "reads `downloadclient` from radarr's API on port 7878" in why
    assert "/var/lib/radarr/config.xml" in why and "0 matches" in why
    assert "Do the write through the same API the check reads" in why
    assert any("grep -c -i -F" in c and "config.xml" in c for c in seen), seen


@pytest.mark.asyncio
async def test_a_config_that_does_know_the_setting_is_left_alone(monkeypatch):
    """qBittorrent's conf really does hold `WebUI\\Password_PBKDF2`, so writing it
    there is right — and this rule must never say otherwise."""
    _transport(monkeypatch, lambda cmd: "3\n")
    texts = [f["content"] for f in FRAME["files"]]
    assert await rp.writes_where_the_check_does_not_read(
        _Spec(), texts, FRAME["verify"], [RADARR, QBIT]) == []


@pytest.mark.asyncio
async def test_an_unreadable_config_is_not_judged(monkeypatch):
    _transport(monkeypatch, lambda cmd: "grep: no such file\n")
    texts = [f["content"] for f in FRAME["files"]]
    assert await rp.writes_where_the_check_does_not_read(
        _Spec(), texts, FRAME["verify"], [RADARR, QBIT]) == []


@pytest.mark.asyncio
async def test_a_block_that_does_not_touch_the_config_is_not_judged(monkeypatch):
    seen = _transport(monkeypatch, lambda cmd: "0\n")
    api_only = ["pct exec 103 -- sh -c 'curl -X POST -d @/tmp/dc.json "
                "http://127.0.0.1:7878/api/v3/downloadclient'"]
    assert await rp.writes_where_the_check_does_not_read(
        _Spec(), api_only, FRAME["verify"], [RADARR]) == []
    assert seen == [], "nothing to read when the config is not written"


@pytest.mark.asyncio
async def test_no_check_or_no_services_judges_nothing(monkeypatch):
    _transport(monkeypatch, lambda cmd: "0\n")
    texts = [f["content"] for f in FRAME["files"]]
    assert await rp.writes_where_the_check_does_not_read(_Spec(), texts, [], [RADARR]) == []
    assert await rp.writes_where_the_check_does_not_read(_Spec(), texts, FRAME["verify"], []) == []
    assert await rp.writes_where_the_check_does_not_read(None, texts, FRAME["verify"], [RADARR]) == []


@pytest.mark.asyncio
async def test_one_judgment_per_config_and_name(monkeypatch):
    seen = _transport(monkeypatch, lambda cmd: "0\n")
    texts = [f["content"] for f in FRAME["files"]] * 2
    out = await rp.writes_where_the_check_does_not_read(
        _Spec(), texts, FRAME["verify"], [RADARR])
    assert len(out) == 1 and len(seen) == 1


def test_the_layer_runs_it():
    """Verify the lane: a rule `unmet` never calls is no rule."""
    import inspect
    src = inspect.getsource(rp.unmet)
    assert "writes_where_the_check_does_not_read(spec, texts, verify, services)" in src


def test_the_refusal_asks_the_drafter_again():
    """Verify the lane: a refusal whose text is not registered parks the frame."""
    from app.modules.supervised_runs import refusal_kinds
    refused = [{"why": "… Do the write through the same API the check reads."}]
    assert refusal_kinds({"refused": refused}) == {"same API the check reads"}
