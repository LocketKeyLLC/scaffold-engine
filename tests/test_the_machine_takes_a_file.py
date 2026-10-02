"""§17.1271 — the channel can be handed a FILE, so nothing has to be quoted.

THE UNDERLYING ISSUE behind a run of near-identical defects, and the operator's
own diagnosis of this session: *"we keep just putting a bandaid on to a gushing
wound."*

The supervised channel forbids a heredoc and a redirect for good reasons, and the
only way left to put a 90-line script on the machine was

    printf '%s\n' 'line' 'line' … | tee /tmp/add_indexers.py

where the shell's quoting and the script's own quoting collide. THREE of four
drafts for ADD115 died there, on the same mistake, with the rule and a worked
example in the prompt both times:

    print(f"added: {entry[\"name\"]}")

Inside a single-quoted shell word that backslash is literal, so the interpreter
is handed `{entry[\"name\"]}` and refuses it. §17.1270 repairs that one shape;
this removes the need to quote at all. The content crosses as an MCP parameter,
no shell sees it, and the approval covers the path and a digest of the exact
bytes.
"""
import hashlib
import importlib.util
import inspect
import os
import pathlib
import tempfile

import pytest

from app.modules import assist_supervised as sw
from app.modules import supervised_runs as sr

REPO = pathlib.Path(__file__).resolve().parent.parent


def _runner():
    spec = importlib.util.spec_from_file_location("lr", REPO / "scripts" / "local_runner_mcp.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ───────────────────────────── the contract between the two ends

def test_the_canonical_message_is_byte_equal_at_both_ends():
    """Both ends must derive the SAME bytes or every write is refused — the same
    rule the other approval helpers already live under."""
    r = _runner()
    assert inspect.getsource(r.write_file_message) == inspect.getsource(sw.write_file_message)
    assert r.WRITE_FILE_VERB == sw.WRITE_FILE_VERB


def test_the_message_binds_the_path_and_the_content():
    """A digest, not the content: a 40 KB script does not belong in a log line,
    and a digest pins it exactly as well."""
    a = sw.write_file_message("/tmp/x.py", "print(1)")
    assert a != sw.write_file_message("/tmp/x.py", "print(2)"), "content must be bound"
    assert a != sw.write_file_message("/tmp/y.py", "print(1)"), "path must be bound"
    assert hashlib.sha256(b"print(1)").hexdigest() in a


def test_an_approval_for_one_file_does_not_verify_another():
    r = _runner()
    msg = sw.write_file_message("/tmp/x.py", "print(1)")
    ap = sw.mint_approval(msg, "tok")
    assert r.verify_approval(msg, ap, "tok", seen={})[0]
    other = sw.write_file_message("/tmp/x.py", "import os; os.system('rm -rf /')")
    assert not r.verify_approval(other, ap, "tok", seen={})[0]


# ───────────────────────────── the runner's path guards

@pytest.mark.parametrize("path,fragment", [
    ("x.py", "not an absolute path"),
    ("/tmp/../etc/passwd", "'..'"),
    ("/etc/passwd", "outside"),
])
def test_a_write_outside_the_allowed_directories_is_refused(path, fragment):
    r = _runner()
    ok, why = r.write_file_allowed(path, ["/tmp"])
    assert not ok and fragment in why


def test_a_symlink_cannot_redirect_the_write():
    """The classic way /tmp becomes /etc — at the target and at the parent."""
    r = _runner()
    with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as outside:
        os.symlink("/etc/passwd", f"{d}/evil")
        ok, why = r.write_file_allowed(f"{d}/evil", [d])
        assert not ok and "symlink" in why
        os.symlink(outside, f"{d}/door")
        ok, why = r.write_file_allowed(f"{d}/door/x", [d])
        assert not ok and "outside" in why


def test_file_writing_is_off_without_an_allowed_directory():
    """The write channel being on does not imply this: it is its own grant."""
    r = _runner()
    ok, why = r.write_file_allowed("/tmp/x", [])
    assert not ok and "writing files is off" in why


def test_the_default_is_scratch_space_only():
    r = _runner()
    assert r.DEFAULT_WRITE_FILE_DIRS == ("/tmp",)
    assert r.write_file_allowed("/tmp/add_indexers.py", list(r.DEFAULT_WRITE_FILE_DIRS))[0]


# ───────────────────────────── the notation, and what it fixes

_RUNBOOK = '''## Write these files

### /tmp/add_indexers.py
```python
import json
entry = {"name": "Anidex"}
print(f"added: {entry["name"]}")
```

## Run this

```bash
python3 /tmp/add_indexers.py
```

## Verify

- how many indexers: `curl -s http://192.168.1.21:9696/api/v1/indexer`
'''


def test_the_quoting_that_killed_three_drafts_is_simply_valid_here():
    """The whole case for the feature: written plainly, the exact content that
    could not survive `printf … | tee` is valid Python."""
    files = sr.file_writes(_RUNBOOK)
    assert [f["path"] for f in files] == ["/tmp/add_indexers.py"]
    assert 'entry["name"]' in files[0]["content"], "quotes must survive verbatim"
    assert sr.file_writes_will_not_work(files) == []


def test_a_python_file_is_compiled_before_it_is_offered():
    bad = [{"path": "/tmp/x.py", "content": "def f(\n"}]
    found = sr.file_writes_will_not_work(bad)
    assert found and "not valid Python" in found[0]["why"]
    assert "goes through a shell" in found[0]["why"], "say why escaping is not the fix"
    assert "need no escaping" in found[0]["why"]


def test_a_file_the_engine_cannot_judge_is_written_as_given():
    assert sr.file_writes_will_not_work([{"path": "/tmp/notes.txt", "content": "anything\n"}]) == []


@pytest.mark.parametrize("path", ["relative.py", "/tmp/../etc/x.py"])
def test_the_engine_refuses_what_the_runner_would_refuse(path):
    """Both ends judge the path, so the operator sees the refusal at draft time
    instead of a failed write after approving."""
    assert sr.file_writes_will_not_work([{"path": path, "content": "x = 1\n"}])


def test_an_empty_fence_is_not_a_file():
    assert sr.file_writes_will_not_work([{"path": "/tmp/x.py", "content": "   "}])


def test_a_path_without_a_fence_is_not_guessed_at():
    """Prose under a path heading is someone thinking out loud, not a file."""
    assert sr.file_writes("## Write these files\n\n### /tmp/x.py\njust prose\n") == []


def test_no_section_means_no_files():
    assert sr.file_writes("## Run this\n\n```bash\nls\n```\n") == []


# ───────────────────────────── capability, and not trapping an old runner

def test_the_notation_is_only_offered_when_the_runner_has_the_tool():
    """An older helper cannot take a file, and telling the drafter to use a
    notation that machine would refuse is how a capability becomes a trap."""
    src = pathlib.Path(sr.__file__).read_text(encoding="utf-8")
    i = src.index("system = EXECUTION_SYSTEM_RUNBOOK")
    window = src[i:i + 900]
    assert 'can_write_files' in window and "FILE_RULES" in window


def test_the_capability_travels_with_the_policy():
    src = pathlib.Path(sw.__file__).read_text(encoding="utf-8")
    assert 'pol["can_write_files"] = can_write_files' in src
    assert "WRITE_FILE_TOOL in names" in src


def test_an_old_runner_gets_no_file_section_parsed():
    """Belt and braces: even if a draft used the notation, a runner that cannot
    take a file must not have one framed for it."""
    import ast
    src = pathlib.Path(sr.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "frame_run")
    body = ast.unparse(fn)
    assert 'file_writes(runbook) if (policy or {}).get(\'can_write_files\')' in body


# ───────────────────────────── the run, and what the operator approves

def test_the_frame_carries_every_file_with_its_content():
    """An approval covers the files as well as the commands, so they are on the
    card — §17.1270's rule, applied to a bigger payload."""
    src = pathlib.Path(sr.__file__).read_text(encoding="utf-8")
    i = src.index("def frame_run(")
    assert '"files": [{"path": f["path"]' in src[i:]
    spa = (pathlib.Path(sr.__file__).parents[1] / "ui" / "static" / "views" / "theater.js").read_text(encoding="utf-8")
    assert "d.files" in spa and "Would write" in spa


def test_files_are_written_before_the_commands_that_run_them():
    src = pathlib.Path(sr.__file__).read_text(encoding="utf-8")
    i = src.index("async def resolve_run(")
    body = src[i:]
    assert body.index("write_files_on(spec, _files)") < body.index("run_block(spec, runnable")


def test_the_success_count_includes_the_files():
    """A block whose file failed to write must not read as a clean run."""
    src = pathlib.Path(sr.__file__).read_text(encoding="utf-8")
    i = src.index("async def resolve_run(")
    assert "len(_files) + len(runnable)" in src[i:]


def test_a_write_record_has_the_same_shape_as_a_command_record():
    """One sequence, one success computation, no special case."""
    src = pathlib.Path(sw.__file__).read_text(encoding="utf-8")
    i = src.index("async def write_files_on(")
    rec = src[i:src.index("async def run_block(", i)]
    for field in ('"ok"', '"exit"', '"unreachable"', '"informational"', '"command"', '"output"'):
        assert field in rec, f"a write record is missing {field}"


# ───── §17.1273 — a capability that never reaches its reader is dead code

@pytest.mark.asyncio
async def test_write_policy_RETURNS_the_capability(monkeypatch):
    """§17.1273 — this is the test §17.1271 should have had.

    `can_write_files` was set on the parsed response and then dropped by the
    fresh dict `write_policy` returns, so no caller ever saw it: the file
    notation was never added to a prompt and `frame_run` never parsed a file
    section, on a runner that HAD the tool. Live, measured in the trace: the
    system prompt came back 8,014 characters with no file rule in it.

    The §17.1271 test asserted that the ASSIGNMENT existed in the source, which
    is vacuous with respect to what the function hands back. This drives the real
    function and reads its return value.
    """
    import json as _json
    from app.modules import mcp_client

    class _Res:
        def __init__(self, text): self.text, self.structured = text, None

    async def _tools(spec, *, use_cache=True):
        return [{"name": n} for n in ("run_readonly", "write_policy", "write_file", "run_supervised")]

    async def _call(spec, tool, args):
        return _Res(_json.dumps({"allow": ["ANY"], "sudo": True, "helper": "18", "secrets": []}))

    monkeypatch.setattr(mcp_client, "list_tools", _tools)
    monkeypatch.setattr(mcp_client, "call_tool", _call)
    spec = type("S", (), {"name": "t-write-file", "headers": {}})()
    pol = await sw.write_policy(spec, use_cache=False)
    assert pol is not None
    assert pol.get("can_write_files") is True, "the capability must reach the caller"


@pytest.mark.asyncio
async def test_an_older_helper_reports_it_cannot(monkeypatch):
    """And the other direction, so the flag is not simply always true."""
    import json as _json
    from app.modules import mcp_client

    class _Res:
        def __init__(self, text): self.text, self.structured = text, None

    async def _tools(spec, *, use_cache=True):
        return [{"name": n} for n in ("run_readonly", "write_policy", "run_supervised")]

    async def _call(spec, tool, args):
        return _Res(_json.dumps({"allow": ["ANY"], "sudo": True, "helper": "17", "secrets": []}))

    monkeypatch.setattr(mcp_client, "list_tools", _tools)
    monkeypatch.setattr(mcp_client, "call_tool", _call)
    spec = type("S", (), {"name": "t-no-write-file", "headers": {}})()
    pol = await sw.write_policy(spec, use_cache=False)
    assert pol is not None and pol.get("can_write_files") is False


def test_every_capability_the_probe_learns_is_carried_to_callers():
    """The drift guard: a key set on `pol` and then not listed in the dict the
    function returns is invisible, which is how this one hid."""
    import ast
    src = pathlib.Path(sw.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "write_policy")
    body = ast.unparse(fn)
    # keys the probe assigns onto the parsed response for later use
    assigned = {m for m in ("can_write_files",) if f"pol['{m}']" in body or f'pol["{m}"]' in body}
    i = body.index("out = ")
    returned = body[i:]
    missing = sorted(k for k in assigned if k not in returned)
    assert not missing, f"write_policy learns {missing} and never hands it to a caller"


# ───── §17.1274 — the gates read the file, follow the wrapper, and survive a corpse
#
# Live, the first frame drawn after §17.1273 made the file channel real came back
# `refused: []` around a 103-line script looping over every public indexer, and
# the run died at indexer 40 of 89 on a TimeoutError its `except HTTPError` never
# saw. Two gates had gone blind the day the channel opened (they read commands,
# the script was a FILE), one would have been blind anyway (the loop called a
# local `api_post`, not `urlopen`), and the survive-a-corpse rule was prose.

def _fx(name: str) -> str:
    return (pathlib.Path(__file__).parent / "fixtures" / name).read_text(encoding="utf-8")


def _file(path: str, content: str) -> list[dict]:
    return [{"path": path, "content": content}]


def test_the_live_file_that_ran_is_refused_as_a_file():
    from app.modules import supervised_runs as sr
    src = _fx("add115_file_loop.py")
    assert sr.loops_the_network_without_a_budget(["python3 /tmp/add_indexers.py"]) == [], \
        "the command alone says nothing about the file -- which is how it got through"
    found = sr.loops_the_network_without_a_budget(["python3 /tmp/add_indexers.py"],
                                                  _file("/tmp/add_indexers.py", src))
    assert found, "the script that ran live loops over `public` -- however many the schema returns"
    assert found[0]["command"] == "the file /tmp/add_indexers.py"
    assert "`public`" in found[0]["why"] and "api_post" in found[0]["why"], found[0]["why"]


def test_the_live_file_dies_on_one_hang():
    from app.modules import supervised_runs as sr
    found = sr.loop_dies_on_one_dead_party(["python3 /tmp/add_indexers.py"],
                                           _file("/tmp/add_indexers.py", _fx("add115_file_loop.py")))
    assert found and found[0]["command"] == "the file /tmp/add_indexers.py"
    why = found[0]["why"]
    assert "TimeoutError" in why and "api_post" in why and "dies at the first one that hangs" in why
    assert "URLError" in why and "INSIDE" in why, "the remedy must be concrete"


def test_the_apps_file_with_a_literal_list_is_left_alone():
    """`apps = [radarr, sonarr]` is this machine's own two things; neither gate
    may touch it -- the vacuity check that keeps the second half of ADD115 runnable."""
    from app.modules import supervised_runs as sr
    files = _file("/tmp/connect_apps.py", _fx("add115_file_apps.py"))
    assert sr.loops_the_network_without_a_budget(["python3 /tmp/connect_apps.py"], files) == []
    assert sr.loop_dies_on_one_dead_party(["python3 /tmp/connect_apps.py"], files) == []


def test_a_wrapper_around_a_network_call_counts():
    import ast
    from app.modules import supervised_runs as sr
    # named so it is NOT itself in _NETWORK_CALL -- the live wrapper was `api_post`
    src = "import urllib.request\ndef send_it(u):\n    return urllib.request.urlopen(u)\npublic = fetch()\nfor e in public:\n    send_it(e)\n"
    assert sr._network_wrappers(ast.parse(src)) == {"send_it"}
    found = sr.loops_the_network_without_a_budget([], _file("/tmp/w.py", src))
    assert found and "through `send_it`" in found[0]["why"]


def test_a_wrapper_of_a_wrapper_counts_too():
    import ast
    from app.modules import supervised_runs as sr
    src = ("import urllib.request\ndef raw(u):\n    return urllib.request.urlopen(u)\n"
           "def post(u):\n    return raw(u)\npublic = fetch()\nfor e in public:\n    post(e)\n")
    assert sr._network_wrappers(ast.parse(src)) == {"raw", "post"}
    assert sr.loops_the_network_without_a_budget([], _file("/tmp/w.py", src))


def test_a_local_helper_that_touches_nothing_remote_is_not_a_wrapper():
    from app.modules import supervised_runs as sr
    src = "def fmt(x):\n    return x.upper()\npublic = fetch()\nfor e in public:\n    print(fmt(e))\n"
    assert sr.loops_the_network_without_a_budget([], _file("/tmp/f.py", src)) == []
    assert sr.loop_dies_on_one_dead_party([], _file("/tmp/f.py", src)) == []


def test_catching_the_hang_at_the_call_site_passes():
    from app.modules import supervised_runs as sr
    src = ("import urllib.request, urllib.error\npublic = fetch()\nfor e in public[:10]:\n"
           "    try:\n        urllib.request.urlopen(e, timeout=15)\n"
           "    except (urllib.error.URLError, TimeoutError):\n        print('unreachable')\n")
    assert sr.loop_dies_on_one_dead_party([], _file("/tmp/g.py", src)) == []
    assert sr.loops_the_network_without_a_budget([], _file("/tmp/g.py", src)) == [], "10 × 15 s fits in 180"


def test_catching_the_hang_inside_the_wrapper_passes():
    from app.modules import supervised_runs as sr
    src = ("import urllib.request\ndef post(u):\n    try:\n        return urllib.request.urlopen(u, timeout=15)\n"
           "    except Exception:\n        return None\npublic = fetch()\nfor e in public[:10]:\n    post(e)\n")
    assert sr.loop_dies_on_one_dead_party([], _file("/tmp/h.py", src)) == []


def test_catching_only_http_errors_is_not_surviving():
    """Exactly the live shape: HTTPError is a status; a hang is an exception."""
    from app.modules import supervised_runs as sr
    src = ("import urllib.request, urllib.error\npublic = fetch()\nfor e in public[:10]:\n"
           "    try:\n        urllib.request.urlopen(e, timeout=15)\n    except urllib.error.HTTPError:\n        pass\n")
    assert sr.loop_dies_on_one_dead_party([], _file("/tmp/i.py", src))


def test_a_slice_whose_timeouts_outlive_the_budget_is_refused():
    from app.modules import supervised_runs as sr
    src = "import urllib.request\npublic = fetch()\nfor e in public[:10]:\n    urllib.request.urlopen(e, timeout=60)\n"
    found = sr.loops_the_network_without_a_budget([], _file("/tmp/j.py", src))
    assert found and "600" in found[0]["why"] and "against a 180-second budget" in found[0]["why"], found
    assert "timeout=15" in found[0]["why"]


def test_a_slice_with_short_timeouts_is_a_batch():
    from app.modules import supervised_runs as sr
    src = "import urllib.request\npublic = fetch()\nfor e in public[:10]:\n    urllib.request.urlopen(e, timeout=15)\n"
    assert sr.loops_the_network_without_a_budget([], _file("/tmp/k.py", src)) == []


def test_a_written_script_reading_a_secret_the_command_never_passes():
    from app.modules import supervised_runs as sr
    files = _file("/tmp/x.py", 'import os\nk = os.environ["PROWLARR_API_KEY"]\n')
    assert sr.script_secret_not_passed(["python3 /tmp/x.py"]) == [], "the command alone said nothing"
    found = sr.script_secret_not_passed(["python3 /tmp/x.py"], files)
    assert found and "PROWLARR_API_KEY" in found[0]["why"]
    assert sr.script_secret_not_passed(['PROWLARR_API_KEY="$PROWLARR_API_KEY" python3 /tmp/x.py'], files) == []


def test_every_payload_gate_in_frame_run_sees_the_files():
    """The drift guard: a payload gate called with `cmds` alone is blind to the
    §17.1271 channel, which is how the first three went blind."""
    import ast
    from app.modules import supervised_runs as sr
    tree = ast.parse(pathlib.Path(sr.__file__).read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "frame_run")
    gates = {"script_secret_not_passed", "loops_the_network_without_a_budget", "loop_dies_on_one_dead_party"}
    seen = set()
    for c in ast.walk(fn):
        if isinstance(c, ast.Call) and getattr(c.func, "id", None) in gates:
            seen.add(c.func.id)
            assert len(c.args) == 2 and ast.unparse(c.args[1]) in ("files", "shape_files"), f"{c.func.id} does not see the files"
    assert seen == gates, f"frame_run does not call {gates - seen}"


def test_frame_run_withholds_run_for_a_looping_file():
    """End to end at the frame: the file the operator would approve is refused
    before Run is offered, and the refusal can be redrafted (§17.1269)."""
    from app.modules import supervised_runs as sr
    runbook = ("## Write these files\n### /tmp/loop.py\n```python\n" + _fx("add115_file_loop.py") +
               "\n```\n\n## Run this\n```bash\npython3 /tmp/loop.py\n```\n\n## Verify\n- `pct list`\n")
    policy = {"allow": ["ANY"], "sudo": True, "helper": "18", "secrets": [], "can_write_files": True}
    spec = type("S", (), {"name": "t-runner", "headers": {}})()
    frame = sr.frame_run({"node_key": "ADD115", "title": "t"}, runbook, spec, policy)
    assert frame["files"] and frame["commands"] == ["python3 /tmp/loop.py"]
    assert frame["refused"], "the looping file must be refused at the frame"
    assert "run" not in {o["id"] for o in frame["options"]}
    for r in frame["refused"]:
        assert any(s in r["why"] for s in sr._SHAPE_REFUSALS), f"unredraftable refusal: {r['why'][:80]}"


def test_the_new_refusals_are_registered_for_redraft():
    from app.modules import supervised_runs as sr
    assert "dies at the first one that hangs" in sr._SHAPE_REFUSALS
    assert "against a" in sr._SHAPE_REFUSALS


def test_the_rules_say_what_the_gates_enforce():
    from app.modules import supervised_runs as sr
    r = sr.CHANNEL_RULES
    assert "URLError" in r and "TimeoutError" in r, "the hang-is-an-exception rule"
    assert "timeout=60" in r and "600 seconds" in r, "the per-request timeout rule"


def test_a_file_write_is_on_the_activity_list_without_its_content():
    """§17.1205's list is how the operator watches the machine; §17.1271 added a
    tool it never recorded. The content is the one thing it must not keep."""
    import json as _json
    from app.modules import runner_activity as ra
    ra.reset()

    class _Out:
        text = "wrote /tmp/x.py (11 bytes, 1 lines)"
        structured = None
        is_error = False

    spec = type("S", (), {"name": "r"})()
    ra.note_result(spec, "write_file", {"path": "/tmp/x.py", "content": "SECRET BODY", "approval": "a"}, _Out(), None)
    e = ra.recent()[0]
    assert e["tool"] == "write_file" and e["kind"] == "write"
    assert e["command"] == "write_file /tmp/x.py (11 bytes)"
    assert "SECRET BODY" not in _json.dumps(e)
    assert "write_file" in ra.COMMAND_TOOLS


# ───── §17.1276 — who is being called, not only what the method is named; and a
# remedy that fits the file channel.
#
# The first live outing of §17.1274: the budget gate correctly refused `for entry
# in public:`; the hang gate refused the SAME draft -- which caught every hang
# correctly -- because `entry.get("name")` matched the bare name list. And the
# redraft, told to "send several commands", batched INSIDE one script, which is
# still one command and one budget.

def test_a_dict_get_is_not_the_network():
    from app.modules import supervised_runs as sr
    src = ("import json\npublic = fetch()\nfor entry in public:\n    name = entry.get('name', 'x')\n"
           "    body = dict(entry)\n    print(json.dumps(body))\n")
    assert sr.loops_the_network_without_a_budget([], _file("/tmp/d.py", src)) == []
    assert sr.loop_dies_on_one_dead_party([], _file("/tmp/d.py", src)) == []


def test_the_hang_safe_live_draft_passes_the_hang_gate_and_only_the_budget_refuses():
    """The real first draft after §17.1274 went live: it catches (URLError,
    TimeoutError, socket.timeout, OSError) around api_post, loops over all 89."""
    from app.modules import supervised_runs as sr
    files = _file("/tmp/add_indexers.py", _fx("add115_file_hangsafe.py"))
    assert sr.loop_dies_on_one_dead_party(["python3 /tmp/add_indexers.py"], files) == [], \
        "a draft that catches the hang must not be refused for not catching it"
    found = sr.loops_the_network_without_a_budget(["python3 /tmp/add_indexers.py"], files)
    assert found and "`public`" in found[0]["why"] and "api_post" in found[0]["why"]


def test_the_file_remedy_says_argv_slices_and_one_command_per_batch():
    from app.modules import supervised_runs as sr
    found = sr.loops_the_network_without_a_budget(["python3 /tmp/add_indexers.py"],
                                                  _file("/tmp/add_indexers.py", _fx("add115_file_hangsafe.py")))
    why = found[0]["why"]
    assert "sys.argv" in why and "python3 /tmp/add_indexers.py 0 10" in why and "python3 /tmp/add_indexers.py 10 20" in why
    assert "never a loop over batches inside one script" in why
    # a command payload keeps the original remedy; the argv advice is the file channel's
    cmd_found = sr.loops_the_network_without_a_budget([_fixture_cmd()])
    assert cmd_found and "sys.argv" not in cmd_found[0]["why"]


def _fixture_cmd() -> str:
    return (pathlib.Path(__file__).parent / "fixtures" / "add115_unbounded_loop.txt").read_text(encoding="utf-8").strip()


def test_batching_inside_one_script_is_still_one_budget():
    from app.modules import supervised_runs as sr
    src = ("import urllib.request\npublic = fetch()\nfor i in range(0, len(public), 10):\n"
           "    batch = public[i:i + 10]\n    for e in batch:\n        urllib.request.urlopen(e, timeout=15)\n")
    assert sr.loops_the_network_without_a_budget([], _file("/tmp/b.py", src)), "the whole list still runs in one command"


def test_argv_slices_with_a_hang_safe_call_pass_both_gates():
    from app.modules import supervised_runs as sr
    src = ("import sys, urllib.request, urllib.error\nstart, end = int(sys.argv[1]), int(sys.argv[2])\n"
           "public = fetch()\nfor e in public[start:end]:\n    try:\n        urllib.request.urlopen(e, timeout=15)\n"
           "    except (urllib.error.URLError, TimeoutError, OSError):\n        print('unreachable')\n")
    assert sr.loops_the_network_without_a_budget([], _file("/tmp/s.py", src)) == []
    assert sr.loop_dies_on_one_dead_party([], _file("/tmp/s.py", src)) == []


@pytest.mark.parametrize("src", [
    "import requests\npublic = fetch()\nfor u in public:\n    requests.get(u)\n",
    "import requests as rq\npublic = fetch()\nfor u in public:\n    rq.post(u, json={})\n",
    "import requests\ns = requests.Session()\npublic = fetch()\nfor u in public:\n    s.get(u)\n",
    "from requests import get\npublic = fetch()\nfor u in public:\n    get(u)\n",
    "import httpx\nc = httpx.Client()\npublic = fetch()\nfor u in public:\n    c.get(u)\n",
    "import subprocess\npublic = fetch()\nfor h in public:\n    subprocess.run(['ssh', h, 'true'])\n",
])
def test_a_network_receiver_is_the_network(src):
    from app.modules import supervised_runs as sr
    assert sr.loops_the_network_without_a_budget([], _file("/tmp/n.py", src)), src


@pytest.mark.parametrize("src", [
    "public = fetch()\nfor u in public:\n    get(u)\n",                       # bare `get`, imported from nowhere
    "public = fetch()\nfor u in public:\n    cache.get(u)\n",                 # a cache, not a client
    "public = fetch()\nfor u in public:\n    job.run()\n",                    # a method called run
    "import re\npublic = fetch()\nfor u in public:\n    re.compile(u).match('x')\n",
])
def test_an_ambiguous_name_on_a_non_network_receiver_is_not(src):
    from app.modules import supervised_runs as sr
    assert sr.loops_the_network_without_a_budget([], _file("/tmp/m.py", src)) == [], src
    assert sr.loop_dies_on_one_dead_party([], _file("/tmp/m.py", src)) == [], src


def test_a_wrapper_is_still_found_through_the_receiver_rule():
    import ast
    from app.modules import supervised_runs as sr
    src = "import requests\ndef fetch_one(u):\n    return requests.get(u)\ndef helper(x):\n    return x.get('k')\n"
    assert sr._network_wrappers(ast.parse(src)) == {"fetch_one"}


# ───── §17.1276b — a constructor is not the network; the redraft note fits the channel

def test_the_second_hang_safe_live_draft_passes_the_hang_gate():
    """`req = urllib.request.Request(...)` is built OUTSIDE the wrapper's try and
    sends nothing; the urlopen inside the try catches every hang. The hang gate
    refused it live for the constructor."""
    from app.modules import supervised_runs as sr
    files = _file("/tmp/add_indexers.py", _fx("add115_file_hangsafe2.py"))
    assert sr.loop_dies_on_one_dead_party(["python3 /tmp/add_indexers.py"], files) == []
    found = sr.loops_the_network_without_a_budget(["python3 /tmp/add_indexers.py"], files)
    assert found and "api_request" in found[0]["why"]


def test_a_request_constructor_is_not_a_network_call():
    import ast
    from app.modules import supervised_runs as sr
    src = "import urllib.request\nreq = urllib.request.Request('http://x')\n"
    tree = ast.parse(src)
    call = next(c for c in ast.walk(tree) if isinstance(c, ast.Call))
    assert sr._is_network_call(call, sr._net_context(tree)) is False
    assert "Request" not in sr._NETWORK_CALL


def test_the_remedy_slices_the_collection_not_the_enumerate():
    from app.modules import supervised_runs as sr
    found = sr.loops_the_network_without_a_budget(["python3 /tmp/add_indexers.py"],
                                                  _file("/tmp/add_indexers.py", _fx("add115_file_hangsafe2.py")))
    why = found[0]["why"]
    assert "`public_indexers[:20]`" in why and "public_indexers[start:end]" in why
    assert "enumerate(public_indexers)[" not in why


def test_the_redraft_note_fits_the_file_channel():
    from app.modules import supervised_runs as sr
    refused = [{"command": "the file /tmp/x.py", "why": "the file /tmp/x.py loops over `public` -- waits on something off this machine. Bound it."}]
    note = sr.shape_retry_note({"refused": refused, "file_channel": True})
    assert "## Write these files" in note and "sys.argv" in note and "python3 /tmp/x.py 0 10" in note
    assert "printf '%s" not in note, "the file channel does not teach the printf | tee recipe"
    assert "Never a loop over batches inside one script" in note
    assert "URLError" in note
    old = sr.shape_retry_note({"refused": refused, "file_channel": False})
    assert "printf '%s" in old and "## Write these files" not in old, "a runner without the tool keeps the shell remedy"


def test_the_frame_says_which_channel_it_was_drawn_for():
    from app.modules import supervised_runs as sr
    spec = type("S", (), {"name": "t", "headers": {}})()
    runbook = "## Run this\n```bash\npct list\n```\n\n## Verify\n- `pct list`\n"
    on = sr.frame_run({"node_key": "X", "title": "t"}, runbook, spec, {"allow": ["ANY"], "can_write_files": True})
    off = sr.frame_run({"node_key": "X", "title": "t"}, runbook, spec, {"allow": ["ANY"], "can_write_files": False})
    assert on["file_channel"] is True and off["file_channel"] is False


# ───── §17.1277 — a redraft that trades one refusal for another gets one more try, told both
#
# Live, after §17.1276b: draft 1 was hang-safe and unbatched (refused: budget);
# the redraft batched by argv -- nine `python3 /tmp/add_indexers.py N M` -- and
# dropped the hang handling (refused: hang). §17.1269 kept draft 1. Each draft
# fixed the refusal it was shown and lost what the other had right, and the
# loop allowed exactly one redraft.

def test_the_batched_redraft_passes_the_budget_and_fails_only_the_hang_gate():
    from app.modules import supervised_runs as sr
    files = _file("/tmp/add_indexers.py", _fx("add115_file_batched_not_hangsafe.py"))
    cmds = [f"python3 /tmp/add_indexers.py {a} {a + 10}" for a in range(0, 90, 10)]
    assert sr.loops_the_network_without_a_budget(cmds, files) == [], "public[start:end] at timeout=15 is a batch"
    found = sr.loop_dies_on_one_dead_party(cmds, files)
    assert found and "api_request" in found[0]["why"], "it catches HTTPError and RuntimeError only"


def test_the_two_live_drafts_were_refused_for_disjoint_kinds():
    from app.modules import supervised_runs as sr
    one = {"refused": sr.loops_the_network_without_a_budget(["python3 /tmp/a.py"], _file("/tmp/a.py", _fx("add115_file_hangsafe2.py")))}
    two = {"refused": sr.loop_dies_on_one_dead_party(["python3 /tmp/a.py 0 10"], _file("/tmp/a.py", _fx("add115_file_batched_not_hangsafe.py")))}
    k1, k2 = sr.refusal_kinds(one), sr.refusal_kinds(two)
    assert k1 and k2 and not (k1 & k2), (k1, k2)


def test_refusal_kinds_reads_the_registry_markers():
    from app.modules import supervised_runs as sr
    assert sr.refusal_kinds({"refused": [{"why": "x dies at the first one that hangs y"}]}) == {"dies at the first one that hangs"}
    assert sr.refusal_kinds({"refused": [{"why": "a permission refusal"}]}) == set()
    assert sr.refusal_kinds({}) == set()


def test_the_note_told_both_keeps_what_the_earlier_draft_had_right():
    from app.modules import supervised_runs as sr
    first = {"refused": [{"command": "the file /tmp/x.py", "why": "the file /tmp/x.py loops over `public` -- waits on something off this machine."}], "file_channel": True}
    second = {"refused": [{"command": "the file /tmp/x.py", "why": "the file /tmp/x.py … dies at the first one that hangs."}], "file_channel": True}
    note = sr.shape_retry_note(second, previous=first)
    assert "THE DRAFT BEFORE THAT was refused for something different" in note
    assert "loops over `public`" in note and "dies at the first one that hangs" in note
    assert "Do not trade one refusal for another" in note
    assert "THE DRAFT BEFORE THAT" not in sr.shape_retry_note(second), "without a previous attempt the note is unchanged"


def test_the_pause_tries_a_third_time_when_the_kinds_differ():
    """Wiring: the second redraft exists, is gated on disjoint kinds, is told
    both, and a rejected redraft's reasons are logged in full."""
    import ast
    from app.modules import execution_agent as ea
    src = pathlib.Path(ea.__file__).read_text(encoding="utf-8")
    i = src.index("async def _pause_for_decision(")
    body = src[i:]
    j = body.index("\nasync def ", 10) if "\nasync def " in body[10:] else len(body)
    body = body[:j]
    assert "supervised_run_redraft_rejected" in body, "a rejected redraft's reasons must be logged in full"
    assert "refusal_kinds(frame)" in body and "refusal_kinds(second)" in body
    assert "not (k1 & k2)" in body, "the third attempt is only for DISJOINT kinds -- a loop is not progress"
    tree = ast.parse(src)
    calls = [c for c in ast.walk(tree) if isinstance(c, ast.Call) and getattr(c.func, "attr", None) == "shape_retry_note"]
    assert any(any(k.arg == "previous" for k in c.keywords) for c in calls), "the second note must carry the first attempt"
    assert "supervised_run_redraft_again_rejected" in body, "a third refusal parks on the best frame and says so"


# ───── §17.1277b — bounded through assignment; the batching shape stated up front

def test_the_correct_argv_redraft_passes_both_gates():
    """The live redraft the budget gate refused: `batch = public[start:end]`
    then `for entry in batch:` -- a slice the author chose, by another name."""
    import ast
    from app.modules import supervised_runs as sr
    src = _fx("add115_file_argv_batched.py")
    files = _file("/tmp/add_indexers.py", src)
    cmds = [f"python3 /tmp/add_indexers.py {a} {min(a + 10, 89)}" for a in range(0, 89, 10)]
    assert "batch" in sr._bounded_names(ast.parse(src))
    assert sr.loops_the_network_without_a_budget(cmds, files) == []
    assert sr.loop_dies_on_one_dead_party(cmds, files) == []


def test_a_name_assigned_a_slice_is_bounded_and_a_comprehension_is_not():
    import ast
    from app.modules import supervised_runs as sr
    tree = ast.parse("public = [d for d in schema if d]\nbatch = public[a:b]\nagain = batch\n")
    names = sr._bounded_names(tree)
    assert "batch" in names and "again" in names and "public" not in names


def test_the_budget_arithmetic_sees_through_the_name():
    from app.modules import supervised_runs as sr
    src = "import urllib.request\npublic = fetch()\nbatch = public[:10]\nfor e in batch:\n    urllib.request.urlopen(e, timeout=60)\n"
    found = sr.loops_the_network_without_a_budget([], _file("/tmp/t.py", src))
    assert found and "600" in found[0]["why"]


def test_a_name_reassigned_to_a_response_is_still_unbounded():
    import ast
    from app.modules import supervised_runs as sr
    assert "items" not in sr._bounded_names(ast.parse("items = [1, 2]\nitems = fetch()\n"))


def test_file_rules_state_the_batching_shape_and_the_enforced_budget():
    from app.modules import supervised_runs as sr
    from app.modules.assist_supervised import RUN_COMMAND_TIMEOUT_S
    r = sr.FILE_RULES
    assert "sys.argv" in r and "items[start:end]" in r and "python3 /tmp/add_indexers.py 10 20" in r
    assert f"{int(RUN_COMMAND_TIMEOUT_S)}-second budget" in r, "the number in the prompt must be the one enforced"
    assert "INSIDE one script is still one" in r


def test_a_range_over_the_lists_length_is_not_bounded():
    from app.modules import supervised_runs as sr
    src = ("import urllib.request\npublic = fetch()\nfor i in range(0, len(public), 10):\n    batch = public[i:i + 10]\n"
           "    for e in batch:\n        urllib.request.urlopen(e, timeout=15)\n")
    found = sr.loops_the_network_without_a_budget([], _file("/tmp/r.py", src))
    assert found and "range(0, len(public), 10)" in found[0]["why"], "name the OUTERMOST unbounded loop"


def test_a_range_of_a_constant_is_bounded():
    from app.modules import supervised_runs as sr
    src = "import urllib.request\nfor i in range(3):\n    urllib.request.urlopen('http://x', timeout=15)\n"
    assert sr.loops_the_network_without_a_budget([], _file("/tmp/c.py", src)) == []


# ───── §17.1278 — whether a field is NAMED, not whether the key is present
#
# The clean argv-batched block ran: batch 1 stopped on its FIRST indexer with
# `"propertyName": ""` + "Unable to connect to indexer … 502" -- an availability
# error by the engine's own §17.1266 rule -- because the script tested
# `"propertyName" in str(resp)`. The key is always there.

def test_the_live_script_that_tested_the_key_is_refused():
    from app.modules import supervised_runs as sr
    files = _file("/tmp/add_indexers.py", _fx("add115_file_key_presence.py"))
    found = sr.classifies_by_key_presence(["python3 /tmp/add_indexers.py 0 10"], files)
    assert found and found[0]["command"] == "the file /tmp/add_indexers.py"
    why = found[0]["why"]
    assert "'propertyName' in str(resp)" in why, "quote the test it made (ast-rendered)"
    assert "NAMED" in why and "e.get('propertyName') or ''" in why
    assert any(s in why for s in sr._SHAPE_REFUSALS), "must be redraftable"


def test_testing_whether_a_field_is_named_passes():
    from app.modules import supervised_runs as sr
    src = ("errors = resp if isinstance(resp, list) else []\n"
           "named = any((e.get('propertyName') or '').strip() for e in errors)\n"
           "if named:\n    sys.exit(1)\n")
    assert sr.classifies_by_key_presence([], _file("/tmp/ok.py", src)) == []


@pytest.mark.parametrize("line", [
    'if "propertyName" in resp:',
    'if "propertyName" in body_text:',
    'if "propertyName" not in str(resp):',
    "if 'propertyName' in json.dumps(resp):",
])
def test_every_membership_test_of_the_key_is_caught(line):
    from app.modules import supervised_runs as sr
    assert sr.classifies_by_key_presence([], _file("/tmp/k.py", f"{line}\n    pass\n"))


def test_the_key_presence_gate_sees_the_files_in_frame_run():
    import ast
    from app.modules import supervised_runs as sr
    tree = ast.parse(pathlib.Path(sr.__file__).read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "frame_run")
    calls = [c for c in ast.walk(fn) if isinstance(c, ast.Call) and getattr(c.func, "id", None) == "classifies_by_key_presence"]
    assert calls and len(calls[0].args) == 2 and ast.unparse(calls[0].args[1]) in ("files", "shape_files")


def test_the_live_output_is_judged_as_stopped_on_a_dead_party():
    from app.modules import supervised_runs as sr
    why = sr.stopped_on_a_dead_party(_fx("add115_output_stopped_on_a_corpse.txt"))
    assert why and "Anidex" in why and "no field named" in why and "502" in why
    assert "nothing in the body was wrong" in why


def test_a_named_field_is_a_real_validation_failure():
    from app.modules import supervised_runs as sr
    body = 'VALIDATION FAILURE on X: [{"propertyName": "Name", "errorMessage": "Should be unique"}]'
    assert sr.stopped_on_a_dead_party(body) is None
    assert sr.stopped_on_a_dead_party('[{"propertyName": "", "errorMessage": "must be between 1 and 50"}]') is None, \
        "an empty field with a non-availability message is not a corpse"
    assert sr.stopped_on_a_dead_party("") is None


def test_the_dead_party_judgment_is_wired_into_the_failure_reason():
    from app.modules import supervised_runs as sr
    src = pathlib.Path(sr.__file__).read_text(encoding="utf-8")
    i = src.index("async def resolve_run(")
    body = src[i:]
    assert "dead_reason = ((stopped_on_a_dead_party(" in body
    j = body.index("reason = mask_secrets(")
    assert "dead_reason if dead_reason else" in body[j:j + 3000], "the judgment must reach the reason the next draft reads"
    assert body.index("dead_reason if dead_reason else") < body.index("repeated_reason if repeated_reason else"), \
        "a corpse is judged before a repeat"


# ───── §17.1278b — the body's SHAPE decides, not a phrase list

def test_the_live_phrase_list_script_is_refused():
    from app.modules import supervised_runs as sr
    found = sr.classifies_by_key_presence(["python3 /tmp/add_indexers.py 0 10"],
                                          _file("/tmp/add_indexers.py", _fx("add115_file_phrase_list.py")))
    assert found and "by a phrase list" in found[0]["why"]
    assert "'unable to connect'" in found[0]["why"] and "CloudFlare" in found[0]["why"]
    assert any(s in found[0]["why"] for s in sr._SHAPE_REFUSALS)


def test_the_shape_rule_alone_passes():
    from app.modules import supervised_runs as sr
    src = ("prop = (first.get('propertyName') or '').strip()\nif prop:\n    sys.exit(1)\nelse:\n"
           "    unreachable.append(name)\nif 'should be unique' in body_text.lower():\n    already += 1\n")
    assert sr.classifies_by_key_presence([], _file("/tmp/ok.py", src)) == []


def test_the_cloudflare_output_is_judged_as_a_corpse_too():
    from app.modules import supervised_runs as sr
    why = sr.stopped_on_a_dead_party(_fx("add115_output_stopped_on_cloudflare.txt"))
    assert why and "0Magnet" in why and "CloudFlare" in why and "no field named" in why


# ───── §17.1278c — a 400 branch that never reads the field; the classifier, given

def test_the_live_script_that_never_read_the_field_is_refused():
    from app.modules import supervised_runs as sr
    found = sr.classifies_by_key_presence(["python3 /tmp/add_indexers.py 0 10"],
                                          _file("/tmp/add_indexers.py", _fx("add115_file_no_field_test.py")))
    assert found and "without reading the body's `propertyName`" in found[0]["why"]
    assert "def whose_fault(errors):" in found[0]["why"], "the remedy is the code, not a sentence"
    assert any(s in found[0]["why"] for s in sr._SHAPE_REFUSALS)


def test_the_given_helper_passes_every_gate_it_is_pasted_under():
    from app.modules import supervised_runs as sr
    src = ("import json, urllib.request, urllib.error\n" + sr.WHOSE_FAULT_HELPER +
           "\nstatus, raw = post()\nif status == 400:\n    verdict = whose_fault(json.loads(raw))\n"
           "    if verdict == 'bad_request':\n        sys.exit(1)\n")
    files = _file("/tmp/h.py", src)
    assert sr.classifies_by_key_presence([], files) == []
    assert sr.payload_will_not_compile([]) == []
    assert sr.file_writes_will_not_work(files) == []


def test_a_400_branch_without_the_field_is_the_defect_not_any_400_mention():
    """A script with no 400 branch at all is not refused for lacking the field."""
    from app.modules import supervised_runs as sr
    assert sr.classifies_by_key_presence([], _file("/tmp/x.py", "import urllib.request\nurllib.request.urlopen('http://x')\n")) == []


def test_the_rules_carry_the_helper_verbatim():
    from app.modules import supervised_runs as sr
    assert sr.WHOSE_FAULT_HELPER.strip() in sr.CHANNEL_RULES
    assert "paste this helper" in sr.CHANNEL_RULES


# ───── §17.1279 — a template is not a bad request

def _helper():
    from app.modules import supervised_runs as sr
    ns: dict = {}
    exec(sr.WHOSE_FAULT_HELPER, ns)
    return ns["whose_fault"]


def test_the_given_helper_tells_the_four_verdicts_apart():
    wf = _helper()
    template = [{"propertyName": "BaseUrl", "errorMessage": "'Base Url' must not be empty.", "attemptedValue": ""},
                {"propertyName": "BaseUrl", "errorMessage": "must be valid URL that starts with http(s)://", "attemptedValue": ""}]
    assert wf(template) == "needs_input", "Torrent RSS Feed wants a URL only the operator has"
    assert wf([{"propertyName": "AppProfileId", "errorMessage": "'App Profile Id' must be greater than '0'", "attemptedValue": 0}]) == "bad_request"
    assert wf([{"propertyName": "AppProfileId", "errorMessage": "required", "attemptedValue": None}]) == "bad_request", "null is not a typed-empty field"
    assert wf([{"propertyName": "", "errorMessage": "Unable to access 16mag.net, blocked by CloudFlare Protection."}]) == "unreachable"
    assert wf([{"propertyName": "Name", "errorMessage": "Should be unique", "attemptedValue": "YTS"}]) == "duplicate"
    assert wf("not a list") == "unreachable", "garbage is not a named field"


def test_a_stale_pasted_helper_is_refused_and_the_current_one_passes():
    from app.modules import supervised_runs as sr
    stale = _file("/tmp/add_indexers.py", _fx("add115_file_stale_helper.py"))
    found = sr.classifies_by_key_presence(["python3 /tmp/add_indexers.py 0 10"], stale)
    assert found and "has no 'needs_input' verdict" in found[0]["why"] and "def whose_fault" in found[0]["why"]
    current = _file("/tmp/c.py", "import json\n" + sr.WHOSE_FAULT_HELPER + "\nif status == 400:\n    v = whose_fault(json.loads(raw))\n")
    assert sr.classifies_by_key_presence([], current) == []


def test_the_template_stop_is_judged_by_name_and_field():
    from app.modules import supervised_runs as sr
    why = sr.stopped_on_a_template(_fx("add115_output_stopped_on_a_template.txt"))
    assert why and "Torrent RSS Feed" in why and "BaseUrl" in why and "not a bad request" in why
    assert sr.stopped_on_a_template(_fx("add115_output_stopped_on_cloudflare.txt")) is None, "a corpse is the other judgment's"
    assert sr.stopped_on_a_dead_party(_fx("add115_output_stopped_on_a_template.txt")) is None, "and a template is not a corpse"


def test_the_template_judgment_reaches_the_reason():
    from app.modules import supervised_runs as sr
    src = pathlib.Path(sr.__file__).read_text(encoding="utf-8")
    i = src.index("async def resolve_run(")
    assert "or stopped_on_a_template(" in src[i:]


def test_unreachable_lines_are_not_repeated_failures():
    """38 `unreachable: <name>` lines across a run must never read as one error repeated."""
    from app.modules import supervised_runs as sr
    out = "\n".join(f"unreachable: tracker{i} (HTTP 400: blocked by CloudFlare)" for i in range(38))
    assert sr.repeated_identical_failures(out) is None


# ───── §17.1280 — a placeholder inside a written file is an input too
#
# Live: both scripts carried `PROWLARR_URL = "http://<PROWLARR_IP>:9696"`; the
# placeholder pipeline read commands and verify lines only, the frame said
# `inputs: []`, nothing was asked or discovered, and the file reached the disk
# with the literal `<PROWLARR_IP>` -- `urlopen error [Errno -2] Name or service
# not known`.

def test_the_live_files_placeholders_are_inputs():
    from app.modules import supervised_runs as sr
    files = [{"path": "/tmp/add_indexers.py", "content": _fx("add115_file_placeholders.py")},
             {"path": "/tmp/connect_apps.py", "content": _fx("add115_file_placeholders_apps.py")}]
    cmds = ["python3 /tmp/add_indexers.py 0 10", "python3 /tmp/connect_apps.py"]
    assert sr.inputs_for(cmds, [], "") == [], "the commands alone name no input -- which is how it got through"
    names = [i["name"] for i in sr.inputs_for(cmds, [], "", files)]
    assert names == ["PROWLARR_IP", "RADARR_IP", "SONARR_IP"], names
    assert all(not i["secret"] for i in sr.inputs_for(cmds, [], "", files))


def test_fill_files_substitutes_and_never_leaves_a_placeholder():
    from app.modules import supervised_runs as sr
    files = [{"path": "/tmp/x.py", "content": 'URL = "http://<PROWLARR_IP>:9696"\n'}]
    filled, left = sr.fill_files(files, {"PROWLARR_IP": "192.168.1.21"})
    assert filled[0]["content"] == 'URL = "http://192.168.1.21:9696"\n' and left == []
    _, left = sr.fill_files(files, {})
    assert left == [{"name": "PROWLARR_IP", "why": "missing (used in the file /tmp/x.py)"}]


def test_a_secret_placeholder_in_a_file_is_refused_with_the_remedy():
    from app.modules import supervised_runs as sr
    files = [{"path": "/tmp/x.py", "content": 'KEY = "<PROWLARR_API_KEY>"\n'}]
    inputs = sr.inputs_for(["python3 /tmp/x.py"], [], "", files)
    found = sr.secrets_in_files(files, inputs)
    assert found and "a secret cannot be written into a file" in found[0]["why"]
    assert "config.xml" in found[0]["why"] and 'os.environ["PROWLARR_API_KEY"]' in found[0]["why"]
    assert any(s in found[0]["why"] for s in sr._SHAPE_REFUSALS)
    assert sr.secrets_in_files([{"path": "/tmp/y.py", "content": 'IP = "<PROWLARR_IP>"\n'}],
                               sr.inputs_for([], [], "", [{"path": "/tmp/y.py", "content": 'IP = "<PROWLARR_IP>"\n'}])) == []


def test_frame_run_asks_for_a_files_placeholder_and_keeps_the_file_as_drafted():
    from app.modules import supervised_runs as sr
    runbook = ("## Write these files\n### /tmp/x.py\n```python\nimport urllib.request\n"
               "URL = \"http://<PROWLARR_IP>:9696\"\nurllib.request.urlopen(URL, timeout=15)\n```\n\n"
               "## Run this\n```bash\npython3 /tmp/x.py\n```\n\n## Verify\n- `pct list`\n")
    spec = type("S", (), {"name": "t", "headers": {}})()
    frame = sr.frame_run({"node_key": "X", "title": "t"}, runbook, spec, {"allow": ["ANY"], "can_write_files": True})
    assert [i["name"] for i in frame["inputs"]] == ["PROWLARR_IP"]
    assert "<PROWLARR_IP>" in frame["files"][0]["content"], "the frame keeps the draft; resolve fills the real value"
    assert frame["refused"] == [], frame["refused"]
    assert "run" in {o["id"] for o in frame["options"]}


def test_resolve_run_fills_the_files_before_writing_them():
    from app.modules import supervised_runs as sr
    src = pathlib.Path(sr.__file__).read_text(encoding="utf-8")
    i = src.index("async def resolve_run(")
    body = src[i:]
    assert body.index("_files, _left = fill_files(_files, values)") < body.index("write_files_on(spec, _files)")
    assert '"outcome": "inputs_missing", "problems": _left' in body


def test_every_payload_gate_in_frame_run_judges_the_filled_files():
    """The gates must see the file as it will be written (dummies in), and the
    secret check must run: a `<X>` outside a string would otherwise look like a
    syntax error, and a secret would be asked for instead of refused."""
    from app.modules import supervised_runs as sr
    src = pathlib.Path(sr.__file__).read_text(encoding="utf-8")
    i = src.index("def frame_run("); body = src[i:src.index("\ndef ", i + 10)]
    for g in ("file_writes_will_not_work", "script_secret_not_passed", "loops_the_network_without_a_budget",
              "loop_dies_on_one_dead_party", "classifies_by_key_presence"):
        assert f"{g}(cmds, shape_files)" in body or f"{g}(shape_files)" in body, g
    assert "secrets_in_files(files, inputs)" in body
