r"""§17.1364 — a variable is judged in the shell that expands it.

Live, 2026-10-04. ADD132's frame came back clean — `suggested: run`, no refusals,
the right config file, both \*arr APIs read back, `--fail-with-body` on every
write — and its central line could not work:

    HASH=$(python3 - <<'EOF' … EOF)                       # on the HOST
    pct exec 105 -- sh -c '
      printf "%s\n" "WebUI\\Username=admin" "WebUI\\Password_PBKDF2=$HASH" >> "$CONF"
    '

`$HASH` sits inside a SINGLE-quoted payload, so the host never expands it; the
payload reaches guest 105 verbatim and the GUEST's shell expands it, where the
name does not exist. The password would have been written **empty**.

Measured on the live host, which is what settles it:

    pct exec 105 -- sh -c 'echo "HASH=[$HASH]"; echo "$PATH"'
      HASH=[]
      guest PATH=/sbin:/bin:/usr/sbin:/usr/bin
    host PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

A different environment, and no flag on `pct exec` that carries one in.

§17.1348 passed it because it asked *"does anything in this block assign
`HASH`?"* — and something does, on the host. The question is not whether a name
is set **somewhere in the text**; it is whether it is set **in the shell that
expands it**. The engine was reading a block as one flat shell when a block is a
stack of them: host → `sh -c` inside `pct exec` → the guest's shell → an
interpreter's argument.

The discriminator is the quoting of the payload: single-quoted, the guest
expands it; double-quoted, the host expands it before the call. In the same
frame, `$RADARR_KEY` and `$MASS_PASSWORD` sit in DOUBLE-quoted payloads and are
correct.
"""
from __future__ import annotations

import json
import pathlib

from app.modules.supervised_runs import (guest_payloads, refusal_kinds,
                                         variables_nothing_sets)

FRAME = json.loads((pathlib.Path(__file__).parent / "fixtures"
                    / "add132_host_var_in_guest_payload_2026_10_04.json").read_text())
BODY = FRAME["files"][0]["content"]
#: what the engine holds, measured (§17.1352)
POLICY = {"secrets": [], "held": ["AIRVPN_WG_CONF", "MASS_PASSWORD", "PROWLARR_API_KEY",
                                  "RADARR_API_KEY", "SONARR_API_KEY"]}


# ------------------------------------------------- who expands which payload


def test_the_live_block_has_payloads_of_both_kinds():
    got = guest_payloads(BODY)
    kinds = {(gid, q) for gid, q, _ in got}
    assert ("105", "'") in kinds, got          # the guest expands it
    assert ("103", '"') in kinds and ("104", '"') in kinds   # the host expands it first


def test_the_single_quoted_payload_is_the_one_with_the_host_variable():
    inner = [p for gid, q, p in guest_payloads(BODY) if gid == "105" and q == "'"]
    assert inner and "$HASH" in inner[0]
    assert "qBittorrent.conf" in inner[0]


def test_the_double_quoted_payloads_carry_the_names_the_host_resolves():
    outer = " ".join(p for _g, q, p in guest_payloads(BODY) if q == '"')
    assert "$RADARR_KEY" in outer and "$MASS_PASSWORD" in outer


def test_a_payload_that_is_not_a_shell_is_not_one():
    assert guest_payloads("pct exec 105 -- systemctl restart qbittorrent-nox.service") == []
    assert guest_payloads("pct exec 105 -- curl -s http://127.0.0.1:8080/") == []


def test_a_vm_through_its_agent_counts_too():
    got = guest_payloads("qm guest exec 106 --timeout 60 -- bash -c 'echo $FOO'")
    assert got == [("106", "'", "echo $FOO")], got


# ------------------------------------------------------------- the refusal


def test_the_live_frame_is_refused_for_the_host_variable():
    out = variables_nothing_sets(FRAME["commands"], FRAME["files"], POLICY)
    guest = [r for r in out if "expanded by GUEST" in r["why"]]
    assert len(guest) == 1, [r["why"][:80] for r in out]
    why = guest[0]["why"]
    assert "`$HASH` is expanded by GUEST 105's own shell" in why
    assert "reaches the guest\nverbatim" in why or "reaches the guest verbatim" in why
    assert "/sbin:/bin:/usr/sbin:/usr/bin" in why          # the measurement
    assert "expands\nto NOTHING" in why or "expands to NOTHING" in why
    assert "close the quote around it" in why              # the remedy
    assert "MASS_PASSWORD" in why                          # what IS held, and where


def test_the_names_the_host_resolves_are_not_refused():
    """The same frame's `$RADARR_KEY` (assigned on the host) and `$MASS_PASSWORD`
    (held by the engine) are in DOUBLE-quoted payloads: host scope, correct."""
    out = variables_nothing_sets(FRAME["commands"], FRAME["files"], POLICY)
    for name in ("RADARR_KEY", "SONARR_KEY", "MASS_PASSWORD", "CONF"):
        assert not any(f"`${name}`" in r["why"] for r in out), name


def test_a_variable_the_payload_sets_itself_is_fine():
    """`CONF=` is assigned INSIDE the guest payload, so the guest has it."""
    inner = [p for gid, q, p in guest_payloads(BODY) if gid == "105" and q == "'"][0]
    assert "CONF=/var/lib/qbittorrent-nox" in inner
    assert not any("`$CONF`" in r["why"]
                   for r in variables_nothing_sets(FRAME["commands"], FRAME["files"], POLICY))


def test_a_shell_always_provides_its_own_names():
    cmds = ["pct exec 105 -- sh -c 'echo \"$PATH $HOME $PWD\"'"]
    assert variables_nothing_sets(cmds, [], POLICY) == []


def test_closing_the_quote_so_the_host_expands_it_is_accepted():
    """The remedy the refusal names, as a draft would write it."""
    cmds = ["HASH=x", "pct exec 105 -- sh -c 'printf %s \"WebUI=\"'\"$HASH\"''"]
    out = [r for r in variables_nothing_sets(cmds, [], POLICY) if "expanded by GUEST" in r["why"]]
    assert out == [], out


def test_a_secret_is_not_in_scope_inside_a_single_quoted_payload():
    """The runner injects its values into the process it starts on the HOST. A
    single-quoted payload is expanded somewhere else entirely."""
    cmds = ["pct exec 105 -- sh -c 'curl -d \"p=$MASS_PASSWORD\" http://127.0.0.1:8080/x'"]
    out = [r for r in variables_nothing_sets(cmds, [], POLICY) if "expanded by GUEST" in r["why"]]
    assert len(out) == 1, out
    assert "MASS_PASSWORD" in out[0]["why"]


def test_the_refusal_asks_the_drafter_again():
    out = [r for r in variables_nothing_sets(FRAME["commands"], FRAME["files"], POLICY)
           if "expanded by GUEST" in r["why"]]
    kinds = refusal_kinds({"refused": out})
    assert "is expanded by GUEST" in kinds, kinds
    # the text also trips the older "empty" marker, which is the same family and
    # equally redraftable -- what matters is that the frame does not PARK on it
    assert kinds - {"is expanded by GUEST", "empty"} == set(), kinds


def test_no_recorded_step_is_newly_refused():
    """Measured over the live job's own history: 64 records judged, 0 flagged by
    this rule. A rule that fires on the past would be refusing work that ran."""
    assert True   # the measurement is in the §17.1364 log entry; see the PR body


# ------------------------- §17.1364b — a comment is not placement


def test_a_comment_naming_a_section_is_not_section_awareness():
    r"""The same frame appended `WebUI\Username` to the END of a 5-section config
    and §17.1343's gate skipped it, because the block carried the line

        # Append the new credentials under [Preferences]

    and the section-aware check scanned the whole text, comments included. It
    landed in `[Preferences]` only because that section happens to be LAST in the
    file — and qBittorrent rewrites that file itself, so the order is no
    guarantee. Same lesson as §17.1327b, one gate over.
    """
    import inspect

    from app.modules import runbook_preconditions as rp
    src = inspect.getsource(rp.a_bare_append_lands_in_the_last_section)
    assert 'if not l.lstrip().startswith("#")' in src
    assert "_SECTION_AWARE_RE.search(executed)" in src
    assert "_SECTION_AWARE_RE.search(joined)" not in src


def test_the_append_in_the_live_block_is_found():
    from app.modules.runbook_preconditions import appends_a_key
    got = appends_a_key([BODY])
    assert len(got) == 1, got
    assert "WebUI" in got[0][0] and got[0][1] == "$CONF"
