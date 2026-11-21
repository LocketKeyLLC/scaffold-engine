"""§17.1967 — a test that ran and found faults is done, with its findings; a test whose own commands broke is failed.

Live (panel job 7cc2a7ac, 2026-11-20): T15 "Browser-test page sections" ran its Playwright script, which printed ten
`PASS` lines, two `FAIL … stuck on "Loading"` lines and "=== 10 of 12 sections passed ===", and exited 1 -- because it
found two faults. T17 (use the panel's media and engine controls once) added the movie, then the engine control answered
`curl: (22) … error: 500` -- the fault it was there to find. T9 "Curl API routes direct" (to "isolate Caddy vs backend
faults") printed `200` for eleven routes and `000` for five. All three were recorded `failed`; the job blocked behind
them, and the operator skipped each one by hand so that the steps that compile, diagnose and fix (T18, T19, F1–F4)
could run on what the tests had found.

A test's job is to find out. One that ran and reported faults did its job: it is recorded `done`, its findings in a
`## Findings` section of the record (which `_result_of` hands every step downstream) and in its
`last_verification_reason`, which begins `done with findings` -- the mark §17.1644's "only if T… found failures" reads as
a failure found, so a fix step conditional on this test still runs.

Narrow on purpose, so no real failure passes as a finding:
- the step must DECLARE that it tests (title "test …"/"browser-test …"/"exercise …", after an optional `<place>:`, or
  text that says it finds/isolates/reports faults, or "Report pass/fail") -- never a bare "verify"/"check"/"confirm",
  which gates what comes after;
- the run must show the test WORKING: at least one probe passed AND at least one found a fault. A test whose every probe
  failed (wrong address, dead credential, the tool missing) cannot be told from a broken test, and stays failed;
- the command that stopped the block must itself carry the fault (an HTTP error, a `FAIL` line, a timeout) and exit the
  way a test reports one (1, or curl's 22/28);
- nothing in the run may show the test's own tooling breaking (command not found, a syntax error, a missing module,
  permission denied, a refused credential), and a run the engine already distrusts (a dropped response, a refusal, a cut
  script, a repeated failure, checks that printed nothing, a missing "Done when" print) is never rescued here.
"""
from __future__ import annotations

import re
from typing import Optional

#: the mark at the head of a done-with-findings record's last_verification_reason (read by conditional_steps)
FINDINGS_MARK = "done with findings"

_PLACE = r"\s*(?:[^:\n]{1,60}:\s*)?"
#: "Test …", "Proxmox host: browser-test …", "Smoke-test …", "Exercise the Palworld start/stop control …",
#: "Use the panel's controls once and confirm each effect"
_TEST_TITLE_RE = re.compile(
    _PLACE + r"(?:(?:browser|smoke|load|stress|e2e|end-to-end|api|ui|route)[- ])?tests?\b(?!\s+(?:file|data|user|account)s?\b)"
    r"|" + _PLACE + r"exercise\b"
    r"|" + _PLACE + r"use\b[^\n]{0,120}\bconfirm\s+(?:each|the|its)\s+effects?\b", re.I)
#: the step's own words say its purpose is to find faults: "to isolate Caddy vs backend faults", "report what fails"
_FINDS_FAULTS_RE = re.compile(
    r"\b(?:find|finds|isolate|locate|surface|report|record|list)\b(?:\s+[\w/.'-]+){0,4}?\s+"
    r"(?:faults?|failures?|what\s+(?:fails|failed|breaks|is\s+broken)|which\s+(?:\w+\s+){0,2}fail(?:ed)?|"
    r"pass/fail|PASS/FAIL)\b", re.I)


def finds_faults(node: Optional[dict]) -> bool:
    """§17.1967 — does the step declare that its purpose is to test, i.e. to find faults? Its title leads with a test
    verb (after an optional `<place>:`), or its own words -- before its "Done when" -- say it finds or reports faults."""
    title = str((node or {}).get("title") or "")
    if _TEST_TITLE_RE.match(title):
        return True
    text = " ".join(str((node or {}).get(k) or "") for k in ("title", "description", "prompt_template"))
    work = re.split(r"\bDone when\b", text, maxsplit=1, flags=re.I)[0]
    return bool(_FINDS_FAULTS_RE.search(work))


#: the test's OWN tooling broke: what it printed is not a finding about the thing under test
_BROKE_RE = re.compile(
    r"command not found|syntax error|unexpected (?:token|EOF|end of file)|unbound variable|bad substitution|"
    r"No such file or directory|Cannot find module|ERR_MODULE_NOT_FOUND|ModuleNotFoundError|ImportError|"
    r"Executable doesn't exist|browserType\.launch|Traceback \(most recent call last\)|"
    r"\b[Pp]ermission denied\b|Operation not permitted|Could not resolve host|"
    r"returned error: 40[137]\b|\bUsage: |\b(?:TypeError|ReferenceError|SyntaxError|Uncaught)\b|"
    r"^\s+at .+:\d+:\d+\)?$", re.I | re.M)
_FAIL_LINE_RE = re.compile(r"^\s*(?:FAIL(?:ED)?|✗|❌)\b.*$", re.M)
_PASS_LINE_RE = re.compile(r"^\s*(?:PASS(?:ED)?|OK|✓|✅)\b", re.M)
#: "=== 10 of 12 sections passed ===", "3/5 passed"
_OF_PASSED_RE = re.compile(r"\b(\d+)\s*(?:of|/)\s*(\d+)\b[^\n]{0,40}\bpass(?:ed)?\b", re.I)
#: pytest's "2 failed, 9 passed"
_PYTEST_RE = re.compile(r"\b(\d+) failed\b[^\n]*?\b(\d+) passed\b|\b(\d+) passed\b[^\n]*?\b(\d+) failed\b")
_CURL_ERR_RE = re.compile(r"curl: \(22\)[^\n]*?error:?\s*(\d{3})")
_STATUS_LINE_RE = re.compile(r"HTTP/\d(?:\.\d)?\s+(\d{3})|(?:^|\s)(000|[1-5]\d\d)(?=\s|$)")
_HTTP_PROBE_RE = re.compile(r"\b(?:curl|wget|http)\b")
_WRITES_CODE_RE = re.compile(r"http_code|--write-out|\s-w\s|\s-[a-zA-Z]*[iI]\b")
_FAILS_ON_HTTP_RE = re.compile(r"--fail(?:-with-body)?\b|\s-[a-zA-Z]*f[a-zA-Z]*\b")
#: how a test reports a fault: 1 (a test runner's "some failed"), curl's 22 (HTTP error) and 28 (timed out)
_REPORTING_EXITS = {1, 22, 28}


def _judge_code(code: str) -> Optional[bool]:
    """True = the probe passed, False = it found a fault, None = says nothing (401/403 are the test's own access)."""
    c = int(code)
    if c == 0 or c >= 500 or c in (404, 405, 408):
        return False
    if 200 <= c < 400:
        return True
    return None


def _probe(e: dict) -> tuple[list[str], list[str]]:
    """What one command shows: ``(passes, faults)``, each a short line."""
    cmd, out = str(e.get("command") or ""), str(e.get("output") or "")
    passes, faults = [], []
    for m in _FAIL_LINE_RE.finditer(out):
        faults.append(m.group(0).strip()[:200])
    passes += [m.group(0).strip() for m in _PASS_LINE_RE.finditer(out)]
    for m in _OF_PASSED_RE.finditer(out):
        n, total = int(m.group(1)), int(m.group(2))
        if 0 < n < total:
            passes.append(m.group(0).strip())
            faults.append(m.group(0).strip()[:200])
    for m in _PYTEST_RE.finditer(out):
        failed, passed = (m.group(1), m.group(2)) if m.group(1) else (m.group(4), m.group(3))
        if int(failed) and int(passed):
            passes.append(m.group(0).strip())
            faults.append(m.group(0).strip()[:200])
    if _HTTP_PROBE_RE.search(cmd):
        exit_ = e.get("exit")
        err = _CURL_ERR_RE.search(out)
        if err and _judge_code(err.group(1)) is False:
            faults.append(f"`{cmd[:100]}` answered HTTP {err.group(1)}: {out.strip()[-160:]}")
        elif exit_ == 28 and not _WRITES_CODE_RE.search(cmd):            # a written-out code line says it already
            faults.append(f"`{cmd[:100]}` timed out: {out.strip()[-160:]}")
        elif exit_ == 0 and _FAILS_ON_HTTP_RE.search(cmd) and not _WRITES_CODE_RE.search(cmd):
            passes.append(f"`{cmd[:100]}` answered")
        if _WRITES_CODE_RE.search(cmd):
            for ln in out.splitlines():
                if len(ln) > 200:
                    continue
                m = _STATUS_LINE_RE.search(ln)
                if not m:
                    continue
                verdict = _judge_code(m.group(1) or m.group(2))
                if verdict is True:
                    passes.append(ln.strip())
                elif verdict is False:
                    faults.append(ln.strip())
    return passes, faults


def findings_of_a_run(node: Optional[dict], executed: list[dict], refuted: Optional[list[dict]] = None,
                      not_run: Optional[list[str]] = None) -> tuple[str, str]:
    """§17.1967 — for a run the engine would record failed: ``(findings section, reason)`` when it is a test that ran
    and found faults, else ``("", "")`` and the failure stands. See the module docstring for every condition."""
    if not finds_faults(node) or not executed:
        return "", ""
    if any(e.get("refused") or e.get("unreachable") for e in executed):
        return "", ""
    if any(_BROKE_RE.search(str(e.get("output") or "")) for e in executed):
        return "", ""
    # a check that printed nothing (§17.1738) or a "Done when" print that never came (§17.1630) is not a finding:
    # nothing was read, so nothing was found
    if any((v or {}).get("kind") in ("blank", "done_when_print") for v in refuted or []):
        return "", ""
    stop = [e for e in executed if not e.get("ok") and not e.get("informational")]
    passes: list[str] = []
    faults: list[str] = []
    for e in executed:
        p, f = _probe(e)
        passes += p
        faults += f
    if stop:
        last = stop[-1]
        if last is not executed[-1] or last.get("exit") not in _REPORTING_EXITS or not _probe(last)[1]:
            return "", ""
    elif not refuted:
        return "", ""                        # nothing said the run failed: not this rule's to settle
    if not passes or not faults:
        return "", ""
    faults = list(dict.fromkeys(faults))
    lines = [f"- {f}" for f in faults[:20]] + ([f"- … and {len(faults) - 20} more"] if len(faults) > 20 else [])
    lines += [f"- the step's own check: {str(v.get('reason') or v.get('claim') or '')[:240]}" for v in (refuted or [])[:3]]
    if not_run:
        lines.append("- not run, because the block stopped on the fault above: "
                     + "; ".join(f"`{c[:80]}`" for c in not_run[:5]))
    section = ("## Findings — the test ran and found faults (§17.1967)\n\n"
               f"This step tests, and it worked: {len(passes)} probe(s) passed and {len(faults)} found a fault. It is "
               "recorded done WITH these findings -- they are what the steps after it fix:\n" + "\n".join(lines))
    reason = (f"{FINDINGS_MARK} — the test ran ({len(passes)} passed) and found {len(faults)} fault(s): "
              + "; ".join(faults[:3]))[:1000]
    return section, reason
