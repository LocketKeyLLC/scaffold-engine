"""§17.1376 — which machine runs a line is a property of the BLOCK, not the line.

Live, 2026-10-05: §17.1375 taught the drafter to send a request body through a
file, and ADD132's next draft did the whole job in Python — one `pct_exec` helper
wrapping `subprocess.run(["pct","exec",str(ctid),…])`, with `read_api_key`,
`api_get`, `api_put` and `api_post` built on it. The frame came back with THREE
refusals and `suggested: myself`:

    this line runs on the HOST and reaches port 7878 on loopback, and 7878
    belongs to radarr in guest 103 … or run the call inside the guest with
    `pct exec 103 -- …`

Every one of those calls was already inside the guest, by the very means the
refusal recommends. §17.1368 asked "does this line run on the host?" with a regex
for a literal `pct exec` beside the URL; §17.1358 asked the mirrored question the
same way and went blind instead of wrong. One resolver now answers for both.
"""
import json
import pathlib

import pytest

from app.modules.service_truth import (SOME_GUEST, THE_HOST, ServiceTruth,
                                       loopback_on_the_host,
                                       values_from_another_guest,
                                       where_each_line_runs)

FIX = pathlib.Path(__file__).parent / "fixtures"
LIVE = json.loads((FIX / "add132_python_helpers_2026_10_05.json").read_text())
BODY = LIVE["files"][0]["content"]

SERVICES = [
    ServiceTruth(guest="103", name="radarr", unit="radarr.service", state="active",
                 address="192.168.1.22", ports=("7878",), data_dir="/var/lib/radarr",
                 configs=("/var/lib/radarr/config.xml",)),
    ServiceTruth(guest="104", name="sonarr", unit="sonarr.service", state="active",
                 address="192.168.1.23", ports=("8989",), data_dir="/var/lib/sonarr",
                 configs=("/var/lib/sonarr/config.xml",)),
    ServiceTruth(guest="105", name="qbittorrent-nox", unit="qbittorrent-nox.service",
                 state="active", address="192.168.1.24", ports=("61661", "8080"),
                 data_dir="/var/lib/qbittorrent-nox"),
]


def _judge(fn, text):
    return fn([], [{"path": "/tmp/block.py", "content": text}], SERVICES)


# --------------------------------------------------------------- the live frame

def test_the_live_draft_is_no_longer_refused():
    """The three refusals the operator's job actually carried."""
    assert _judge(loopback_on_the_host, BODY) == []


def test_the_live_draft_was_refused_three_times_before():
    """The fixture records what the shipped gate did, so this cannot silently pass."""
    assert LIVE["refused_before"] == 3


def test_every_loopback_in_the_live_draft_resolves_to_a_guest():
    runs_on = where_each_line_runs(BODY)
    loopbacks = [i for i, ln in enumerate(BODY.split("\n"), 1) if "127.0.0.1" in ln]
    assert loopbacks, "the fixture must contain loopback lines"
    for i in loopbacks:
        assert runs_on.get(i, THE_HOST) != THE_HOST, BODY.split("\n")[i - 1]


def test_the_guest_is_named_when_the_call_names_it():
    """`pct_exec(105, …)` runs in 105; `pct_exec(ctid, …)` runs in a guest unnamed."""
    runs_on = where_each_line_runs(BODY)
    named = [runs_on[i] for i, ln in enumerate(BODY.split("\n"), 1)
             if "8080/api/v2/auth/login" in ln]
    assert named == ["105"], named


# ------------------------------------------------------- the helpers themselves

def test_a_caller_of_a_dispatcher_is_a_dispatcher():
    """Only `pct_exec` holds the literal argv; the other four reach the guest
    only by calling it, so membership is a fixpoint, not a scan."""
    from app.modules.service_truth import _dispatchers_in_python
    found = _dispatchers_in_python(BODY)
    for name in ("pct_exec", "read_api_key", "api_get", "api_put", "api_post"):
        assert name in found, (name, sorted(found))


def test_the_guest_parameter_is_found_by_name_not_position():
    from app.modules.service_truth import _guest_arg_index
    assert _guest_arg_index(["cmd", "ctid"]) == 1
    assert _guest_arg_index(["ctid", "port", "key"]) == 0
    assert _guest_arg_index(["vmid", "cmd"]) == 0
    assert _guest_arg_index(["port", "path"]) == 0      # fallback, nothing reads as a guest
    assert _guest_arg_index([]) is None


# ---------------------------------------------- the question asked without an AST

def test_a_block_inside_a_fence_still_gets_an_answer():
    """A model's answer is markdown around code, so `ast.parse` fails on most real
    text. Measured on the 318-trace corpus: the AST-only resolver left 10 traces
    of this same false positive standing, every one inside a fence."""
    fenced = "## Do this next\n\n```bash\ncat > /tmp/f.py <<'EOF'\n" + BODY + "\nEOF\n```\n"
    assert _judge(loopback_on_the_host, fenced) == []


def test_a_command_built_into_a_variable_flows_into_the_guest():
    """The corpus shape: the loopback is on the assignment, the dispatch a line later."""
    block = (
        "import subprocess\n"
        "def run_pct(ct, cmd):\n"
        "    return subprocess.run(['pct','exec',str(ct),'--','sh','-c',cmd])\n"
        "def get_clients(ct, port, key):\n"
        "    cmd = f\"curl -s -H 'X-Api-Key: {key}' http://127.0.0.1:{port}/api/v3/x\"\n"
        "    return run_pct(ct, cmd)\n"
    )
    assert _judge(loopback_on_the_host, block) == []


def test_a_variable_the_wrapper_itself_consumes_flows_too():
    """`['pct','exec',str(ct),'--','sh','-c', curl_cmd]` — the variable never
    passes through a call's parens, so the span rule alone cannot see it."""
    block = (
        "def run_pct(ct, port, path):\n"
        "    curl_cmd = \"curl -s\"\n"
        "    curl_cmd += f\" http://127.0.0.1:{port}{path}\"\n"
        "    cmd = ['pct', 'exec', str(ct), '--', 'sh', '-c', curl_cmd]\n"
        "    return subprocess.run(cmd)\n"
    )
    assert _judge(loopback_on_the_host, block) == []


def test_a_shell_function_dispatches_too():
    block = ("in_guest() {\n"
             "  pct exec \"$1\" -- sh -c \"$2\"\n"
             "}\n"
             "in_guest 103 'curl -s http://127.0.0.1:7878/api/v3/downloadclient'\n")
    assert _judge(loopback_on_the_host, block) == []


def test_a_shell_variable_the_wrapper_expands_flows_too():
    block = ("CMD=\"curl -s http://127.0.0.1:7878/api/v3/downloadclient\"\n"
             "pct exec 103 -- sh -c \"$CMD\"\n")
    assert _judge(loopback_on_the_host, block) == []


# ------------------------------------------------- the gate must still bite

def test_a_host_side_urllib_call_is_still_refused():
    """§17.1368's own defect, and the live shape of 4 corpus traces: the helper
    reaches the API with `urllib` FROM THE HOST and is no dispatcher at all."""
    block = (
        "import urllib.request\n"
        "def api(ctid, port, key, path):\n"
        "    url = f'http://127.0.0.1:{port}{path}'\n"
        "    return urllib.request.urlopen(urllib.request.Request(url)).read()\n"
        "api(103, 7878, k, '/api/v3/downloadclient')\n"
    )
    out = _judge(loopback_on_the_host, block)
    assert len(out) == 1, out
    assert "7878" in out[0]["why"] and "radarr" in out[0]["why"]
    assert "192.168.1.22" in out[0]["why"]


def test_a_bare_host_side_curl_is_still_refused():
    out = _judge(loopback_on_the_host,
                 "curl -s http://127.0.0.1:7878/api/v3/downloadclient/1\n")
    assert len(out) == 1, out


def test_a_helper_that_never_reaches_a_guest_is_not_a_dispatcher():
    from app.modules.service_truth import _dispatchers_in_python
    assert _dispatchers_in_python(
        "def helper(ctid, cmd):\n    return cmd.upper()\n") == {}


# ------------------------------------- the mirrored question, same resolver

def test_the_mirrored_gate_sees_a_crossing_through_a_helper():
    """§17.1358 found the guest from `pct exec N` on the line, so a helper-
    dispatched block was invisible to it: no refusal rather than a wrong one.
    Here sonarr's port is reached inside guest 103, where nothing of sonarr's is."""
    block = (
        "import subprocess\n"
        "def pct_exec(ctid, cmd):\n"
        "    return subprocess.run(['pct','exec',str(ctid),'--','sh','-c',cmd])\n"
        "def api_get(ctid, port, path):\n"
        "    return pct_exec(ctid, f'curl -s http://127.0.0.1:{port}{path}')\n"
        "api_get(103, 8989, '/api/v3/downloadclient')\n"
    )
    out = _judge(values_from_another_guest, block)
    assert len(out) == 1, out
    assert "guest 103" in out[0]["why"] and "sonarr" in out[0]["why"]
    assert "104" in out[0]["why"]


def test_the_mirrored_gate_stays_silent_when_the_guest_is_unknown():
    """`api_get(ctid, 8989, …)` names no guest, so no claim can be made. A gate
    that guessed here would refuse the live draft, which is exactly what §17.1376
    exists to stop."""
    assert _judge(values_from_another_guest, BODY) == []


def test_an_unnamed_guest_is_not_the_host():
    runs_on = where_each_line_runs(
        "def pct_exec(ct, cmd):\n"
        "    return subprocess.run(['pct','exec',str(ct),'--','sh','-c',cmd])\n"
        "pct_exec(ctid, 'curl http://127.0.0.1:7878/')\n")
    assert runs_on.get(3) == SOME_GUEST


# --------------------------------------------------- one resolver, not two

@pytest.mark.parametrize("fn", ["loopback_on_the_host", "values_from_another_guest"])
def test_both_gates_ask_the_resolver(fn):
    """Each gate that asked this question with its own line regex got a different
    answer; a second implementation is how that returns ([[feedback_sibling_call_sites_drift]])."""
    import inspect

    import app.modules.service_truth as st
    src = inspect.getsource(getattr(st, fn))
    assert "where_each_line_runs(t)" in src, fn


def test_no_gate_judges_the_machine_with_its_own_line_regex():
    """The resolver is the only reader of the wrapper patterns."""
    import pathlib as _p
    src = (_p.Path(__file__).parents[1] / "app" / "modules" / "service_truth.py").read_text()
    body = src.split("def loopback_on_the_host", 1)[1]
    assert "_WRAPPED_RE.search(ln)" not in body, \
        "loopback_on_the_host must ask where_each_line_runs, not the line"
