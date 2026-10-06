"""§17.1380 — the engine TYPE-CHECKS the program it drafts.

ADD132 ("Make Radarr and Sonarr actually drive qBittorrent, and prove the
connection") drafted FOURTEEN times across §17.1343–1379. Reading the arc back,
the drafts split cleanly in two:

* the shell-side defects were about MACHINES — a host variable expanded in a
  guest payload, a body file written on the host and read in the guest, a pipe
  that escapes `pct exec`, a config the service rewrites. `bash -n` parses all
  nine shell drafts without complaint and shellcheck reports no error or warning
  on any of them (measured: 12 advisories, all `info`/`warning` style — SC2016
  on a deliberately single-quoted `$`, SC2086, SC2034). Those needed what they
  got: `service_truth` and the machine-contradiction gates.
* the Python-side defects were about the PROGRAM, and every one of them is what
  a type checker says in under a second.

The engine runs pyright on its own source in CI (the "Tool audit gate" job) and
has had an execution sandbox since §17.434 — for CodeGen DAG nodes only. It had
never run either over a program it writes for the operator's machines. So each
crash arrived live, on the operator's host, and earned one narrow static gate:
§17.1377 for a heredoc that never imports `os`, §17.1379 for a call its own
`def` rejects. This module is the general check behind those specific ones, so
the NEXT shape is refused before the operator is ever asked to approve it.

Measured, on the real corpus — ADD132's own drafts, 11 fixtures, 22 programs
(11 standalone files + 11 heredoc programs extracted from the shell drafts):

    mypy --check-untyped-defs      3 errors, every one real, zero false positives
      call-arg      2   `Unexpected keyword argument "input" for "pct_exec"`  (§17.1379, both call sites)
      name-defined  1   `Name "os" is not defined`                            (§17.1377)

    pyright (the CI tool)          the SAME three, and nothing else
    pyflakes                       the name only — it does not check arity, so it would have missed §17.1379
    bash -n / shellcheck           nothing on the nine shell drafts

mypy is the one that ships: it finds what pyright finds, it is pure Python so it
pins in `requirements.txt` with no node runtime in the image, it needs no
network (`--ignore-missing-imports`), and warm it costs 0.16 s for one program
and 0.24 s for eleven — CPU-bound, local, no I/O off the box.

`--check-untyped-defs` is not optional here: mypy skips the body of an
unannotated function by default, and every one of these drafts is unannotated.
Without it the §17.1379 TypeError is not reported (measured: zero errors on that
fixture until the flag was added).

Only three error codes are refused, and each one is an exception the program
raises on that line no matter what data it is given. Everything else mypy can
say — a type opinion, an unresolved import, an unused import, an attribute on a
value it cannot see — is left alone: a drafted program is dynamic by nature and
this gate says nothing where it cannot know. It is also fail-soft but LOUD
(§17.1359: a swallowed line disabled fifteen gates for a day), so a missing or
broken mypy logs a warning and refuses nothing.
"""
from __future__ import annotations

import ast
import logging
import os
import re
import subprocess
import sys
import tempfile

logger = logging.getLogger("scaffold")

#: §17.1380 — the mypy error codes that are a crash in waiting, each with the
#: exception Python actually raises at that line. A code is in this table only
#: because a real draft of this operator's own job produced it, or because the
#: interpreter provably raises on it regardless of the data in play.
_FATAL_CODES: dict[str, str] = {
    # §17.1377, measured: a heredoc whose only import was `json` reaching for
    # `os.environ[...]`.
    "name-defined": "NameError",
    "used-before-def": "NameError",
    # §17.1379, measured: `def pct_exec(ctid, cmd)` called as
    # `pct_exec(ctid, cmd, input=body)` — at both call sites.
    "call-arg": "TypeError",
}

#: `file.py:12: error: Some message  [code]`
_LINE_RE = re.compile(
    r"^(?P<file>[^:]+):(?P<line>\d+):(?:\d+:)? error: (?P<msg>.*?)\s+\[(?P<code>[\w-]+)\]\s*$")

#: Warm the cache across calls — 1.2 s cold, 0.16 s warm (measured). A stale
#: cache costs nothing: the file names are per-run and the sources change.
_CACHE_DIR = os.path.join(tempfile.gettempdir(), "scaffold-mypy-cache")

_TIMEOUT_S = 25.0


def a_program_the_type_checker_says_will_raise(programs: list[tuple]) -> list[dict]:
    """§17.1380 — refusals for the programs mypy says raise when they run.

    ``programs`` is ``[(label, source), ...]`` and comes from
    ``supervised_runs._inline_programs`` — the ONE extractor, so a program-level
    judgment cannot see one shape and miss the other (§17.1379). Nothing is
    judged when mypy cannot be run.
    """
    runnable = [(label, src) for label, src in (programs or []) if _parses(src)]
    if not runnable:
        return []
    findings = _mypy_findings(runnable)
    if findings is None:
        return []                              # could not run: nothing to say
    out: list[dict] = []
    for label, line, code, msg in findings:
        raises = _FATAL_CODES[code]
        out.append({"command": f"{label}: line {line}", "why": (
            f"the engine type-checked this program before offering it to run, and that line "
            f"cannot work: {msg}. Python raises {raises} the moment it runs, so the block stops "
            f"there having changed nothing -- and under `set -e` nothing after it runs either. "
            + _REMEDY[code])})
    return out


#: What to do about it, in the drafter's terms. The remedy, not just the refusal
#: (§17.1375: the drafter is TOLD the shape, not only refused).
_REMEDY: dict[str, str] = {
    "name-defined": (
        "Import or assign the name. MEASURED live (§17.1377): a draft's `python3 - <<'EOF'` said "
        "`import json` and then reached for `os.environ[...]`, while the next program in the same "
        "block had `import json, os` -- the draft knew the import and dropped it once."),
    "used-before-def": (
        "Move the definition above the line that uses it, or pass the value in as an argument."),
    "call-arg": (
        "Make the call match the definition, or the definition match the call. MEASURED live "
        "(§17.1379): `def pct_exec(ctid, cmd)` called as `pct_exec(ctid, cmd, input=body)` gave "
        "`TypeError: pct_exec() got an unexpected keyword argument 'input'` at both call sites, "
        "after that draft had everything else right. To forward stdin, give the helper the "
        "parameter and pass it on: `def pct_exec(ctid, cmd, input=None): "
        "subprocess.run([...], input=input, ...)`."),
}


def _parses(src: str) -> bool:
    """§17.1380 — only a program the engine can parse is handed to the checker.

    An unparseable heredoc is not this gate's judgment to make: the shell may
    still rewrite it (§17.1364 expands the variables), the engine and the guest
    can be on different Python versions, and `_inline_programs` already drops an
    unparseable `.py` file for the same reason. So `syntax` never appears in the
    refusals -- the compile gates (§17.1257) own that, on the shape they can see.
    """
    try:
        ast.parse(src or "")
        return True
    except (SyntaxError, ValueError):
        return False


def _mypy_findings(programs: list[tuple]) -> list[tuple] | None:
    """[(label, line, code, message)] for the fatal codes, or None if mypy could not run."""
    # §17.1359, and MEASURED on this gate's own first run: `python -m mypy` with
    # no mypy installed exits 1 -- the same code as "errors found" -- and prints
    # nothing on stdout, so parsing the output read as "the program is clean".
    # The gate reported 14 programs clean including the fixture that IS the
    # §17.1379 TypeError. Ask whether the checker is there before trusting its
    # silence.
    import importlib.util
    if importlib.util.find_spec("mypy") is None:
        logger.warning("program_typecheck_unavailable reason=mypy_not_installed "
                       "interpreter=%s", sys.executable)
        return None
    try:
        with tempfile.TemporaryDirectory(prefix="scaffold-typecheck-") as tmp:
            names: dict[str, str] = {}
            for i, (label, src) in enumerate(programs):
                name = f"program_{i}.py"
                names[name] = str(label)
                with open(os.path.join(tmp, name), "w", encoding="utf-8") as fh:
                    fh.write(src if src.endswith("\n") else src + "\n")
            proc = subprocess.run(
                [sys.executable, "-m", "mypy",
                 "--no-error-summary", "--no-color-output", "--no-pretty",
                 "--hide-error-context", "--show-error-codes",
                 # a drafted program imports what the GUEST has, not what this
                 # image has, so an unresolved import is never a defect here
                 "--ignore-missing-imports",
                 # mypy skips an unannotated function's body without this, and
                 # every drafted program is unannotated (measured: §17.1379's
                 # TypeError is invisible without it)
                 "--check-untyped-defs",
                 "--cache-dir", _CACHE_DIR,
                 *sorted(names)],
                cwd=tmp, capture_output=True, text=True, timeout=_TIMEOUT_S)
    except FileNotFoundError:
        # §17.1359 — fail-soft, but never silently: a gate that is off because a
        # tool is missing has to say so where someone reads it.
        logger.warning("program_typecheck_unavailable reason=interpreter_missing")
        return None
    except subprocess.TimeoutExpired:
        logger.warning("program_typecheck_timeout programs=%d timeout=%.0fs",
                       len(programs), _TIMEOUT_S)
        return None
    except Exception as exc:                   # pragma: no cover - defensive
        logger.warning("program_typecheck_failed err=%r", exc)
        return None

    # 0 = clean, 1 = errors found. Anything else is mypy failing, not the
    # program failing (no module named mypy, a bad flag, an internal crash).
    if proc.returncode not in (0, 1):
        logger.warning("program_typecheck_unavailable rc=%s err=%s",
                       proc.returncode, (proc.stderr or proc.stdout or "")[:300])
        return None

    # Exit 1 means "errors found", so exit 1 with nothing parseable on stdout is
    # mypy failing to say anything -- never a clean program.
    lines = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]
    if proc.returncode == 1 and not lines:
        logger.warning("program_typecheck_unavailable rc=1_no_output err=%s",
                       (proc.stderr or "")[:300])
        return None

    out: list[tuple] = []
    for raw in lines:
        m = _LINE_RE.match(raw.strip())
        if not m:
            continue
        code = m.group("code")
        if code not in _FATAL_CODES:
            continue
        label = names.get(os.path.basename(m.group("file")))
        if label is None:
            continue
        out.append((label, int(m.group("line")), code, m.group("msg").strip()))
    return out
