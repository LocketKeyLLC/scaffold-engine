"""§17.1696 — a check the draft offered and the engine could not keep is named in the "no check" refusal."""
from app.modules import supervised_runs as sr

# live, job e5e22b92 T13's Verify (three drafts, each refused "no check at all")
LIVE = ('## Verify\n\n- `pct exec 102 -- bash -c "curl -s -H \\"X-Api-Key: \\$(grep -oP \'(?<=<ApiKey>)[^<]+\' '
        '/var/lib/prowlarr/config.xml)\\" http://192.168.1.21:9696/api/v1/indexer | python3 -c \\"import sys,json; '
        '[print(i[\'name\'], i[\'enable\']) for i in json.load(sys.stdin) if i[\'name\'] in [\'Internet Archive\','
        '\'Torrent Downloads\']]\\""`\n')
GREP = ('## Verify\n\n- `pct exec 102 -- bash -c "curl -s -H \\"X-Api-Key: \\$(grep -oP \'(?<=<ApiKey>)[^<]+\' '
        '/var/lib/prowlarr/config.xml)\\" http://127.0.0.1:9696/api/v1/indexer | grep -o \'\\"enable\\": *[a-z]*\'"`\n')


def test_the_live_check_is_named_with_why():
    assert sr.verify_commands(LIVE) == []
    dropped = sr.dropped_checks(LIVE)
    assert len(dropped) == 1 and "program" in dropped[0][1]
    note = sr._dropped_note(LIVE)
    assert "DID offer a check" in note and "grep -o" in note


def test_the_templates_escaped_grep_check_is_kept():
    """§17.1697 — the template's `bash -c "…\\"…\\$(…)…"` slot is a read once its escapes are resolved"""
    assert sr.verify_commands(GREP) and "grep -o" in sr.verify_commands(GREP)[0]
    assert sr.dropped_checks(GREP) == [] and sr._dropped_note(GREP) == ""


def test_prose_is_not_a_dropped_check():
    assert sr.dropped_checks("## Verify\n\nThe `enable` flag is `true` and `Internet Archive` answers.\n") == []


def test_double_quote_escapes_are_resolved_as_bash_does():
    from app.modules.shell_ast import analyze
    f = analyze('bash -c "echo \\"a\\" \\$HOME \\\\ \\n"')
    assert f.commands[0][2] == 'echo "a" $HOME \\ \\n'      # \n is not an escape inside "…"



def test_a_queue_check_of_live_length_is_kept():
    """§17.1702 — live, job e5e22b92 RC3's Sonarr queue check was 451 characters and was dropped twice"""
    filt = r"""grep -o '\"title\": *\"[^\"]*Robot.Chicken[^\"]*\"\|\"status\": *\"[a-z]*\"\|\"episodeId\": *[0-9]*\|\"trackedDownloadState\": *\"[a-zA-Z]*\"\|\"trackedDownloadStatus\": *\"[a-zA-Z]*\"'"""
    cmd = (r'''pct exec 104 -- bash -c "curl -s -H \"X-Api-Key: \$(grep -oP '(?<=<ApiKey>)[^<]+' /var/lib/sonarr/config.xml)\" '''
           r"""'http://127.0.0.1:8989/api/v3/queue?page=1&pageSize=500&includeEpisode=true&includeSeries=true' | """ + filt
           + r''' | head -n 60 | sort | uniq -c | sort -rn | head -n 40 | cat"''')
    assert 400 < len(cmd) <= 800, len(cmd)
    assert sr.verify_commands("## Verify\n\n- `" + cmd + "`\n") == [cmd]


def test_a_quoted_ampersand_inside_an_escaped_wrapper_is_not_unsafe():
    """§17.1704 — live, job e5e22b92 RC3's check (drawn three times) was refused for a quoted `&`"""
    from app.modules.assist_state_check import read_only_command
    live = (r'''pct exec 104 -- bash -c "curl -s -H \"X-Api-Key: \$(grep -oP '(?<=<ApiKey>).*?(?=</ApiKey>)' /var/lib/sonarr/config.xml)\" '''
            r'''\"http://localhost:8989/api/v3/queue?page=1&pageSize=100\" | grep -o '\"title\":\"[^\"]*Robot Chicken[^\"]*\",\"status\":\"[^\"]*\",\"episodeId\":[0-9]*' | head -5"''')
    assert read_only_command(live, runner_side=True)
    # the inside is still screened: an UNQUOTED `&` inside the wrapper backgrounds the curl
    bad = r'''pct exec 104 -- bash -c "curl -s http://localhost:8989/api/v3/queue?page=1&pageSize=100 | head -5"'''
    assert not read_only_command(bad, runner_side=True)
    # and a bare command keeps the raw screen
    assert not read_only_command("curl -s --url=http://x/?a=1&b=2", runner_side=True)
