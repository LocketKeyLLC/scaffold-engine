"""§17.1377 — a body in a file is only a body if the reader's machine can see it.

Live, 2026-10-05. §17.1375 taught the drafter to send a request body through a
file; §17.1376 stopped refusing the correct draft. The next draft was ACCEPTED —
`suggested: run`, zero refusals — and could not have worked:

    python3 - <<'EOF'                                        # on the HOST
    import json
    … {"name": "password", "value": os.environ['MASS_PASSWORD']} …
    json.dump(client, open('/tmp/radarr_dc_update.json', 'w'))
    EOF
    pct exec 103 -- sh -c 'curl … -d @/tmp/radarr_dc_update.json …'   # inside 103

Two defects, neither visible to any gate. The heredoc never imports `os`, so it
raises NameError and `set -euo pipefail` ends the block having changed nothing.
And the body file is written on the host while curl reads it inside guest 103,
whose `/tmp` is a different filesystem — which is exactly what FILE_RULES' own
§17.1375 worked example showed it to do.
"""
import json
import pathlib

from app.modules.service_truth import (SOME_GUEST, THE_HOST,
                                       a_file_read_on_another_machine,
                                       where_each_line_runs)
from app.modules.supervised_runs import (FILE_RULES,
                                         a_program_that_uses_a_name_it_never_defines)

FIX = pathlib.Path(__file__).parent / "fixtures"
LIVE = json.loads((FIX / "add132_body_on_the_wrong_machine_2026_10_05.json").read_text())
BODY = LIVE["files"][0]["content"]


def _block(text):
    return [{"path": "/tmp/block.sh", "content": text}]


# ------------------------------------------------- the live draft, both defects

def test_the_live_draft_was_accepted_with_no_refusals():
    """What the shipped engine did, recorded so this file cannot silently pass."""
    assert LIVE["accepted_with"] == {"suggested": "run", "refused": 0}


def test_the_body_files_are_refused_for_being_on_the_wrong_machine():
    out = a_file_read_on_another_machine(LIVE["commands"], LIVE["files"])
    assert len(out) == LIVE["defects"]["cross_machine_body_files"] == 2, out
    paths = sorted(w["why"].split("`")[1] for w in out)
    assert paths == ["/tmp/radarr_dc_update.json", "/tmp/sonarr_dc_update.json"]


def test_the_refusal_names_both_machines_and_the_two_remedies():
    why = a_file_read_on_another_machine(LIVE["commands"], LIVE["files"])[0]["why"]
    assert "written on the host" in why and "read on guest 103" in why
    assert "-d @-" in why                      # pipe it
    assert "pct exec" in why                   # or create it in the guest


def test_the_heredoc_that_never_imports_os_is_refused():
    out = a_program_that_uses_a_name_it_never_defines(LIVE["commands"], LIVE["files"])
    assert len(out) == LIVE["defects"]["undefined_name_in_heredoc"] == 1, out
    assert "`os`" in out[0]["why"] and "NameError" in out[0]["why"]


def test_the_sibling_heredoc_that_does_import_os_is_not_refused():
    """The same block's Sonarr program says `import json, os` — the draft knew the
    import and dropped it once, so a gate that flagged both would be guessing."""
    out = a_program_that_uses_a_name_it_never_defines(LIVE["commands"], LIVE["files"])
    assert len(out) == 1


# ------------------------------------------------------- every correct shape

def test_a_file_pushed_into_the_guest_is_accepted():
    """`pct push` is a write on the DESTINATION. Measured: without this the gate
    refused a correct corpus draft twice for pushing its payload into guest 103."""
    assert a_file_read_on_another_machine([], _block(
        "python3 -c 'open(\"/tmp/b.json\",\"w\").write(\"{}\")'\n"
        "pct push 103 /tmp/b.json /tmp/b.json\n"
        "pct exec 103 -- sh -c 'curl -d @/tmp/b.json http://127.0.0.1:7878/x'\n")) == []


def test_a_body_piped_over_stdin_is_accepted():
    assert a_file_read_on_another_machine([], _block(
        "python3 /tmp/body.py | pct exec 103 -- sh -c "
        "'curl -d @- http://127.0.0.1:7878/x'\n")) == []


def test_a_file_created_inside_the_guest_that_reads_it_is_accepted():
    """The shape FILE_RULES now recommends. It is ONE `pct exec` whose argument
    spans four lines, so the resolver has to carry the machine across the quote."""
    assert a_file_read_on_another_machine([], _block(
        "pct exec 103 -- sh -c 'cat > /tmp/b.json <<\"JSON\"\n"
        "{\"a\": 1}\n"
        "JSON\n"
        "curl -d @/tmp/b.json http://127.0.0.1:7878/x'\n")) == []


def test_the_host_writing_and_the_host_reading_is_accepted():
    assert a_file_read_on_another_machine([], _block(
        "python3 -c 'open(\"/tmp/b.json\",\"w\").write(\"{}\")'\n"
        "curl -d @/tmp/b.json http://192.168.1.22:7878/x\n")) == []


def test_a_path_nothing_in_the_block_creates_is_not_judged():
    """A file that already exists on the machine is not this mistake."""
    assert a_file_read_on_another_machine([], _block(
        "pct exec 103 -- sh -c 'curl -d @/etc/radarr/body.json http://127.0.0.1:7878/x'\n")) == []


# ------------------------------------------------------------- and it bites

def test_one_guest_writing_and_another_reading_is_refused():
    out = a_file_read_on_another_machine([], _block(
        "pct exec 103 -- sh -c 'echo x > /tmp/b.json'\n"
        "pct exec 104 -- sh -c 'curl -d @/tmp/b.json http://127.0.0.1:8989/x'\n"))
    assert len(out) == 1, out
    assert "guest 103" in out[0]["why"] and "guest 104" in out[0]["why"]


def test_the_file_channel_counts_as_a_host_write():
    """The runner lays the file sections down beside the block, on the machine
    that runs it — so reading one inside a guest is the same mistake."""
    out = a_file_read_on_another_machine(
        ["pct exec 103 -- sh -c 'curl -d @/tmp/payload.json http://127.0.0.1:7878/x'"],
        [{"path": "/tmp/payload.json", "content": '{"a": 1}'}])
    assert len(out) == 1, out
    assert "written on the host" in out[0]["why"]


def test_an_unnamed_guest_against_a_named_one_is_not_a_claim():
    """`SOME_GUEST` may well BE guest 103, so the gate must not guess."""
    assert a_file_read_on_another_machine([], _block(
        "def run(ct, cmd):\n"
        "    subprocess.run(['pct','exec',str(ct),'--','sh','-c',cmd])\n"
        "run(ct, 'echo x > /tmp/b.json')\n"
        "pct exec 103 -- sh -c 'curl -d @/tmp/b.json http://127.0.0.1:7878/x'\n")) == []


def test_a_redirection_the_dispatching_shell_performs_is_the_hosts():
    """The engine's OWN vm template, which this gate refused on its first cut:

        qm guest exec "$GID" … -- bash -c "cat > /root/.scaffold_step.sh" < /tmp/in_vm_106_remote.sh

    The host's shell opens that path and feeds it to `qm guest exec` as stdin —
    nothing inside the VM sees it. A `-d @path` is the opposite: the program
    inside interprets it."""
    assert a_file_read_on_another_machine([], _block(
        "cat > /tmp/in_vm_106_remote.sh <<'SH'\necho hi\nSH\n"
        'qm guest exec "$GID" --timeout 60 --pass-stdin 1 -- bash -c '
        '"cat > /root/.scaffold_step.sh" < /tmp/in_vm_106_remote.sh\n')) == []


def test_a_wrapper_writing_by_redirection_writes_on_the_host():
    """`pct exec 103 -- curl … > /tmp/out.json` lands on the host, so a host-side
    reader of it is right and must not be refused."""
    assert a_file_read_on_another_machine([], _block(
        "pct exec 103 -- curl -s http://127.0.0.1:7878/api/v3/x > /tmp/out.json\n"
        "curl -d @/tmp/out.json http://192.168.1.22:7878/y\n")) == []


def test_a_redirection_inside_the_guests_own_command_is_the_guests():
    """Inside the quoted argument the guest's shell performs it, so a guest
    reader is right and a HOST reader of that path is the mistake."""
    out = a_file_read_on_another_machine([], _block(
        "pct exec 103 -- sh -c 'curl -s http://127.0.0.1:7878/x > /tmp/g.json'\n"
        "curl -d @/tmp/g.json http://192.168.1.22:7878/y\n"))
    assert len(out) == 1, out
    assert "written on guest 103" in out[0]["why"] and "read on the host" in out[0]["why"]


def test_a_redirection_inside_a_command_substitution_is_the_guests():
    """From the corpus (trace 1695): quoting restarts inside `$( … )`, so a flat
    quote scan mis-pairs this and reads the guest's `<` as the host's."""
    assert a_file_read_on_another_machine([], _block(
        'qm guest exec 106 -- sh -c "echo k >> /home/u/.ssh/authorized_keys"\n'
        'COUNT="$(qm guest exec 106 -- sh -c "wc -l < /home/u/.ssh/authorized_keys"'
        " | tr -d '[:space:]')\"\n")) == []


# -------------------------------------------- the resolver carries the machine

def test_a_quoted_argument_spanning_lines_keeps_its_machine():
    runs_on = where_each_line_runs(
        "pct exec 103 -- sh -c 'cat > /tmp/b.json <<\"JSON\"\n{}\nJSON\ncurl -d @/tmp/b.json x'\n")
    assert [runs_on.get(i) for i in (1, 2, 3, 4)] == ["103"] * 4


def test_a_backslash_continuation_keeps_its_machine():
    """Live: a corpus draft put `pct exec 103 -- curl … \\` on one line and its URL
    four lines down, and the URL read as the host's — two false refusals."""
    runs_on = where_each_line_runs(
        "pct exec 103 -- curl -s -X PUT \\\n"
        "  -H 'X-Api-Key: k' \\\n"
        "  -d '{}' \\\n"
        "  http://127.0.0.1:7878/api/v3/downloadclient/1\n")
    assert [runs_on.get(i) for i in (1, 2, 3, 4)] == ["103"] * 4


def test_the_line_after_a_closed_quote_is_the_hosts_again():
    runs_on = where_each_line_runs(
        "pct exec 103 -- sh -c 'echo a\necho b'\n"
        "curl http://127.0.0.1:7878/x\n")
    assert runs_on.get(1) == "103" and runs_on.get(2) == "103"
    assert runs_on.get(3, THE_HOST) == THE_HOST


# ---------------------------------------- the rules teach both shapes up front

def test_the_rules_teach_that_the_file_must_be_where_the_reader_is():
    """§17.1375's lesson: a gate that refuses without the rules teaching the shape
    is a loop. The example in FILE_RULES was itself this defect."""
    assert "THE FILE MUST EXIST ON THE MACHINE THAT READS IT" in FILE_RULES
    assert "A guest's `/tmp` is not the host's" in FILE_RULES


def test_the_rules_no_longer_show_the_host_writing_what_a_guest_reads():
    """The worked example used to be `python3 /tmp/body.py` then
    `pct exec 103 -- sh -c '… -d @/tmp/body.json'`, which is what the drafter copied."""
    assert "python3 /tmp/body.py\npct exec" not in FILE_RULES
    assert "python3 /tmp/body.py | pct exec" in FILE_RULES


def test_the_rules_teach_the_heredoc_import_rule():
    assert "A HEREDOC IS A WHOLE PROGRAM" in FILE_RULES


def test_the_rules_own_recommended_shapes_pass_their_own_gate():
    """Whatever FILE_RULES shows, the gate must accept — otherwise the engine
    refuses its own advice, which is how this defect got written in the first place."""
    import re
    for block in re.findall(r"```bash\n(.*?)```", FILE_RULES, re.S):
        assert a_file_read_on_another_machine([], _block(block)) == [], block


# ------------------------------------- a refusal nobody can redraft is a dead end

def test_both_refusals_drive_a_redraft_not_a_park():
    """§17.1269 — `shape_retry_note` matches a refusal by TEXT against
    `_SHAPE_REFUSALS`. Unregistered, these two would hand the operator a
    greyed-out Run and nothing to do; both are shapes the drafter can fix, so
    both must redraft. The standing §17.1269 guard caught this on the first run.
    """
    import app.modules.supervised_runs as sr
    for refused in (a_program_that_uses_a_name_it_never_defines(LIVE["commands"], LIVE["files"]),
                    a_file_read_on_another_machine(LIVE["commands"], LIVE["files"])):
        assert refused, "precondition: the live draft is refused"
        assert sr.shape_retry_note({"kind": "run", "refused": refused}), \
            "an unregistered refusal produces no redraft — the §17.1269 dead end"


def test_the_redraft_note_carries_the_remedy_the_rules_teach():
    import app.modules.supervised_runs as sr
    note = sr.shape_retry_note({"kind": "run", "refused":
                                a_file_read_on_another_machine(LIVE["commands"], LIVE["files"])})
    assert "-d @-" in note and "pct exec" in note


def test_both_kinds_are_named_so_a_repeat_is_detectable():
    """§17.1277 — `refusal_kinds` is how a redraft that repeats the same shape is
    recognised; a refusal missing from the registry is invisible to it."""
    import app.modules.supervised_runs as sr
    kinds = sr.refusal_kinds({"refused":
        a_file_read_on_another_machine(LIVE["commands"], LIVE["files"])
        + a_program_that_uses_a_name_it_never_defines(LIVE["commands"], LIVE["files"])})
    assert kinds == {"different filesystems", "never imports or assigns"}, kinds


# --------------------------------------------------- wired, and failing loud

def test_both_gates_run_in_frame_run():
    import inspect

    import app.modules.supervised_runs as sr
    src = inspect.getsource(sr.frame_run)
    assert "a_program_that_uses_a_name_it_never_defines(cmds, shape_files)" in src
    assert "a_file_read_on_another_machine(cmds, shape_files)" in src


def test_a_signature_drift_in_the_service_gates_is_not_swallowed():
    """§17.1359 — a swallowed TypeError here once disabled ~15 gates for a day."""
    import pathlib as _p
    src = (_p.Path(__file__).parents[1] / "app" / "modules" / "supervised_runs.py").read_text()
    body = src.split("service_truth_gates_failed")[0]
    assert "except TypeError:" in body.rsplit("_st.loopback_on_the_host", 1)[1]


def test_an_unparseable_program_is_not_judged():
    """Not every heredoc is Python the gate can read; silence beats a guess."""
    assert a_program_that_uses_a_name_it_never_defines([], _block(
        "python3 - <<'EOF'\nthis is ( not python\nEOF\n")) == []
