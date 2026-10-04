"""§17.1213 — do not run a block the machine already contradicts, and say what
would fix it.

Two steps ran on the operator's host while the engine had a read-only channel to
it, and both were doomed before they were sent:

    ADD21  pct exec 111 -- pm2 start 1   ->  exited 255: container '111' not running!
    ADD82  pct exec 106 -- apt-get …     ->  106 is a VM, not a container

The first had a prerequisite the DAG never expressed (ADD50 "Start container
111" is still pending). The second is simply the wrong tool for that guest: `pct`
addresses containers, 106 is a VM, and one `qm list` says so.

The engine could have known both. It had the channel, it had been reading that
host all evening, and it ran them anyway — then reported the failures as though
the machine had surprised it.

So: before a hands-on block is offered, the guests it names are checked against
what the host actually reports. `pct list` and `qm list`, read-only, once each.
A contradiction is not a decision for anyone — it goes into the frame's
`refused`, which turns Run off and flips the suggestion to "I'll do it myself"
— and the refusal NAMES the remedy, including the plan step that would satisfy
it when one exists. The operator asked for exactly that: *"if something needs
the user's input, the engine should request it from the user."* A greyed button
is not a request.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

logger = logging.getLogger("scaffold")

#: `pct exec 111 -- …`, `qm set 106 --scsi0 …`. The verb may be absent
#: (`qm 106` is not valid, so a verb is required).
_GUEST_RE = re.compile(r"\b(pct|qm)\s+([a-z][a-z-]*)\s+(\d{3,5})\b")

#: `pct list` → `VMID Status Lock Name`
_PCT_ROW = re.compile(r"^\s*(\d{3,5})\s+(\S+)", re.M)
#: `qm list` → `VMID NAME STATUS …`
_QM_ROW = re.compile(r"^\s*(\d{3,5})\s+(\S+)\s+(\S+)", re.M)

#: Verbs that need the guest actually RUNNING, not merely defined.
#: §17.1300 — `reboot` too: live, ADD68's block ran `pct reboot 111` on a STOPPED container (a stopped
#: guest cannot reboot; `pct start` is the verb). `shutdown`/`stop` on a stopped guest are §17.1240's.
#: §17.1303 — `ssh|scp|sftp|ssh-copy-id … [user@]<ipv4>`: the literal address a block reaches.
_SSH_TARGET_RE = re.compile(r"(?<![\w./-])(?:ssh|ssh-copy-id|scp|sftp)(?![\w-])[^\n|;&]*?(?:\s|@)((?:[a-z_][a-z0-9_-]{0,31}|<[A-Z0-9_]+>)@)?((?:\d{1,3}\.){3}\d{1,3})\b")

_NEEDS_RUNNING = frozenset({"exec", "enter", "push", "pull", "reboot"})

#: §17.1240 — verbs whose job is ALREADY DONE, which is not the same as working.
#: `pct start` on a running container exits non-zero ("already running"), so a
#: retry of a start that succeeded fails, and a step that is genuinely finished
#: gets recorded as broken. Live: ADD50's `pct start 111` worked, its response
#: was lost, and every retry after that could only fail because 111 was up. The
#: remedy is the guard chain CHANNEL_RULES already asks for.
_ALREADY = {"start": "running", "stop": "stopped", "shutdown": "stopped"}

#: §17.1243 — verbs that BRING A GUEST INTO EXISTENCE. For these, "there is no
#: guest N on this host" is not a blocker, it is the reason the command is being
#: run. Live, ADD111 (set up Pi-hole): `pct create 130 …` was refused with "there
#: is no guest 130 on this host — `pct list` and `qm list` do not have it", and
#: `pct start 130` and `pct exec 130 -- …` later in the SAME block were refused
#: for the same reason, so a correct 11-command runbook was unrunnable and the
#: frame fell back to "I'll do it myself". A create is refused only for the
#: opposite reason: the id is already taken.
_CREATES = frozenset({"create", "restore", "clone"})


def guests_in(commands: list[str]) -> list[tuple[str, str, str]]:
    """``[(tool, verb, id)]`` the commands address, in order, deduplicated."""
    out: list[tuple[str, str, str]] = []
    for c in commands or []:
        for m in _GUEST_RE.finditer(str(c)):
            t = (m.group(1), m.group(2), m.group(3))
            if t not in out:
                out.append(t)
    return out


def parse_pct_list(text_out: str) -> dict[str, str]:
    """``{ctid: status}``. The header row is skipped by the digit anchor."""
    return {m.group(1): m.group(2).lower() for m in _PCT_ROW.finditer(text_out or "")}


def parse_qm_list(text_out: str) -> dict[str, str]:
    """``{vmid: status}`` — `qm list` puts NAME between the id and the status."""
    return {m.group(1): m.group(3).lower() for m in _QM_ROW.finditer(text_out or "")}


def parse_pct_names(text_out: str) -> dict[str, str]:
    """``{ctid: name}`` from `pct list` (VMID Status [Lock] Name): the name is the last column."""
    out: dict[str, str] = {}
    for ln in (text_out or "").split("\n"):
        parts = ln.split()
        if len(parts) >= 3 and re.fullmatch(r"\d{3,5}", parts[0]) and not parts[-1].lower().startswith("name"):
            out[parts[0]] = parts[-1]
    return out


def parse_qm_names(text_out: str) -> dict[str, str]:
    """``{vmid: name}`` from the same listing (§17.1288f — a plan step names a
    VM by its name as often as by its id: "the AI VM", `ai-vm`)."""
    return {m.group(1): m.group(2) for m in _QM_ROW.finditer(text_out or "")}


#: `lvs -o lv_name,data_percent pve` → `vm-106-disk-0 0.00` (a thin LV; a thick one has no Data%)
_LVS_ROW = re.compile(r"^\s*(vm-(\d{3,5})-(?:disk|cloudinit)-\d+)\s+\S+g?\s*(\d+\.\d{2})?(?=\s|$)", re.M)


def parse_lvs(text_out: str) -> dict[str, list[dict]]:
    """``{vmid: [{name, data_percent}]}`` — §17.1288p. `data_percent` is None
    for a thick LV (nothing reported) and a float for a thin one; 0.0 means
    the volume has never been written: no partition table, no OS."""
    out: dict[str, list[dict]] = {}
    for ln in (text_out or "").split("\n"):
        m = _LVS_ROW.match(ln.rstrip())
        if not m:
            continue
        pct = m.group(3)
        out.setdefault(m.group(2), []).append({"name": m.group(1), "data_percent": float(pct) if pct else None})
    return out


async def read_inventory(spec) -> Optional[dict]:
    """§17.1288f — the two listings, read ONCE per pause and handed to every
    draft's `unmet`. ``None`` when the host cannot be read (then nothing is
    refused: a blocker is never invented out of blindness)."""
    if spec is None:
        return None
    pct_out, qm_out = await _read(spec, "pct list"), await _read(spec, "qm list")
    cts, vms = parse_pct_list(pct_out), parse_qm_list(qm_out)
    if not cts and not vms:
        logger.warning("preconditions_unreadable — nothing refused")
        return None
    inv = {"cts": cts, "vms": vms, "names": {**parse_pct_names(pct_out), **parse_qm_names(qm_out)}}   # §17.1316 — containers have names too
    # §17.1288p — and what is ON the guests' disks, cheaply: a thin volume at
    # Data% 0.00 has never been written, so the guest it belongs to has no OS.
    lvs_out = await _read(spec, "lvs --noheadings -o lv_name,lv_size,data_percent pve")
    inv["disks"] = parse_lvs(lvs_out)
    isos = await _read(spec, "ls /var/lib/vz/template/iso")
    inv["isos"] = [ln.strip() for ln in (isos or "").split("\n") if ln.strip().endswith(".iso")]
    return inv


#: §17.1288f — `VM 106`, `container 111`, `CT 120`: the guest a step is ABOUT.
_SUBJECT_RE = re.compile(r"\b(?:VM|CT|LXC|container|guest)\s*#?\s*(\d{3,5})\b", re.I)
#: a bare `ssh` (not `ssh-copy-id`, not a path)
_SSH_RE = re.compile(r"(?<![\w./-])ssh(?![\w-])")
#: §17.1299 — an unattended install with cloud-init seeds this host's key too (`--sshkeys`)
_KEY_STEP_RE = re.compile(r"ssh.*\bkey\b|public\s*key|authorized_keys|ssh-copy-id|cloud-init|cloud image|unattended", re.I)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def key_known_for(gid: str, name: str, plan: Optional[list[dict]]) -> Optional[str]:
    """§17.1288g — the FINISHED plan step that put this host's key on guest
    `gid` (named by id or by name), or None. Live: ADD26 "Install the SSH
    public key on the AI VM (192.168.1.129)" is done, for VM 110 `ai-vm`;
    nothing of the kind exists for VM 106."""
    for n in plan or []:
        if (n.get("status") or "") != "done":
            continue
        title = str(n.get("title") or "")
        if not _KEY_STEP_RE.search(title):
            continue
        if re.search(rf"\b{re.escape(gid)}\b", title) or (name and _norm(name) and _norm(name) in _norm(title)):
            return f"{n.get('node_key')} · {title[:60]}"
    return None


_ID_ASSIGN_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=[\"']?(\d{3,5})[\"']?\s*$", re.M)


def _resolve_ids(text_value: str) -> str:
    """§17.1288j — `VMID=106` then `qm start "$VMID"` addresses 106. Live, the
    one draft that had everything right was refused for "nothing starts it"
    because the start said `"$VMID"`. Only whole-line numeric assignments are
    resolved; the value is a guest id or it is nothing to us."""
    out = str(text_value or "")
    for m in _ID_ASSIGN_RE.finditer(out):
        name, val = m.group(1), m.group(2)
        out = re.sub(rf'"?\$\{{{re.escape(name)}\}}"?|"?\${re.escape(name)}\b"?', val, out)
    return out


resolve_ids = _resolve_ids      # §17.1308 — machine_truth.step_needs reads the same block


def _first_line_with(texts: list[str], pattern: "re.Pattern[str]") -> str:
    for t in texts:
        for ln in str(t).split("\n"):
            if pattern.search(ln):
                return ln.strip()[:200]
    return ""


#: `cat > f <<'EOF'`, `<<-MARK`, `<<"M"` — a heredoc opener and its marker
_HEREDOC_OPEN_RE = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")


def heredoc_depths(text: str) -> list[int]:
    """The heredoc depth of every line of a shell text.

    §17.1338 — depth says who runs a line. Depth 0 is the script itself; depth 1 is
    a heredoc body the script writes and then RUNS (live, the container template
    pushes `<<'REMOTE'` into the guest and runs it there); depth 2 and deeper is
    content that body writes to a file — a Node route, a config — which nothing in
    this step executes. An ssh there is the service's problem, not the block's.
    """
    out: list[int] = []
    stack: list[str] = []
    for ln in str(text or "").split("\n"):
        if stack and ln.strip() == stack[-1]:
            stack.pop()
            out.append(len(stack) + 1)       # the terminator belongs to the body
            continue
        out.append(len(stack))
        m = _HEREDOC_OPEN_RE.search(ln)
        if m:
            stack.append(m.group(2))
    return out


def executed_lines(text: str, max_depth: int = 1) -> list[str]:
    """The lines of `text` a step actually runs (heredoc depth <= `max_depth`)."""
    lines = str(text or "").split("\n")
    return [ln for ln, d in zip(lines, heredoc_depths(text), strict=False) if d <= max_depth]


def written_content_lines(text: str, min_depth: int = 2) -> list[str]:
    """The lines `text` WRITES into a file rather than runs."""
    lines = str(text or "").split("\n")
    return [ln for ln, d in zip(lines, heredoc_depths(text), strict=False) if d >= min_depth]


def ssh_target_in(line: str) -> tuple[str, str]:
    """``(user, host)`` the ssh on this line reaches, as far as it can be read.

    §17.1338 — the key rule used to attribute every ssh to the step's SUBJECT
    guest. Live, ADD122 writes a Node route into container 111 whose code ssh's
    OUT to the PalWorld VM, and the refusal said "nothing has put this host's key
    on guest 111" and told the drafter to prefix an ssh the block does not run.
    An ssh is judged by the target it names.
    """
    m = _SSH_TARGET_RE.search(str(line or ""))
    return ((str(m.group(1) or "").rstrip("@"), str(m.group(2) or ""))) if m else ("", "")


def _guest_for_target(host: str, inv: Optional[dict], subjects: list[str]) -> str:
    """The guest id `host` names: by measured address, by a `<NAME_IP>` style
    placeholder against the inventory's names, else ''. Never the subject by
    default — that assumption is the defect this fixes."""
    if not host:
        return ""
    names = (inv or {}).get("names") or {}
    for gid, nm in names.items():
        word = re.sub(r"[^a-z0-9]", "", str(nm or "").lower())
        if word and word[:6] and word[:6] in re.sub(r"[^a-z0-9]", "", host.lower()):
            return str(gid)
    for gid, addrs in ((inv or {}).get("addresses") or {}).items():
        if host in (addrs if isinstance(addrs, (list, tuple)) else [addrs]):
            return str(gid)
    return ""


#: an address the file itself names: a literal, or a `<GUEST_IP>` placeholder
_FILE_HOST_RE = re.compile(r"(?<![\w.])((?:\d{1,3}\.){3}\d{1,3})(?![\w.])|<([A-Z][A-Z0-9_]*_IP)>")


def host_a_file_reaches(content: str, line: str) -> str:
    """The host a file's ssh goes to: the ssh line's own target, else the address
    the FILE names (`const PALWORLD_IP = '192.168.1.106'`), else ''.

    §17.1338 — ADD122's route holds `execFileSync('ssh', [...])` with the address in
    a constant at the top of the file. Reading only the ssh line finds nothing, and
    assuming the step's subject names the wrong machine entirely.
    """
    _u, host = ssh_target_in(line)
    if host:
        return host
    hosts: list[str] = []
    for m in _FILE_HOST_RE.finditer(str(content or "")):
        h = m.group(1) or (f"<{m.group(2)}>" if m.group(2) else "")
        if h and not h.endswith(".0") and not h.endswith(".255") and h not in hosts:
            hosts.append(h)
    return hosts[0] if len(hosts) == 1 else ""


_THIS_GUEST = "this step's guest"


def _file_ssh_needs_its_own_credential(files: Optional[list[dict]], subjects: list[str],
                                       inv: Optional[dict], plan: Optional[list[dict]]) -> list[dict]:
    """§17.1338 — a written file whose code ssh's somewhere at RUNTIME.

    The remedy is never "prefix the ssh in this block": there is no such ssh. The
    service the file becomes needs a credential of its own on the target, which is
    a step of its own. Said once per (file, target), naming the file, the host it
    reaches and the guest it reaches FROM.
    """
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    here = subjects[0] if subjects else ""
    for f in files or []:
        content = str((f or {}).get("content") or "")
        path = str((f or {}).get("path") or "") or "the written file"
        written = written_content_lines(content)
        for line in written:
            if not _SSH_RE.search(line) or "sshpass" in line or "ssh-copy-id" in line:
                continue
            if line.lstrip().startswith("#"):
                continue
            host = host_a_file_reaches("\n".join(written), line)
            if not host:
                continue                  # nothing readable to name: no judgment
            target = _guest_for_target(host, inv, subjects)
            if target and key_known_for(target, ((inv or {}).get("names") or {}).get(target, ""), plan):
                continue                  # a finished step put a key there
            if (path, host) in seen:
                continue
            seen.add((path, host))
            out.append({"command": line.strip()[:200], "why": (
                f"{path} runs `ssh` at RUNTIME, from inside guest {here or _THIS_GUEST} to {host}"
                + (f" (guest {target})" if target else "")
                + ". Nothing has given that service a credential there, so the request fails with publickey "
                  "the first time anyone uses the page -- while this step's checks pass, because the file is "
                  "on disk. This is not a prefix for this block: it needs a step that generates a key for the "
                  f"service user in guest {here or '?'} and installs it on {host}, with this step waiting for "
                  "it. A password in the service's environment is the other way; a key is the one that "
                  "survives a restart.")})
            break                          # one judgment per file
    return out


#: §17.1343 — `printf '%s\n' 'Key=Value' >> /path/conf`, `echo "K=V" >> "$CONF"`:
#: a key appended to the END of a file.
_APPEND_KEY_RE = re.compile(
    r"\b(?:printf|echo|tee\s+-a)\b[^\n>]*?(?P<kv>['\"][^'\"\n]*=[^'\"\n]*['\"])?[^\n>]*>>\s*"
    r"(?P<path>\"[^\"]+\"|'[^']+'|\$\{?[A-Za-z_]\w*\}?|/[^\s<>|;&]+)")
#: a section header being written in the same block, or a tool that knows sections
_SECTION_AWARE_RE = re.compile(
    # a `[Section]` literal — plain, or escaped inside a sed address — or a tool that
    # understands sections
    r"\\?\[[A-Za-z][\w .-]*\\?\]|crudini|configparser|ConfigParser|augtool", re.I)


def appends_a_key(texts: list[str]) -> list[tuple[str, str]]:
    """``[(line, path)]`` — every bare append of a `key=value` line, with its target."""
    out: list[tuple[str, str]] = []
    for t in texts:
        for ln in str(t or "").split("\n"):
            if ln.lstrip().startswith("#") or "=" not in ln:
                continue
            m = _APPEND_KEY_RE.search(ln)
            if not m:
                continue
            path = m.group("path").strip("\"'")
            if (ln, path) not in out:
                out.append((ln, path))
    return out


async def a_bare_append_lands_in_the_last_section(spec, texts: list[str], gid: str, reading=None) -> list[dict]:
    r"""§17.1343 — a key appended to a SECTIONED config is read as a different setting.

    Live, 2026-10-04: ADD130 appended `Session\DefaultSavePath=/media/downloads` to
    qBittorrent's config with `>>`. The run reported success, the key was in the file,
    and the step's check found it — but `>>` writes to the END, which landed it under
    `[Preferences]`, while every other `Session\` key in that file sits under
    `[BitTorrent]` and the app reads it as `BitTorrent/Session\DefaultSavePath`. The
    setting was inert, and nothing said so.

    The file itself settles it, so the file is read: a `grep -c '^\['` through the
    runner. Unreadable means no judgment.
    """
    if spec is None or not gid:
        return []
    out: list[dict] = []
    joined = "\n".join(str(t or "") for t in texts)
    for ln, path in appends_a_key(texts):
        if _SECTION_AWARE_RE.search(joined):
            continue                           # the block already places it in a section
        if path.startswith("$"):
            m = re.search(rf"^\s*{re.escape(path.lstrip('${').rstrip('}'))}=[\"']?(/[^\s\"']+)",
                          joined, re.M)
            if not m:
                continue                       # a variable the engine cannot resolve: no judgment
            path = m.group(1)
        # §17.1353 — `_read` returns the LISTING, not `(ok, text)`. This site unpacked
        # it as a pair, so every call raised ValueError into `unmet`'s warning and this
        # rule never once fired in production. A mocked read hid it: all four of its
        # tests stubbed `_read` with a 2-tuple the real function never returns.
        out_text = await _read(spec, f"pct exec {gid} -- sh -c 'grep -c \"^\\[\" {path}'")
        if not str(out_text or "").strip():
            # §17.1363 — "nothing is refused out of blindness" was right and silent:
            # the blindness itself is now in the frame.
            if reading is not None:
                reading.gap(f"the section layout of {path}",
                            "the runner could not count its `[section]` headers, so a bare "
                            "append into it could not be judged")
            continue
        try:
            sections = int((out_text or "0").strip().split("\n")[-1])
        except (TypeError, ValueError):
            continue
        if sections <= 0:
            continue                           # a flat config: a bare append is right
        out.append({"command": ln.strip()[:200], "why": (
            f"{path} is a SECTIONED config ({sections} `[section]` headers, read just now), and `>>` "
            f"appends to the END of the file -- the key lands in whatever section happens to be last and "
            f"the application reads it as a different setting. Live (§17.1343), "
            f"`Session\\DefaultSavePath` landed under `[Preferences]` while every other `Session\\` key "
            f"in that file sits under `[BitTorrent]`: the run reported success, the key was in the file, "
            f"the check found it, and nothing read it. Put the line INSIDE its section (a `sed` range on the "
            f"header, or the file's own tool) and make the check read the SECTION, not just the key.")})
    return out


#: §17.1361 — the guest a line addresses: `pct exec 105 -- …`.
_IN_GUEST_LINE_RE = re.compile(r"\b(?:pct\s+exec|qm\s+guest\s+exec)\s+(?P<gid>\d{3,5})\b", re.I)
#: §17.1353 — an in-place substitution: `sed -i 's|PAT|REPL|' PATH`, `perl -pi -e "s/…/…/" PATH`.
#: The flag token is read from the words BEFORE the expression, so `sed -n`/`sed -e` are not it.
_INPLACE_FLAG_RE = re.compile(r"(?<![\w-])-[A-Za-z.]*i[A-Za-z.]*(?![\w-])")
#: the first `s<delim>PATTERN<delim>` of the expression
_SUB_RE = re.compile(r"(?<![\w$])s(?P<d>[|/#,@:!~])(?P<pat>(?:\\.|(?!(?P=d)).)*?)(?P=d)")
#: §17.1361 — `/ADDRESS/a text`, `/ADDRESS/i text`, `/ADDRESS/c text`: the address
#: must EXIST in the file or the append writes nothing. Live, ADD132 ran
#: `sed -i '/^\[Preferences\]/a WebUI\\Username=admin…' …/qBittorrent-data.conf`
#: against a file with no `[Preferences]` in it; nothing was written, exit 0, and the
#: step's own login check caught it only afterwards.
_ADDR_RE = re.compile(r"(?<![\w$])(?P<d>[/|#])(?P<pat>(?:\\.|(?!(?P=d)).)+?)(?P=d)\s*(?P<verb>[aic])\b")
#: the last word of the line: the file the edit lands in
_EDIT_TARGET_RE = re.compile(r"(\"[^\"]+\"|'[^']+'|/[^\s<>|;&]+)\s*$")
#: `cp SRC DST` / `install … SRC DST` — where a file this block edits came from
_COPIED_RE = re.compile(
    r"^\s*(?:sudo\s+)?(?:cp|install)\s+(?:-\S+\s+)*(?P<src>\"[^\"]+\"|'[^']+'|/[^\s<>|;&]+)\s+"
    r"(?P<dst>\"[^\"]+\"|'[^']+'|/[^\s<>|;&]+)\s*$")
#: what may be embedded in a double-quoted `grep -F` word: no quote, no `$`, no backtick
_ANCHOR_SAFE_RE = re.compile(r"[A-Za-z0-9_<>/=:,.+ -]{4,80}")


def in_place_substitutions(texts: list[str]) -> list[tuple[str, str, str]]:
    """``[(line, path, pattern)]`` for every in-place substitution with a literal target."""
    out: list[tuple[str, str, str]] = []
    for t in texts:
        for raw in str(t or "").split("\n"):
            ln = raw.strip()
            if ln.startswith("#") or not re.search(r"(?<![\w-])(?:sed|perl)(?![\w-])", ln):
                continue
            head = re.split(r"['\"]", ln, maxsplit=1)[0]
            if not _INPLACE_FLAG_RE.search(head):
                continue
            target = _EDIT_TARGET_RE.search(ln)
            if not target:
                continue
            # §17.1361 — a substitution's PATTERN and an append's ADDRESS are the same
            # question: does the file hold this text? Neither can fail for missing it.
            found = _SUB_RE.search(ln) or _ADDR_RE.search(ln)
            if not found:
                continue
            out.append((ln[:200], target.group(1).strip("\"'"), found.group("pat")))
    return out


def literal_anchor(pattern: str) -> str:
    """The longest run of plain text a regex pattern must find verbatim, or ``""``.

    `<ContentType>.*</ContentType>` must find `</ContentType>`; `^${KEY}=.*` holds a
    shell expansion this cannot resolve, so it yields nothing and is not judged.
    """
    pat = str(pattern or "")
    if pat.startswith("^"):
        pat = pat[1:]
    if pat.endswith("$") and not pat.endswith("\\$"):
        pat = pat[:-1]
    # §17.1361 — a sed address escapes its brackets: `^\[Preferences\]`. Those are
    # the regex's own punctuation, not a shell escape, so they are unwrapped before
    # the question is asked; anything else backslashed is still left alone.
    for esc, plain in ((r"\[", "["), (r"\]", "]"), (r"\.", "."), (r"\/", "/"), (r"\-", "-")):
        pat = pat.replace(esc, plain)
    if any(c in pat for c in ("$", "`", "\\")):
        return ""                              # an expansion or an escape: not ours to resolve
    best = max(re.split(r"[.*+?\[\]()^${}|]+", pat), key=len, default="")
    return best if len(best) >= 4 and re.search(r"[A-Za-z0-9]", best) else ""


def copied_from(texts: list[str], path: str) -> str:
    """The source a `cp`/`install` in this same block puts at `path` (backups aside)."""
    for t in texts:
        for raw in str(t or "").split("\n"):
            m = _COPIED_RE.match(raw)
            if not m or ".bak" in raw:
                continue
            if m.group("dst").strip("\"'") == str(path):
                return m.group("src").strip("\"'")
    return ""


async def an_in_place_edit_the_file_cannot_match(spec, texts: list[str], gid: str, reading=None) -> list[dict]:
    r"""§17.1353 — a substitution whose pattern the file does not hold changes nothing.

    Live, 2026-10-04: ADD133 gave two new Jellyfin libraries their content type with
    `sed -i 's|<ContentType>.*</ContentType>|<ContentType>movies</ContentType>|'` over a
    copy of an existing library's `options.xml`. That file has no `ContentType` element —
    Jellyfin 10.11 keeps a library's type in a `<type>.collection` marker file — so the
    edit matched nothing, exited 0, and changed nothing. The step reported success, its
    check read only the `.mblink` paths, and both libraries came out as mixed content.

    `sed -i` cannot fail for not matching, so the file is what settles it: one
    `grep -c -F` through the runner, against the target or, when this block copies the
    target into place, against the source it copies. Unreadable means no judgment.
    """
    if spec is None:
        return []
    out: list[dict] = []
    seen: set = set()
    for ln, path, pat in in_place_substitutions(texts):
        # §17.1361 — read the file in the guest the LINE addresses. ADD132's edit ran
        # `pct exec 105 -- sed -i …` while the step's subject guest was 103, where
        # that path does not exist: the read would have come back "No such file" and
        # the inert edit would have gone unjudged.
        _g = _IN_GUEST_LINE_RE.search(ln)
        here = _g.group("gid") if _g else str(gid or "")
        if not here.isdigit():
            continue
        anchor = literal_anchor(pat)
        if not path.startswith("/") or not anchor or not _ANCHOR_SAFE_RE.fullmatch(anchor):
            continue
        if (path, anchor) in seen:
            continue
        seen.add((path, anchor))
        # The runner's read-only channel takes a bare `grep`, not a shell script: an
        # assignment is "not a known read-only command". So: the target, and when the
        # block copies the target into place (ADD133 did), the source it copies -- a
        # file that does not exist yet answers with grep's own "No such file", which
        # is not a count and so is not a judgment.
        where, count = "", None
        for cand in [path] + [c for c in (copied_from(texts, path),) if c and c != path]:
            got = str(await _read(
                spec, f'pct exec {here} -- grep -c -F -- "{anchor}" {cand}') or "").strip()
            tail = got.split("\n")[-1].strip() if got else ""
            if tail.isdigit():
                where, count = cand, int(tail)
                break
        if count is None:
            if reading is not None:
                reading.gap(path, f"could not be read in guest {here}, so an edit whose "
                                  f"pattern it may not contain could not be judged")
            continue
        if count > 0:
            continue                                        # the pattern is there
        out.append({"command": ln, "why": (
            f"this edit changes NOTHING: `{anchor}` does not appear in {where} -- read just now, 0 "
            f"matches. `sed -i` cannot fail for matching nothing: it exits 0, the file is untouched, "
            f"the step reports success and the setting is never set. Live (§17.1353), ADD133 set two "
            f"Jellyfin libraries' content type with `s|<ContentType>.*</ContentType>|...|` against a "
            f"file holding no such element; both libraries came out as mixed content and the check, "
            f"which read only the `.mblink` paths, passed. Read the file and change what it actually "
            f"holds. When the setting is not in that file at all, the application keeps it elsewhere "
            f"-- another file, a marker file, or its own API -- so find where, and make this step's "
            f"check read the setting back out rather than checking that a file exists.")})
    return out


#: §17.1359 — an API path the block's CHECK reads: `…/api/v3/downloadclient`,
#: `…/api/v2/app/preferences`. The last path segment is the name of the thing.
_API_PATH_RE = re.compile(
    r"https?://(?P<host>\[[0-9a-fA-F:]+\]|[A-Za-z0-9_.-]+):(?P<port>\d{2,5})"
    r"(?P<path>/[A-Za-z0-9_./-]*)")
#: a name written INTO a file: an XML element, or a `Key=`/`"key":` assignment
_WRITES_NAME_RE = re.compile(
    r"</?(?P<el>[A-Za-z][A-Za-z0-9_]{3,40})>|(?P<key>[A-Za-z][A-Za-z0-9_\\]{3,40})\s*=|"
    r"\\?\"(?P<jkey>[A-Za-z][A-Za-z0-9_]{3,40})\\?\"\s*:")


def _api_names_the_checks_read(verify: list[str]) -> dict:
    """``{port: {name}}`` — what each check reads, and from which service's API."""
    out: dict = {}
    for c in verify or []:
        for m in _API_PATH_RE.finditer(str(c or "")):
            segs = [s for s in (m.group("path") or "").split("/") if s]
            # drop the api/version prefix: `api`, `v3`, `v2`
            names = [s for s in segs if s.lower() != "api" and not re.fullmatch(r"v\d+", s.lower())]
            if names:
                out.setdefault(m.group("port"), set()).add(names[-1].lower())
    return out


async def writes_where_the_check_does_not_read(spec, texts: list[str], verify: list[str],
                                               services: Optional[list] = None, reading=None) -> list[dict]:
    r"""§17.1359 — the work writes a setting into a config the file has no concept of,
    while the step's own check reads that same setting from the service's API.

    Live, 2026-10-04. ADD132's clean frame — no refusals, Run suggested — did the
    qBittorrent half correctly and then, for each *arr:

        pct exec 103 -- systemctl stop radarr.service
        pct exec 103 -- python3 …  # insert <DownloadClientConfig>… into /var/lib/radarr/config.xml
        pct exec 103 -- systemctl start radarr.service

    while its check read `http://127.0.0.1:7878/api/v3/downloadclient`. Measured on
    the machine:

        grep -c -i downloadclient /var/lib/radarr/config.xml   ->  0
        elements in that file: ApiKey Port BindAddress AuthenticationMethod … UrlBase
        /var/lib/radarr/radarr.db                              ->  CREATE TABLE "DownloadClients"
        /api/v3/downloadclient                                 ->  200

    Download clients live in the database, behind the API. The block would have
    bounced both services, polluted two configs, registered nothing — and its own
    check would have come back empty. What pushed it there is the engine's own fact
    line, *"config … (the service rewrites it: stop it before editing)"*, which
    reads as "this is where you configure it".

    The check is the authority: §17.1345 already forces it to read the real result.
    So when the work writes a name into a config and the check reads that same name
    from that service's API, and the config the engine reads has no instance of it,
    the API is the surface. Unreadable means no judgment.
    """
    if spec is None or not services:
        return []
    want = _api_names_the_checks_read(verify)
    if not want:
        return []
    out: list[dict] = []
    seen: set = set()
    joined = "\n".join(str(t or "") for t in texts or [])
    for svc in services:
        ports = {str(p) for p in (getattr(svc, "ports", ()) or ())}
        names: set = set()
        for port, got in want.items():
            if port in ports:
                names |= got
        cfg = str(getattr(svc, "config", "") or "")
        gid = str(getattr(svc, "guest", "") or "")
        if not names or not cfg.startswith("/") or not gid.isdigit():
            continue
        if cfg not in joined:
            continue                       # the block does not write this config
        for name in sorted(names):
            if (cfg, name) in seen:
                continue
            seen.add((cfg, name))
            # the block introduces that name into the file
            if not any(name == (m.group("el") or m.group("key") or m.group("jkey") or "").lower()
                       or (m.group("el") or m.group("key") or m.group("jkey") or "").lower().startswith(name)
                       for m in _WRITES_NAME_RE.finditer(joined)):
                continue
            got = await _read(spec, f'pct exec {gid} -- grep -c -i -F -- "{name}" {cfg}')
            tail = str(got or "").strip().split("\n")[-1].strip()
            if not tail.isdigit():
                if reading is not None:
                    reading.gap(cfg, f"could not be read in guest {gid}, so whether it holds "
                                     f"`{name}` is unknown")
                continue
            if int(tail) > 0:
                continue                   # the config does know it
            api = next((f"port {p}" for p in sorted(ports) if p in want), "its API")
            out.append({"command": f"writes `{name}` into {cfg}", "why": (
                f"this step's own check reads `{name}` from {getattr(svc, 'name', 'the service')}'s API on "
                f"{api}, and the work writes it into {cfg} -- a file that has no instance of `{name}` "
                f"(read just now, 0 matches). The two are different surfaces and the check is the one that "
                f"settles it: a setting the API serves lives where the API keeps it, which for these apps "
                f"is their database, not their config file. Live (§17.1359), ADD132 would have stopped "
                f"{getattr(svc, 'name', 'the service')}, inserted a `<DownloadClientConfig>` element into a "
                f"config whose only elements are the app's own (ApiKey, Port, BindAddress, "
                f"AuthenticationMethod ...), restarted it, registered nothing, and failed its own check. "
                f"Do the write through the same API the check reads.")})
    return out


async def unmet(commands: list[str], spec, *, plan: Optional[list[dict]] = None,
                files: Optional[list[dict]] = None, node: Optional[dict] = None,
                inventory: Optional[dict] = None, truth=None,
                services: Optional[list] = None, verify: Optional[list[str]] = None,
                reading=None) -> list[dict]:
    """``[{command, why}]`` for every command the host contradicts.

    `plan` is the job's nodes, so a refusal can name the step that would make
    this one runnable rather than leaving the operator to find it. Fail-soft:
    if the host cannot be read, nothing is refused — this must never invent a
    blocker out of its own blindness.

    §17.1288f — the FILES a block writes are read too (the live ADD82 script
    addressed VM 106 only from inside `/tmp/install_agent_106.sh`), and the
    guest the STEP is about is checked even when no command names it: a
    stopped guest that nothing in the block starts, and an ssh into a guest
    that holds no key of this host's, are both contradictions the engine can
    see before sending anything.
    """
    texts = [_resolve_ids(t) for t in [str(c) for c in commands or []]
             + [str((f or {}).get("content") or "") for f in files or []]]
    guests = guests_in(texts)
    subject = str((node or {}).get("title") or "") + "\n" + str((node or {}).get("description") or "")
    subjects = list(dict.fromkeys(_SUBJECT_RE.findall(subject))) if node else []
    # §17.1316 — "Install PalWorld server" names VM 106 by its name; the pause measured it,
    # so the truth's guest is the step's subject for every rule below.
    _tg = str(getattr(truth, "gid", "") or "")
    if _tg and _tg not in subjects:
        subjects.append(_tg)
    uses_ssh = any(_SSH_RE.search(t) for t in texts)
    # §17.1338 — which texts are COMMANDS the block runs, and which are the
    # contents of files it writes. An ssh inside a written file runs later, from
    # the guest the file lands in, against whatever target the file names: it is
    # not this block reaching into its own subject.
    out: list[dict] = []
    inv = inventory if inventory is not None else (await read_inventory(spec) if spec is not None else None)
    # §17.1344 — an id no machine has is not a guest. Live, "in-container 999:996"
    # (the services' uid and gid) read as guest 999, and the host-side `chown` was
    # refused for "not reaching guest 999". The inventory settles it, so this runs
    # AFTER it is read; with no inventory, or for an id this block creates, nothing
    # changes.
    from app.modules.machine_truth import known_guest
    subjects = [g for g in subjects if known_guest(g, inv, subject + "\n" + "\n".join(texts))]
    # §17.1288g — an ssh into the step's guest with no key of ours on it and
    # none copied in this block. Judged from the plan (the engine's own record
    # of what it finished), so it needs no host read.
    if uses_ssh and subjects:
        # §17.1290b — cloud-init's `--sshkeys` installs this host's key as surely as ssh-copy-id does
        copies = any("ssh-copy-id" in t or "--sshkeys" in t for t in texts)
        # §17.1338 — only the lines the step RUNS decide this rule. A line two
        # heredocs deep is content a script writes to a file (a Node route, a
        # config); it is judged below, against the host that content names.
        def _bare(t: str) -> list[str]:
            return [ln for ln in executed_lines(t)
                    if _SSH_RE.search(ln) and "sshpass" not in ln and "ssh-copy-id" not in ln
                    and not ln.lstrip().startswith("#")]
        cmd_bare = [ln for t in texts for ln in _bare(str(t))]
        if cmd_bare and not copies:
            names = (inv or {}).get("names") or {}
            for gid in subjects:
                known = key_known_for(gid, names.get(gid, ""), plan)
                if known:
                    continue
                out.append({"command": cmd_bare[0].strip()[:200], "why": (
                    f"nothing has put this host's key on guest {gid}: no finished step installed one there "
                    f"and this block copies none, so `ssh -o BatchMode=yes` is refused by the guest before "
                    f"anything runs (publickey). Before the first ssh: `SSHPASS=\"$MASS_PASSWORD\" sshpass -e "
                    f"ssh-copy-id -o StrictHostKeyChecking=accept-new \"$USER@$IP\"` -- or prefix the ssh "
                    f"itself with `SSHPASS=\"$MASS_PASSWORD\" sshpass -e`. The password travels by name; "
                    f"nothing here can type one.")})
                break
        if not copies:
            out.extend(_file_ssh_needs_its_own_credential(files, subjects, inv, plan))
    # §17.1343 — a key appended to the END of a SECTIONED config is read as a
    # different setting: the file itself settles it, so the file is read.
    if subjects:
        try:
            out.extend(await a_bare_append_lands_in_the_last_section(spec, texts, subjects[0], reading=reading))
        except Exception as exc:
            logger.warning("append_section_check_failed err=%r", exc)
    # §17.1353 — an in-place substitution whose pattern the file does not hold is inert.
    if subjects:
        try:
            out.extend(await an_in_place_edit_the_file_cannot_match(spec, texts, subjects[0], reading=reading))
        except Exception as exc:
            logger.warning("inert_edit_check_failed err=%r", exc)
    # §17.1360 — the work writes a setting into a config that has no concept of it
    # while the step's own check reads it from the service's API.
    if services and verify:
        try:
            out.extend(await writes_where_the_check_does_not_read(spec, texts, verify, services, reading=reading))
        except Exception as exc:
            logger.warning("surface_check_failed err=%r", exc)
    # §17.1303 — the block reaches the step's guest at an address the machine
    # contradicts. Live, ADD84 (VM 106, measured at 192.168.1.106 by its MAC in
    # `ip neigh`, pinned as PALWORLD_IP) drew `ssh <PALWORLD_USER>@192.168.1.127`
    # -- VM 110's old address, borrowed from another step's text. A measured
    # address beats a written one; a step that names the other machine itself
    # is left alone (ADD49 legitimately reaches 192.168.1.127).
    addr = str(getattr(truth, "address", "") or "")
    tgid = str(getattr(truth, "gid", "") or "")
    if addr and tgid and tgid in subjects:
        hit = next(((t, m) for t in texts for m in _SSH_TARGET_RE.finditer(t)
                    if m.group(2) != addr and m.group(2) not in subject), None)
        if hit:
            t, m = hit
            kind = "container" if getattr(truth, "kind", "") == "ct" else "VM"
            line = t[t.rfind("\n", 0, m.start()) + 1:].split("\n", 1)[0].strip()
            out.append({"command": line[:200], "why": (
                f"`{m.group(2)}` is not {kind} {tgid}'s address: the engine measured `{addr}` for its MAC "
                f"`{getattr(truth, 'mac', '') or '?'}` (`ip neigh`, read just now), and the step names no other "
                f"machine. Reach {kind} {tgid} at `{addr}` -- or, since its guest agent "
                f"{'answers' if getattr(truth, 'agent', False) else 'may answer'}, through `qm guest exec {tgid}` "
                f"with no address at all.")})
    # §17.1313 — the block fetches by name inside a guest the truth measured as unable
    # to resolve: refuse it, with the inserted fix step named when the truth has one.
    if getattr(truth, "resolves", None) is False and tgid and tgid in subjects:
        from app.modules.machine_truth import _NET_FETCH_RE
        hit = _first_line_with(texts, _NET_FETCH_RE)
        if hit:
            kind = "container" if getattr(truth, "kind", "") == "ct" else "VM"
            out.append({"command": hit[:200], "why": (
                f"{kind} {tgid} cannot resolve names: `getent hosts deb.debian.org` inside it printed nothing (read just now), "
                f"so this download hangs until the runner's 180 s timeout -- it did, twice. The resolver is set on the "
                f"host (`{'qm' if kind == 'VM' else 'pct'} set {tgid} --nameserver "
                f"\"{getattr(truth, 'dns_hint', '') or '<DNS_SERVER>'}\"` + a reboot); the engine inserts that step and this one waits for it.")})
    if (not guests and not subjects) or not inv:
        return out
    cts, vms = inv["cts"], inv["vms"]
    # §17.1288f — the step is about a stopped guest, the block needs it up
    # (an ssh, or a wait for `running`) and nothing in the block starts it.
    # Live: the script waited 60 s for VM 106 to be running, then swept and
    # read `ip neigh`, then sshed -- and `qm list` had said `stopped` all along.
    for gid in subjects:
        status = cts.get(gid) if gid in cts else vms.get(gid)
        if status != "stopped":
            continue
        starts = any(re.search(rf"\b(?:pct|qm)\s+start\s+{gid}\b", t) for t in texts)
        waits = any(re.search(rf"\b(?:pct|qm)\s+status\s+{gid}\b.*running", t) for t in texts)
        if starts or not (uses_ssh or waits):
            continue
        tool = "pct" if gid in cts else "qm"
        fix = _step_that_starts(gid, plan)
        out.append({"command": _first_line_with(texts, re.compile(rf"\b{gid}\b|(?<![\w./-])ssh(?![\w-])")) or f"ssh into {gid}", "why": (
            f"{'container' if tool == 'pct' else 'VM'} {gid} is stopped (`{tool} list`, read just now) and nothing "
            f"in this block starts it -- it {'waits for it to be running and then ' if waits else ''}reaches into "
            f"it, which cannot happen. Start it first, guarded: `{tool} status {gid} | grep -q running || "
            f"{tool} start {gid}`, then wait for it with a loop of reads, and only then find its address "
            f"(sweep the bridge's /24 and read `ip neigh` for its MAC, up to 12 × 5 s) and ssh."
            + (f" {fix} is the plan step that starts it, and it has not run yet." if fix else ""))})
    # §17.1288p — the step reaches INTO a VM whose only disks have never been
    # written: there is no OS to log into. Live, ADD82 ran twice against VM 106
    # (`qm list` running, `bridge fdb` never saw its MAC, scsi0 read 222 KB in
    # 30 min) before anyone read `lvs`: vm-106-disk-0 100G Data% 0.00, blkid
    # empty. ADD5 "Install Ubuntu Server" was recorded done, and ADD53 later
    # removed that disk (`pvesm free`) and created an empty one. The engine
    # had the read all along.
    disks = inv.get("disks") or {}
    # §17.1290b — the step that INSTALLS the OS is the one step the empty disk
    # does not block: live, ADD117 (cloud image + cloud-init) was refused for
    # the very condition it exists to end, because its script sshes in after.
    installs_os = bool(re.search(r"\binstall\b.*\b(?:ubuntu|debian|operating system|os\b|server \d\d\.\d\d)", subject, re.I)) \
        or any(re.search(r"\bqm\s+importdisk\b|\bqm\s+disk\s+import\b|local-lvm:cloudinit|--cicustom\b", t) for t in texts)
    for gid in subjects:
        if installs_os:
            break
        if gid not in vms or not (uses_ssh or any(re.search(rf"\bqm\s+guest\s+exec\s+{gid}\b", t) for t in texts)):
            continue
        mine = [d for d in disks.get(gid, []) if "cloudinit" not in d["name"]]
        if not mine or any(d["data_percent"] is None or d["data_percent"] > 0.0 for d in mine):
            continue
        names = ", ".join(d["name"] for d in mine)
        installed = next((f"{n.get('node_key')} · {str(n.get('title'))[:50]}" for n in plan or []
                          if (n.get("status") or "") == "done"
                          and re.search(r"\binstall\b.*\b(?:ubuntu|debian|os|server)\b", str(n.get("title") or ""), re.I)
                          and (gid in str(n.get("title") or "") or _norm((inv.get("names") or {}).get(gid, "")) in _norm(str(n.get("title") or "")))), "")
        isos = ", ".join((inv.get("isos") or [])[:3]) or "none found in /var/lib/vz/template/iso"
        out.append({"command": _first_line_with(texts, _SSH_RE) or f"ssh into {gid}", "why": (
            f"VM {gid}'s disk {names} has never been written (`lvs` Data% 0.00; no partition table, no "
            f"filesystem): there is no OS to log into, so an ssh or a guest agent cannot exist there yet. "
            + (f"{installed} is recorded done, but a later step recreated the disk, so that install is gone. "
               if installed else "")
            + f"Ubuntu must be installed on VM {gid} before this step (ISOs on the host: {isos}) -- an OS "
            f"install is a console step, or an autoinstall from the ISO; it is not this step.")})
        break
    if not guests:
        if out:
            logger.warning("preconditions_unmet count=%d first=%r", len(out), out[0]["why"][:120])
        return out
    commands = texts
    # §17.1243 — guests an earlier command in this same block brings into being.
    # `guests_in` preserves command order, so a create is always seen before the
    # start and the exec that follow it.
    made: set[str] = set()
    # §17.1305 — a guest this very block STARTS before it reaches in is running by
    # then. Live, ADD88's draft began `pct start 120` and its `pct exec 120` was
    # refused as "stopped"; the engine's own run_in_container template (guarded
    # start in the script, then push + exec) was refused the same way -- the
    # rule read the listing and never the block. `guests_in` keeps command order.
    started: set[str] = set()
    for tool, verb, gid in guests:
        if verb == "start":
            started.add(gid)
        cmd = next((c for c in commands if re.search(rf"\b{tool}\s+{verb}\s+{gid}\b", str(c))), f"{tool} {verb} {gid}")
        is_ct, is_vm = gid in cts, gid in vms
        if verb in _CREATES:
            if is_ct or is_vm:
                out.append({"command": cmd, "why": (
                    f"id {gid} is already taken on this host — it is "
                    f"{'a container' if is_ct else 'a VM'} — so `{tool} {verb} {gid}` would collide "
                    f"with it. Proxmox shares one id space between containers and VMs, so pick an id "
                    f"in neither `pct list` nor `qm list`. If this step ALREADY created {gid} and is "
                    f"being run again to finish what comes after, guard the create instead of "
                    f"repeating it: `{tool} status {gid} >/dev/null 2>&1 || {tool} {verb} {gid} …` — "
                    f"then the rest of the block can run without building it twice.")})
            else:
                made.add(gid)
            continue
        if gid in made:
            continue                  # created earlier in this very block
        if tool == "pct" and is_vm and not is_ct:
            out.append({"command": cmd, "why": (
                f"{gid} is a VM on this host, not a container — `pct` cannot address it. "
                f"The same thing for a VM is `qm {verb}` (or `qm guest exec` inside it).")})
        elif tool == "qm" and is_ct and not is_vm:
            out.append({"command": cmd, "why": (
                f"{gid} is a container on this host, not a VM — `qm` cannot address it. Use `pct {verb}`.")})
        elif not is_ct and not is_vm:
            out.append({"command": cmd, "why": f"there is no guest {gid} on this host — `pct list` and `qm list` do not have it."})
        elif verb in _NEEDS_RUNNING:
            status = cts.get(gid) if is_ct else vms.get(gid)
            if status and status != "running" and gid not in started:
                fix = _step_that_starts(gid, plan)
                kind = "container" if is_ct else "VM"
                if verb == "reboot":
                    tail = f" A stopped guest cannot reboot: write `{tool} start {gid}` instead, which also applies the new config."
                elif fix:
                    tail = f" {fix} is the step that starts it, and it has not run yet."
                else:
                    tail = f" Start it first (`{tool} start {gid}`)."
                out.append({"command": cmd, "why": f"{kind} {gid} is {status}, so `{tool} {verb}` fails before it starts." + tail})
        elif verb in _ALREADY:
            status = cts.get(gid) if is_ct else vms.get(gid)
            if status and status == _ALREADY[verb] and not _guarded(cmd, tool, verb, gid):
                kind = "container" if is_ct else "VM"
                out.append({"command": cmd, "why": (
                    f"{kind} {gid} is ALREADY {status}, and `{tool} {verb}` on it exits non-zero — so "
                    f"this step would be recorded as broken for work that is already done. Make it "
                    f"idempotent instead: `{tool} status {gid} | grep -q {status} || {tool} {verb} {gid}`, "
                    f"where the check and the action are each a whole command.")})
    if out:
        logger.warning("preconditions_unmet count=%d first=%r", len(out), out[0]["why"][:120])
    return out


def _guarded(cmd: str, tool: str, verb: str, gid: str) -> bool:
    """§17.1249 — is this action already behind a check on the same guest?

    `pct status 130 | grep -q running || pct start 130` is the idempotent form
    §17.1240's OWN refusal asks for, and §17.1240 refused it: `guests_in` sees a
    `start` on a running container and never notices the `||` in front of it. A
    gate that rejects the remedy it recommends is worse than no gate — live, it
    was the single refusal standing between ADD111 and a correct block.

    Guarded means: the action sits after a `||`, and something before it reads
    the same guest. That is the whole shape — a check that fails hands over to
    the fix, and a check that passes skips it.
    """
    text_value = str(cmd or "")
    action = re.compile(rf"\b{re.escape(tool)}\s+{re.escape(verb)}\s+{re.escape(gid)}\b")
    # §17.1294 — in a FILE, `if qm status N | grep -q running; then … else qm start N; fi` is the same
    # guard in the shell's other spelling: a status read of the SAME guest earlier in the script
    # counts. Live, the engine's own watch_guest_boot template was refused for its `else qm start`.
    if "\n" in text_value:
        m = action.search(text_value)
        if m and re.search(rf"\b{re.escape(tool)}\s+status\s+{re.escape(gid)}\b", text_value[:m.start()]):
            return True
    if "||" not in text_value:
        return False
    parts = text_value.split("||")
    for i, part in enumerate(parts):
        if i == 0 or not action.search(part):
            continue
        before = "||".join(parts[:i])
        if re.search(rf"\b{re.escape(gid)}\b", before):
            return True
    return False


def _step_that_starts(gid: str, plan: Optional[list[dict]]) -> Optional[str]:
    """The pending plan step whose title says it starts this guest — so the
    refusal points at the fix instead of describing the problem twice."""
    for n in plan or []:
        if (n.get("status") or "") not in ("pending", "failed"):
            continue
        title = str(n.get("title") or "")
        if gid in title and re.search(r"\bstart\b", title, re.I):
            return f"{n.get('node_key')} · {title[:60]}"
    return None


async def _read(spec, command: str) -> str:
    """One read-only listing. Never raises; a refusal is not an answer."""
    try:
        from app.modules.assist_local_runner import _plain_output, not_evidence
        from app.modules.mcp_client import call_tool
        res = await call_tool(spec, "run_readonly", {"command": command, "timeout_s": 15})
        out = _plain_output(res)
        return "" if not_evidence(out, bool(res.is_error)) else out
    except Exception as exc:
        logger.warning("precondition_read_failed cmd=%r err=%r", command, exc)
        return ""
