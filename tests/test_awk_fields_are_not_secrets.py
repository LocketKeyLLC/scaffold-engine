"""§17.1638 — awk's `$NF` is not a stored secret.

Live (job 0fb4ea7f MC1, 2026-10-18, runner helper 23): the approved read-only block stopped at its fifth line,
`… | awk '{print $NF}' | sort | uniq -c` — "this command needs $NF, which neither the engine sent nor this runner's
secrets file holds". The runner's `secret_refs` reads every `$UPPER` as a secret, inside single quotes too.
"""
from __future__ import annotations

import subprocess
from unittest.mock import MagicMock

from app.modules import supervised_runs as sr

LIVE = ("ipmitool sel list | grep -E \"Correctable ECC|Uncorrectable ECC\" | grep -v \"DIMMC1(CPU1)\" "
        "| awk '{print $NF}' | sort | uniq -c")


def _runner_refs(command: str) -> list[str]:
    import importlib.util
    spec = importlib.util.spec_from_file_location("lr", "scripts/local_runner_mcp.py")
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod.secret_refs(command)


def test_the_runner_really_demands_NF():
    """The pre-image, through the runner's own parser."""
    assert _runner_refs(LIVE) == ["NF"]


def test_the_engine_writes_the_field_so_the_runner_asks_nothing():
    cmds, _, notes = sr.awk_fields_the_runner_reads_as_secrets([LIVE], [])
    assert "awk '{print $(NF)}'" in cmds[0] and notes, cmds
    assert _runner_refs(cmds[0]) == []
    # bash-level: awk prints the same field either way
    for prog in ("{print $NF}", "{print $(NF)}"):
        r = subprocess.run(["awk", prog], input="a b ECC\n", capture_output=True, text=True)
        assert r.stdout == "ECC\n", (prog, r.stdout)


def test_only_single_quoted_awk_fields_are_rewritten():
    keep = ['echo "it\'s $HOME"', 'echo "$NF"', "printf '%s' x", 'echo "don\'t" "$NF"']   # $NF in "…" IS the shell's
    assert sr.awk_fields_the_runner_reads_as_secrets(keep, [])[0] == keep


def test_another_single_quoted_upper_name_is_refused_before_run_unless_held():
    assert sr.single_quoted_names_the_runner_reads_as_secrets(["sed 's/$FOO/x/' f"], {"held": []})
    assert not sr.single_quoted_names_the_runner_reads_as_secrets(["bash -c 'x # $MASS_PASSWORD'"],
                                                                 {"secrets": ["MASS_PASSWORD"]})


def test_the_frame_repairs_the_live_line():
    node = {"node_key": "MC1", "title": "On the Proxmox host: check CPU1 memory channel C", "tool": "Shell",
            "description": "Read only."}
    spec = MagicMock(); spec.name = "pve-runner"; spec.headers = {"X-Runner-Token": "t"}
    frame = sr.frame_run(node, f"## Run this\n\n```bash\n{LIVE}\n```\n", spec, {"allow": ["ANY"], "sudo": True, "helper": "23"},
                         inventory={"vms": {}, "cts": {}, "names": {}})
    assert "$(NF)" in frame["commands"][0] and not frame["refused"], (frame["commands"], frame["refused"])
    assert any("$(…)" in str(f) for f in frame["engine_fixed"]), frame["engine_fixed"]


def test_the_frame_refuses_another_single_quoted_name_and_redrafts():
    node = {"node_key": "X", "title": "Rename a token in a file", "tool": "Shell", "description": "On the host."}
    spec = MagicMock(); spec.name = "pve-runner"; spec.headers = {"X-Runner-Token": "t"}
    frame = sr.frame_run(node, "## Run this\n\n```bash\nsed -i 's/$FOO/bar/' /tmp/x.conf\n```\n", spec,
                         {"allow": ["ANY"], "sudo": True, "helper": "23"}, inventory={"vms": {}, "cts": {}, "names": {}})
    assert any("inside single quotes is left alone by the shell" in r["why"] for r in frame["refused"]), frame["refused"]
    assert sr.shape_retry_note(frame)


def test_a_name_the_block_binds_is_not_refused():
    """The engine's own probe binds `TOK=$(cat)` inside its single-quoted payload; helper 23 leaves it alone."""
    block = "pct exec 111 -- sh -c 'TOK=$(cat); curl -s -u \"x:$TOK\" http://127.0.0.1:1/' <<< \"$PANEL_PASSWORD\""
    assert _runner_refs(block) == ["PANEL_PASSWORD"]
    assert not sr.single_quoted_names_the_runner_reads_as_secrets([block], {"held": ["PANEL_PASSWORD"]})
