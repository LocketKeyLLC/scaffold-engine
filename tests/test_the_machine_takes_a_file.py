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
            assert len(c.args) == 2 and ast.unparse(c.args[1]) == "files", f"{c.func.id} does not see the files"
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
