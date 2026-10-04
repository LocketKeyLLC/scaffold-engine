r"""§17.1355 — the block mistook the engine's own scaffolding for the work.

Live, 2026-10-04. ADD132's frame ("Make Radarr and Sonarr actually drive
qBittorrent, and prove the connection") parked with Run suggested. The whole of
what it would have run inside container 103:

    set -e
    export DEBIAN_FRONTEND=noninteractive
    MASS_PASSWORD="$MASS_PASSWORD" bash /tmp/in_ct_103.sh

`/tmp/in_ct_103.sh` is the wrapper the ENGINE writes to carry the step out, so
the step was told to run itself. Inside the guest that path does not exist, so
it fails; if it did exist it would recurse. There is no Radarr, Sonarr or
qBittorrent work in it at all — and every other gate reads the COMMANDS, which
are the template's own `bash /tmp/in_ct_103.sh start|wait|last` and perfectly
correct.

Its only check was

    pct exec 103 -- bash -c "ls -la /tmp/in_ct_103.sh"

— the same engine file, looked for on the wrong machine (the wrapper lives on
the host), whose existence says nothing about any of the three services.
§17.1345 demands a check and this satisfies it while confirming nothing: the
hole §17.1343 fell through, one level down.

Measured over every recorded step of the live job before shipping: 10 records
carry written files and neither rule fires on any of them.
"""
from __future__ import annotations

import json
import pathlib

from app.modules.supervised_runs import (a_check_that_only_reads_the_scaffolding,
                                         refusal_kinds,
                                         the_block_runs_its_own_scaffolding)

FRAME = json.loads((pathlib.Path(__file__).parent / "fixtures"
                    / "add132_frame_runs_itself_2026_10_04.json").read_text())
WRAPPER = "/tmp/in_ct_103.sh"


# ------------------------------------------------- a file that runs itself


def test_the_live_frame_is_refused_for_running_its_own_wrapper():
    out = the_block_runs_its_own_scaffolding(FRAME["files"])
    assert len(out) == 1, out
    assert out[0]["command"] == WRAPPER
    why = out[0]["why"]
    assert "RUNS ITSELF" in why and "would recurse" in why
    assert "not a call to the engine's own wrapper" in why


def test_the_templates_own_mentions_of_other_paths_are_not_self_reference():
    """The wrapper legitimately writes `…_remote.sh` and pushes it to
    `/root/.scaffold_step.sh`; only the file's OWN path counts."""
    body = FRAME["files"][0]["content"]
    assert "/tmp/in_ct_103_remote.sh" in body and "/root/.scaffold_step.sh" in body
    clean = body.replace('MASS_PASSWORD="$MASS_PASSWORD" bash /tmp/in_ct_103.sh',
                         'curl -s -X POST http://127.0.0.1:7878/api/v3/downloadclient')
    assert the_block_runs_its_own_scaffolding(
        [{"path": WRAPPER, "content": clean}]) == []


def test_each_shape_of_invocation_counts():
    for line in (f"bash {WRAPPER}", f"sh -x {WRAPPER}", f"source {WRAPPER}",
                 f". {WRAPPER}", f"{WRAPPER}", f"K=v bash {WRAPPER}",
                 f"true && bash {WRAPPER}"):
        out = the_block_runs_its_own_scaffolding(
            [{"path": WRAPPER, "content": f"set -e\n{line}\n"}])
        assert out, line


def test_merely_naming_the_path_is_not_running_it():
    """A comment, or a line that writes to it, is not an invocation."""
    for line in (f"# the wrapper is {WRAPPER}", f"echo done > {WRAPPER}.log",
                 f"ls -la {WRAPPER}"):
        assert the_block_runs_its_own_scaffolding(
            [{"path": WRAPPER, "content": f"set -e\n{line}\n"}]) == [], line


def test_a_file_with_real_work_is_left_alone():
    work = ("set -e\n"
            'curl -s -X POST -H "X-Api-Key: $RADARR_API_KEY" '
            'http://127.0.0.1:7878/api/v3/downloadclient -d @/tmp/dc.json\n')
    assert the_block_runs_its_own_scaffolding([{"path": WRAPPER, "content": work}]) == []


# ------------------------------------------- a check that reads only the scaffolding


def test_the_live_checks_only_target_the_engines_own_script():
    out = a_check_that_only_reads_the_scaffolding(FRAME["verify"], FRAME["files"])
    assert len(out) == 1, out
    assert out[0]["command"] == FRAME["verify"][0]
    why = out[0]["why"]
    assert "reads only the engine's OWN script" in why
    assert "lives on the\nhost" in why or "lives on the host" in why
    assert "Name a command that reads the RESULT" in why


def test_one_check_that_reads_something_else_is_enough():
    verify = [f'pct exec 103 -- ls -la {WRAPPER}',
              'pct exec 103 -- curl -s -o /dev/null -w "%{http_code}" '
              'http://192.168.1.105:8080/api/v2/app/version']
    assert a_check_that_only_reads_the_scaffolding(verify, FRAME["files"]) == []


def test_reading_a_config_the_block_wrote_is_a_real_check():
    """§17.1343 asks for exactly this, so it must not be refused."""
    files = [{"path": "/tmp/in_ct_105.sh", "content": "set -e\ntrue\n"}]
    verify = ['pct exec 105 -- grep -A2 "^\\[BitTorrent\\]" '
              '/var/lib/qbittorrent-nox/.config/qBittorrent/qBittorrent.conf']
    assert a_check_that_only_reads_the_scaffolding(verify, files) == []


def test_the_payload_path_and_the_pushed_path_count_as_scaffolding_too():
    files = [{"path": "/tmp/in_ct_103.sh", "content": "set -e\ntrue\n"}]
    for cmd in ('pct exec 103 -- ls /tmp/in_ct_103_remote.sh',
                'pct exec 103 -- cat /root/.scaffold_step.log',
                'pct exec 103 -- wc -c /tmp/in_vm_106.sh'):
        assert a_check_that_only_reads_the_scaffolding([cmd], files), cmd


def test_a_check_with_no_path_at_all_is_not_this_rules_business():
    """`systemctl is-active radarr` names no path; §17.1288's rules judge it."""
    files = [{"path": WRAPPER, "content": "set -e\ntrue\n"}]
    assert a_check_that_only_reads_the_scaffolding(
        ["pct exec 103 -- systemctl is-active radarr"], files) == []


def test_no_check_is_left_to_the_rule_that_owns_it():
    """§17.1345 refuses an empty check; this rule must not double up."""
    assert a_check_that_only_reads_the_scaffolding([], FRAME["files"]) == []


# ----------------------------------------------------------- the redraft lane


def test_both_refusals_ask_the_drafter_again():
    """Verify the lane: a refusal whose text is not in `_SHAPE_REFUSALS` parks
    the frame instead of redrafting it."""
    refused = (the_block_runs_its_own_scaffolding(FRAME["files"])
               + a_check_that_only_reads_the_scaffolding(FRAME["verify"], FRAME["files"]))
    assert len(refused) == 2
    assert refusal_kinds({"refused": refused}) == {
        "this file RUNS ITSELF", "reads only the engine's OWN script"}


def test_the_framer_runs_both_gates():
    """A gate the framer never calls is no gate."""
    import inspect

    from app.modules import supervised_runs as sr
    src = inspect.getsource(sr.frame_run)
    assert "the_block_runs_its_own_scaffolding(shape_files)" in src
    assert "a_check_that_only_reads_the_scaffolding(verify, shape_files)" in src


def test_the_fixture_carries_no_secret_value():
    """The frame the engine built had the password inlined; the fixture keeps
    the NAME only."""
    body = FRAME["files"][0]["content"]
    assert 'MASS_PASSWORD="$MASS_PASSWORD"' in body


# ------------------------------------------- the slot the engine fills itself


def test_both_drafter_slots_say_the_wrapper_is_the_engines():
    """§17.1355 root cause. The payload the model wrote,
    `MASS_PASSWORD="$MASS_PASSWORD" bash /tmp/in_ct_103.sh`, is the SHAPE of the
    `run_in_vm` template's own run line —

        run='MASS_PASSWORD="$MASS_PASSWORD" GUEST_USER="<…>" bash /tmp/in_vm_{GID}.sh'

    — reproduced as the step's work, and the check went into the template's own
    `{VERIFY_INSIDE}` slot. Neither slot's instructions said that path belongs to
    the engine, so a gate alone would keep refusing a draft the engine invited.
    """
    from app.modules.runbook_templates import (FREE_PARAM_SYSTEM,
                                               FREE_PARAM_SYSTEM_VERIFY)
    for text in (FREE_PARAM_SYSTEM, FREE_PARAM_SYSTEM_VERIFY):
        assert "/tmp/in_ct_*.sh" in text and "/tmp/in_vm_*.sh" in text
        assert "/root/.scaffold_step*" in text
    assert "must never invoke" in FREE_PARAM_SYSTEM
    assert "confirms nothing" in FREE_PARAM_SYSTEM_VERIFY


def test_the_run_line_that_taught_it_still_exists():
    """The line the model copied is real, and is the template's, not a step's."""
    from app.modules.runbook_templates import REACH_VM_SSH_AND_RUN
    run = REACH_VM_SSH_AND_RUN.run
    run = run if isinstance(run, str) else "\n".join(run)
    assert 'MASS_PASSWORD="$MASS_PASSWORD"' in run and "bash /tmp/in_vm_{GID}.sh" in run
