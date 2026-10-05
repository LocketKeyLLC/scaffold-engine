"""§17.1379 — a program is a program wherever it lives, and a call must match.

Live, 2026-10-05. §17.1378's rules worked: ADD132's next draft set the password
INSIDE the `fields` entry by name, POSTed the whole object to `/test`, and raised
`priority` only when it was below 1. It still could not run:

    def pct_exec(ctid, cmd):
        return run(["pct", "exec", str(ctid), "--", "sh", "-c", cmd])
    …
    def api_put(ctid, port, key, path, body):
        r = pct_exec(ctid, "curl … -d @- …", input=body)

    TypeError: pct_exec() got an unexpected keyword argument 'input'

at both call sites, so the block would die at the first Radarr PUT having changed
nothing. And the draft was a `.py` FILE — the shape FILE_RULES recommends — which
§17.1377's program gate never looked at, so a missing import there was unjudged too.
"""
import json
import pathlib

import app.modules.supervised_runs as sr
from app.modules.supervised_runs import (FILE_RULES,
                                         a_call_its_own_function_cannot_accept,
                                         a_program_that_uses_a_name_it_never_defines)

FIX = pathlib.Path(__file__).parent / "fixtures"
LIVE = json.loads((FIX / "add132_call_its_own_function_rejects_2026_10_05.json").read_text())


def _py(text, path="/tmp/p.py"):
    return [{"path": path, "content": text}]


# ------------------------------------------------------------- the live draft

def test_the_draft_was_accepted_with_no_refusals():
    assert LIVE["accepted_with"] == {"suggested": "run", "refused": 0}


def test_what_the_draft_got_right_is_recorded():
    """It is the first draft to put the credential where the API keeps it, so a
    later change that loses that is a regression, not an improvement."""
    assert "password set inside the fields entry by name" in LIVE["what_it_got_right"]


def test_both_call_sites_are_refused():
    out = a_call_its_own_function_cannot_accept(LIVE["commands"], LIVE["files"])
    assert len(out) == LIVE["defects"]["calls_its_own_function_cannot_accept"] == 2, out
    assert all("pct_exec" in h["command"] for h in out)


def test_the_refusal_quotes_the_typeerror_that_was_measured():
    why = a_call_its_own_function_cannot_accept(LIVE["commands"], LIVE["files"])[0]["why"]
    assert "unexpected keyword argument 'input'" in why
    assert "`input`" in why and "TypeError" in why
    assert "pass it on to `subprocess.run`" in why     # the remedy


def test_the_draft_that_is_otherwise_right_is_not_refused_for_anything_else():
    """§17.1378's two gates must be quiet on it — it fixed exactly what they ask."""
    assert sr.a_credential_set_where_the_api_does_not_keep_it(
        LIVE["commands"], LIVE["files"]) == []
    assert sr.a_validating_endpoint_sent_an_empty_body(
        LIVE["commands"], LIVE["files"]) == []


# -------------------------------------- a program is a program wherever it lives

def test_a_py_file_is_judged_for_undefined_names():
    """§17.1377 judged only heredocs, so this exact defect in a `.py` file — the
    shape FILE_RULES RECOMMENDS — went straight past it."""
    assert len(a_program_that_uses_a_name_it_never_defines(
        ["python3 /tmp/x.py"], _py("import json\nprint(os.environ['X'])\n", "/tmp/x.py"))) == 1


def test_a_heredoc_is_still_judged():
    assert len(a_program_that_uses_a_name_it_never_defines(
        [], [{"path": "/tmp/x.sh",
              "content": "python3 - <<'EOF'\nimport json\nprint(os.environ['X'])\nEOF\n"}])) == 1


def test_a_file_run_by_python_without_a_py_suffix_is_judged():
    assert len(a_program_that_uses_a_name_it_never_defines(
        ["python3 /tmp/prog"], _py("import json\nprint(sys.argv)\n", "/tmp/prog"))) == 1


def test_a_file_nothing_runs_as_python_is_not_judged():
    assert a_program_that_uses_a_name_it_never_defines(
        ["cat /tmp/notes"], _py("import json\nprint(os.environ['X'])\n", "/tmp/notes")) == []


def test_one_extractor_feeds_both_judgments():
    """Two extractors is how one of them ends up seeing a shape the other misses
    ([[feedback_sibling_call_sites_drift]])."""
    import inspect
    for fn in (a_program_that_uses_a_name_it_never_defines,
               a_call_its_own_function_cannot_accept):
        assert "_inline_programs(commands, files)" in inspect.getsource(fn), fn.__name__


# ------------------------------------------------ what the gate must not claim

def test_the_corrected_signature_is_accepted():
    assert a_call_its_own_function_cannot_accept([], _py(
        "import subprocess\n"
        "def pct_exec(ctid, cmd, input=None):\n"
        "    return subprocess.run(['pct','exec',str(ctid),'--','sh','-c',cmd], input=input)\n"
        "pct_exec(103, 'curl', input='{}')\n")) == []


def test_a_kwargs_definition_accepts_anything():
    assert a_call_its_own_function_cannot_accept([], _py(
        "def f(a, **kw):\n    return a\n"
        "f(1, input='x', whatever=2)\n")) == []


def test_a_splatted_call_is_not_judged():
    assert a_call_its_own_function_cannot_accept([], _py(
        "def f(a, b):\n    return a\n"
        "args = {'a': 1, 'b': 2}\n"
        "f(**args)\n")) == []
    assert a_call_its_own_function_cannot_accept([], _py(
        "def f(a, b):\n    return a\n"
        "xs = [1, 2]\n"
        "f(*xs)\n")) == []


def test_a_vararg_definition_takes_any_positionals():
    assert a_call_its_own_function_cannot_accept([], _py(
        "def f(a, *rest):\n    return a\n"
        "f(1, 2, 3, 4)\n")) == []


def test_a_keyword_only_parameter_is_accepted():
    assert a_call_its_own_function_cannot_accept([], _py(
        "def f(a, *, input=None):\n    return a\n"
        "f(1, input='x')\n")) == []


def test_a_function_defined_twice_is_not_judged():
    """Which definition is live depends on order and branches; silence beats a guess."""
    assert a_call_its_own_function_cannot_accept([], _py(
        "def f(a):\n    return a\n"
        "def f(a, b=None):\n    return a\n"
        "f(1, b=2)\n")) == []


def test_an_imported_callable_is_not_judged():
    """Only the program's OWN functions have a signature this gate knows."""
    assert a_call_its_own_function_cannot_accept([], _py(
        "import subprocess\n"
        "subprocess.run(['x'], input='y', capture_output=True)\n")) == []


def test_too_many_positionals_is_refused():
    out = a_call_its_own_function_cannot_accept([], _py(
        "def f(a, b):\n    return a\n"
        "f(1, 2, 3)\n"))
    assert len(out) == 1, out
    assert "positional" in out[0]["why"] or "takes fewer" in out[0]["why"]


# --------------------------------------------- taught, wired, and redraftable

def test_the_rules_say_a_py_file_counts_and_name_the_remedy():
    assert "A PROGRAM IS A WHOLE PROGRAM, in a heredoc or in a `.py` file" in FILE_RULES
    assert "unexpected keyword argument 'input'" in FILE_RULES
    assert "input=input" in FILE_RULES


def test_the_rules_own_example_passes_the_gate():
    import re
    for block in re.findall(r"```python\n(.*?)```", FILE_RULES, re.S):
        assert a_call_its_own_function_cannot_accept([], _py(block)) == [], block


def test_the_gate_runs_in_frame_run():
    import inspect
    assert "a_call_its_own_function_cannot_accept(cmds, shape_files)" in \
        inspect.getsource(sr.frame_run)


def test_the_refusal_drives_a_redraft():
    refused = a_call_its_own_function_cannot_accept(LIVE["commands"], LIVE["files"])
    assert refused and sr.shape_retry_note({"kind": "run", "refused": refused})
