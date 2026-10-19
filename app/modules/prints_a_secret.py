r"""§17.1652 — a command that reads a service's credential must USE it, never print it.

Live (job 5db1b088, 2026-10-18), twice in one run: T6's check was `pct exec 102 -- bash -c "grep -oP
'(?<=<ApiKey>)[^<]+' /var/lib/prowlarr/config.xml"` -- Prowlarr's API key, printed into the run's output, the job's
record and the operator's frame -- and T9v2 drafted `sqlite3 … SELECT AccessToken FROM ApiKeys …` to print Jellyfin's
key. Both were refused only for other reasons. The engine's own reads put the key inside `$( … )` as a header value, so
it reaches the service and nothing else.

A credential read (an `<ApiKey>` extraction, a `SELECT AccessToken`, a `<Password>`) is refused when its output is the
command's own output: not inside a `$( … )` substitution.
"""
from __future__ import annotations

import re

_CREDENTIAL_READ_RE = re.compile(
    r"<ApiKey>|\bSELECT\s+AccessToken\b|<Password>|\bapi_?key\s*=\s*[\"']?\$?\(?\s*cat\b", re.I)


def _substitution_depth(cmd: str, at: int) -> int:
    """How many `$(` are open at `at` (single quotes inside a substitution are its own; we only count parens)."""
    depth, k = 0, 0
    while k < at and k < len(cmd):
        if cmd.startswith("$(", k):
            depth += 1
            k += 2
            continue
        if cmd[k] == ")" and depth:
            depth -= 1
        k += 1
    return depth


#: the shell readers that print what they read; a python program reads into a variable and is judged by what it prints
_READER_RE = re.compile(r"\b(grep|sed|awk|cut|cat|sqlite3|head|tail)\b(?P<flags>(?:\s+-[A-Za-z]+)*)")


def _prints_here(s: str, at: int) -> bool:
    """The credential read at `at` is a shell reader's output: a reader before it on the same pipeline segment, not a
    count/quiet grep, and not inside a `python3 -c` program."""
    head = s[:at]
    if re.search(r"\bpython3?\s+-c\b", head):
        return False
    seg = re.split(r"\|\||&&|;|\||\$\(|\bsh\s+-c\s+['\"]|\bbash\s+-c\s+['\"]", head)[-1]
    m = None
    for m in _READER_RE.finditer(seg):
        pass
    if m is None:
        return False
    if m.group(1) == "grep" and re.search(r"-[A-Za-z]*[cqlL]|--(?:count|quiet|files-with-matches)", m.group("flags") or ""):
        return False
    return True


def prints_a_credential(commands: list[str], verify: list[str]) -> list[dict]:
    out: list[dict] = []
    for c in list(commands or []) + list(verify or []):
        s = str(c)
        for m in _CREDENTIAL_READ_RE.finditer(s):
            if _substitution_depth(s, m.start()) == 0 and _prints_here(s, m.start()):
                out.append({"command": s[:200], "why": (
                    "this command READS a service's credential and PRINTS it: its output lands in the run's record, the "
                    "job and the operator's screen (live, T6's check printed Prowlarr's API key). Use the key where it is "
                    "read -- `-H \"X-Api-Key: $(sed -n 's:.*<ApiKey>\\(.*\\)</ApiKey>.*:\\1:p' <config.xml>)\"` -- or "
                    "check only that it is there (`grep -c '<ApiKey>' <config.xml>`) (§17.1652).")})
                break
    return out


#: §17.1673 — an *arr settings collection (or one item of it): every indexer / download client / notification / app
#: carries its credentials in its `fields` -- API keys, passkeys, cookies, passwords
_ARR_SETTINGS_RE = re.compile(r"\bcurl\b[^|;&\n]*?https?://(?:127\.0\.0\.1|localhost|[\d.]+):\d{2,5}/api/v\d+/"
                              r"(?P<res>indexer|downloadclient|notification|applications|indexerproxy|importlist)"
                              r"(?:/\d+)?(?=[\"'\s?]|$)")


def prints_arr_settings(commands: list[str], verify: list[str]) -> list[dict]:
    """§17.1673 — a read that PRINTS an *arr's settings list puts every credential in it into the record.

    Live (job 5db1b088 NB1 "Diagnose nekoBT indexer failure", 2026-10-19): the first command was `curl -s -H "X-Api-Key:
    $PROWLARR_API_KEY" http://127.0.0.1:9696/api/v1/indexer` -- the whole indexer list, whose `fields` hold each
    private tracker's API key, passkey or cookie, into the run's output and the job. A body sent nowhere (`-o
    /dev/null`), only counted (`| grep -c`, `| wc`), or read by a program that prints chosen fields is fine; the
    failure reason lives in /api/v1/indexerstatus, which carries no credential."""
    out: list[dict] = []
    for c in list(commands or []) + list(verify or []):
        # a `$( … )` substitution (the engine's in-guest key read, with its own `|`) is one word to the shell
        s = re.sub(r"\$\((?:[^()]|\((?:[^()]|\([^()]*\))*\))*\)", "SUBST", str(c))
        for m in _ARR_SETTINGS_RE.finditer(s):
            call = s[m.start():]
            call = re.split(r"\|\||&&|;", call, maxsplit=1)[0]
            if re.search(r"\s-o\s+/dev/null\b|--output\s+/dev/null\b|\|\s*(?:grep\s+-[A-Za-z]*c|wc)\b", call):
                continue
            # only chosen matches printed (`| grep -o '"name": *"[^"]*"'`), and not a credential's
            g = re.search(r"\|\s*grep\s+-[A-Za-z]*o[A-Za-z]*\s+(?P<pat>'[^']*'|\"[^\"]*\"|\S+)", call)
            if g and not re.search(r"key|pass|cookie|token|secret|pid|auth", g.group("pat"), re.I):
                continue
            # a WRITE's response echoes only what that step sent; this rule is about reading what others stored
            if re.search(r"\s(?:-X|--request)\s*[\"']?(?:POST|PUT|PATCH|DELETE)\b|\s(?:-d|--data(?:-raw|-binary)?|--json)(?:\s|=)",
                         s[max(s.rfind("curl", 0, m.end()), 0):m.end() + len(call)]):
                continue
            if re.search(r"\bpython3?\s+-c\b", s[:m.start()]):
                continue
            res = m.group("res")
            out.append({"command": str(c)[:200], "why": (
                f"this read PRINTS the {res} settings of an *arr app, and each entry's `fields` carry its credentials "
                "(API keys, passkeys, cookies, passwords) -- they would land in the run's record and the job (live, NB1 "
                f"dumped Prowlarr's whole indexer list). Read what the step needs: why an indexer fails is in "
                "`/api/v1/indexerstatus` (no credentials); its name and enable flag can be filtered "
                "(`| grep -o '\"name\": *\"[^\"]*\"'`); or send the body nowhere (`-o /dev/null -w '%{http_code}'`) (§17.1673).")})
            break
    return out
