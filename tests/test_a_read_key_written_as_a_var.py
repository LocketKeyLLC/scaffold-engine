r"""§17.1500 — a readable service key written as a shell variable, not the placeholder.

Live, job c75d77fa T3v2 (2026-10-12, the draw after §17.1499 folded the nested
`$(pct exec …)`). The Jellyfin-token read came back as::

    pct exec 101 -- curl … -H "X-Emby-Token: $JELLYFIN_API_KEY" …

The drafter reasoned by analogy from the *arr keys, which ARE stored and so arrive
as `$NAME` injected by the runner — but Jellyfin's key is READ, not stored
(§17.1382), so nothing sets that variable and the gate refuses it. The engine holds
the read; the drafter just named it the wrong way.

`read_a_readable_key_written_as_a_var` normalises `$NAME` / `${NAME}` / bare `<NAME>`
for a readable, non-stored key into the `<NAME>` placeholder and puts the call in the
guest's own shell, so `read_on_the_machine` fills it where the key lives. A STORED
secret's `$NAME` is left for the runner to inject.
"""
from __future__ import annotations

import subprocess

from app.modules import machine_values as mv

INV = {"names": {"101": "jellyfin", "102": "prowlarr", "103": "radarr", "104": "sonarr"}}
STORED = {"RADARR_API_KEY", "SONARR_API_KEY", "PROWLARR_API_KEY", "MASS_PASSWORD"}


def _chain(cmd: str):
    """What frame_run does: normalise+promote, then read_on_the_machine."""
    c, v, _ = mv.read_a_readable_key_written_as_a_var([cmd], [], STORED)
    c, v, _ = mv.read_on_the_machine(c, v, INV)
    return c[0]


def _filled_and_valid(out: str) -> bool:
    if "JELLYFIN_API_KEY" in out or "$(python3 -c" not in out:
        return False
    return subprocess.run(["bash", "-n", "-c", out], capture_output=True, text=True).returncode == 0


def test_the_live_dollar_name_form_is_read_in_the_guest():
    out = _chain('pct exec 101 -- curl -s --fail-with-body -H "X-Emby-Token: $JELLYFIN_API_KEY" '
                 'http://127.0.0.1:8096/Library/VirtualFolders')
    assert out.startswith("pct exec 101 -- sh -c '")
    assert _filled_and_valid(out)
    assert "http://127.0.0.1:8096/Library/VirtualFolders" in out


def test_the_brace_form_inside_an_existing_sh_c_is_filled():
    out = _chain("pct exec 101 -- sh -c 'curl -H \"T: ${JELLYFIN_API_KEY}\" http://127.0.0.1:8096/x'")
    assert _filled_and_valid(out)


def test_a_bare_placeholder_outside_a_guest_shell_is_promoted_and_filled():
    out = _chain('pct exec 101 -- curl -H "T: <JELLYFIN_API_KEY>" http://127.0.0.1:8096/x')
    assert out.startswith("pct exec 101 -- sh -c '") and _filled_and_valid(out)


def test_a_stored_secret_variable_is_left_for_the_runner_to_inject():
    cmd = 'pct exec 103 -- curl -s -H "X-Api-Key: $RADARR_API_KEY" http://127.0.0.1:7878/api/v3/indexer'
    out, _, notes = mv.read_a_readable_key_written_as_a_var([cmd], [], STORED)
    assert out == [cmd] and not notes


def test_a_host_side_command_is_left_alone():
    # The read only works inside the guest that owns the DB; a host-side $NAME is
    # not this transform's to rewrite (the host form fails unprivileged, §17.1342).
    cmd = 'curl -H "X-Emby-Token: $JELLYFIN_API_KEY" http://192.168.1.20:8096/x'
    out, _, notes = mv.read_a_readable_key_written_as_a_var([cmd], [], STORED)
    assert out == [cmd] and not notes


def test_the_unset_var_gate_refuses_the_original_and_is_quiet_after_the_rewrite():
    from app.modules.supervised_runs import variables_nothing_sets
    policy = {"secrets": sorted(STORED), "held": [], "can_write_files": True}
    orig = 'pct exec 101 -- curl -s --fail-with-body -H "X-Emby-Token: $JELLYFIN_API_KEY" http://127.0.0.1:8096/x'
    assert variables_nothing_sets([orig], [], policy), "the raw $JELLYFIN_API_KEY must be refused"
    rewritten, _, _ = mv.read_a_readable_key_written_as_a_var([orig], [], STORED)
    rewritten, _, _ = mv.read_on_the_machine(rewritten, [], INV)
    assert variables_nothing_sets(rewritten, [], policy) == [], "nothing unset remains after the read is filled in"


def test_checks_are_rewritten_too():
    _, verify, notes = mv.read_a_readable_key_written_as_a_var(
        [], ['pct exec 101 -- curl -s -H "X-Emby-Token: $JELLYFIN_API_KEY" http://127.0.0.1:8096/x'], STORED)
    assert verify[0].startswith("pct exec 101 -- sh -c '") and notes


def test_a_stored_readable_key_inside_a_guest_payload_is_read_there():
    r"""§17.1503 — a stored *arr key inside a single-quoted `sh -c '…'` payload cannot
    be injected (host injection does not reach the guest shell), so it is read from the
    service's own config INSIDE that guest. Live (job c75d77fa T20)."""
    cmd = ("pct exec 104 -- sh -c 'curl -s --fail-with-body -H \"X-Api-Key: $SONARR_API_KEY\" "
           "\"http://127.0.0.1:8989/api/v3/series/lookup?term=big%20bang%20theory\"'")
    c, _, _ = mv.read_a_readable_key_written_as_a_var([cmd], [], STORED)
    c, _, _ = mv.read_on_the_machine(c, [], INV)
    out = c[0]
    assert "$SONARR_API_KEY" not in out, "the stored key must not be left unexpanded in a guest shell"
    assert "/var/lib/sonarr/config.xml" in out and "$(cat" in out, "read from its own config in the guest"
    assert subprocess.run(["bash", "-n", "-c", out], capture_output=True, text=True).returncode == 0


def test_a_stored_readable_key_on_the_host_is_still_injected():
    r"""§17.1503 — on the HOST (double-quoted, not a single-quoted payload) the runner
    injects the stored key, so it is left as `$NAME`; only the guest-payload position reads."""
    cmd = 'pct exec 104 -- curl -s -H "X-Api-Key: $SONARR_API_KEY" http://127.0.0.1:8989/api/v3/series'
    out, _, notes = mv.read_a_readable_key_written_as_a_var([cmd], [], STORED)
    assert out == [cmd] and not notes


def test_a_stored_non_readable_secret_in_a_guest_payload_is_left_alone():
    # MASS_PASSWORD is a secret but not an *arr/Jellyfin key the engine can read — not this path.
    cmd = "pct exec 105 -- sh -c 'curl -d \"p=$MASS_PASSWORD\" http://127.0.0.1:8080/x'"
    out, _, notes = mv.read_a_readable_key_written_as_a_var([cmd], [], STORED)
    assert out == [cmd] and not notes


def test_frame_run_runs_the_rewrite_before_the_reader():
    import inspect
    from app.modules import supervised_runs as sr
    src = inspect.getsource(sr)
    assert "read_a_readable_key_written_as_a_var(cmds, verify, _stored_names)" in src
    assert src.index("read_a_readable_key_written_as_a_var") < src.index("read_on_the_machine(cmds")
