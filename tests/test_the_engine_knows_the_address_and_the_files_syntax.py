"""§17.1401 — a literal address maps to its guest, and a quoted heredoc is the file's syntax.

Live, 2026-10-06. ADD137 put the control panel's key on VM 106 — proven off the
machine: `pct exec 111 -- ssh … aedefruscio@192.168.1.106 hostname` answered
`palworld-server`. The very next step, ADD122 (a backend in CT 111 that ssh's to
192.168.1.106), was handed back to the operator behind two refusals:

1. "Nothing has given that service a credential there". `_guest_for_target` maps a
   literal IP to a guest through `inventory["addresses"]`, which `read_inventory`
   never filled — so the "a finished step put a key there" skip could not fire for
   ANY file that names an address. The engine held `PALWORLD_IP = 192.168.1.106`
   in its own substitutions; the draft wrote it.
2. "`$PALWORLD_USER` is read here and nothing sets it". It sat in
   `` `${PALWORLD_USER}@${PALWORLD_IP}` `` — a JavaScript template literal inside
   `cat > routes/palworld-settings.js <<'EOF'`. The shell never expands it.
"""
from __future__ import annotations

import json
import pathlib

import pytest

from app.modules import runbook_preconditions as rp
from app.modules import supervised_runs as sr
from app.modules.runbook_preconditions import addresses_the_engine_holds
from app.modules.supervised_runs import variables_nothing_sets, without_quoted_data_heredocs

FIX = pathlib.Path(__file__).parent / "fixtures"
LIVE = json.loads((FIX / "add122_runtime_ssh_and_js_template_2026_10_06.json").read_text())
NAMES = {"101": "jellyfin", "102": "prowlarr", "106": "palworld-server", "110": "ai-vm", "111": "control-panel"}
#: the job's own substitutions (2026-10-06), the *_IP ones
SUBS = {"SWITCH_IP": "192.168.1.3", "JELLYFIN_IP": "192.168.1.20", "PALWORLD_IP": "192.168.1.106",
        "PROWLARR_IP": "192.168.1.21", "MEDIA_VLAN_IP": "10.10.20.1", "TITLE": "Sintel"}
PLAN = [{"node_key": "ADD137", "status": "done",
         "title": "Give the control panel an ssh key it can use on VM 106 (palworld-server)"}]
HELD = {"held": ["MASS_PASSWORD"]}


# ── 1. the address ───────────────────────────────────────────────────────────

def test_the_engines_bindings_map_to_guests():
    assert addresses_the_engine_holds(SUBS, NAMES) == {
        "106": ["192.168.1.106"], "101": ["192.168.1.20"], "102": ["192.168.1.21"]}


def test_a_binding_naming_no_guest_or_no_address_is_ignored():
    out = addresses_the_engine_holds({"SWITCH_IP": "192.168.1.3", "TITLE_IP": "Sintel"}, NAMES)
    assert out == {}


def test_an_ambiguous_word_names_no_guest():
    assert addresses_the_engine_holds({"MEDIA_IP": "10.0.0.5"}, {"1": "media-a", "2": "media-b"}) == {}


@pytest.mark.asyncio
async def test_the_live_add122_is_not_refused_for_a_credential_add137_installed():
    inv = {"cts": {"111": "running"}, "vms": {"106": "running"}, "names": NAMES,
           "addresses": addresses_the_engine_holds(SUBS, NAMES)}
    out = await rp.unmet(LIVE["commands"], None, files=LIVE["files"], node=LIVE, inventory=inv, plan=PLAN)
    assert not [r for r in out if "Nothing has given that service a credential" in r["why"]], out


@pytest.mark.asyncio
async def test_without_the_finished_key_step_it_is_still_refused():
    """Vacuity guard: the rule still bites when nothing put a key there."""
    inv = {"cts": {"111": "running"}, "vms": {"106": "running"}, "names": NAMES,
           "addresses": addresses_the_engine_holds(SUBS, NAMES)}
    out = await rp.unmet(LIVE["commands"], None, files=LIVE["files"], node=LIVE, inventory=inv, plan=[])
    assert [r for r in out if "Nothing has given that service a credential" in r["why"]]


def test_the_inventory_gets_the_addresses_where_drafting_reads_it():
    import inspect
    from app.modules import execution_agent
    src = inspect.getsource(execution_agent)
    assert '_inv["addresses"] = addresses_the_engine_holds(' in src


# ── 2. the file's syntax ─────────────────────────────────────────────────────

def test_the_live_js_template_literal_is_not_a_shell_reference():
    out = variables_nothing_sets(LIVE["commands"], LIVE["files"], HELD)
    assert not [r for r in out if "`$PALWORLD_USER`" in r["why"]], [r["why"][:90] for r in out]


def test_a_data_heredoc_body_is_removed_and_a_shell_one_kept():
    text = ("cat > /tmp/r.sh <<'REMOTE'\n"
            "echo \"$GUEST_VAR\"\n"
            "cat > /opt/a.js <<'EOF'\n"
            "const u = `${USER_IN_JS}`;\n"
            "\n"                                  # a blank line must not end the body
            "const v = `${ALSO_JS}`;\n"
            "EOF\n"
            "echo after\n"
            "REMOTE\n")
    out = without_quoted_data_heredocs(text)
    assert "$GUEST_VAR" in out, "a shell script's body is still judged"
    assert "USER_IN_JS" not in out and "ALSO_JS" not in out
    assert "echo after" in out


@pytest.mark.parametrize("body", [
    'cat > /etc/x.conf <<EOF\nuser=$NOPE\nEOF\n',            # UNQUOTED: the shell expands it
    "cat > /tmp/run.sh <<'EOF'\necho $NOPE\nEOF\n",          # a shell script: expanded when it runs
])
def test_what_is_still_judged(body):
    out = variables_nothing_sets([body], [], HELD)
    assert [r for r in out if "`$NOPE`" in r["why"]], body


@pytest.mark.parametrize("body", [
    "cat > /opt/a.js <<'EOF'\nconst x = `${NOPE}`;\nEOF\n",
    'cat > /etc/systemd/system/x.service <<"EOF"\nExecReload=/bin/kill -HUP ${NOPE}\nEOF\n',
    "tee /opt/app.py <<\\EOF\nprint(f'{NOPE}')\nx = '$NOPE'\nEOF\n",
])
def test_a_data_files_own_syntax_is_left_alone(body):
    assert [r for r in variables_nothing_sets([body], [], HELD) if "`$NOPE`" in r["why"]] == []
