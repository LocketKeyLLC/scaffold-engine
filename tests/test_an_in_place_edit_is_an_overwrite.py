"""§17.1341 — an edit in place is an overwrite, so a copy is kept first.

Live, 2026-10-04. The step that points qBittorrent's downloads at the shared
media path drafted this, against the config the service is running on:

    sed -i "s|^${KEY}=.*|${KEY}=/media/downloads|" "$CONF"
    echo "${KEY}=/media/downloads" >> "$CONF"

No copy kept. §17.1314's rule reads lines that REPLACE a whole file (`cat >`,
`tee <<`, `cp`, `mv`, `install`), so an in-place edit and an append went past it.
The lesson that rule exists for — ADD100's only copy of a working backend was
overwritten by a cut draw, recoverable from nothing — applies the same way to a
config a running service reads.

Also measured here rather than assumed: `[ -e "$P" ] && cp -a …` does NOT end a
`set -e` script when the file is absent. Bash exempts a failing command that is
not the last in an `&&` list, so no `|| true` is needed. A fix for that was
written, tested against bash, and thrown away.
"""
from __future__ import annotations

import subprocess

from app.modules import runbook_templates as rt

#: the live draft's own lines, with the config in a shell variable
LIVE = """set -e
CONF=/var/lib/qbittorrent-nox/.config/qBittorrent/qBittorrent.conf
if grep -q "^${KEY}=" "$CONF"; then
  sed -i "s|^${KEY}=.*|${KEY}=/media/downloads|" "$CONF"
else
  echo "${KEY}=/media/downloads" >> "$CONF"
fi
systemctl restart qbittorrent-nox
"""


def test_the_live_draft_kept_no_copy():
    assert 'sed -i "s|^${KEY}=.*' in LIVE and "cp -a" not in LIVE


def test_an_in_place_edit_gets_a_copy_first():
    out = rt.keep_a_copy_before_overwrites(LIVE)
    lines = out.split("\n")
    i = next(j for j, ln in enumerate(lines) if ln.strip().startswith("sed -i"))
    assert 'cp -a "$CONF"' in lines[i - 1], lines[i - 1]
    assert '[ -e "$CONF" ]' in lines[i - 1]


def test_an_append_to_an_existing_file_does_too():
    out = rt.keep_a_copy_before_overwrites(LIVE)
    lines = out.split("\n")
    i = next(j for j, ln in enumerate(lines) if ln.strip().startswith("echo") and ">>" in ln)
    assert 'cp -a "$CONF"' in lines[i - 1], lines[i - 1]


def test_a_quoted_variable_is_a_path():
    """`"$CONF"` is where the file is; the quotes are not part of it."""
    out = rt.keep_a_copy_before_overwrites('sed -i "s/a/b/" "$CONF"\n')
    assert '[ -e "$CONF" ] && cp -a "$CONF" "$CONF.bak.$(date +%Y%m%d%H%M%S)"' in out


def test_a_literal_path_works_for_both_shapes():
    out = rt.keep_a_copy_before_overwrites(
        "printf '%s\\n' 'extra=1' >> /etc/sysctl.conf\nsed -i -e 's/a/b/' /etc/hosts\n")
    assert out.count("cp -a") == 2, out
    assert '"/etc/sysctl.conf"' in out and '"/etc/hosts"' in out


def test_the_old_shapes_still_get_theirs():
    out = rt.keep_a_copy_before_overwrites("cat > /opt/x.js <<'EOF'\nhello\nEOF\n")
    assert out.count("cp -a") == 1 and "/opt/x.js" in out


def test_nothing_inside_a_heredoc_is_touched():
    """A `sed -i` in CONTENT a script writes is not a command this script runs."""
    out = rt.keep_a_copy_before_overwrites("cat > /opt/f.sh <<'EOF'\nsed -i 's/a/b/' /etc/motd\nEOF\n")
    assert out.count("cp -a") == 1, out
    assert "/etc/motd" not in out.split("EOF")[0].replace("cat > /opt/f.sh <<'", "")


def test_a_comment_is_not_an_edit():
    out = rt.keep_a_copy_before_overwrites("# sed -i 's/a/b/' /etc/hosts\n")
    assert "cp -a" not in out


def test_the_result_is_valid_shell():
    out = rt.keep_a_copy_before_overwrites(LIVE)
    p = subprocess.run(["bash", "-n"], input="#!/bin/bash\n" + out, text=True, capture_output=True)
    assert p.returncode == 0, p.stderr


def test_a_guard_whose_file_is_absent_does_not_end_a_set_e_script():
    """Measured, not assumed: bash exempts a failing command that is not the last
    in an `&&` list, so the copy line is safe on a first write."""
    script = '#!/bin/bash\nset -e\nP=/tmp/definitely-not-here-$$\n' \
             '[ -e "$P" ] && cp -a "$P" "$P.bak"\necho REACHED\n'
    p = subprocess.run(["bash"], input=script, text=True, capture_output=True)
    assert p.returncode == 0 and "REACHED" in p.stdout, (p.returncode, p.stdout, p.stderr)
