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


# ───── §17.1281 — a check that answers nothing is not a check; a secret never reaches the record
#
# ADD115 ran clean and was marked done -- on four verify checks that came back
# BLANK: `curl -s -H "X-Api-Key: $PROWLARR_API_KEY"` through `run_readonly`, which
# expands nothing, so the request carried an empty key. And the fifth check
# `cat`'d config.xml: the key the engine stores by reference reached the stored
# record in clear.

@pytest.mark.asyncio
async def test_a_check_that_references_a_secret_runs_with_the_blocks_environment(monkeypatch):
    from app.modules import assist_local_runner as lr, assist_supervised as sw, supervised_runs as sr
    sent = {"readonly": [], "supervised": [], "env": None}

    async def fake_probes(spec, probes, **kw):
        sent["readonly"] += [p["command"] for p in probes]
        return "".join(f'== {p["id"]} ==\nplain-answer\n' for p in probes), [{"id": p["id"], "command": p["command"], "ok": True, "ran": True} for p in probes]

    async def fake_block(spec, cmds, *, env=None, **kw):
        sent["supervised"] += cmds; sent["env"] = env
        return [{"command": c, "output": '[{"id": 1}]', "exit": 0, "ok": True, "refused": False, "unreachable": False} for c in cmds]

    monkeypatch.setattr(lr, "run_probes", fake_probes)
    monkeypatch.setattr(sw, "run_block", fake_block)
    cmds = ["pct exec 102 -- cat /var/lib/prowlarr/config.xml",
            'curl -s -H "X-Api-Key: $PROWLARR_API_KEY" http://192.168.1.21:9696/api/v1/indexer',
            "pct list"]
    pasted, ran = await sr.run_verify(type("S", (), {"name": "r"})(), cmds, {"PROWLARR_API_KEY": "k"})
    assert sent["readonly"] == [cmds[0], cmds[2]] and sent["supervised"] == [cmds[1]]
    assert sent["env"] == {"PROWLARR_API_KEY": "k"}, "the referenced value travels with the check"
    assert "== V2 ==\n[{\"id\": 1}]" in pasted and "== V1 ==\nplain-answer" in pasted and "== V3 ==" in pasted
    assert [r["id"] for r in ran] == ["V1", "V2", "V3"]
    assert sr._has_evidence(pasted, ran)


@pytest.mark.asyncio
async def test_a_refused_or_unreachable_check_leaves_no_marker(monkeypatch):
    from app.modules import assist_supervised as sw, supervised_runs as sr

    async def fake_block(spec, cmds, *, env=None, **kw):
        return [{"command": c, "output": "(refused by the local runner: no)", "exit": None, "ok": False, "refused": True, "unreachable": False} for c in cmds]

    monkeypatch.setattr(sw, "run_block", fake_block)
    pasted, ran = await sr.run_verify(type("S", (), {"name": "r"})(), ['curl -H "X-Api-Key: $K" http://x'], {"K": "v"})
    assert pasted == "" and ran and ran[0]["ran"] is False, "no marker: not evidence (§17.1204)"


def test_scrub_masks_held_values_and_secret_shapes():
    from app.modules import supervised_runs as sr
    text = ("<Config>\n  <ApiKey>d1203e8a86644c0dbde984de037c3a70</ApiKey>\n  <Password>hunter22</Password>\n"
            '$ curl -s -H "X-Api-Key: zzzzzzzzzzzzzzzzzzzz" http://x\n{"apiKey": "abcdefabcdefabcdefabcdef"}\nvalue=SECRETVALUE123\n')
    out = sr.scrub_run_output(text, {"MASS_PASSWORD": "SECRETVALUE123"})
    assert "d1203e8a86644c0dbde984de037c3a70" not in out and "</ApiKey>" in out, out
    assert "hunter22" not in out and "zzzzzzzzzzzzzzzzzzzz" not in out and "abcdefabcdefabcdefabcdef" not in out
    assert "SECRETVALUE123" not in out and "***" in out
    assert sr.scrub_run_output("already present: YTS\nunreachable: Anidex", {}) == "already present: YTS\nunreachable: Anidex"


def test_resolve_run_verifies_through_run_verify_and_scrubs_before_the_record():
    from app.modules import supervised_runs as sr
    src = pathlib.Path(sr.__file__).read_text(encoding="utf-8")
    i = src.index("async def resolve_run(")
    body = src[i:src.index("\nasync def _verify_verdicts(", i)]
    assert "_lr.run_probes(" not in body, "every verify check in resolve_run goes through run_verify"
    assert body.count("await run_verify(spec, verify_cmds, secret_env)") >= 2, "both the first read and the §17.1239 re-read"
    assert body.index('e["output"] = scrub_run_output(e.get("output"), secret_env)') < body.index("_executed_report(runbook, spec.name, executed, verify_out)")
    assert "verify_out = scrub_run_output(verify_out, secret_env)" in body


# ───── §17.1282 — a value is safe WHERE IT IS SPLICED
#
# ADD26: the engine read the host's own public key off the machine, offered it,
# prefilled it into `echo "<OPERATOR_SSH_PUBKEY>" >> ~/.ssh/authorized_keys` -- and
# refused it at approval: "no spaces". The bare-token rule was applied to a value
# that sits inside double quotes.

PUBKEY = "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAACAQCrD5xn6976YO6y3l8gucPeYqql2ErgozFVzkfq/xkvdaAgaR9TBi61MgpczUIuBD2jVwrbadqUKISa4r1Nz" * 3 + " root@pve"
ADD26_CMDS = ["qm status 110 | grep -q running || qm start 110",
              "ssh-copy-id -i /root/.ssh/id_rsa.pub <AI_VM_USER>@192.168.1.129",
              "ssh <AI_VM_USER>@192.168.1.129 'mkdir -p ~/.ssh && echo \"<OPERATOR_SSH_PUBKEY>\" >> ~/.ssh/authorized_keys'"]


def test_the_hosts_own_key_is_accepted_where_it_is_spliced():
    from app.modules import supervised_runs as sr
    clean, problems = sr.check_inputs(["AI_VM_USER", "OPERATOR_SSH_PUBKEY"],
                                      {"AI_VM_USER": "aedefruscio", "OPERATOR_SSH_PUBKEY": PUBKEY}, texts=ADD26_CMDS)
    assert problems == [], problems
    assert clean["OPERATOR_SSH_PUBKEY"] == PUBKEY and len(PUBKEY) > 200 and " " in PUBKEY


def test_the_contexts_are_read_off_the_block():
    from app.modules import supervised_runs as sr
    assert sr.placeholder_contexts("AI_VM_USER", ADD26_CMDS) == {"bare"}, "the user is a bare word in both ssh commands"
    assert sr.placeholder_contexts("OPERATOR_SSH_PUBKEY", ADD26_CMDS) == {"sq"}, \
        "the OUTERMOST quoting decides: the key sits inside the ssh payload's single quotes, where its inner double quotes are literal"
    assert sr.placeholder_contexts("K", ['echo "<K>" >> f']) == {"dq"}
    assert sr.placeholder_contexts("NOPE", ADD26_CMDS) == set()


def test_a_bare_placeholder_keeps_the_one_token_rule():
    from app.modules import supervised_runs as sr
    _, problems = sr.check_inputs(["AI_VM_USER"], {"AI_VM_USER": "aede fruscio"}, texts=ADD26_CMDS)
    assert problems and "no spaces" in problems[0]["why"]
    _, problems = sr.check_inputs(["X"], {"X": "a b"})
    assert problems, "without texts the historical rule stands everywhere"


def test_quoted_values_still_refuse_what_would_escape_the_quotes():
    from app.modules import supervised_runs as sr
    dq = ['echo "<K>" > f']
    for bad in ['has"quote', "has$dollar", "has`tick", "has\\\\slash", "has\\nnewline"]:
        _, problems = sr.check_inputs(["K"], {"K": bad}, texts=dq)
        assert problems and "double quotes" in problems[0]["why"], bad
    assert sr.check_inputs(["K"], {"K": "it's fine"}, texts=dq)[1] == [], "a single quote inside double quotes is harmless"
    sq = ["echo '<K>' > f"]
    assert sr.check_inputs(["K"], {"K": "it's not"}, texts=sq)[1], "a single quote inside single quotes ends them"
    assert sr.check_inputs(["K"], {"K": 'say "hi" $x'}, texts=sq)[1] == [], "double quotes and $ are literal inside single quotes"
    _, problems = sr.check_inputs(["K"], {"K": "x" * 5000}, texts=dq)
    assert problems and "4096" in problems[0]["why"]


def test_a_placeholder_used_both_bare_and_quoted_meets_the_strictest_rule():
    from app.modules import supervised_runs as sr
    texts = ['echo "<K>"', "cat <K>"]
    assert sr.check_inputs(["K"], {"K": "a b"}, texts=texts)[1], "bare anywhere means one token"


def test_resolve_run_checks_values_against_the_block_they_go_into():
    from app.modules import supervised_runs as sr
    src = pathlib.Path(sr.__file__).read_text(encoding="utf-8")
    i = src.index("async def resolve_run(")
    body = src[i:i + 6000]
    assert "check_inputs([i[\"name\"] for i in asked], inputs," in body
    assert "texts=commands + verify_cmds +" in body, "files count too"


# ───── §17.1283 — a runner that elevates only the head of a line

GUARD = "qm status 110 | grep -q running || qm start 110"


def test_a_compound_write_on_a_v18_runner_is_refused_with_the_one_line_remedy():
    from app.modules import supervised_runs as sr
    found = sr.compound_write_on_a_head_only_runner([GUARD], {"sudo": True, "helper": "18", "allow": ["ANY"]})
    assert found and "qm start 110" in found[0]["why"] and "ONE command per line" in found[0]["why"]
    assert "helper 19" in found[0]["why"]
    assert any(s in found[0]["why"] for s in sr._SHAPE_REFUSALS), "redraftable: the fix needs no operator"


def test_helper_19_or_no_sudo_or_a_read_compound_passes():
    from app.modules import supervised_runs as sr
    assert sr.compound_write_on_a_head_only_runner([GUARD], {"sudo": True, "helper": "19"}) == []
    assert sr.compound_write_on_a_head_only_runner([GUARD], {"sudo": False, "helper": "18"}) == [], "no sudo: nothing to elevate"
    assert sr.compound_write_on_a_head_only_runner(["qm status 110 | grep -q running"], {"sudo": True, "helper": "18"}) == [], "reads after the head are fine"
    assert sr.compound_write_on_a_head_only_runner(["qm start 110"], {"sudo": True, "helper": "18"}) == [], "a single command is elevated whole already"


def test_the_head_only_gate_is_wired_into_frame_run():
    from app.modules import supervised_runs as sr
    src = pathlib.Path(sr.__file__).read_text(encoding="utf-8")
    i = src.index("def frame_run("); body = src[i:src.index("\ndef ", i + 10)]
    assert "compound_write_on_a_head_only_runner(cmds, policy)" in body


# ───── §17.1284b/c — the shell's own words are not writes; ssh needs a password and a wait

WAIT_LOOP = "for i in 1 2 3 4 5 6 7 8 9 10; do ping -c 1 -W 2 192.168.1.129 >/dev/null 2>&1 && break; sleep 5; done"


def test_a_wait_loop_of_reads_passes_the_head_only_gate():
    from app.modules import supervised_runs as sr
    assert sr.compound_write_on_a_head_only_runner([WAIT_LOOP], {"sudo": True, "helper": "18"}) == [], \
        "`break`, `sleep`, `done` are not writes"
    assert sr.compound_write_on_a_head_only_runner(["qm status 110 | grep -q running || qm start 110"], {"sudo": True, "helper": "18"}), \
        "the real write after an operator is still refused"


@pytest.mark.parametrize("seg", ["do", "done", "break", "then", "fi", "exit 1", "true", ":", "continue"])
def test_shell_words_are_not_commands(seg):
    from app.modules import supervised_runs as sr
    assert sr._shell_keyword_only(seg)


@pytest.mark.parametrize("seg", ["qm start 110", "done something", "pct exec 1 -- true"])
def test_a_program_is_not_a_shell_word(seg):
    from app.modules import supervised_runs as sr
    assert not sr._shell_keyword_only(seg)


def test_the_rules_say_how_ssh_gets_a_held_password_and_a_wait():
    from app.modules import supervised_runs as sr
    r = sr.CHANNEL_RULES
    assert 'SSHPASS="$MASS_PASSWORD" sshpass -e' in r and "StrictHostKeyChecking=accept-new" in r
    assert "apt-get install -y sshpass" in r and "A VM you just STARTED is not up yet" in r


# ───── §17.1285 — a step about a guest whose commands never leave the host

ADD82 = {"node_key": "ADD82", "title": "Install and enable QEMU Guest Agent in VM 106",
         "description": "Install qemu-guest-agent inside the palworld-server guest (VM 106) and confirm it answers."}
ADD82_CMDS = ["sudo apt-get update", "sudo apt-get install -y qemu-guest-agent",
              "sudo systemctl enable --now qemu-guest-agent", "systemctl is-active qemu-guest-agent"]


def test_the_live_host_side_draft_for_a_vm_step_is_refused():
    from app.modules import supervised_runs as sr
    found = sr.commands_never_reach_the_guest(ADD82_CMDS, ADD82)
    assert found and "VM/CT 106" in found[0]["why"] and "qm guest exec 106" in found[0]["why"]
    assert any(s in found[0]["why"] for s in sr._SHAPE_REFUSALS)


@pytest.mark.parametrize("cmds", [
    ["qm guest exec 106 -- apt-get install -y qemu-guest-agent"],
    ["pct exec 102 -- apt-get install -y curl"],
    ["qm set 106 --agent 1", "qm reboot 106"],                     # host-side work ON the guest
    ["ssh root@192.168.1.129 'apt-get install -y qemu-guest-agent'"],
    ["qm status 106", "qm agent 106 ping"],                         # reads only
])
def test_a_block_that_reaches_or_acts_on_the_guest_passes(cmds):
    from app.modules import supervised_runs as sr
    assert sr.commands_never_reach_the_guest(cmds, ADD82) == [], cmds


def test_a_step_with_no_guest_subject_is_left_alone():
    from app.modules import supervised_runs as sr
    assert sr.commands_never_reach_the_guest(ADD82_CMDS, {"title": "Install the NVIDIA driver on the Proxmox host"}) == []


def test_the_guest_gate_is_wired_into_frame_run():
    from app.modules import supervised_runs as sr
    src = pathlib.Path(sr.__file__).read_text(encoding="utf-8")
    i = src.index("def frame_run("); body = src[i:src.index("\ndef ", i + 10)]
    assert "commands_never_reach_the_guest(cmds, node, shape_files)" in body, "the gate reads the files too (§17.1286)"


# ───── §17.1287b — it is the WRITES that must reach the guest

def test_a_read_only_ping_beside_a_host_side_install_does_not_satisfy_the_guest_gate():
    from app.modules import supervised_runs as sr
    found = sr.commands_never_reach_the_guest(ADD82_CMDS + ["qm agent 106 ping"], ADD82)
    assert found and found[0]["command"] == "sudo apt-get update"
    assert "reaches nothing" in found[0]["why"]


def test_a_file_run_by_the_write_reaches_the_guest_for_it():
    from app.modules import supervised_runs as sr
    files = [{"path": "/tmp/vm106_agent.sh", "content": 'ssh u@$IP "apt-get install -y qemu-guest-agent"\n'}]
    assert sr.commands_never_reach_the_guest(['MASS_PASSWORD="$MASS_PASSWORD" bash /tmp/vm106_agent.sh'], ADD82, files) == []
    other = [{"path": "/tmp/other.sh", "content": 'ssh u@$IP true\n'}]
    assert sr.commands_never_reach_the_guest(["bash /tmp/vm106_agent.sh"], ADD82, other), "a file the write does not run reaches nothing for it"


def test_a_mix_of_a_reaching_write_and_a_host_side_write_names_the_host_side_one():
    from app.modules import supervised_runs as sr
    found = sr.commands_never_reach_the_guest(["qm set 106 --agent 1", "sudo apt-get install -y qemu-guest-agent"], ADD82)
    assert found and found[0]["command"] == "sudo apt-get install -y qemu-guest-agent"


# ───── §17.1287c — a loop of reads is not a write

def test_a_wait_loop_is_not_a_write_the_guest_gate_refuses():
    from app.modules import supervised_runs as sr
    loop = "for i in 1 2 3 4 5 6 7 8 9 10 11 12; do ping -c 1 -W 2 <PALWORLD_IP> >/dev/null 2>&1 && break; sleep 5; done"
    reaching = ["qm start 106", loop, 'ssh user@<PALWORLD_IP> "sudo apt-get install -y qemu-guest-agent"']
    assert sr.commands_never_reach_the_guest(reaching, ADD82) == [], "the start acts on 106, the loop reads, the ssh reaches"
    assert sr.commands_never_reach_the_guest([loop, "sudo apt-get install -y qemu-guest-agent"], ADD82)[0]["command"] == "sudo apt-get install -y qemu-guest-agent"


def test_writing_segments_judges_the_programs_not_the_shells_words():
    from app.modules import supervised_runs as sr
    loop = "for i in 1 2 3 4 5 6 7 8 9 10 11 12; do ping -c 1 -W 2 <PALWORLD_IP> >/dev/null 2>&1 && break; sleep 5; done"
    assert sr.writing_segments(loop) == [], "ping, sleep, break, do, done: nothing writes"
    assert sr.writing_segments("qm status 110 | grep -q running || qm start 110") == ["qm start 110"]
    assert sr.writing_segments("sudo apt-get update && sudo apt-get install -y qemu-guest-agent") == [
        "sudo apt-get update", "sudo apt-get install -y qemu-guest-agent"]
    assert sr.writing_segments("cat /etc/hosts") == []
    assert sr.writing_segments("ping -c 1 <HOST_IP>") == [], "a placeholder is a value, not a redirect"
    assert sr.writing_segments("until ping -c 1 -W 2 <HOST_IP>; do sleep 5; done") == []
    assert sr.writing_segments("for h in a b; do ssh user@$h 'sudo apt-get install -y x'; done") == [
        "ssh user@$h 'sudo apt-get install -y x'"], "a loop that WRITES still writes"


def test_the_head_only_runner_gate_ignores_a_placeholder_too():
    from app.modules import supervised_runs as sr
    policy = {"sudo": True, "helper": "18"}
    loop = "for i in 1 2 3; do ping -c 1 -W 2 <PALWORLD_IP> >/dev/null 2>&1 && break; sleep 5; done"
    assert sr.compound_write_on_a_head_only_runner([loop], policy) == []
    hit = sr.compound_write_on_a_head_only_runner(["qm status 110 | grep -q running || qm start 110"], policy)
    assert hit and "qm start 110" in hit[0]["why"]


# ───── §17.1288 — the first frame that reached the VM, read line by line

ADD82_RUN = 'MASS_PASSWORD="$MASS_PASSWORD" PALWORLD_USER="<PALWORLD_USER>" bash /tmp/install_agent_106.sh'
ADD82_VERIFY = ["qm agent 106 ping",
                "ssh -o BatchMode=yes <PALWORLD_USER>@<PALWORLD_IP> 'systemctl is-active qemu-guest-agent'"]


def _add82_files(content=None):
    return [{"path": "/tmp/install_agent_106.sh", "content": content or _fx("add82_install_agent_106.sh")}]


def test_a_placeholder_only_the_verify_uses_is_refused():
    from app.modules import supervised_runs as sr
    hits = sr.verify_needs_a_value_the_run_never_used([ADD82_RUN], ADD82_VERIFY, _add82_files())
    assert [h["command"] for h in hits] == [ADD82_VERIFY[1]]
    assert "<PALWORLD_IP>" in hits[0]["why"] and "PALWORLD_USER" not in hits[0]["why"], "the user IS used by the run"
    assert sr.verify_needs_a_value_the_run_never_used([ADD82_RUN], ["qm agent 106 ping"], _add82_files()) == []
    assert sr.verify_needs_a_value_the_run_never_used(
        ["curl -s http://<PROWLARR_IP>:9696/ping"], ["curl -s http://<PROWLARR_IP>:9696/api/v1/health"]) == [], \
        "a placeholder the run used may check too"


def test_the_live_scripts_secret_rides_the_ssh_command_line():
    from app.modules import supervised_runs as sr
    hits = sr.secret_in_an_ssh_command_line([ADD82_RUN], _add82_files())
    assert len(hits) == 1 and "$MASS_PASSWORD" in hits[0]["why"] and "expands on the REMOTE" in hits[0]["why"]
    assert hits[0]["command"].startswith("/tmp/install_agent_106.sh: ssh -o BatchMode=yes")
    assert "<<< \"$MASS_PASSWORD\"" in hits[0]["why"], "the remedy is stdin"
    double = _fx("add82_install_agent_106.sh").replace(
        "'echo \"$MASS_PASSWORD\" | sudo -S apt-get update && echo \"$MASS_PASSWORD\" | sudo -S apt-get install -y qemu-guest-agent && echo \"$MASS_PASSWORD\" | sudo -S systemctl enable --now qemu-guest-agent'",
        '"echo $MASS_PASSWORD | sudo -S apt-get update"')
    hits = sr.secret_in_an_ssh_command_line([ADD82_RUN], _add82_files(double))
    assert len(hits) == 1 and "process list" in hits[0]["why"]
    fixed = _fx("add82_install_agent_106.sh").replace(
        "'echo \"$MASS_PASSWORD\" | sudo -S apt-get update && echo \"$MASS_PASSWORD\" | sudo -S apt-get install -y qemu-guest-agent && echo \"$MASS_PASSWORD\" | sudo -S systemctl enable --now qemu-guest-agent'",
        "\"sudo -S -p '' bash -c 'apt-get update && apt-get install -y qemu-guest-agent && systemctl enable --now qemu-guest-agent'\" <<< \"$MASS_PASSWORD\"")
    assert sr.secret_in_an_ssh_command_line([ADD82_RUN], _add82_files(fixed)) == [], "the stdin form passes"
    assert sr.secret_in_an_ssh_command_line(
        ['SSHPASS="$MASS_PASSWORD" sshpass -e ssh-copy-id -o StrictHostKeyChecking=accept-new "$U@$IP"'], []) == [], \
        "sshpass's environment is not a command line"
    assert sr.secret_in_an_ssh_command_line(['ssh u@h "systemctl is-active $UNIT"'], []) == [], "not every variable is a secret"


def test_the_live_script_reads_the_neighbour_table_cold():
    from app.modules import supervised_runs as sr
    hits = sr.reads_the_neighbour_table_cold([ADD82_RUN], _add82_files())
    assert len(hits) == 1 and hits[0]["command"].startswith("/tmp/install_agent_106.sh: IP=$(ip neigh show")
    assert "nmap -sn" in hits[0]["why"] and "seq 1 254" in hits[0]["why"]
    warmed = _fx("add82_install_agent_106.sh").replace(
        "# 3. Wait for the guest", 'nmap -sn 192.168.1.0/24 >/dev/null\n# 3. Wait for the guest')
    assert sr.reads_the_neighbour_table_cold([ADD82_RUN], _add82_files(warmed)) == []
    swept = _fx("add82_install_agent_106.sh").replace(
        "# 3. Wait for the guest",
        'for h in $(seq 1 254); do ping -c 1 -W 1 "192.168.1.$h" >/dev/null 2>&1 & done; wait\n# 3. Wait for the guest')
    assert sr.reads_the_neighbour_table_cold([ADD82_RUN], _add82_files(swept)) == []
    assert sr.reads_the_neighbour_table_cold(["ip neigh show | grep -i bc:24:11:e8:9f:7a"], []), "a bare read is cold too"
    assert sr.reads_the_neighbour_table_cold(["ip addr show vmbr0"], []) == []


def test_frame_run_refuses_the_live_add82_script_for_all_three():
    from app.modules import supervised_runs as sr
    runbook = ("## Write these files\n### /tmp/install_agent_106.sh\n```bash\n" + _fx("add82_install_agent_106.sh") +
               "\n```\n\n## Run this\n```bash\n" + ADD82_RUN + "\n```\n\n## Verify\n- `" + ADD82_VERIFY[0] +
               "`\n- `" + ADD82_VERIFY[1] + "`\n")
    policy = {"allow": ["ANY"], "sudo": True, "helper": "19", "secrets": ["MASS_PASSWORD"], "can_write_files": True}
    spec = type("S", (), {"name": "pve-runner", "headers": {}})()
    frame = sr.frame_run(ADD82, runbook, spec, policy)
    assert frame["commands"] and frame["files"]
    kinds = sr.refusal_kinds(frame)
    assert {"inside an ssh command line", "reads the neighbour table cold"} <= kinds, kinds
    assert "runs in the runner's own shell on the Proxmox HOST" not in kinds, "the script does reach the guest"
    assert "run" not in {o["id"] for o in frame["options"]}
    for r in frame["refused"]:
        assert any(s in r["why"] for s in sr._SHAPE_REFUSALS), f"unredraftable refusal: {r['why'][:80]}"
    # §17.1288d — the verify-only placeholder is TRIMMED, not refused: the check
    # is gone from the frame, the input with it, and the frame says so.
    assert "appears only in the verify" not in kinds
    assert frame["verify"] == [ADD82_VERIFY[0]]
    assert [i["name"] for i in frame["inputs"]] == ["PALWORLD_USER"], "PALWORLD_IP left with the check"
    assert len(frame["engine_fixed"]) == 1 and "dropped the check" in frame["engine_fixed"][0]


def test_a_check_the_run_cannot_fill_is_dropped_only_when_another_check_remains():
    """§17.1288d — with no other check the refusal stands: the redraft must give one."""
    from app.modules import supervised_runs as sr
    runbook = ("## Write these files\n### /tmp/install_agent_106.sh\n```bash\n" + _fx("add82_install_agent_106.sh") +
               "\n```\n\n## Run this\n```bash\n" + ADD82_RUN + "\n```\n\n## Verify\n- `" + ADD82_VERIFY[1] + "`\n")
    policy = {"allow": ["ANY"], "sudo": True, "helper": "19", "secrets": ["MASS_PASSWORD"], "can_write_files": True}
    spec = type("S", (), {"name": "pve-runner", "headers": {}})()
    frame = sr.frame_run(ADD82, runbook, spec, policy)
    assert "appears only in the verify" in sr.refusal_kinds(frame)
    assert frame["verify"] == [ADD82_VERIFY[1]] and frame["engine_fixed"] == []
    assert "PALWORLD_IP" in [i["name"] for i in frame["inputs"]]


def test_the_1288_refusals_are_registered_for_redraft():
    from app.modules import supervised_runs as sr
    for sig in ("appears only in the verify", "inside an ssh command line", "reads the neighbour table cold"):
        assert sig in sr._SHAPE_REFUSALS


# ───── §17.1288e — the refusal outranks the step's own text

def test_the_retry_note_outranks_a_specification_that_says_no_way_in():
    """Live: ADD82's specification said "the host has NO way in … done at the
    console" and quoted the host-side lines; the redraft obeyed it over the
    refusal twice in a row. The note must say which wins."""
    from app.modules import supervised_runs as sr
    frame = {"refused": sr.commands_never_reach_the_guest(ADD82_CMDS, ADD82), "file_channel": True}
    assert frame["refused"], "fixture: the host-side block is refused"
    note = sr.shape_retry_note(frame)
    assert "THIS NOTE OUTRANKS THE SPECIFICATION ABOVE" in note
    assert '"no way in"' in note and '"done at the console"' in note
    assert "inside the script's ssh payload" in note
    assert "YOUR REDRAFT REPEATED" not in note


def test_a_redraft_refused_for_the_same_kind_is_told_so_once():
    from app.modules import supervised_runs as sr
    first = {"refused": sr.commands_never_reach_the_guest(ADD82_CMDS, ADD82), "file_channel": True}
    second = {"refused": sr.commands_never_reach_the_guest(ADD82_CMDS[:2], ADD82), "file_channel": True}
    assert sr.refusal_kinds(first) == sr.refusal_kinds(second)
    note = sr.shape_retry_note(second, previous=first, repeated=True)
    assert note.startswith("YOUR PREVIOUS DRAFT WAS REFUSED BY THE RUNNER'S GATE")
    assert "YOUR REDRAFT REPEATED THE SAME REFUSED SHAPE" in note
    assert "THE DRAFT BEFORE THAT was refused for something different" not in note, "it was not different"
    assert "THIS NOTE OUTRANKS THE SPECIFICATION ABOVE" in note


def test_the_pause_tries_a_third_time_on_a_repeated_kind_too():
    """Wiring: identical kinds get the one bounded extra draft, flagged `repeated`."""
    import ast
    from app.modules import execution_agent as ea
    src = pathlib.Path(ea.__file__).read_text(encoding="utf-8")
    i = src.index("async def _pause_for_decision(")
    body = src[i:]
    j = body.index("\nasync def ", 10) if "\nasync def " in body[10:] else len(body)
    body = body[:j]
    assert "or k2 == k1" in body, "a redraft refused for exactly the same kinds gets one more, told so"
    assert "same=%s" in body, "the log says whether the third draft is for a repeat"
    tree = ast.parse(src)
    calls = [c for c in ast.walk(tree) if isinstance(c, ast.Call) and getattr(c.func, "attr", None) == "shape_retry_note"]
    assert any(any(k.arg == "repeated" for k in c.keywords) for c in calls), "the third note must carry `repeated`"


# ───── §17.1288h — a literal account into the step's guest is a guess

ADD82_RUN_V3 = 'MASS_PASSWORD="$MASS_PASSWORD" bash /tmp/install_agent_106.sh'


def _v3_files(content=None):
    return [{"path": "/tmp/install_agent_106.sh", "content": content or _fx("add82_install_agent_106_v3.sh")}]


def test_root_into_vm_106_is_an_assumption():
    from app.modules import supervised_runs as sr
    hits = sr.ssh_assumes_the_guests_account([ADD82_RUN_V3], ADD82, {}, _v3_files())
    assert len(hits) == 1 and hits[0]["command"].startswith('/tmp/install_agent_106.sh: SSHPASS="$MASS_PASSWORD" sshpass -e ssh-copy-id')
    assert "`root@" in hits[0]["why"] and "is an assumption" in hits[0]["why"]
    assert "<PALWORLD_USER>@$IP" in hits[0]["why"], hits[0]["why"]


def test_a_placeholder_account_a_pin_or_the_step_text_is_not_a_guess():
    from app.modules import supervised_runs as sr
    asked = _fx("add82_install_agent_106_v3.sh").replace('root@"$IP"', '<PALWORLD_USER>@"$IP"')
    assert sr.ssh_assumes_the_guests_account([ADD82_RUN_V3], ADD82, {}, _v3_files(asked)) == []
    pinned = {"substitutions": {"PALWORLD_USER": "root"}}
    assert sr.ssh_assumes_the_guests_account([ADD82_RUN_V3], ADD82, pinned, _v3_files()) == [], "a pin names the account"
    add26 = {"node_key": "ADD26", "title": "Install the SSH public key on the AI VM (192.168.1.129)",
             "description": "ssh aedefruscio@192.168.1.129 must work without a password afterwards. VM 110."}
    cmds = ['SSHPASS="$MASS_PASSWORD" sshpass -e ssh-copy-id -o StrictHostKeyChecking=accept-new aedefruscio@192.168.1.129']
    assert sr.ssh_assumes_the_guests_account(cmds, add26, {}, []) == [], "the step's own text names it"
    host_step = {"node_key": "ADD17", "title": "Install the NVIDIA driver on the Proxmox host", "description": "On the host."}
    assert sr.ssh_assumes_the_guests_account(["ssh root@192.168.1.1 uptime"], host_step, {}, []) == [], "not a step about a guest"
    assert sr.ssh_assumes_the_guests_account(["ssh root@pve uptime"], ADD82, {}, []) == [], "the host's own shell"


def test_frame_run_refuses_the_live_fourth_draft_for_the_guessed_account_only():
    from app.modules import supervised_runs as sr
    runbook = ("## Write these files\n### /tmp/install_agent_106.sh\n```bash\n" + _fx("add82_install_agent_106_v3.sh") +
               "\n```\n\n## Run this\n```bash\n" + ADD82_RUN_V3 + "\n```\n\n## Verify\n- `qm agent 106 ping`\n")
    policy = {"allow": ["ANY"], "sudo": True, "helper": "19", "secrets": ["MASS_PASSWORD"], "can_write_files": True}
    spec = type("S", (), {"name": "pve-runner", "headers": {}})()
    frame = sr.frame_run(ADD82, runbook, spec, policy, env={"profile": "You work as root@pve."})
    assert sr.refusal_kinds(frame) == {"is an assumption"}, [r["why"][:90] for r in frame["refused"]]
    assert "run" not in {o["id"] for o in frame["options"]}
    fixed = runbook.replace('root@"$IP"', '<PALWORLD_USER>@"$IP"')
    frame = sr.frame_run(ADD82, fixed, spec, policy, env={"profile": "You work as root@pve."})
    assert frame["refused"] == [] and [i["name"] for i in frame["inputs"]] == ["PALWORLD_USER"]
    assert frame["inputs"][0]["suggestions"] == [], "the host shell's root is not offered for the guest"
    assert "run" in {o["id"] for o in frame["options"]}


def test_the_guest_word_for_the_placeholder():
    from app.modules import supervised_runs as sr
    assert sr._guest_word("Install qemu-guest-agent inside the palworld-server guest (VM 106)", "106") == "PALWORLD"
    assert sr._guest_word("Start VM 106 (palworld-server)", "106") == "PALWORLD"
    assert sr._guest_word("Do a thing in VM 106", "106") == "VM106"
    assert sr._guest_word("Do a thing in VM 106", "106", {"system_state": {"106": {"kind": "vm", "attrs": {"name": "palworld-server"}}}}) == "PALWORLD"


# ───── §17.1288i — a ladder with one rung left is not a loop

def test_the_live_1142_runbook_passes_every_shape_gate():
    from app.modules import supervised_runs as sr
    runbook = _fx("add82_runbook_1142.md")
    policy = {"allow": ["ANY"], "sudo": True, "helper": "19", "secrets": ["MASS_PASSWORD"], "can_write_files": True}
    spec = type("S", (), {"name": "pve-runner", "headers": {}})()
    frame = sr.frame_run(ADD82, runbook, spec, policy, env={"profile": "You work as root@pve."})
    assert frame["refused"] == [], [r["why"][:90] for r in frame["refused"]]
    assert [i["name"] for i in frame["inputs"]] == ["PALWORLD_USER"] and frame["inputs"][0]["suggestions"] == []
    stopped = [{"command": "ssh …", "why": "VM 106 is stopped (`qm list`, read just now) and nothing in this block starts it"}]
    frame = sr.frame_run(ADD82, runbook, spec, policy, env={"profile": "x"}, preconditions=stopped)
    assert len(frame["refused"]) == 1 and "run" not in {o["id"] for o in frame["options"]}


def test_the_pause_climbs_one_more_rung_when_every_draft_made_progress():
    """Wiring: a third draft refused for kinds disjoint from BOTH earlier drafts
    gets one last draft; the best frame wins, a later draft winning a tie."""
    from app.modules import execution_agent as ea
    src = pathlib.Path(ea.__file__).read_text(encoding="utf-8")
    i = src.index("async def _pause_for_decision(")
    body = src[i:src.index("\nasync def ", i + 10)]
    assert "not (k3 & (k1 | k2))" in body, "the fourth draft is only for a third refused for something NEW"
    assert "supervised_run_redraft_last" in body and "supervised_run_redraft_last_rejected" in body
    assert "cands = [f for f in (fourth, third, second, frame) if f.get(\"commands\")]" in body, \
        "the latest draft must come first so it wins a tie on refusals"
    assert "len(third[\"refused\"]) <= len(frame[\"refused\"]) and not (k3 & k1)" in body, \
        "a third draft that fixed what the first was refused for wins a tie with it"
    assert body.count("supervised_runs.draft_runbook(") == body.count("preconditions=await _pre_for("), \
        "every draft is framed against its own preconditions"


def test_installing_the_means_of_reaching_the_guest_is_not_a_host_write_that_misses_it():
    from app.modules import supervised_runs as sr
    ok = ["apt-get install -y sshpass", 'MASS_PASSWORD="$MASS_PASSWORD" bash /tmp/install_agent_106.sh']
    files = [{"path": "/tmp/install_agent_106.sh", "content": _fx("add82_install_agent_106_v3.sh")}]
    assert sr.commands_never_reach_the_guest(ok, ADD82, files) == []
    assert sr.commands_never_reach_the_guest(["apt-get install -y sshpass", "sudo apt-get install -y qemu-guest-agent"], ADD82, files)[0]["command"] == "sudo apt-get install -y qemu-guest-agent"


# ───── §17.1288j — the one draft that had everything right (trace 1145)

def test_the_live_1145_runbook_is_clean_at_the_frame():
    from app.modules import supervised_runs as sr
    runbook = _fx("add82_runbook_1145.md")
    policy = {"allow": ["ANY"], "sudo": True, "helper": "19", "secrets": ["MASS_PASSWORD"], "can_write_files": True}
    spec = type("S", (), {"name": "pve-runner", "headers": {}})()
    frame = sr.frame_run(ADD82, runbook, spec, policy, env={"profile": "You work as root@pve."})
    assert frame["refused"] == [], [r["why"][:120] for r in frame["refused"]]
    assert [i["name"] for i in frame["inputs"]] == ["PALWORLD_USER"] and "run" in {o["id"] for o in frame["options"]}


def test_a_name_the_script_assigns_is_not_read_from_the_environment():
    from app.modules import supervised_runs as sr
    files = [{"path": "/tmp/x.sh", "content": 'PASS="${MASS_PASSWORD:?required}"\nSSHPASS="$PASS" sshpass -e ssh-copy-id u@h\n'}]
    assert sr.script_secret_not_passed(['MASS_PASSWORD="$MASS_PASSWORD" bash /tmp/x.sh'], files) == []
    hits = sr.script_secret_not_passed(["bash /tmp/x.sh"], files)
    assert hits and "MASS_PASSWORD" in hits[0]["why"] and "PASS=" not in hits[0]["why"].split("reads", 1)[1][:30]
    unassigned = [{"path": "/tmp/y.sh", "content": 'SSHPASS="$PASS" sshpass -e ssh-copy-id u@h\n'}]
    assert sr.script_secret_not_passed(["bash /tmp/y.sh"], unassigned), "a name read and never assigned still is"


def test_the_machines_contradictions_are_redraftable():
    """§17.1288j — a precondition refusal reaches the retry note and counts as a kind."""
    from app.modules import supervised_runs as sr
    pre = [{"command": "ssh …", "why": "VM 106 is stopped (`qm list`, read just now) and nothing in this block starts it -- start it first, guarded: `qm status 106 | grep -q running || qm start 106`"},
           {"command": "ssh …", "why": "nothing has put this host's key on guest 106: no finished step installed one there and this block copies none"}]
    frame = {"refused": pre, "file_channel": True}
    assert sr.refusal_kinds(frame) == {"is stopped (`", "nothing has put this host's key on guest"}
    note = sr.shape_retry_note(frame)
    assert "qm status 106 | grep -q running || qm start 106" in note and "nothing has put this host's key" in note
    for marker in ("is a VM on this host, not a container", "is ALREADY", "there is no guest"):
        assert marker in sr._SHAPE_REFUSALS


# ───── §17.1288k — the run command after the files is still the run command

def test_the_live_1148_runbook_has_its_run_command_after_the_file():
    from app.modules import supervised_runs as sr
    rb = _fx("add82_runbook_1148.md")
    assert sr.runbook_commands(rb) == ['PALWORLD_USER="<PALWORLD_USER>" MASS_PASSWORD="$MASS_PASSWORD" bash /tmp/install_guest_agent.sh']
    files = sr.file_writes(rb)
    assert [f["path"] for f in files] == ["/tmp/install_guest_agent.sh"]
    assert "bash /tmp/install_guest_agent.sh" not in files[0]["content"], "the run fence is not part of the file"
    policy = {"allow": ["ANY"], "sudo": True, "helper": "19", "secrets": ["MASS_PASSWORD"], "can_write_files": True}
    spec = type("S", (), {"name": "pve-runner", "headers": {}})()
    frame = sr.frame_run(ADD82, rb, spec, policy, env={"profile": "You work as root@pve."})
    assert frame["commands"] and frame["files"]
    assert sr.refusal_kinds(frame) == {"reads the neighbour table cold"}, [r["why"][:90] for r in frame["refused"]]


def test_a_file_nothing_runs_is_a_registered_refusal():
    from app.modules import supervised_runs as sr
    rb = "## Write these files\n### /tmp/x.sh\n```bash\necho hi\n```\n\n## Verify\n- `qm agent 106 ping`\n"
    policy = {"allow": ["ANY"], "sudo": True, "helper": "19", "secrets": [], "can_write_files": True}
    spec = type("S", (), {"name": "pve-runner", "headers": {}})()
    frame = sr.frame_run(ADD82, rb, spec, policy)
    assert frame["commands"] == [] and frame["files"]
    assert "and nothing runs it" in sr.refusal_kinds(frame)
    assert "bash /tmp/x.sh" in frame["refused"][-1]["why"]
    assert sr.shape_retry_note(frame), "the redraft is told"


def test_the_chain_goes_on_from_a_draft_that_has_files_but_no_command():
    from app.modules import execution_agent as ea
    src = pathlib.Path(ea.__file__).read_text(encoding="utf-8")
    i = src.index("async def _pause_for_decision(")
    body = src[i:src.index("\nasync def ", i + 10)]
    assert '(second["commands"] or second.get("files")) and k2' in body
    assert '(third["commands"] or third.get("files")) and k3' in body


# ───── §17.1288l — a sweep is a read; an address the block reads is not an input; the host is not the guest

def test_a_sweep_is_a_read():
    from app.modules.assist_state_check import read_only_command
    from app.modules import supervised_runs as sr
    assert read_only_command("nmap -sn 192.168.1.0/24 >/dev/null 2>&1")
    assert read_only_command("arp-scan --localnet") and read_only_command("fping -g 192.168.1.0/24")
    assert not read_only_command("nmap -sn 192.168.1.0/24 -oN /tmp/hosts.txt"), "a report is a write"
    assert not read_only_command("nmap --script vuln 192.168.1.5"), "NSE scripts are not a read"
    live = ("for i in 1 2 3 4 5 6 7 8 9 10 11 12; do ping -c 1 -W 2 192.168.1.156 >/dev/null 2>&1 && break; sleep 5; done; "
            "nmap -sn 192.168.1.0/24 >/dev/null 2>&1 || true; ip neigh show | grep -i bc:24:11:e8:9f:7a")
    assert sr.writing_segments(live) == []


def test_the_live_1151_runbook_is_refused_for_what_is_wrong_and_not_for_the_sweep():
    from app.modules import supervised_runs as sr
    rb = _fx("add82_runbook_2057_third.md")
    policy = {"allow": ["ANY"], "sudo": True, "helper": "19", "secrets": ["MASS_PASSWORD"], "can_write_files": True}
    spec = type("S", (), {"name": "pve-runner", "headers": {}})()
    env = {"profile": "root@pve", "system_state": {"host": {"kind": "host", "attrs": {"ip": "192.168.1.156"}},
                                                    "106": {"kind": "vm", "attrs": {"name": "palworld-server"}}}}
    frame = sr.frame_run(ADD82, rb, spec, policy, env=env)
    kinds = sr.refusal_kinds(frame)
    assert kinds == {"inside an ssh command line", "reads the address itself and asks the operator for it",
                     "is this host's own address"}, [r["why"][:90] for r in frame["refused"]]
    assert "runs in the runner's own shell on the Proxmox HOST" not in kinds, "the sweep + neigh read is not a host write"


def test_an_address_the_block_reads_is_not_asked_for():
    from app.modules import supervised_runs as sr
    cmds = ["ip neigh show | grep -i bc:24:11:e8:9f:7a", 'SSHPASS="$MASS_PASSWORD" sshpass -e ssh-copy-id <PALWORLD_USER>@<PALWORLD_IP>']
    hits = sr.reads_the_address_and_asks_for_it(cmds)
    assert len(hits) == 1 and "<PALWORLD_IP>" in hits[0]["why"] and "IP=$(ip neigh show" in hits[0]["why"]
    assert sr.reads_the_address_and_asks_for_it(["ssh u@<PALWORLD_IP> true"]) == [], "no read, no contradiction"
    script = [{"path": "/tmp/x.sh", "content": 'IP=$(ip neigh show | grep -i "$MAC" | awk \'{print $1}\')\nssh u@$IP true\n'}]
    assert sr.reads_the_address_and_asks_for_it(['bash /tmp/x.sh'], script) == [], "carried inside the script"


def test_the_hosts_own_address_is_not_the_guests():
    from app.modules import supervised_runs as sr
    env = {"system_state": {"host": {"kind": "host", "attrs": {"ip": "192.168.1.156"}}}}
    hits = sr.targets_the_host_as_the_guest(["for i in 1 2 3; do ping -c 1 -W 2 192.168.1.156 && break; sleep 5; done"], ADD82, env)
    assert len(hits) == 1 and "192.168.1.156` is this host's own address" in hits[0]["why"]
    assert sr.targets_the_host_as_the_guest(["ping -c 1 192.168.1.129"], ADD82, env) == []
    host_step = {"node_key": "ADD17", "title": "Install the NVIDIA driver on the Proxmox host", "description": "On the host."}
    assert sr.targets_the_host_as_the_guest(["ssh root@192.168.1.156 nvidia-smi"], host_step, env) == [], "a host step may address the host"
    assert sr.targets_the_host_as_the_guest(["ping 192.168.1.156"], ADD82, {}) == [], "no map, no verdict"
