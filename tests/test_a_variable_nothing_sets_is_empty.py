"""§17.1348 — a variable nothing sets expands to nothing, so the frame refuses it.

Live, 2026-10-04. The engine had just been taught to read an *arr API key off the
machine (§17.1332, corrected by §17.1342), and the very next draft of that step
threw the read away and wrote a NAME instead:

    pct exec 103 -- sh -c 'curl -s -H "X-Api-Key: $RADARR_API_KEY" http://127.0.0.1:7878/api/v3/rootfolder'

Nothing sets `RADARR_API_KEY`: the runner resolves only the secrets it holds
(MASS_PASSWORD here), the block never assigns it, and a container's environment
does not carry it. The header would have gone out EMPTY and the app would have
answered 401 — which reads like a wrong key rather than a value that was never
there. Nothing noticed, because every gate looked for `<PLACEHOLDER>`, not
`$NAME`.
"""
from __future__ import annotations

from app.modules import supervised_runs as sr

POLICY = {"allow": ["ANY"], "secrets": ["MASS_PASSWORD"], "can_write_files": True}


def _vars(cmds, files=None, policy=POLICY):
    return sr.variables_nothing_sets(cmds, files, policy)


def test_the_live_draft_is_refused():
    bad = ['pct exec 103 -- sh -c \'curl -s -H "X-Api-Key: $RADARR_API_KEY" http://127.0.0.1:7878/api/v3/rootfolder\'']
    out = _vars(bad)
    assert len(out) == 1, out
    why = out[0]["why"]
    assert "`$RADARR_API_KEY` is read here and nothing sets it" in why
    assert "holds only MASS_PASSWORD" in why
    assert "expands to NOTHING" in why and "401" in why


def test_a_read_in_the_same_command_is_the_fix():
    ok = ['pct exec 103 -- sh -c \'curl -H "K: $(cat /var/lib/radarr/config.xml | sed -n "s:x:y:p")" http://z\'']
    assert _vars(ok) == []


def test_a_secret_the_runner_holds_is_satisfied():
    assert _vars(['MASS_PASSWORD="$MASS_PASSWORD" bash /tmp/x.sh']) == []
    assert _vars(['echo "$MASS_PASSWORD"'], None, {"secrets": []}), "with no secrets held, it is empty"


def test_a_value_the_block_assigns_is_satisfied():
    assert _vars(['bash -c \'K=$(cat /x); curl -H "K: $K" http://y\'']) == []
    assert _vars(['bash /tmp/s.sh'], [{"path": "/tmp/s.sh", "content": 'KEY="$(cat /x)"\ncurl -H "K: $KEY" http://y\n'}]) == []


def test_the_command_line_that_sets_it_for_a_file_counts():
    """§17.1191's shape: the runner expands the secret in the COMMAND, and the file
    reads it from the environment that command creates."""
    cmds = ['MASS_PASSWORD="$MASS_PASSWORD" GUEST_USER="aedefruscio" bash /tmp/in_vm_106.sh']
    files = [{"path": "/tmp/in_vm_106.sh",
              "content": 'GID=106\nqm status "$GID" | grep -q running || qm start "$GID"\necho "$GUEST_USER" "$MASS_PASSWORD" >/dev/null\n'}]
    assert _vars(cmds, files) == []


def test_a_file_variable_nothing_sets_is_still_caught():
    cmds = ['bash /tmp/s.sh']
    files = [{"path": "/tmp/s.sh", "content": 'curl -H "K: $RADARR_API_KEY" http://y\n'}]
    assert len(_vars(cmds, files)) == 1


def test_every_way_a_shell_binds_a_name_counts():
    """All of these are in the engine's own templates."""
    for cmd in ('for h in $(seq 1 254); do ping -c1 "$net.$h" & done'.replace("$net", "$GID"),
                'while read -r line; do echo "$line"; done < /tmp/x',
                'read PORT; echo "$PORT"',
                'f() { local tmp=/tmp/a; echo "$tmp"; }'):
        out = _vars([cmd.replace("$GID", "10.0.0")])
        assert out == [] or all("GID" in r["why"] for r in out), (cmd, out)


def test_awk_fields_are_not_shell_variables():
    """`awk '{print $NF}'` lives inside the address sweep; `$NF` is awk's."""
    assert _vars(["""nmap -sn 192.168.1.0/24 | awk '/^Nmap scan report/ {ip=$NF} tolower($0) ~ "mac" {print ip}'"""]) == []
    assert _vars(['awk "{print \\$1, \\$NR, \\$FS}" /tmp/x']) == []


def test_the_shells_own_names_are_not_flagged():
    assert _vars(['echo "$HOME $PATH $USER $PWD $SECONDS $RANDOM $1 $9"']) == []


def test_one_refusal_per_command():
    bad = ['curl -H "A: $ONE" -H "B: $TWO" http://x']
    assert len(_vars(bad)) == 1, "the first unset name is enough to withhold Run"


def test_the_frame_refuses_it_and_the_chain_redrafts():
    """Verify the lane: the gate is called, and its marker is registered so the
    drafter is asked again instead of the frame parking with Run greyed out."""
    import inspect
    src = inspect.getsource(sr.frame_run)
    assert "variables_nothing_sets(cmds, files, policy)" in src
    assert "_empty_vars" in src.split("refused = list(refused)")[1][:200]
    assert any("and nothing sets it" in m for m in sr._SHAPE_REFUSALS)
    note = sr.shape_retry_note({"refused": [{"command": "x", "why": "`$K` is read here and nothing sets it: …"}],
                                "commands": ["x"]})
    assert note


def test_nothing_is_judged_with_no_commands():
    assert _vars([]) == [] and _vars(None) == []
