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
            if re.search(r"\bpython3?\s+-c\b", s[:m.start()]) or re.search(r"\|\s*python3?\s+-c\b", call):
                continue                      # a program reads the list and prints only what it chooses
            res = m.group("res")
            out.append({"command": str(c)[:200], "why": (
                f"this read PRINTS the {res} settings of an *arr app, and each entry's `fields` carry its credentials "
                "(API keys, passkeys, cookies, passwords) -- they would land in the run's record and the job (live, NB1 "
                f"dumped Prowlarr's whole indexer list). Read what the step needs: why an indexer fails is in "
                "`/api/v1/indexerstatus` (no credentials); its name and enable flag can be filtered "
                "(`| grep -o '\"name\": *\"[^\"]*\"'`); or send the body nowhere (`-o /dev/null -w '%{http_code}'`) (§17.1673).")})
            break
    return out


#: §17.1958 — a whole systemd unit printed: `systemctl cat <unit>`, or a reader of a unit file or drop-in
_UNIT_PRINT_RE = re.compile(r"\bsystemctl\s+(?:--\S+\s+)*cat\b|\b(?:cat|head|tail|less|more)\s+(?:-\S+\s+)*[\"']?\S*"
                            r"(?:\.service|\.socket|\.timer|/systemd/system/\S+\.d/\S+\.conf)\b")
#: …and `systemctl show`, which prints the same values as `Environment=` unless its properties are chosen without it
_UNIT_SHOW_RE = re.compile(r"\bsystemctl\s+(?:--\S+\s+)*show\b[^|;&\n]*")
#: …filtered so the Environment lines never reach the output: dropped, counted, chosen lines that are not them, or nowhere
_UNIT_FILTERED_RE = re.compile(r"\|\s*grep\s+-[A-Za-z]*v[A-Za-z]*\s+['\"]?\^?\s*Environment|\|\s*(?:grep\s+-[A-Za-z]*[cq]|wc)\b"
                               r"|>\s*/dev/null|\|\s*grep\s+(?:-[A-Za-z]+\s+)*(?P<pat>'[^']*'|\"[^\"]*\"|\S+)")


def _shows_the_environment(call: str) -> bool:
    """A `systemctl show` call prints `Environment=` when it chooses no property, or chooses that one."""
    props = [p for ps in re.findall(r"(?:-p|--property)[\s=]+([\w,=-]+)", call) for p in ps.split(",")]
    return not props or any(p.startswith("Environment") for p in props)


def prints_a_units_environment(commands: list[str], verify: list[str]) -> list[dict]:
    """§17.1958 — a read that PRINTS a whole systemd unit prints its `Environment=` lines, and a unit is where a service's
    keys often live.

    Live (job 7cc2a7ac T4 "Create restore point", 2026-11-20): the backup step ran `pct exec 111 -- systemctl cat
    control-panel.service` and, as its check, `cat /root/panel-backup-…/etc/systemd/system/control-panel.service` -- the
    control panel keeps the *arr and Proxmox keys it uses in that unit's `Environment=` lines, so both would have put
    them into the run's record and the job. A unit read whose output drops those lines, counts, chooses other lines, or
    goes nowhere is fine."""
    out: list[dict] = []
    for c in list(commands or []) + list(verify or []):
        s = str(c)
        show = next((m for m in _UNIT_SHOW_RE.finditer(s) if _shows_the_environment(m.group(0))), None)
        if show:
            out.append({"command": s[:200], "why": (
                "this `systemctl show` PRINTS the unit's environment (`Environment=` -- every property when none is chosen, "
                "or `-p Environment`), which is where a service keeps the keys it uses (live, T4's redraft read "
                "control-panel.service's Environment this way). Choose the properties the step needs without it: "
                "`systemctl show <unit> -p ExecStart -p WorkingDirectory -p User -p FragmentPath` (§17.1958).")})
            continue
        for m in _UNIT_PRINT_RE.finditer(s):
            rest = re.split(r"\|\||&&|;", s[m.start():], maxsplit=1)[0]
            f = _UNIT_FILTERED_RE.search(rest)
            if f and not (f.group("pat") and re.search(r"Environment|key|pass|token|secret", f.group("pat"), re.I)):
                continue
            out.append({"command": s[:200], "why": (
                "this read PRINTS a whole systemd unit, and its `Environment=` lines are where a service keeps the keys it "
                "uses (live, T4 printed control-panel.service, which holds the panel's *arr and Proxmox keys) -- they would "
                "land in the run's record and the job. Read what the step needs: `systemctl show <unit> -p ExecStart -p "
                "WorkingDirectory -p User -p FragmentPath`, or `systemctl cat <unit> | grep -v '^Environment'`; a copy is "
                "checked by `cmp` or `sha256sum` against its source, not by printing it (§17.1958).")})
            break
    return out
