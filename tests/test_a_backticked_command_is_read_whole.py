"""§17.1347 — a backticked command is read whole, quotes and all.

Live, 2026-10-04: ADD131 ("set Radarr's root folder to /media/movies and Sonarr's
to /media/tv") carries its work as backticked commands:

    `pct exec 103 -- sh -c 'curl -s -X POST -H "X-Api-Key: $(…)" -d "{…}" http://127.0.0.1:7878/api/v3/rootfolder'`

`_INLINE_RE` accepted either a backtick or a single quote as the delimiter and
forbade BOTH inside the body, so that span was cut at the first inner quote. The
classifier saw `pct exec 103 -- sh -c` (which writes nothing) and a dangling
fragment `curl -s -X POST -H "X-Api-Key: $` (which does not parse), decided the
step was not hands-on, and the executor ran it as a model task. It wrote
instructions three times and failed on the §17.1183 gate:

    this step produced INSTRUCTIONS, not work. It wrote 5 commands for a machine
    the engine can reach … and none of them ran

A backtick ends a backticked span; the quotes inside belong to the command.
"""
from __future__ import annotations

from app.modules.step_classify import step_commands, step_is_hands_on

#: ADD131's own sentence, as stored
LIVE = ("Two commands, one per machine, run from the host: "
        "`pct exec 103 -- sh -c 'curl -s -X POST -H \"X-Api-Key: $(cat /var/lib/radarr/config.xml)\" "
        "-H \"Content-Type: application/json\" -d \"{\\\"path\\\":\\\"/media/movies\\\"}\" "
        "http://127.0.0.1:7878/api/v3/rootfolder'` and "
        "`pct exec 104 -- sh -c 'curl -s -X POST -H \"X-Api-Key: $(cat /var/lib/sonarr/config.xml)\" "
        "-d \"{\\\"path\\\":\\\"/media/tv\\\"}\" http://127.0.0.1:8989/api/v3/rootfolder'`.")


def test_the_whole_command_survives_its_inner_quotes():
    cmds = [c for c, _ in step_commands(LIVE)]
    whole = [c for c in cmds if c.startswith("pct exec 103") and "rootfolder" in c]
    assert whole, cmds[:3]
    assert "sh -c 'curl" in whole[0] and "-X POST" in whole[0]


def test_both_machines_commands_are_found():
    cmds = [c for c, _ in step_commands(LIVE)]
    assert any(c.startswith("pct exec 103") and "7878" in c for c in cmds)
    assert any(c.startswith("pct exec 104") and "8989" in c for c in cmds)


def test_the_step_is_hands_on_again():
    node = {"node_key": "ADD131", "tool": "LLM",
            "title": "Set Radarr's root folder to /media/movies and Sonarr's to /media/tv",
            "description": LIVE}
    on, why = step_is_hands_on(node)
    assert on is True, why


def test_a_single_quoted_command_still_reads():
    """The other delimiter keeps its old meaning: it ends at the next quote."""
    cmds = [c for c, _ in step_commands("Then run 'systemctl restart radarr' on it.")]
    assert "systemctl restart radarr" in cmds


def test_prose_in_backticks_is_still_not_a_command():
    """§17.1288q — a quoted run of prose has no plausible command head."""
    cmds = [c for c, _ in step_commands("The operator calls it `the media box` in conversation.")]
    assert cmds == [], cmds


def test_a_backticked_path_is_not_a_command():
    cmds = [c for c, _ in step_commands("It lives at `/var/lib/radarr/config.xml` on that machine.")]
    assert cmds == [], cmds


def test_the_same_command_is_not_counted_twice():
    one = "Run `systemctl is-active radarr` and then `systemctl is-active radarr` again."
    assert [c for c, _ in step_commands(one)].count("systemctl is-active radarr") == 1


def test_a_long_command_is_not_truncated_at_the_old_limit():
    """The old body cap was 150 characters; a real `pct exec … sh -c` line is longer."""
    long_cmd = ("pct exec 105 -- sh -c 'systemctl stop qbittorrent-nox; sed -i \"s|a|b|\" "
                "/var/lib/qbittorrent-nox/.config/qBittorrent/qBittorrent.conf; systemctl start qbittorrent-nox'")
    assert len(long_cmd) > 150
    cmds = [c for c, _ in step_commands(f"Do this: `{long_cmd}` and check after.")]
    assert long_cmd in cmds, cmds
