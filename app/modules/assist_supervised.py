"""§17.1185 — the supervised write channel: the engine runs a step's commands
on the operator's machine, one approved block at a time.

The founding rule stands — the engine has no way to touch the operator's
machine — with ONE fenced exception that the operator opens on purpose, per
machine, at install time. The local runner (`scripts/local_runner_mcp.py`)
installed with ``--write-allow "apt-get install" "pct set" …`` exposes
``run_supervised``; this module is the engine's half:

* **the operator approves the block** — ``POST /assist/{sid}/run`` is the
  approval: an authenticated call naming the exact block text; nothing runs
  from a model reply on its own;
* **the engine gates first** — the same catastrophic denylist and allow-list
  match the runner applies (byte-equal helpers, pinned by
  tests/test_local_runner.py), so a refusal is known BEFORE anything is sent
  and shown as a refusal, never filed as output;
* **every command is signed** — an approval ``{id, nonce, exp, sig}`` over the
  exact bytes, keyed by a derivation of the shared token (no second secret to
  distribute), short-lived, single-use on the runner;
* **the record is the transcript** — the output lands as an operator turn
  marked ``[local-runner]`` exactly as a paste would, so the verifier, the
  ledger and the fix loop see it unchanged, and re-enters the turn loop.

Auto mode (§17.1183's hands-on steps) is NOT wired here yet: this is the
channel and its first consumer, the walkthrough.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from typing import Any, Optional

logger = logging.getLogger("scaffold")

MARK = "[local-runner]"
WRITE_TOOL = "run_supervised"
POLICY_TOOL = "write_policy"
APPROVAL_TTL = 600
MAX_BLOCK_COMMANDS = 24

# ---------------------------------------------------------------------------
# Byte-equal with scripts/local_runner_mcp.py (tests/test_local_runner.py).
# ---------------------------------------------------------------------------

_SUDO_RE = re.compile(r"^\s*sudo\s+(?:-[A-Za-z]+\s+)*")

_CATASTROPHIC = (
    (re.compile(r"(?:^|\s)rm\s+(?:-[a-zA-Z]*[rR][a-zA-Z]*\s+)+(?:--no-preserve-root\s+)?(?:/|/\*|/(?:etc|boot|usr|bin|sbin|lib|lib64|var|home|root|dev|proc|sys|opt|srv)(?:/\*)?)(?:\s|$)"), "recursive delete of a system directory"),
    (re.compile(r"(?:^|\s)(?:mkfs(?:\.\w+)?|wipefs|blkdiscard|sgdisk|sfdisk|parted|fdisk)\s"), "disk or filesystem destroyer"),
    (re.compile(r"(?:^|\s)dd\s.*\bof=/dev/(?!null|zero)"), "dd onto a device"),
    (re.compile(r">\s*/dev/(?:sd|nvme|vd|hd|mapper|md)"), "redirect onto a device"),
    (re.compile(r":\(\)\s*\{"), "fork bomb"),
    # §17.1189 — "host power" must not swallow GUEST power. `pct reboot 111` /
    # `qm reboot 106` cycle one container or VM and are ordinary build steps,
    # while `qm start`/`pct start`/`qm stop` were never on this list at all —
    # so the denylist refused the middle of a lifecycle it otherwise allows.
    # Measured on the real plan: 1 of 21 pending hands-on steps lost this way.
    # The power words still bite as a command HEAD (bare, or through sudo).
    (re.compile(r"(?:^|[;&|]\s*)(?:sudo\s+(?:-[A-Za-z]+\s+)*)?(?:shutdown|poweroff|halt|reboot|init\s+[06]|systemctl\s+(?:poweroff|halt|reboot|kexec))(?:\s|$)"), "host power — do that by hand"),
    (re.compile(r"(?:^|\s)(?:iptables|ip6tables|nft)\s+(?:-F|--flush|flush\s+ruleset)"), "firewall flush"),
    (re.compile(r"(?:^|\s)(?:systemctl\s+(?:stop|disable|mask)\s+local-runner-mcp|userdel\s+(?:-r\s+)?scaffold-runner|rm\s.*scaffold-runner)"), "the runner's own service"),
    (re.compile(r"(?:^|\s)(?:chmod|chown)\s+-[a-zA-Z]*R[a-zA-Z]*\s+\S+\s+/(?:\s|$)"), "recursive mode/owner change of /"),
)


def catastrophic(cmd: str) -> str:
    """The reason this command is refused even with an approval, or ''."""
    flat = " ".join((cmd or "").split())
    for rx, why in _CATASTROPHIC:
        if rx.search(flat):
            return why
    return ""


#: §17.1199 — the operator's alternative to enumerating commands. With this on
#: the list, any command that is not on the catastrophic denylist may run —
#: still only inside a block the operator approved, still signed per command,
#: still single-use. The trust boundary becomes the MACHINE and the APPROVAL,
#: which is how Ansible (`become`), Jenkins agents and self-hosted runners all
#: work, instead of a whitelist that can only be discovered by failing.
ANY = "ANY"


def write_allowed(cmd: str, allow: list[str], judge_read=None) -> tuple[bool, str]:
    """``(ok, why)`` for the supervised channel. Every segment is either
    read-only (per the read gate) or, once a leading ``sudo`` is stripped,
    a whole-token prefix match of one ``--write-allow`` entry (an entry ending
    in ``/`` matches any path under it). Redirects and substitutions are
    refused here (write files with ``tee``); an empty allow-list refuses
    everything.

    §17.1199 — the single entry ``ANY`` means the operator has said this
    machine trusts what they approve: everything but the catastrophic denylist
    passes, shape rules included. The shape rules exist to make PREFIX MATCHING
    sound — a `$(…)` can hide a command from a prefix — and there is no prefix
    to match here; the operator reads the literal text and presses the button,
    which is the same thing they were doing by hand.
    """
    judge_read = judge_read or read_only
    prefixes = [p.strip() for p in (allow or []) if p and p.strip()]
    if not prefixes:
        return False, "no --write-allow list on this runner"
    if not (cmd or "").strip():
        return False, "empty"
    if ANY in prefixes:
        return True, ""
    if "<<" in cmd or "$(" in cmd or "`" in cmd:
        return False, "substitution/heredoc"
    masked = mask_quoted(cmd)
    if re.search(r"(?<![<>])>(?!\s*/dev/null|&\d)", masked) or ">>" in masked:
        return False, "redirect — write the file with tee"
    for segment in split_segments(cmd):
        seg = segment.strip()
        if not seg:
            continue
        if judge_read(seg)[0]:
            continue
        bare = _SUDO_RE.sub("", seg, count=1).strip()
        if not any(bare == p or bare.startswith(p + " ") or (p.endswith("/") and bare.startswith(p)) for p in prefixes):
            return False, f"not on the write-allow list: {bare[:60]}"
    return True, ""


def approval_key(token: str) -> bytes:
    """The signing key for approvals: derived from the shared token, so the
    channel needs no second secret. Same derivation in the engine."""
    import hashlib
    return hmac.new((token or "").encode(), b"scaffold-runner supervised writes v1", hashlib.sha256).digest()


def approval_message(approval_id: str, nonce: str, exp: int, command: str) -> bytes:
    return f"{approval_id}\n{nonce}\n{exp}\n{command}".encode()


# ---------------------------------------------------------------------------
# Engine-side adapters onto the state-check gate (the runner has its own copies).
# ---------------------------------------------------------------------------

def read_only(cmd: str) -> tuple[bool, str]:
    from app.modules.assist_state_check import read_only_command
    ok = bool(read_only_command(cmd))
    return ok, "" if ok else "not read-only"


def split_segments(cmd: str) -> list[str]:
    """§17.1151 — split on `||`, `&&`, `;`, `|` OUTSIDE quotes. The naive split
    broke `grep -E 'net0|hostpci'` into an unbalanced quote → "unparsable"
    (live, three refusals in a row on the operator's step)."""
    out: list[str] = []
    buf: list[str] = []
    q: str | None = None
    i = 0
    while i < len(cmd):
        ch = cmd[i]
        if q:
            buf.append(ch)
            if ch == "\\" and q == '"' and i + 1 < len(cmd):
                buf.append(cmd[i + 1]); i += 2; continue
            if ch == q:
                q = None
        elif ch in ("'", '"'):
            q = ch; buf.append(ch)
        elif ch == "\\" and i + 1 < len(cmd):
            buf.append(ch); buf.append(cmd[i + 1]); i += 2; continue
        elif cmd.startswith(("||", "&&"), i):
            out.append("".join(buf)); buf = []; i += 2; continue
        elif ch in (";", "|"):
            out.append("".join(buf)); buf = []
        else:
            buf.append(ch)
        i += 1
    out.append("".join(buf))
    return [o for o in out if o.strip()]


def mask_quoted(cmd: str) -> str:
    """The command with the INSIDE of quoted strings blanked — so a `>` in a
    quoted regex (`grep -o "<Name>[^<]*</Name>"`, live: refused as 'redirect')
    or a `|` in a quoted pattern is not read as shell syntax."""
    out: list[str] = []
    q: str | None = None
    i = 0
    while i < len(cmd):
        ch = cmd[i]
        if q:
            if ch == "\\" and q == '"' and i + 1 < len(cmd):
                out.append("  "); i += 2; continue
            if ch == q:
                q = None; out.append(ch)
            else:
                out.append(" ")
        elif ch in ("'", '"'):
            q = ch; out.append(ch)
        elif ch == "\\" and i + 1 < len(cmd):
            out.append("  "); i += 2; continue
        else:
            out.append(ch)
        i += 1
    return "".join(out)


# ---------------------------------------------------------------------------
# The channel.
# ---------------------------------------------------------------------------

def mint_approval(command: str, token: str, *, ttl: int = APPROVAL_TTL, now: Optional[int] = None) -> dict:
    """A single-use, short-lived approval of these exact bytes."""
    now = int(time.time()) if now is None else now
    aid, nonce, exp = secrets.token_hex(6), secrets.token_hex(12), now + int(ttl)
    sig = hmac.new(approval_key(token), approval_message(aid, nonce, exp, command), hashlib.sha256).hexdigest()
    return {"id": aid, "nonce": nonce, "exp": exp, "sig": sig}


def runner_token(spec) -> str:
    headers = getattr(spec, "headers", None) or {}
    return str(headers.get("X-Runner-Token") or "") if isinstance(headers, dict) else ""


_POLICY_TTL = 300.0
_policy_cache: dict[str, tuple[float, Optional[dict]]] = {}
#: §17.1204 — everything the runner reports about its OWN setup, kept apart from
#: `_policy_cache`. That one holds the write-channel verdict, whose None means
#: "no writes allowed"; this one's None means "the runner could not be asked".
#: Rebuilding an install line needs those two apart — see `runner_setup`.
_setup_cache: dict[str, tuple[float, Optional[dict]]] = {}


def clear_policy_cache(name: Optional[str] = None) -> None:
    if name is None:
        _policy_cache.clear()
        _setup_cache.clear()
    else:
        _policy_cache.pop(name, None)
        _setup_cache.pop(name, None)


def cached_policy(spec) -> tuple[bool, Optional[dict]]:
    """``(known, policy)`` from the cache alone — never calls out. Recipe
    detection reads this (detectors must not touch the operator's machines)."""
    hit = _policy_cache.get(getattr(spec, "name", "") or "")
    if hit and hit[0] > time.monotonic():
        return True, hit[1]
    return False, None


def cached_setup(spec) -> tuple[bool, Optional[dict]]:
    """§17.1204 — ``(known, setup)`` from the cache alone, never calling out.
    The connection page builds install lines and is documented as cheap, but
    those lines REPLACE the service: it needs what the runner holds without
    paying for a round trip to find out."""
    hit = _setup_cache.get(getattr(spec, "name", "") or "")
    if hit and hit[0] > time.monotonic():
        return True, hit[1]
    return False, None


_SECRET_REF_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


async def write_policy(spec, *, use_cache: bool = True) -> Optional[dict]:
    """``{allow, sudo, helper, secrets}`` when the runner exposes the supervised
    channel with a non-empty allow-list; None when it does not (an older
    helper, or one installed without ``--write-allow``). Cached per runner
    for a few minutes: the assist view asks on every mount."""
    from app.modules import mcp_client
    name = getattr(spec, "name", "") or ""
    if use_cache:
        known, pol = cached_policy(spec)
        if known:
            return pol
    try:
        names = {t.get("name") for t in await mcp_client.list_tools(spec, use_cache=use_cache)}
        if WRITE_TOOL not in names or POLICY_TOOL not in names:
            return None
        res = await mcp_client.call_tool(spec, POLICY_TOOL, {})
        raw = res.text or ""
        st = getattr(res, "structured", None)
        if isinstance(st, dict) and isinstance(st.get("result"), str):
            raw = st["result"]
        pol = json.loads(raw)
    except Exception as exc:
        logger.warning("supervised_policy_unavailable runner=%s err=%r", getattr(spec, "name", "?"), exc)
        _policy_cache[name] = (time.monotonic() + 60.0, None)
        _setup_cache[name] = (time.monotonic() + 60.0, None)
        return None
    allow = [str(a) for a in (pol.get("allow") or []) if str(a).strip()]
    # §17.1191 — the NAMES the runner can resolve (`$NAME` in a command), never
    # their values. An older helper does not report the field; [] then means
    # "this runner holds no secrets", which is the safe reading.
    secrets = sorted({str(n) for n in (pol.get("secrets") or []) if _SECRET_REF_NAME.match(str(n))})
    # §17.1204 — the whole of what `--install` would REPLACE, recorded before the
    # write-channel view narrows it. The two fields below are exactly the grants
    # the repair line used to drop: they were parsed nowhere, so helper v17
    # reporting them bought nothing. `None` (not `[]`) for a helper that does not
    # report the field at all — "it did not say" is not "it has none".
    _setup_cache[name] = (time.monotonic() + _POLICY_TTL, {
        "allow": allow, "sudo": bool(pol.get("sudo")), "helper": str(pol.get("helper") or ""),
        "secrets": secrets,
        "sudo_allow": ([str(a) for a in pol["sudo_allow"] if str(a).strip()]
                       if isinstance(pol.get("sudo_allow"), list) else None),
        "secrets_file": (str(pol["secrets_file"]) if pol.get("secrets_file") else None),
    })
    out = ({"allow": allow, "sudo": bool(pol.get("sudo")), "helper": str(pol.get("helper") or ""),
            "secrets": secrets} if allow else None)
    _policy_cache[name] = (time.monotonic() + _POLICY_TTL, out)
    return out


async def runner_setup(spec, *, use_cache: bool = True) -> Optional[dict]:
    """§17.1204 — every grant ``--install`` would REPLACE, as the runner reports
    it: ``{allow, sudo, helper, secrets, sudo_allow, secrets_file}``. ``None``
    when the runner could not be asked at all; ``sudo_allow``/``secrets_file``
    are ``None`` when the helper is too old to report them.

    Deliberately separate from `write_policy`. That function's ``None`` is a
    verdict about one thing — "no supervised write channel" — and it answers
    ``None`` both for a runner that holds nothing and for one that never
    replied. Rebuilding an install line needs those apart: read "holds nothing"
    as "unknown" and the fallback grants prefixes the operator never approved.
    """
    name = getattr(spec, "name", "") or ""
    if use_cache:
        hit = _setup_cache.get(name)
        if hit and hit[0] > time.monotonic():
            return hit[1]
    await write_policy(spec, use_cache=False)   # one call out; it fills both caches
    hit = _setup_cache.get(name)
    return hit[1] if hit else None


_SENTINEL_RE = re.compile(r'^\s*echo\s+"== S:[^"]+ =="\s*$')


def block_commands(block: str) -> list[str]:
    """The commands of a pasted/approved block, in order: fence markers,
    blank lines and comments dropped, a leading ``$ `` prompt stripped. The
    step sentinel echo is kept (it is read-only and proves the block ran to
    its end, §17.1159)."""
    out: list[str] = []
    for raw in (block or "").splitlines():
        ln = raw.strip()
        if not ln or ln.startswith("#") or ln.startswith("```"):
            continue
        ln = re.sub(r"^\$\s+", "", ln)
        out.append(ln)
    return out[:MAX_BLOCK_COMMANDS]


def gate_block(commands: list[str], allow: list[str]) -> tuple[list[str], list[dict]]:
    """``(runnable, refused)`` — the engine's own verdict before anything is
    sent; ``refused`` carries ``{command, why}`` for the operator."""
    runnable, refused = [], []
    for c in commands:
        why = catastrophic(c)
        if not why:
            ok, why = write_allowed(c, allow)
            why = "" if ok else why
        if why:
            refused.append({"command": c, "why": why})
        else:
            runnable.append(c)
    return runnable, refused


def _exit_of(text_out: str) -> tuple[Optional[int], str]:
    m = re.match(r"\[exit (\d+|timeout)\]\n?", text_out or "")
    if not m:
        return None, text_out or ""
    code = None if m.group(1) == "timeout" else int(m.group(1))
    return code, (text_out or "")[m.end():]


async def run_block(spec, commands: list[str], *, on_progress=None,
                    env: dict[str, str] | None = None) -> list[dict]:
    """Run the approved commands in order through the runner; stop at the
    first failure (a write block is a sequence — what follows assumed the
    previous step worked). ``[{command, output, exit, ok, approval_id}]``.

    §17.1193 — ``env`` carries the values for the ``$NAME`` references in these
    commands, OUT OF BAND: the command string (and the approval signed over it)
    keeps ``$NAME``, so nothing here puts a secret in the bytes, the log line,
    or the target's process table. Only the names a command actually references
    are sent, and the runner refuses any it did not ask for.
    """
    from app.modules import mcp_client
    token = runner_token(spec)
    done: list[dict] = []
    for i, cmd in enumerate(commands, 1):
        ap = mint_approval(cmd, token)
        payload = {"command": cmd, "approval": ap, "timeout_s": 180}
        needed = {n: v for n, v in (env or {}).items() if f"${n}" in cmd or "${" + n + "}" in cmd}
        if needed:
            payload["env"] = needed
        try:
            res = await mcp_client.call_tool(spec, WRITE_TOOL, payload)
            raw = res.text or ""
            st = getattr(res, "structured", None)
            if isinstance(st, dict) and isinstance(st.get("result"), str):
                raw = st["result"]
            unreachable = False
        except Exception as exc:
            # §17.1201 — a transport error is NOT a command result. "SSE stream
            # ended without a response" means the call did not come back; it
            # says nothing about whether the command ran. Reporting that as
            # `exited None` invites a retry of a write whose outcome nobody
            # knows. Live on ADD65: `sleep 10 && qm agent 106 ping`.
            raw = f"(runner error: {exc})"
            unreachable = True
        code, out = _exit_of(raw)
        refused = raw.startswith("(refused by the local runner")
        # §17.1201 — a READ-ONLY command's non-zero exit is an ANSWER, not a
        # failure of the block. `qm config 106 | grep scsi0` exits 1 when the
        # disk is gone — which is the check PASSING — and that aborted the run
        # on ADD81 after the write it was verifying had already succeeded. The
        # writes are the sequence; a read in the middle is information.
        informational = (not refused and not unreachable and code not in (0, None)
                         and read_only(cmd)[0])
        ok = (code == 0) and not refused
        done.append({"command": cmd, "output": out.rstrip(), "exit": code, "ok": ok, "approval_id": ap["id"],
                     "refused": refused, "unreachable": unreachable, "informational": informational})
        logger.warning("supervised_run runner=%s approval=%s exit=%s refused=%s informational=%s cmd=%r",
                       getattr(spec, "name", "?"), ap["id"], code, refused, informational, cmd[:120])
        if on_progress is not None:
            try:
                await on_progress(i, len(commands))
            except Exception:
                pass
        if not ok and not informational:
            break
    return done


def record_text(spec_name: str, executed: list[dict], *, approved_by: str = "you") -> str:
    """The operator turn the run becomes — a paste's shape (``$ cmd`` then
    output, ``rc=N`` for a failure), marked so the transcript shows the engine
    ran it and that the operator approved it."""
    n = len(executed)
    head = (f"{MARK} ran this step's block through your local runner ({spec_name}) with {approved_by}r approval — "
            f"{n} command{'s' if n != 1 else ''} ran ON THE TARGET MACHINE itself, as the runner's account"
            + ("; it stopped at the first failure" if executed and not executed[-1]["ok"] else "") + ":")
    parts = [head]
    for e in executed:
        body = e["output"] if e["output"] else "(no output)"
        rc = f"\nrc={e['exit']}" if e["exit"] not in (None, 0) else ""
        parts.append(f"$ {e['command']}\n{body}{rc}")
    return "\n".join(parts)


def render_note(spec_name: str, executed: list[dict], refused: list[dict]) -> str:
    """The bubble the operator sees while it happens."""
    body = "\n".join(f"$ {e['command']}\n{e['output'] or '(no output)'}" + (f"\nrc={e['exit']}" if e["exit"] not in (None, 0) else "")
                     for e in executed)
    body = body if len(body) <= 6000 else body[:6000] + "\n… (truncated here; the full output is in the transcript)"
    tail = ""
    if executed and not executed[-1]["ok"]:
        last = executed[-1]
        tail = ("\n\n⚠️ Stopped at `" + last["command"][:80] + "` — "
                + ("the runner refused it: " + last["output"][:200] if last.get("refused") else f"it exited {last['exit']}")
                + ". Nothing after it ran.")
    if refused:
        tail += "\n\nNot sent (refused by the engine's gate before anything ran): " + "; ".join(
            f"`{r['command'][:60]}` — {r['why']}" for r in refused)
    return (f"⚡ You approved this block, so I ran it through {spec_name} ({len(executed)} command"
            f"{'s' if len(executed) != 1 else ''}):\n\n```\n{body}\n```" + tail)
