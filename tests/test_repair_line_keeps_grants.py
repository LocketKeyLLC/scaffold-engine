"""§17.1204 — the line that repairs a runner must neither disarm it nor arm it.

`--install` replaces the service outright. Every grant a runner has is a flag on
that line: the write list, the root-read list, the values file. A repair line
that carries only `--port` and `--token` is therefore a revocation dressed as a
fix, and it is the line the engine hands the operator at the exact moment
something is already wrong.

Live shape this is built from: a Proxmox host running helper v15, trusted with
`--write-allow ANY` and root, probed by the engine, told "refresh it with the
same one line as the install" — a line with no `--write-allow` on it at all.
Running it would have picked up the read fix and closed the write channel in the
same command, and the blocked job would have gone backwards.

The mirror of that is just as bad and is why the runner's own answer is the
authority here: the base `local_runner` recipe installs with NO `--write-allow`,
so a read-only runner is the DEFAULT. Filling its repair line from the open plan
hands it `lvremove` and `qm stop` under a sentence saying nothing changed.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules import assist_supervised as sw
from app.modules import engine_setup as es
from app.modules.assist_supervised import ANY

BASE = {"host": "192.168.1.156", "port": 8790, "token": "tok123",
        "script_url": "https://example.invalid/local_runner_mcp.py"}

#: What this operator's open plan needs — present on every real probe, so every
#: case below carries it. The point of most of these tests is that it does NOT
#: reach the line when the runner answered for itself.
PLAN = {"allow": ["qm set", "lvremove"], "sudo_allow": ["qm config"]}


def _setup(**over) -> dict:
    """The shape `assist_supervised.runner_setup` returns. `sudo_allow` and
    `secrets_file` are None for v15/v16 — "did not say", not "has none"."""
    return {"allow": [], "sudo": False, "helper": "17", "secrets": [],
            "sudo_allow": None, "secrets_file": None, **over}


def _stale(**checks) -> str:
    return es.runner_repair_block({"class": "stale_helper", "checks": {**BASE, "plan_grants": PLAN, **checks},
                                   "detail": "the helper running there is version 15"})


def _token(**checks) -> str:
    return es.runner_repair_block({"class": "token", "checks": {**BASE, "plan_grants": PLAN, **checks},
                                   "detail": "rejected the token"})


# ── carrying a runner's own grants forward ────────────────────────────────

def test_a_refresh_keeps_the_machine_trusted():
    """The operator's case exactly: ANY plus root, refreshing for a read fix."""
    out = _stale(runner_setup=_setup(allow=[ANY], sudo=True, helper="15"))
    assert f'--write-allow "{ANY}"' in out, out
    assert "--token tok123" in out
    assert "stays trusted with whatever you approve" in out


def test_a_refresh_keeps_an_enumerated_list_and_its_read_grant():
    out = _stale(runner_setup=_setup(allow=["qm set", "pct set"], sudo=True,
                                     sudo_allow=["qm config", "pct status"]))
    for flag in ('--write-allow "qm set" "pct set"', '--sudo-allow "qm config" "pct status"'):
        assert flag in out, out
    assert "stay allowed" in out and "root-read grant(s) stay" in out


def test_trusted_mode_does_not_also_enumerate_reads():
    """ANY writes `NOPASSWD: ALL`, so a read list beside it is noise that can
    only disagree with itself (§17.1202)."""
    out = _stale(runner_setup=_setup(allow=[ANY], sudo=True, sudo_allow=[ANY, "qm config"]))
    assert "--sudo-allow" not in out, out


def test_a_values_file_stays_at_its_own_path():
    """The path the runner reports, not the recipe's default — rewriting it
    points the service at a file that may not exist."""
    out = _stale(runner_setup=_setup(allow=["qm set"], sudo=True, secrets=["PVE_ROOT_PASSWORD"],
                                     secrets_file="/opt/custom/values.env"))
    assert "--secrets-file /opt/custom/values.env" in out, out
    assert es.RUNNER_SECRETS_PATH not in out, out


def test_an_older_helper_that_reports_no_path_still_keeps_its_values_file():
    """v15/v16 report the NAMES but not the path. Names present means a file is
    wired in, and the recipe's path is the one that wired it."""
    out = _stale(runner_setup=_setup(allow=["qm set"], sudo=True, secrets=["PVE_ROOT_PASSWORD"], helper="15"))
    assert f"--secrets-file {es.RUNNER_SECRETS_PATH}" in out, out


# ── NOT arming a runner that was left read-only ───────────────────────────

def test_a_read_only_runner_is_not_handed_a_write_channel():
    """The default install has no `--write-allow`. "It holds nothing" is an
    ANSWER, and the plan must not overwrite it — otherwise the one line the
    engine offers to fix a stale helper also grants `lvremove`."""
    out = _stale(runner_setup=_setup(allow=[], sudo_allow=[]))
    assert "--write-allow" not in out, out
    assert "lvremove" not in out, out
    assert "the installer replaces the running service" in out


def test_a_read_only_runner_still_gets_the_read_grant_it_reported():
    out = _stale(runner_setup=_setup(allow=[], sudo_allow=["qm config", "pct status"]))
    assert "--write-allow" not in out, out
    assert '--sudo-allow "qm config" "pct status"' in out, out


def test_an_older_helper_that_cannot_report_reads_falls_back_to_the_plan():
    """v15/v16 say nothing about `--sudo-allow`, and dropping it is the §17.1198
    harm. The plan fills in — and the phrase must not call that "kept"."""
    out = _stale(runner_setup=_setup(allow=["qm set"], sudo=True, sudo_allow=None))
    assert '--sudo-allow "qm config"' in out, out
    assert "root-read grant(s) stay" not in out, out
    assert "gains the 1 root-read grant(s) your plan needs" in out, out


# ── when the runner answered nothing at all ───────────────────────────────

def test_a_rejected_token_falls_back_to_the_plan_and_says_it_is_adding():
    """A `token` failure answers nothing about the machine, so the best the
    engine has is the plan. Claiming those prefixes are "already allowed" is
    how an escalation talks an operator into running it."""
    out = _token(runner_setup=None)
    assert '--write-allow "qm set" "lvremove"' in out and '--sudo-allow "qm config"' in out, out
    assert "already allows" not in out, out
    assert "gains the 2 write prefix(es) your plan needs" in out, out


def test_nothing_known_and_nothing_needed_gets_the_plain_line():
    out = _stale(runner_setup=None, plan_grants={})
    assert "--install" in out and "--write-allow" not in out and "--sudo-allow" not in out, out
    assert "the installer replaces the running service" in out


# ── the seam: what the helper reports must actually reach the line ────────

@pytest.mark.asyncio
async def test_the_helpers_reported_grants_survive_the_engines_policy_read():
    """The bug this file was written for was HERE, not in the builder: the
    helper reported `sudo_allow` and `secrets_file` and `write_policy` parsed
    neither, so bumping the helper to v17 bought nothing and the read grant was
    rebuilt from the plan every time. Drive the real functions.
    """
    spec = MagicMock(); spec.name = "seam-runner"; spec.headers = {"X-Runner-Token": "tok123"}
    reported = {"helper": "17", "allow": ["qm set"], "sudo": True, "max_ttl": 900,
                "sudo_allow": ["qm config", "pct status"], "secrets_file": "/opt/custom/values.env",
                "secrets": ["PVE_ROOT_PASSWORD"]}
    res = MagicMock(); res.structured = None; res.text = json.dumps(reported)
    sw.clear_policy_cache()
    with patch("app.modules.mcp_client.list_tools",
               new=AsyncMock(return_value=[{"name": "run_supervised"}, {"name": "write_policy"}])), \
         patch("app.modules.mcp_client.call_tool", new=AsyncMock(return_value=res)):
        setup = await sw.runner_setup(spec, use_cache=False)
    assert setup["sudo_allow"] == ["qm config", "pct status"]
    assert setup["secrets_file"] == "/opt/custom/values.env"
    line, kept = es.repair_install_line({**BASE, "plan_grants": PLAN, "runner_setup": setup})
    assert '--sudo-allow "qm config" "pct status"' in line, line
    assert "--secrets-file /opt/custom/values.env" in line, line
    assert "stay" in kept and "gains" not in kept, kept


@pytest.mark.asyncio
async def test_write_policy_keeps_its_own_contract():
    """`write_policy` gates whether a block may run at all, and None there means
    "no write channel". Widening the setup read must not widen that verdict, or
    a read-only runner starts accepting approved blocks."""
    spec = MagicMock(); spec.name = "contract-runner"
    res = MagicMock(); res.structured = None
    res.text = json.dumps({"helper": "17", "allow": [], "sudo": False, "sudo_allow": ["qm config"]})
    sw.clear_policy_cache()
    with patch("app.modules.mcp_client.list_tools",
               new=AsyncMock(return_value=[{"name": "run_supervised"}, {"name": "write_policy"}])), \
         patch("app.modules.mcp_client.call_tool", new=AsyncMock(return_value=res)):
        assert await sw.write_policy(spec, use_cache=False) is None      # no write channel
        setup = await sw.runner_setup(spec)                              # but it DID answer
    assert setup is not None and setup["allow"] == [] and setup["sudo_allow"] == ["qm config"]


@pytest.mark.asyncio
async def test_a_runner_that_cannot_be_asked_is_not_a_runner_that_holds_nothing():
    """The distinction the whole fix rests on. A failed call is None; an empty
    answer is a dict. Read the first as the second and the plan takes over."""
    spec = MagicMock(); spec.name = "silent-runner"
    sw.clear_policy_cache()
    with patch("app.modules.mcp_client.list_tools", new=AsyncMock(side_effect=RuntimeError("connection reset"))):
        assert await sw.runner_setup(spec, use_cache=False) is None


def test_the_probe_records_the_full_answer_and_only_when_it_asked():
    """`checks["writes"]` is None for both cases, so the builder cannot use it.
    The probe has to record the setup separately, and only for the classes it
    actually put the question to."""
    import inspect
    src = inspect.getsource(es.probe_local_runner)
    assert 'diag["checks"]["runner_setup"]' in src, src
    assert 'runner_setup(spec) if _asked else None' in src, src
    assert '("ok", "stale_helper")' in src, src


# ── one builder, and a helper that reports what it reads ──────────────────

@pytest.mark.parametrize("cls", ["stale_helper", "token"])
def test_neither_repair_class_hand_writes_its_own_install_line(cls):
    """Both were hand-written, and both drifted from `install_line` the moment
    a flag was added to it. One builder or they disagree again."""
    import inspect
    src = inspect.getsource(es.runner_repair_block)
    body = src.split(f'if cls == "{cls}":', 1)[1].split("if cls ==", 1)[0]
    assert "repair_install_line(ch)" in body, body
    assert "local_runner_mcp.py --install" not in body, body


def test_the_helper_reports_the_grants_this_builder_reads():
    """Byte-level: the engine's fallback exists because older helpers do not
    report these. The current helper must."""
    from pathlib import Path
    src = (Path(es.__file__).resolve().parents[2] / "scripts" / "local_runner_mcp.py").read_text()
    policy = src.split("async def write_policy()", 1)[1].split("@mcp.tool", 1)[0]
    assert '"sudo_allow"' in policy and '"secrets_file"' in policy, policy


def test_the_stale_detail_does_not_diagnose_a_cause_it_cannot_know():
    """The gate is build EQUALITY, so it fires for a bump that changed only what
    the helper reports (v16 → v17 did). Asserting "it refuses read-only commands
    the engine now allows" is then simply untrue."""
    import inspect
    src = inspect.getsource(es.diagnose_runner_path)
    assert "it refuses read-only commands the engine now allows" not in src, src
    assert "not the same build" in src and "can refuse" in src, src


# ── the port the runner is registered on ──────────────────────────────────

def test_the_install_line_uses_the_registered_port_not_the_default():
    """`diagnose_runner_path` quotes the registered port in its detail ("nothing
    is listening on port 9001 there"). A repair line reading `--port 8790` under
    that sentence reinstalls the helper where the engine is not looking."""
    base = {"token": "T", "script_url": "https://x.invalid/s.py"}
    assert "--port 9001 " in es.install_line({**base, "port": 9001})          # checks' spelling
    assert "--port 8791 " in es.install_line({**base, "runner_port": "8791"})  # recipe ctx' spelling
    assert f"--port {es.RUNNER_PORT} " in es.install_line(base)                # neither known


def test_the_repair_line_agrees_with_the_port_the_diagnosis_named():
    out = es.runner_repair_block({
        "class": "stale_helper", "detail": "nothing is listening on port 9001",
        "checks": {"host": "10.0.0.9", "port": 9001, "token": "T",
                   "script_url": "https://x.invalid/s.py", "runner_setup": _setup(allow=["qm set"])}})
    assert "--port 9001" in out and f"--port {es.RUNNER_PORT}" not in out, out


# ── the connection page builds the same kind of line ──────────────────────

async def _page(*, secrets, setup, policy, reads, prefixes, endpoint="http://10.0.0.9:9001/mcp/"):
    """Drive the real `GET /setup/machines` body with only its lookups stubbed."""
    import app.routers.machines as m
    spec = MagicMock(); spec.name = "pve-runner"; spec.endpoint = endpoint; spec.enabled = True
    with patch("app.modules.assist_local_runner.runner_spec", new=AsyncMock(return_value=spec)), \
         patch("app.modules.assist_supervised.cached_policy", return_value=(True, policy)), \
         patch("app.modules.assist_supervised.cached_setup", return_value=(True, setup)), \
         patch("app.modules.engine_setup.recipe_context",
               new=AsyncMock(return_value={"runner_port": "8790", "token": "T",
                                           "script_url": "https://x.invalid/s.py"})), \
         patch("app.modules.engine_setup.prefixes_needed", new=AsyncMock(return_value=prefixes)), \
         patch("app.modules.engine_setup.read_prefixes_needed", new=AsyncMock(return_value=reads)), \
         patch("app.modules.runner_secrets.list_secrets", new=AsyncMock(return_value=secrets)):
        return await m.list_machines(db=MagicMock())


@pytest.mark.asyncio
async def test_the_connection_page_keeps_the_values_file_wired_in():
    """Both lines REPLACE the service. Either one without `--secrets-file`
    unwires the file the runner resolves `$NAME` from, and every command
    carrying a value starts failing on a machine the operator just "fixed"."""
    out = await _page(secrets=[{"name": "PVE_ROOT_PASSWORD"}],
                      setup=_setup(allow=["qm set"], secrets=["PVE_ROOT_PASSWORD"],
                                   secrets_file="/opt/custom/values.env"),
                      policy={"allow": ["qm set"], "sudo": True, "helper": "17", "secrets": []},
                      reads=[{"prefix": "qm config", "why": "x"}], prefixes=[{"prefix": "qm set", "why": "x"}])
    for key in ("install", "install_trusted"):
        assert "--secrets-file /opt/custom/values.env" in out[key], (key, out[key])


@pytest.mark.asyncio
async def test_the_connection_page_uses_the_runners_own_port():
    out = await _page(secrets=[], setup=_setup(), policy=None, reads=[], prefixes=[])
    assert out["runner"]["port"] == 9001
    for key in ("install", "install_trusted"):
        assert "--port 9001" in out[key], (key, out[key])


@pytest.mark.asyncio
async def test_the_connection_page_keeps_a_read_grant_the_plan_stopped_naming():
    """§17.1198's union was written for the write list only. The READ list is
    replaced by the same `--install`, so it needed the same union: a runner
    granted `pvesm status` by hand lost it the moment the plan moved on."""
    out = await _page(secrets=[], policy={"allow": ["qm set"], "sudo": True, "helper": "17", "secrets": []},
                      setup=_setup(allow=["qm set"], sudo=True, sudo_allow=["qm config", "pvesm status"]),
                      reads=[{"prefix": "qm config", "why": "a check in this plan reads through /etc/pve"}],
                      prefixes=[{"prefix": "qm set", "why": "x"}])
    assert "pvesm status" in [p["prefix"] for p in out["needed_read_prefixes"]], out["needed_read_prefixes"]
    assert '--sudo-allow "qm config" "pvesm status"' in out["install"], out["install"]


@pytest.mark.asyncio
async def test_no_values_no_secrets_flag():
    """The flag only when there is something to point at — an empty store must
    not wire the service to a file nobody wrote."""
    out = await _page(secrets=[], setup=_setup(allow=["qm set"]), policy=None,
                      reads=[], prefixes=[{"prefix": "qm set", "why": "x"}])
    assert "--secrets-file" not in out["install"] and "--secrets-file" not in out["install_trusted"]


@pytest.mark.asyncio
async def test_the_read_union_does_not_put_any_on_the_enumerated_line():
    """A TRUSTED runner reports `sudo_allow: ["ANY"]` — §17.1202 derives it from
    the write grant, it is not a prefix anyone typed. Unioning it verbatim puts
    `--sudo-allow "ANY"` on the line whose entire purpose is to be the
    alternative to trusting the machine."""
    out = await _page(secrets=[], policy={"allow": [ANY], "sudo": True, "helper": "17", "secrets": []},
                      setup=_setup(allow=[ANY], sudo=True, sudo_allow=[ANY]),
                      reads=[{"prefix": "qm config", "why": "x"}], prefixes=[{"prefix": "qm set", "why": "x"}])
    assert ANY not in [p["prefix"] for p in out["needed_read_prefixes"]], out["needed_read_prefixes"]
    assert f'--sudo-allow "{ANY}"' not in out["install"], out["install"]
    assert '--sudo-allow "qm config"' in out["install"], out["install"]
