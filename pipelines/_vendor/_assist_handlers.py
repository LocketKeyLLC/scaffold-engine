"""§17.296 — /assist command handlers extracted from scaffold_router.py.

§17.280-🟢-1 closeout. scaffold_router.py is the OWUI pipeline entry
point; its size (3941 LOC pre-§17.296) is operationally fixed by OWUI's
auto-discovery model (every `.py` under `/pipelines/` is a candidate
pipeline). The vendor escape hatch — files under `/pipelines/_vendor/`
— is invisible to discovery (underscore-prefixed dir) and safe for
shared code. §17.190 and §17.195 already use this pattern (vendored
`_sse_events.py` and `_next_actions.py`).

§17.296 lifts the ~600 LOC /assist command surface from
scaffold_router.py into this module. Pipeline methods become thin
delegates: each former `_assist_*` method is now a one-liner that
calls into the corresponding function here. Behavior preserved
byte-for-byte; the only visible change to operators is the file
boundary.

Module contract:

- Functions take ``pipe`` (the live Pipeline instance) as their first
  arg. Through ``pipe`` they reach config (``pipe.valves``), per-chat
  helpers (``pipe._chat_id_from_body``, ``pipe._assist_remember``),
  and the streaming SSE plumbing (``pipe._stream_sse_to_queue``).

- The scaffold_router module-level ``_HTTP_SESSION`` and ``_SSE``
  vendor reference are accessed via ``sys.modules["scaffold_router"]``
  so tests that patch them on scaffold_router (the canonical patch
  target) reach this module's call sites without per-test surface
  changes.
"""
from __future__ import annotations

import base64
import json
import queue as _q
import re
import sys
import threading as _th
import time
from typing import Generator

import requests


# §17.505 — recover scaffold_router's module namespace from the live Pipeline
# instance instead of `sys.modules["scaffold_router"]`.
#
# OWUI's pipeline loader (`load_module_from_path` → importlib `exec_module`)
# does NOT register loaded pipelines in `sys.modules`, so the old
# `sys.modules["scaffold_router"]` lookup raised `KeyError: 'scaffold_router'`
# in production — crashing the assist generator mid-stream (OWUI surfaced it as
# `TransferEncodingError: Not enough data to satisfy transfer length header`).
# It only ever worked under the unittest harness, which DOES register the
# module under that name — so every assist test passed while live `/assist`
# always crashed (hence: zero assist sessions ever created).
#
# A Pipeline method's `__globals__` IS scaffold_router's live module `__dict__`
# in both environments, and reads through it honor any test monkeypatch on the
# module. Every caller already has the `pipe` instance in scope.
def _sr_ns(pipe):
    return type(pipe).pipe.__globals__


def _ss(pipe):
    """The shared ``requests.Session`` from scaffold_router."""
    return _sr_ns(pipe)["_HTTP_SESSION"]


def _sse_events_const(pipe):
    """The ``_SSE`` vendor module reference (event-name constants)."""
    return _sr_ns(pipe)["_SSE"]


# ---------------------------------------------------------------------------
# Chatmap helpers — per-chat session memory in /assist/_chatmap/{chat_id}.
# ---------------------------------------------------------------------------


def assist_remember(
    pipe, chat_id: str | None, *,
    session_id: str, last_node_key: str | None = None,
) -> None:
    """Best-effort: stash chat→session in the orchestrator's chatmap.
    Failures are logged and swallowed — explicit-arg flow still works."""
    if not chat_id or not pipe.valves.assist_session_memory_enabled:
        return
    try:
        _ss(pipe).put(
            f"{pipe.valves.orchestrator_url}/assist/_chatmap/{chat_id}",
            json={"session_id": session_id, "last_node_key": last_node_key},
            headers=pipe._auth_headers(),
            timeout=pipe.valves.request_timeout,
        )
    except requests.exceptions.RequestException as e:
        pipe.logger.debug("assist_remember failed: %s", e)


def assist_recall(pipe, chat_id: str | None) -> dict | None:
    if not chat_id or not pipe.valves.assist_session_memory_enabled:
        return None
    try:
        r = _ss(pipe).get(
            f"{pipe.valves.orchestrator_url}/assist/_chatmap/{chat_id}",
            headers=pipe._auth_headers(),
            timeout=pipe.valves.request_timeout,
        )
    except requests.exceptions.RequestException as e:
        pipe.logger.debug("assist_recall failed: %s", e)
        return None
    if r.status_code != 200:
        return None
    return r.json()


def assist_forget(pipe, chat_id: str | None) -> None:
    if not chat_id or not pipe.valves.assist_session_memory_enabled:
        return
    try:
        _ss(pipe).delete(
            f"{pipe.valves.orchestrator_url}/assist/_chatmap/{chat_id}",
            headers=pipe._auth_headers(),
            timeout=pipe.valves.request_timeout,
        )
    except requests.exceptions.RequestException as e:
        pipe.logger.debug("assist_forget failed: %s", e)


# ---------------------------------------------------------------------------
# Parsing + formatting helpers.
# ---------------------------------------------------------------------------


def resolve_session_id(
    pipe, args: list, chat_id: str | None,
) -> tuple[str | None, list]:
    """If args[0] is UUID-shaped, pop and return it as session_id.
    Otherwise look up via chat_id. Returns (session_id, remaining_args)."""
    if args and pipe._UUID_RE.match(args[0]):
        return args[0], args[1:]
    recalled = assist_recall(pipe, chat_id)
    return ((recalled or {}).get("session_id"), args)


def no_session_msg(sub: str) -> str:
    return (
        f"❌ No active assist session in this chat. "
        f"Either start one with `/assist <job_id>` or pass an explicit "
        f"session id, e.g. `/assist {sub} <session_id> ...`."
    )


def extract_fenced(msg: str) -> tuple[str, str]:
    """Split a message into (head, fenced_body). If no triple-backtick
    fence is present, fenced_body is empty and head is msg."""
    if "```" not in msg:
        return msg, ""
    head, _, rest = msg.partition("```")
    # rest may begin with a language tag on the first line; strip the
    # first line if it has no whitespace and is short (looks like 'bash').
    first_nl = rest.find("\n")
    if 0 < first_nl < 30 and " " not in rest[:first_nl] and "`" not in rest[:first_nl]:
        rest = rest[first_nl + 1:]
    body, _, _ = rest.partition("```")
    return head.strip(), body.strip()


def render_step(step: dict) -> str:
    """Format the INTRO for a /assist/next step — shown before the walkthrough.

    §17.626 — plain-language, jargon-light. The operator sees WHAT this step is;
    the actual how-to walkthrough streams right after (assist_next), and
    ``render_step_footer`` then tells them how to report back in plain words.
    Engine internals (node key, tool, dependency keys, the raw LLM prompt) are
    demoted to a muted subtitle + collapsed reference blocks so the useful
    content leads.
    """
    if step.get("status") in ("completed", "abandoned", "cancelled"):
        return (
            f"✅ **This job is {step['status']}.** "
            f"Say **\"show me the result\"** (or `/assist done`) to see the "
            f"compiled output."
        )
    if not step.get("node_key"):
        counts = step.get("step_counts", {})
        counts_str = ", ".join(f"{k}={v}" for k, v in counts.items()) or "n/a"
        # §17.512 — a presented-but-unsubmitted step is now re-surfaced by the
        # orchestrator, so reaching here means nothing is ready: either every
        # step is submitted/skipped (session about to finalize) or the rest are
        # waiting on dependencies.
        return (
            f"⏳ **Nothing to do right now.**\n\n"
            f"Either every step is finished, or the remaining ones are waiting on "
            f"earlier steps to complete. (Progress: {counts_str}.)\n\n"
            f"Say **\"show me the result\"** to wrap up, or check `/jobs` for status."
        )
    # §17.512 — when the orchestrator re-surfaces an already-presented step
    # (nothing new claimable), tell the user this is their current step, not a
    # new one — so this reads as recovery, not a skip.
    re_shown = (
        "↩️ _Picking up your current step — finish it, or say **\"skip\"** to "
        "move on._\n\n" if step.get("re_presented") else ""
    )
    title = step.get("title") or "This step"
    # Muted subtitle: the engine internals a power user might want, de-emphasized
    # so they don't lead. Node key is shown because the slash-command aliases
    # (`/assist submit <node_key>`) still accept it.
    sub_bits = [f"step `{step['node_key']}`"]
    tool = step.get("tool")
    if tool and tool.upper() != "LLM":
        sub_bits.append(f"tool `{tool}`")
    deps = step.get("depends_on") or []
    if deps:
        sub_bits.append("comes after " + ", ".join(f"`{d}`" for d in deps))
    subtitle = " · ".join(sub_bits)

    upstream = step.get("upstream_outputs") or {}
    upstream_block = ""
    if upstream:
        upstream_block = "<details>\n<summary>Results from earlier steps</summary>\n\n"
        for nk, txt in upstream.items():
            preview = txt if len(txt) <= 800 else txt[:800] + f"\n… [{len(txt) - 800} more chars]"
            upstream_block += f"_{nk}:_\n```\n{preview}\n```\n\n"
        upstream_block += "</details>\n\n"
    # §17.486 — the human-facing walkthrough (streamed separately) is the primary
    # content; the raw LLM task prompt is demoted to a collapsed reference.
    raw_block = (
        f"<details>\n<summary>Show the exact task (for reference)</summary>\n\n"
        f"```\n{step.get('base_prompt', '')}\n```\n\n</details>\n\n"
    )
    # §17.675 — a forward-looking position so a first-timer always knows where
    # they are and how much remains (was: title only, no sense of progress).
    # step_counts is attached by the /next endpoint; absent on an older
    # orchestrator → we simply omit the line rather than guess.
    counts = step.get("step_counts") or {}
    total = sum(v for v in counts.values() if isinstance(v, int))
    done = sum(counts.get(s, 0) for s in _DONE_STEP_STATUSES)
    if total:
        remaining = total - done - 1
        tail = f"{remaining} to go" if remaining > 0 else "last step"
        progress = f"**Step {done + 1} of {total}** · {tail}\n\n"
    else:
        progress = ""
    return (
        f"{re_shown}"
        f"{progress}"
        f"### 📋 {title}\n\n"
        f"_{subtitle}_\n\n"
        f"{upstream_block}"
        f"{raw_block}"
    )


# §17.626 — full "how to report back" footer. Teaches the whole plain-language
# control surface; shown once, on the FIRST walkthrough of a session.
_FOOTER_FULL = (
    "\n\n---\n\n"
    "**When you're done, just tell me what happened** — paste any command "
    "output, or say what you did or decided. I'll check it and move you to "
    "the next step.\n\n"
    "- Nothing to paste? A short _\"done\"_ works.  ·  Pass on it? _\"skip\"_.\n"
    "- Hit a problem? Tell me the error (_\"it failed with …\"_) and I'll fix it.\n"
    "- Want me to just do it? _\"you handle this one\"_ (or _\"you do the rest\"_).\n"
    "- Need a fact? Ask (_\"is ZFS safe on non-ECC?\"_) — I'll research it.\n"
    "- Lost? _\"where am I\"_ or _\"show me the plan\"_.\n\n"
    "_Prefer commands? `/assist submit`, `/assist skip`, `/assist fix`, "
    "`/assist handoff`, `/assist research` still work._\n"
)

# §17.647 — trimmed footer for LATER steps. Once the operator has completed a
# step they know the drill; the full 89-word block on every step is noise that
# inflates each response. A one-liner keeps the essentials in reach.
_FOOTER_TRIMMED = (
    "\n\n---\n_Done? Tell me what happened — or _\"skip\"_, or paste an error "
    "to fix. Say _\"where am I\"_ / _\"show me the plan\"_ anytime._\n"
)

# assist_steps statuses that mean a step is finished (so this is not the
# operator's first walkthrough of the session).
_DONE_STEP_STATUSES = ("committed", "skipped", "handed_off", "done")


def render_step_footer(step: dict) -> str:
    """§17.626/§17.647 — the natural-language 'what to do when you're done' block,
    shown AFTER the walkthrough. Full on the first walkthrough (orient the user);
    trimmed once any step has been completed (they already know how to report
    back). First-vs-later is read from the session `step_counts` the /next
    endpoint attaches; absent (older orchestrator) → full, so we never silently
    drop the guidance."""
    counts = step.get("step_counts") or {}
    completed = sum(counts.get(s, 0) for s in _DONE_STEP_STATUSES)
    return _FOOTER_TRIMMED if completed > 0 else _FOOTER_FULL


def render_destructive_banner(meta: dict | None) -> str:
    """§17.492 — a prominent 'review before running' block listing the
    destructive commands the safety gate flagged. Empty string when none."""
    items = (meta or {}).get("destructive") or []
    if not items:
        return ""
    lines = [
        "> ⚠️ **Destructive commands detected — review before you run anything:**",
        ">",
    ]
    for it in items[:8]:
        lines.append(f"> - `{it.get('line', '')}` — {it.get('why', '')}")
    if len(items) > 8:
        lines.append(f"> - …and {len(items) - 8} more")
    lines.append(">")
    lines.append(
        "> Double-check paths and `<PLACEHOLDER>` values, and back up anything "
        "important first."
    )
    return "\n".join(lines) + "\n\n"


def render_guidance(d: dict) -> str:
    """§17.486 — format a /assist/{sid}/guide response as the human walkthrough.

    On a failed/empty generation, degrade gracefully to a pointer at the raw
    task prompt rather than a blank section."""
    node_key = d.get("node_key", "?")
    if d.get("status") != "ready" or not d.get("guidance"):
        return (
            f"⚠️ Couldn't generate a walkthrough for `{node_key}` right now. "
            f"Work from the raw task prompt above, or retry with `/assist guide`."
        )
    meta = d.get("guidance_meta") or {}
    sources = meta.get("research_sources") or []
    out = render_destructive_banner(meta) + f"## 🧭 How to do this step\n\n{d['guidance']}\n"
    if sources:
        cites = ", ".join(
            f"`{s.get('kind')}`: {s.get('query')}" for s in sources
        )
        out += f"\n_Confirmed via research — {cites}._\n"
    if d.get("cached"):
        out += "\n_(cached walkthrough — run `/assist guide` to regenerate)_\n"
    return out


def render_fix(d: dict) -> str:
    """§17.487 — format a /assist/{sid}/fix response (diagnosis + corrected steps)."""
    node_key = d.get("node_key", "?")
    if d.get("status") != "ready" or not d.get("fix"):
        return (
            f"⚠️ Couldn't generate a fix for `{node_key}` right now. "
            f"Try `/assist research <the error>` for raw sources, or rephrase."
        )
    out = render_destructive_banner(d.get("guidance_meta")) + f"## 🔧 Troubleshooting `{node_key}`\n\n{d['fix']}\n"
    meta = d.get("guidance_meta") or {}
    sources = meta.get("research_sources") or []
    if sources:
        cites = ", ".join(f"`{s.get('kind')}`: {s.get('query')}" for s in sources)
        out += f"\n_Confirmed via research — {cites}._\n"
    return out


def render_environment(env: dict | None) -> str:
    """§17.487 — show the session's operator environment (+ §17.499 verbosity)."""
    env = env or {}
    profile = (env.get("profile") or "").strip()
    subs = env.get("substitutions") or {}
    verbosity = env.get("verbosity") or "normal"
    if not profile and not subs and verbosity == "normal":
        return (
            "_No environment set._ Set one so walkthroughs use concrete commands:\n"
            "`/assist env Ubuntu 24.04, apt, bash` or `/assist env HOST_IP=10.0.0.5`.\n"
            "_Verbosity:_ `normal` (change with `/assist verbose terse|detailed`)."
        )
    out = "**Operator environment**\n\n"
    if profile:
        out += f"- Profile: {profile}\n"
    for k, v in subs.items():
        out += f"- `{k}` = `{v}`\n"
    out += f"- Verbosity: `{verbosity}`\n"
    return out


def render_research(d: dict) -> str:
    """§17.486 — format a /assist/{sid}/research response."""
    q = d.get("question", "?")
    sources = d.get("sources") or []
    if not sources:
        return f"🔍 No results found for: _{q}_. Try rephrasing the question."
    out = f"### 🔍 Research: {q}\n\n"
    answer = d.get("answer")
    if answer:
        out += f"{answer}\n\n"
    out += "**Sources:**\n\n"
    for i, s in enumerate(sources, 1):
        body = s.get("text", "")
        preview = body if len(body) <= 600 else body[:600] + f"\n… [{len(body) - 600} more chars]"
        # §17.500 — deep web sources carry the page URL; show it.
        label = f"{s.get('kind')} — {s['url']}" if s.get("url") else s.get("kind")
        out += f"_[{i}] ({label})_\n```\n{preview}\n```\n\n"
    return out


# ---------------------------------------------------------------------------
# Top-level dispatch.
# ---------------------------------------------------------------------------


def handle_assist(
    pipe, msg: str, *, body: dict | None = None,
) -> Generator[str, None, None]:
    """Dispatch /assist subcommands. Per-chat session memory in
    `/assist/_chatmap/{chat_id}` lets users omit `<session_id>` after
    a `/assist <job_id>` start. An explicit UUID-shaped first arg
    always wins over the remembered session — handy when a user is
    juggling two sessions across two chats."""
    chat_id = pipe._chat_id_from_body(body)
    head, fenced = extract_fenced(msg)
    parts = head.split(None, 4)
    cmd = parts[0] if parts else "/assist"

    # /assist help
    if cmd == "/assist/help" or (cmd == "/assist" and len(parts) > 1 and parts[1] == "help"):
        yield pipe._ASSIST_HELP; return

    # /assist <job_id> — start
    if cmd == "/assist":
        if len(parts) < 2:
            yield pipe._ASSIST_HELP; return
        arg1 = parts[1]
        # /assist <subcommand> ... — route to subcommand handler
        if arg1 in ("next", "submit", "skip", "handoff", "pause", "resume",
                    "done", "friction", "guide", "research", "env", "fix",
                    "verbose", "status", "checklist"):
            yield from dispatch_assist_sub(
                pipe, arg1, parts[2:], fenced,
                chat_id=chat_id, raw_head=head,
            ); return
        # Otherwise treat arg1 as job_id
        job_id = arg1
        yield from assist_start(pipe, job_id, chat_id=chat_id); return

    # Slash-form subcommands: /assist/next, /assist/submit, etc.
    if cmd.startswith("/assist/"):
        sub = cmd.split("/", 2)[2]  # "next" / "submit" / ...
        yield from dispatch_assist_sub(
            pipe, sub, parts[1:], fenced,
            chat_id=chat_id, raw_head=head,
        ); return

    yield pipe._ASSIST_HELP


def assist_status(pipe, session_id: str) -> Generator[str, None, None]:
    """§17.520 — render an assist session roll-up (status, job, current step,
    per-status step counts). Backs `/assist status`, which the mirror-divergence
    banner already pointed at but was never implemented (dangling reference).
    GET /assist/{session_id} returns the session + step_counts."""
    try:
        r = _ss(pipe).get(
            f"{pipe.valves.orchestrator_url}/assist/{session_id}",
            headers=pipe._auth_headers(),
            timeout=pipe.valves.request_timeout,
        )
    except requests.exceptions.RequestException as e:
        yield f"❌ Connection error: {e}"; return
    if r.status_code == 404:
        yield f"❌ No assist session `{session_id}`."; return
    if r.status_code >= 400:
        yield f"❌ Could not fetch session: HTTP {r.status_code} {r.text[:200]}"
        return
    try:
        d = r.json()
    except ValueError:
        yield f"❌ Session status: non-JSON reply; raw: {r.text[:200]}"; return
    counts = d.get("step_counts") or {}
    counts_str = ", ".join(f"{k}={v}" for k, v in counts.items()) or "n/a"
    yield (
        f"🔎 **Assist session `{session_id}`**\n\n"
        f"- Status: `{d.get('status', '?')}`\n"
        f"- Job: `{d.get('job_id', '?')}`\n"
        f"- Current step: `{d.get('current_node_key') or '(none)'}`\n"
        f"- Steps: {counts_str}\n"
    )
    # §17.654 — surface captured notes & additions so the operator sees
    # everything the engine is carrying forward, at a glance.
    yield _render_notes_block(d.get("notes"))


def _render_notes_block(notes) -> str:
    """A '📌 Notes & additions' block for the status/results roll-up. Tolerates a
    JSONB list or a JSON string; returns '' when there is nothing to show."""
    if isinstance(notes, str):
        try:
            notes = json.loads(notes)
        except (ValueError, TypeError):
            notes = []
    if not isinstance(notes, list) or not notes:
        return ""
    lines = ["\n📌 **Notes & additions** (carried into every remaining step):\n"]
    for n in notes:
        if not isinstance(n, dict):
            continue
        txt = (n.get("text") or "").strip()
        if not txt:
            continue
        kind = (n.get("kind") or "note").strip()
        nk = n.get("node_key")
        where = f" _(from `{nk}`)_" if nk else ""
        lines.append(f"- **{kind}:** {txt}{where}")
    return "\n".join(lines) + "\n" if len(lines) > 1 else ""


def render_checklist(d: dict) -> str:
    """§17.707 — render the operator-input checklist (decisions to make + info to
    supply), ticking off what's already done and listing values learned so far."""
    items = d.get("items") or []
    _label = {"decision": "Decide", "gather": "Provide"}
    out: list[str] = []
    if not items:
        out.append("📋 **What I need from you:** no decisions or inputs to collect "
                   "from you for this plan — just work the steps (paste each "
                   "command's output and I'll record it).")
    else:
        open_lines, done_lines = [], []
        for it in items:
            mark = "☑" if it.get("done") else "☐"
            lab = _label.get(it.get("kind"), "Input")
            line = f"{mark} **{lab}:** {it.get('title', '?')} _(`{it.get('node_key', '?')}`)_"
            (done_lines if it.get("done") else open_lines).append(line)
        out.append(f"📋 **What I need from you** ({d.get('open_count', 0)} open / "
                   f"{d.get('total', 0)} total)\n")
        if open_lines:
            out.append("\n".join(open_lines))
        if done_lines:
            out.append("\n_Already handled:_\n" + "\n".join(done_lines))
    provided = d.get("provided") or {}
    if provided:
        pairs = ", ".join(f"`{k}`=`{v}`" for k, v in provided.items())
        out.append(f"\n_Provided so far:_ {pairs}")
    # §17.709 — durable facts learned about the operator's real system.
    facts = d.get("facts") or []
    if facts:
        shown = facts[:8]
        bullets = "\n".join(f"- {f}" for f in shown)
        more = f"\n_(+{len(facts) - len(shown)} more)_" if len(facts) > len(shown) else ""
        out.append(f"\n🧠 **Known about your system:**\n{bullets}{more}")
    out.append("\n_This list fills in as you go — paste output or state a "
               "decision and I'll tick it off._")
    return "\n".join(out)


def assist_checklist_cmd(pipe, session_id: str) -> Generator[str, None, None]:
    """§17.707 — GET /assist/{sid}/checklist and render it."""
    try:
        r = _ss(pipe).get(
            f"{pipe.valves.orchestrator_url}/assist/{session_id}/checklist",
            headers=pipe._auth_headers(),
            timeout=pipe.valves.request_timeout,
        )
    except requests.exceptions.RequestException as e:
        yield f"❌ Connection error: {e}"; return
    if r.status_code == 404:
        yield f"❌ No assist session `{session_id}`."; return
    if r.status_code >= 400:
        yield f"❌ Could not fetch checklist: HTTP {r.status_code} {r.text[:200]}"; return
    try:
        d = r.json()
    except ValueError:
        yield f"❌ Checklist: non-JSON reply; raw: {r.text[:200]}"; return
    yield render_checklist(d)


def dispatch_assist_sub(
    pipe, sub: str, args: list, fenced: str, *, chat_id: str | None,
    raw_head: str | None = None,
) -> Generator[str, None, None]:
    # Resolve session_id: explicit UUID arg > recalled-from-chat.
    sid, rest = resolve_session_id(pipe, args, chat_id)
    if sub == "next":
        if not sid:
            yield no_session_msg("next"); return
        yield from assist_next(pipe, sid, chat_id=chat_id); return
    if sub == "submit":
        if not sid:
            yield no_session_msg("submit"); return
        # Node key: explicit arg > remembered last_node_key from chatmap.
        node_key = rest[0] if rest else None
        if not node_key:
            recalled = assist_recall(pipe, chat_id)
            node_key = (recalled or {}).get("last_node_key")
        if not node_key:
            yield (
                "❌ No node specified and no recent step in chat memory. "
                "Run `/assist next` first, or pass `<node_key>` explicitly."
            ); return
        # §17.308 — multi-line evidence ergonomics. When no fence is
        # present AND the operator pasted multi-line content after the
        # command line, capture lines 2+ as evidence (whitespace and
        # newlines preserved). Guard with a node_key-presence check on
        # the first line so the "node_key was on a continuation line"
        # edge case keeps the pre-§17.308 whitespace-join behavior.
        multi_line_evidence = ""
        if not fenced and raw_head and "\n" in raw_head:
            first_line, _, after = raw_head.partition("\n")
            if node_key in first_line and after.strip():
                multi_line_evidence = after
        evidence = (
            fenced
            or multi_line_evidence
            or (" ".join(rest[1:]) if len(rest) > 1 else "")
        )
        yield from assist_submit(pipe, sid, node_key, evidence, chat_id=chat_id); return
    if sub == "skip":
        if not sid:
            yield no_session_msg("skip"); return
        node_key = rest[0] if rest else None
        if not node_key:
            recalled = assist_recall(pipe, chat_id)
            node_key = (recalled or {}).get("last_node_key")
        if not node_key:
            yield (
                "❌ No node specified and no recent step in chat memory. "
                "Run `/assist next` first, or pass `<node_key>` explicitly."
            ); return
        yield from assist_skip(pipe, sid, node_key, chat_id=chat_id); return
    if sub == "handoff":
        if not sid or not rest:
            yield no_session_msg("handoff") if not sid else \
                "Usage: `/assist handoff [<session_id>] <node_key> [single|all]`"; return
        mode = (rest[1] if len(rest) > 1 else "single").lower()
        mode = "all_remaining" if mode in ("all", "all_remaining") else "single"
        yield from assist_handoff(pipe, sid, rest[0], mode); return
    if sub == "pause":
        if not sid:
            yield no_session_msg("pause"); return
        yield from assist_simple_post(pipe, sid, "pause"); return
    if sub == "resume":
        if not sid:
            yield no_session_msg("resume"); return
        yield from assist_simple_post(pipe, sid, "resume"); return
    if sub == "status":
        if not sid:
            yield no_session_msg("status"); return
        yield from assist_status(pipe, sid); return
    if sub == "checklist":  # §17.707
        if not sid:
            yield no_session_msg("checklist"); return
        yield from assist_checklist_cmd(pipe, sid); return
    if sub == "done":
        if not sid:
            yield no_session_msg("done"); return
        yield from assist_done(pipe, sid, chat_id=chat_id); return
    if sub == "friction":
        if not sid:
            yield no_session_msg("friction"); return
        # friction needs node_key and note; remembered node fills the
        # node slot if user types only `/assist friction "the note"`.
        if not rest:
            yield "Usage: `/assist friction [<session_id>] [<node_key>] <note>`"; return
        if len(rest) >= 2:
            node_key, note = rest[0], " ".join(rest[1:])
        else:
            recalled = assist_recall(pipe, chat_id)
            node_key = (recalled or {}).get("last_node_key")
            note = rest[0]
        if not node_key:
            yield (
                "❌ No node in chat memory. Pass `<node_key>` explicitly: "
                "`/assist friction <node_key> <note>`."
            ); return
        yield from assist_friction(pipe, sid, node_key, note); return
    if sub == "guide":
        if not sid:
            yield no_session_msg("guide"); return
        # The node defaults to the session's current step (resolved
        # server-side); chat-memory's last_node_key is a hint. Everything
        # after the (optional) session id — fence or remaining words — is
        # the refine hint, e.g. `/assist guide redo for macOS`.
        refine = fenced or (" ".join(rest).strip() if rest else None)
        recalled = assist_recall(pipe, chat_id)
        node_key = (recalled or {}).get("last_node_key")
        _cmd = (assist_guide_stream_cmd
                if getattr(pipe.valves, "assist_stream", True) else assist_guide_cmd)
        yield from _cmd(
            pipe, sid, node_key=node_key, refine=refine,
            research=pipe.valves.assist_guide_research, force=True,
            chat_id=chat_id,
        ); return
    if sub == "research":
        if not sid:
            yield no_session_msg("research"); return
        question = (fenced or " ".join(rest)).strip()
        if not question:
            yield "Usage: `/assist research [<session_id>] <question>`"; return
        recalled = assist_recall(pipe, chat_id)
        node_key = (recalled or {}).get("last_node_key")
        yield from assist_research_cmd(
            pipe, sid, question, node_key=node_key, chat_id=chat_id,
        ); return
    if sub == "env":
        if not sid:
            yield no_session_msg("env"); return
        text_arg = (fenced or " ".join(rest)).strip()
        if not text_arg:
            yield from assist_env_cmd(pipe, sid, show=True, chat_id=chat_id); return
        # Pull out KEY=value substitutions; the remaining free text is the
        # profile. `/assist env Ubuntu 24.04 HOST_IP=10.0.0.5` → profile +1 sub.
        subs = dict(re.findall(r"([A-Za-z_]\w*)=(\S+)", text_arg))
        profile_text = re.sub(r"[A-Za-z_]\w*=\S+", "", text_arg).strip(" ,\t") or None
        yield from assist_env_cmd(
            pipe, sid, profile=profile_text, substitutions=subs or None,
            chat_id=chat_id,
        ); return
    if sub == "verbose":
        if not sid:
            yield no_session_msg("verbose"); return
        level = (rest[0].lower() if rest else "").strip()
        if level not in ("terse", "normal", "detailed"):
            yield "Usage: `/assist verbose [<session_id>] terse|normal|detailed`"; return
        yield from assist_env_cmd(pipe, sid, verbosity=level, chat_id=chat_id); return
    if sub == "fix":
        if not sid:
            yield no_session_msg("fix"); return
        error_text = (fenced or " ".join(rest)).strip()
        if not error_text:
            yield "Usage: `/assist fix [<session_id>] <error / what went wrong>`"; return
        recalled = assist_recall(pipe, chat_id)
        node_key = (recalled or {}).get("last_node_key")
        yield from assist_fix_cmd(
            pipe, sid, error_text, node_key=node_key, chat_id=chat_id,
        ); return
    yield pipe._ASSIST_HELP


# ---------------------------------------------------------------------------
# Per-subcommand handlers.
# ---------------------------------------------------------------------------


def assist_start(
    pipe, job_id: str, *, chat_id: str | None = None,
) -> Generator[str, None, None]:
    # §17.521 — reject a non-UUID job_id (e.g. a pasted job TITLE like
    # "DeFruscio HomeLab" → only "DeFruscio" reaches here) before the
    # round-trip, with an actionable hint. Otherwise it 4xx's server-side
    # (and pre-§17.521 surfaced as a raw HTTP 500 DataError).
    if not pipe._UUID_RE.match((job_id or "").strip()):
        yield (
            f"❌ `{job_id}` isn't a job id. `/assist` needs the job's **UUID**, "
            f"not its title — run `/jobs` to find it, then "
            f"`/assist <job_id>`."
        )
        return
    try:
        r = _ss(pipe).post(
            f"{pipe.valves.orchestrator_url}/assist/start",
            json={
                "job_id": job_id,
                "handoff_policy": pipe.valves.assist_default_handoff_policy,
                "replan_policy": pipe.valves.assist_default_replan_policy,
            },
            headers=pipe._auth_headers(),
            timeout=pipe.valves.request_timeout,
        )
    except requests.exceptions.RequestException as e:
        yield f"❌ Connection error: {e}"; return
    if r.status_code >= 400:
        yield f"❌ Could not start assist session: HTTP {r.status_code} {r.text[:200]}"; return
    try:
        d = r.json()
    except ValueError as e:
        yield f"❌ Assist start: orchestrator returned non-JSON body ({e}); raw: {r.text[:200]}"; return
    # §17.561/562 — umbrella / 0-node guard. The orchestrator returns HTTP 200
    # with assist_unavailable instead of seeding a phantom empty session (which
    # used to render the cryptic "⏳ No step ready right now."). Surface clear
    # guidance: umbrella work runs autonomously in component children; a 0-node
    # job needs a plan first.
    if isinstance(d, dict) and d.get("assist_unavailable"):
        if d.get("reason") == "umbrella":
            kids = d.get("children") or []
            done = sum(1 for c in kids if c.get("status") == "completed")
            # §17.624 — components whose DAG was predominantly hands-on are
            # parked in 'awaiting_assist' (not auto-completed); those DO need the
            # operator to step through them via /assist <component_id>.
            parked = [c for c in kids if c.get("status") == "awaiting_assist"]
            lines = [
                f"📦 **This is a multi-part job** — its {len(kids)} component(s) "
                f"run **automatically** where the engine can.",
                "",
                f"Progress: **{done}/{len(kids)}** completed"
                + (f", **{len(parked)}** need you (hands-on)." if parked else "."),
                "",
            ]
            for c in kids:
                icon = "✅" if c.get("status") == "completed" else (
                    "🙋" if c.get("status") == "awaiting_assist" else  # §17.624
                    "❌" if c.get("status") in ("failed", "cancelled", "blocked")
                    else "⏳")
                lines.append(
                    f"- {icon} {c.get('title', '')} — `{c.get('status', '')}`"
                )
            if parked:
                lines += [
                    "",
                    "🙋 **These components need you** — they're hands-on work on "
                    "real systems, so they were planned but **not** auto-executed. "
                    "Step through each yourself:",
                ]
                for c in parked:
                    lines.append(
                        f"- `{c.get('title', '')}` → `/assist {c.get('job_id', '')}`"
                    )
            lines += [
                "",
                f"Watch progress or read the assembled result with "
                f"`/results {d.get('job_id', '')}`.",
            ]
            yield "\n".join(lines)
            return
        # reason == 'no_dag'
        yield (
            "ℹ️ This job has no execution plan yet, so there's nothing to "
            "assist with. Build a plan first with `/confirm <job_id>`, then "
            "`/assist <job_id>`."
        )
        return
    sid = d.get("session_id") if isinstance(d, dict) else None
    if not sid:
        yield f"❌ Assist start: orchestrator reply missing `session_id`; raw: {str(d)[:200]}"; return
    assist_remember(pipe, chat_id, session_id=sid)
    resp_job_id = d.get("job_id", job_id)
    pending = d.get("pending_steps", "?")
    # §17.623 — re-open banner. The job had already run to a terminal state
    # (typically an autonomous run that fabricated "done" evidence); assist
    # re-opened it and reset its nodes for a hands-on redo. Tell the operator
    # so they don't think their earlier output vanished silently.
    if isinstance(d, dict) and d.get("reopened"):
        yield (
            f"♻️ **Re-opened `{resp_job_id}` for a hands-on redo.** This job had "
            f"already finished (it ran autonomously), but assist reset its "
            f"{pending} step(s) back to pending so you can do them yourself. "
            f"The prior autonomous deliverable is archived — still viewable with "
            f"`/results {resp_job_id}`.\n\n---\n\n"
        )
    yield (
        f"🤝 **Assist session started** — `{sid}`\n\n"
        f"Job `{resp_job_id}` is now in `assisted_executing` ({pending} pending step(s)).\n\n"
        f"💡 Tip: set your environment with `/assist env <OS, shell, tools>` "
        f"(e.g. `/assist env Ubuntu 24.04, apt, bash`) so walkthroughs use concrete "
        f"commands. Hit an error on any step? `/assist fix <the error>`.\n\n"
        f"Fetching first step...\n\n---\n\n"
    )
    yield from assist_next(pipe, sid, chat_id=chat_id)


def assist_next(
    pipe, session_id: str, *, chat_id: str | None = None,
) -> Generator[str, None, None]:
    try:
        r = _ss(pipe).get(
            f"{pipe.valves.orchestrator_url}/assist/{session_id}/next",
            headers=pipe._auth_headers(),
            timeout=pipe.valves.request_timeout,
        )
    except requests.exceptions.RequestException as e:
        yield f"❌ Connection error: {e}"; return
    if r.status_code == 404:
        yield f"❌ Session `{session_id}` not found."; return
    if r.status_code >= 400:
        yield f"❌ HTTP {r.status_code}: {r.text[:200]}"; return
    # §17.268 — mirror §17.259's _assist_start guards. Pre-fix this was
    # bare r.json() + step.get(...); a non-JSON body would crash the
    # generator with ValueError mid-yield.
    try:
        step = r.json()
    except ValueError as e:
        yield f"❌ Assist next: orchestrator returned non-JSON body ({e}); raw: {r.text[:200]}"; return
    if not isinstance(step, dict):
        yield f"❌ Assist next: orchestrator reply not a dict; raw: {str(step)[:200]}"; return
    # Refresh remembered last_node_key when /next claims a real step.
    # Skip on terminal-status responses where node_key is None.
    if step.get("node_key"):
        assist_remember(
            pipe, chat_id, session_id=session_id, last_node_key=step["node_key"],
        )
    # §17.699 — a divergence on an earlier submit staged a proactive plan-fix
    # proposal (the server flips it to surfaced so this announces exactly once).
    # Surface it BEFORE the step so the operator can re-plan first; a plain
    # yes/no on the next turn resolves it via the existing _replan_decision path.
    notice = step.get("replan_notice")
    affected = (notice or {}).get("proposals") if isinstance(notice, dict) else None
    if affected:
        src = (notice or {}).get("source_node") or "the last step"
        yield (
            f"⚠️ Heads up — what you reported for `{src}` looks like it diverges "
            f"from the plan, so some steps below may no longer fit.\n\n"
        )
        yield _render_replan_surface(
            affected,
            lead=f"This affects **{len([p for p in affected if isinstance(p, dict)])}** "
                 f"pending step(s):",
        )
        yield "\n\n---\n\n"
    yield render_step(step)
    # §17.486 — auto-generate the human walkthrough for the claimed step.
    # Separate POST so the slow LLM call doesn't block the fast /next claim;
    # force=False hits the cache when this step was already guided.
    if step.get("node_key"):
        if getattr(pipe.valves, "assist_auto_guide", True):
            _cmd = (assist_guide_stream_cmd
                    if getattr(pipe.valves, "assist_stream", True) else assist_guide_cmd)
            if _cmd is assist_guide_cmd:
                yield "\n_Generating walkthrough…_\n\n"
            yield from _cmd(
                pipe, session_id, node_key=step["node_key"],
                research=getattr(pipe.valves, "assist_guide_research", True),
                force=False, chat_id=chat_id,
            )
        # §17.626 — natural-language 'report back when done' footer, after the
        # walkthrough so the how-to leads and the call-to-action trails.
        yield render_step_footer(step)


def assist_submit(
    pipe, session_id: str, node_key: str, evidence: str,
    *, chat_id: str | None = None, history: list[dict] | None = None,
) -> Generator[str, None, None]:
    if not evidence:
        yield "Empty evidence. Wrap your output in a triple-backtick fence and resend."; return
    if len(evidence) > pipe.valves.assist_max_evidence_chars:
        yield (f"❌ Evidence is {len(evidence)} chars; cap is "
               f"{pipe.valves.assist_max_evidence_chars}. Trim and resend."); return
    try:
        r = _ss(pipe).post(
            f"{pipe.valves.orchestrator_url}/assist/{session_id}/submit",
            json={
                "node_key": node_key,
                "output": evidence,
                "evidence_kind": "text",
                "action": "submit",
                "history": history or [],  # §17.689 — decision deliberation
            },
            headers=pipe._auth_headers(),
            timeout=pipe.valves.request_timeout,
        )
    except requests.exceptions.RequestException as e:
        yield f"❌ Connection error: {e}"; return
    if r.status_code == 409:
        # Structured detail: {"error_code": "must_claim_first", ...}
        try:
            detail = r.json().get("detail", {})
        except Exception:
            detail = {}
        if isinstance(detail, dict) and detail.get("error_code") == "must_claim_first":
            yield (
                f"⚠️ Step `{node_key}` is still pending — claim it first.\n\n"
                f"Run `/assist next` (no arg in this chat), then resend your submit."
            ); return
    if r.status_code >= 400:
        yield f"❌ HTTP {r.status_code}: {r.text[:200]}"; return
    # §17.268 — mirror §17.259's _assist_start guards.
    try:
        d = r.json()
    except ValueError as e:
        yield f"❌ Assist submit: orchestrator returned non-JSON body ({e}); raw: {r.text[:200]}"; return
    if not isinstance(d, dict):
        yield f"❌ Assist submit: orchestrator reply not a dict; raw: {str(d)[:200]}"; return
    if d.get("no_op"):
        # `d['status']` was a hard access pre-§17.268; .get() preserves
        # the banner when status is omitted from a non-standard reply.
        status_val = d.get("status", "?")
        yield f"ℹ️ Step `{node_key}` already `{status_val}`. No change."; return
    # §17.689 — decision deliberation: the engine assembled a concrete proposal
    # but the decision isn't settled yet. Show it and keep the step open; the
    # operator confirms or adjusts on the next turn (the step is NOT committed).
    if d.get("status") == "deliberating":
        assist_remember(pipe, chat_id, session_id=session_id, last_node_key=node_key)
        gather = d.get("collect_kind") == "gather"
        msg = (d.get("decision_message") or "").strip() or (
            "Here's where this step stands so far."
        )
        hint = (
            "\n\n_Send the remaining details whenever you have them — one piece "
            "at a time is fine. I'll record it once everything's in._"
            if gather else
            "\n\n_Reply to confirm (e.g. \"looks good\") or tell me what to "
            "change — I'll record the final plan once you're happy._"
        )
        yield msg + hint
        return
    # §17.487 — hard-block path: the success-check judged this a failure and
    # `assist_block_on_failed_verify` is on, so the node was NOT marked done.
    if d.get("status") == "verification_failed":
        v = d.get("success_verdict") or {}
        ran = "sandbox" in (v.get("grounded_by") or "")
        head = (
            f"🛑 Ran your code in the sandbox — it **failed**. `{node_key}` not marked done."
            if ran else
            f"🛑 Step `{node_key}` looks like it **failed** — not marked done."
        )
        msg = f"{head}\n\n_{v.get('reason', '')}_\n\n"
        if v.get("suggestion"):
            msg += f"Suggested next move: {v['suggestion']}\n\n"
        msg += (
            "Fix it and resubmit, or get help: `/assist fix <the error>`."
        )
        yield msg; return
    next_nk = d.get("next_node_key")
    # Update remembered node so the next `/assist submit` (no args) is
    # right. None on terminal => clear it so we don't suggest a
    # stale step.
    assist_remember(
        pipe, chat_id, session_id=session_id, last_node_key=next_nk,
    )
    # §17.487 — the success verdict is needed both for the warning block below
    # and for the auto-advance gate, so read it up front.
    verdict = d.get("success_verdict") or {}
    outcome = verdict.get("outcome")
    ran = "sandbox" in (verdict.get("grounded_by") or "")
    # §17.638 — auto-advance: after a clean commit, present the next step in the
    # same turn instead of parking on the finished one (the "output is echoing"
    # symptom — every later turn re-rendered the committed step's walkthrough).
    # Held back on a soft-fail verdict so the operator can redo/fix first.
    auto_advance = (
        getattr(pipe.valves, "assist_auto_advance", True)
        and d.get("status") == "committed"
        and bool(next_nk)
        and outcome != "failed"
    )
    # §17.689/§17.690 — a resolved collect step commits the artifact the engine
    # assembled across turns (not the operator's "looks good"/last portion).
    # Lead with what was recorded.
    decision_msg = (d.get("decision_message") or "").strip()
    _rec_label = ("Recorded" if d.get("collect_kind") == "gather"
                  else "Decision recorded")
    prefix = f"📌 **{_rec_label}.** {decision_msg}\n\n" if decision_msg else ""
    if outcome == "failed":
        # §17.708 — coherent failure framing. Don't say "✅ committed … moving on"
        # and then contradict it with "⚠️ this may have failed". A failed step is
        # recorded but is a FIX-and-retry situation, NOT a plan divergence (the
        # server no longer stages a downstream re-plan for it). Lead with that.
        head = ("🛑 **Ran your code in the sandbox — it errored.**" if ran
                else "⚠️ **This step doesn't look like it succeeded.**")
        _reason = (verdict.get("reason") or "").strip()
        msg = (
            f"{prefix}📝 Recorded your evidence for `{node_key}`, but {head} {_reason}\n\n"
            f"That's usually something to fix here, not a change to the plan — run "
            f"`/assist fix <the error>` for a diagnosis + recovery steps, then redo "
            f"and resubmit."
        )
        if next_nk:
            msg += f" Once it's working, say _\"next\"_ to move on to `{next_nk}`."
    else:
        msg = f"{prefix}✅ Step `{node_key}` committed. "
        if next_nk:
            msg += (f"Moving on to `{next_nk}`…" if auto_advance
                    else f"Next: `{next_nk}`. Run `/assist next` to fetch.")
        else:
            msg += "All steps terminal — run `/assist done` to view compiled output."
    # §17.286 — mirror invariant divergence: assist_steps was updated
    # but dag_nodes was already in a terminal status. Append a
    # warning so the operator sees the race without grepping logs.
    if d.get("mirror_divergence"):
        msg += (
            "\n\n⚠️ **Mirror divergence**: the corresponding DAG node was "
            "already `done` or `skipped` when this submit landed (likely a "
            "concurrent `execute_next_node`). Your evidence is recorded on "
            "the assist step, but the DAG node was NOT overwritten by this "
            "call. Inspect with `/assist status` and re-run if needed."
        )
    # §17.487 — warn mode: surface the success verdict without blocking.
    # §17.491 — when grounded_by includes 'sandbox', the code was actually run.
    # §17.708 — the 'failed' case is now handled in the lead above (coherent
    # fix-first framing); only the positive/ambiguous notes are appended here.
    if outcome == "succeeded" and ran:
        msg += "\n\n✓ _Verified by running your code in the sandbox._"
    elif (outcome == "unclear" and verdict.get("reason")
          and verdict["reason"] != "verification unavailable"):
        msg += f"\n\n_Couldn't confirm success: {verdict['reason']}_"
    # §17.490 — concrete values learned from this step's evidence; surfaced so
    # the operator sees later steps will use them instead of placeholders.
    learned = d.get("learned_substitutions") or {}
    if learned:
        pairs = ", ".join(f"`{k}`=`{v}`" for k, v in learned.items())
        msg += f"\n\n📌 Learned for later steps: {pairs}"
    # §17.709 — durable facts distilled about the operator's system; surfaced so
    # they can see what later steps will ground on (and correct a mis-read).
    facts = d.get("captured_facts") or []
    if facts:
        shown = facts[:6]
        bullets = "\n".join(f"- {f}" for f in shown)
        more = f"\n_(+{len(facts) - len(shown)} more)_" if len(facts) > len(shown) else ""
        msg += f"\n\n🧠 **Noted about your system** (later steps will use this):\n{bullets}{more}"
    # §17.703 — confirm the operator's execution context so they see it stuck
    # (and can correct it if a stray prompt was mis-read).
    ctx = d.get("execution_context") or {}
    if ctx.get("host"):
        who = f"`{ctx.get('user', '')}@{ctx['host']}`".replace("`@", "`")
        verb = "Switched to" if ctx.get("changed") else "Noted — you're working on"
        msg += f"\n\n🖥️ {verb} {who}; later steps will target that single shell."
    yield msg
    # §17.638 — chain straight into the next step (claim + walkthrough) so the
    # operator keeps moving instead of re-reading the finished one. assist_next
    # re-remembers the freshly-claimed node_key, superseding the next_nk hint
    # stashed above.
    if auto_advance:
        yield "\n\n---\n\n"
        yield from assist_next(pipe, session_id, chat_id=chat_id)


def assist_skip(
    pipe, session_id: str, node_key: str, *, chat_id: str | None = None,
) -> Generator[str, None, None]:
    try:
        r = _ss(pipe).post(
            f"{pipe.valves.orchestrator_url}/assist/{session_id}/submit",
            json={"node_key": node_key, "output": "", "action": "skip"},
            headers=pipe._auth_headers(),
            timeout=pipe.valves.request_timeout,
        )
    except requests.exceptions.RequestException as e:
        yield f"❌ Connection error: {e}"; return
    if r.status_code >= 400:
        yield f"❌ HTTP {r.status_code}: {r.text[:200]}"; return
    # §17.275 — mirror §17.259's _assist_start guards.
    try:
        d = r.json()
    except ValueError as e:
        yield f"❌ Assist skip: orchestrator returned non-JSON body ({e}); raw: {r.text[:200]}"; return
    if not isinstance(d, dict):
        yield f"❌ Assist skip: orchestrator reply not a dict; raw: {str(d)[:200]}"; return
    next_nk = d.get("next_node_key")
    assist_remember(
        pipe, chat_id, session_id=session_id, last_node_key=next_nk,
    )
    # §17.639 — auto-advance on skip too (parity with the §17.638 submit path):
    # skipping and then parking on the skipped step is the same dead-end, and a
    # conversational operator won't type `/assist next`.
    auto_advance = (
        getattr(pipe.valves, "assist_auto_advance", True) and bool(next_nk)
    )
    msg = f"⏭ Step `{node_key}` skipped. "
    if next_nk:
        msg += (f"Moving on to `{next_nk}`…" if auto_advance
                else f"Next: `{next_nk}`.")
    else:
        msg += f"All steps terminal — run `/assist done`."
    # §17.286 — mirror invariant divergence (skip path). assist_steps
    # was flipped to 'skipped' but dag_nodes was already terminal.
    if d.get("mirror_divergence"):
        msg += (
            "\n\n⚠️ **Mirror divergence**: the DAG node was already `done` or "
            "`skipped` when this skip landed. The assist step is marked "
            "skipped, but the DAG node was NOT touched."
        )
    yield msg
    if auto_advance:
        yield "\n\n---\n\n"
        yield from assist_next(pipe, session_id, chat_id=chat_id)


def assist_handoff(
    pipe, session_id: str, node_key: str, mode: str,
) -> Generator[str, None, None]:
    # SSE stream — reuse existing _stream_sse_to_queue plumbing.
    yield f"🤖 Handing `{node_key}` back to autonomous executor (mode: `{mode}`)...\n\n"
    url = f"{pipe.valves.orchestrator_url}/assist/{session_id}/handoff"
    body = {"node_key": node_key, "mode": mode}
    # Reuse the generic streaming runner used by /research.
    yield from stream_sse_with_keepalive(pipe, url, body)


def stream_sse_with_keepalive(
    pipe, url: str, body: dict,
) -> Generator[str, None, None]:
    """Minimal SSE consumer for assist handoff. Mirrors the queue loop
    used in _handle_research but emits assist_* events plus the
    standard execution events from the underlying executor."""
    q: _q.Queue = _q.Queue()
    # §17.262 — early-exit plumbing so a GeneratorExit (client
    # disconnect) tears down the daemon reader within reader.join's
    # 5s window instead of leaving it alive until the 24h SSE timeout.
    stop_event = _th.Event()
    r_holder: list = []
    reader = _th.Thread(
        target=pipe._stream_sse_to_queue,
        args=(url, body, q),
        kwargs={"stop_event": stop_event, "r_holder": r_holder},
        daemon=True,
    )
    reader.start()
    sse_const = _sse_events_const(pipe)
    try:
        while True:
            try:
                msg_type, f1, f2 = q.get(timeout=pipe.valves.keepalive_interval)
            except _q.Empty:
                yield "​"; continue
            if msg_type == "connected":
                continue
            if msg_type == "heartbeat":
                yield "​"; continue
            if msg_type == "http_error":
                yield f"⚠️ Handoff failed (HTTP {f1}): {(f2 or '')[:200]}"; return
            if msg_type == "error":
                yield f"\n⚠️ Connection error: {f1}"; return
            if msg_type == "done":
                break
            event_type, data = f1, f2
            try:
                payload = json.loads(data)
            except Exception:
                continue
            # §17.190: event-name vocabulary lives in pipelines/_vendor/_sse_events.py
            # (byte-equal vendor of app/sse_events.py). Pre-§17.190 the two
            # branches below matched ``"node_started"`` / ``"node_completed"``
            # — neither of which is ever emitted by the orchestrator. Those
            # were dead branches; the assist UI lost node-progress rendering
            # during the post-handoff autonomous run. Names now match the
            # actual NODE_START / NODE_DONE emitter constants.
            if event_type == sse_const.ASSIST_HANDOFF_STARTED:
                yield f"\n🟢 Autonomous executor took over `{payload.get('node_key', '?')}`.\n"
            elif event_type == sse_const.ASSIST_HANDOFF_DONE:
                yield f"\n✅ Handoff complete. Run `/assist next {payload.get('session_id', '?')}` to continue.\n"
            elif event_type == sse_const.NODE_START:
                yield f"  ▶ {payload.get('node_key', '?')} — {payload.get('title', '?')}\n"
            elif event_type == sse_const.NODE_DONE:
                yield f"  ✓ {payload.get('node_key', '?')} (model: {payload.get('model', '?')})\n"
            elif event_type == sse_const.NODE_FAILED:
                yield f"  ✗ {payload.get('node_key', '?')}: {payload.get('error', '?')}\n"
            elif event_type == sse_const.ERROR:
                yield f"\n⚠️ {payload.get('detail') or payload}\n"; return

    finally:
        # §17.262 — runs on GeneratorExit (client disconnect) AND on
        # clean break/return. Closing r forces iter_lines to raise →
        # reader's try/except exits; stop_event covers the
        # ReadTimeout cycle path. join's 5s is the upper bound.
        stop_event.set()
        if r_holder:
            try:
                r_holder[0].close()
            except Exception:
                pass
        reader.join(timeout=5)


def assist_guide_cmd(
    pipe, session_id: str, *, node_key: str | None = None,
    refine: str | None = None, research: bool | None = None,
    force: bool = True, chat_id: str | None = None,
    history: list[dict] | None = None,
) -> Generator[str, None, None]:
    """§17.486 — POST /assist/{sid}/guide and render the walkthrough.

    Uses the dedicated `assist_guide_timeout` valve (not `request_timeout`):
    generation is an 8192-token thinking-model call plus an optional research
    pre-pass, well beyond the fast-call default."""
    try:
        r = _ss(pipe).post(
            f"{pipe.valves.orchestrator_url}/assist/{session_id}/guide",
            json={
                "node_key": node_key,
                "refine": refine,
                "research": research,
                "force": force,
                "history": history or [],  # §17.687
            },
            headers=pipe._auth_headers(),
            timeout=getattr(pipe.valves, "assist_guide_timeout", 180),
        )
    except requests.exceptions.RequestException as e:
        yield f"❌ Connection error: {e}"; return
    if r.status_code >= 400:
        yield f"❌ HTTP {r.status_code}: {r.text[:200]}"; return
    try:
        d = r.json()
    except ValueError as e:
        yield f"❌ Assist guide: orchestrator returned non-JSON body ({e}); raw: {r.text[:200]}"; return
    if not isinstance(d, dict):
        yield f"❌ Assist guide: orchestrator reply not a dict; raw: {str(d)[:200]}"; return
    yield render_guidance(d)


def assist_guide_stream_cmd(
    pipe, session_id: str, *, node_key: str | None = None,
    refine: str | None = None, research: bool | None = None,
    force: bool = True, chat_id: str | None = None,
    history: list[dict] | None = None,
) -> Generator[str, None, None]:
    """§17.493 — stream the walkthrough from /assist/{sid}/guide/stream.

    Consumes the SSE stream (assist_guide_delta* → assist_guide_done) on the
    same thread/queue/keepalive skeleton as the handoff consumer. Yields the
    content live; the destructive banner + sources footnote are appended on
    `done` (trailing — we don't know them until generation completes). A cache
    hit arrives as one delta + done(cached) and renders instantly."""
    url = f"{pipe.valves.orchestrator_url}/assist/{session_id}/guide/stream"
    body = {"node_key": node_key, "refine": refine, "research": research,
            "force": force, "history": history or []}  # §17.687
    q: _q.Queue = _q.Queue()
    stop_event = _th.Event()
    r_holder: list = []
    reader = _th.Thread(
        target=pipe._stream_sse_to_queue,
        args=(url, body, q),
        kwargs={"stop_event": stop_event, "r_holder": r_holder},
        daemon=True,
    )
    reader.start()
    sse_const = _sse_events_const(pipe)
    started = False
    got_text = False
    # §17.704 — visible progress while the FIRST token is pending. The research
    # pre-pass (Milvus rerank + web fetch) runs server-side before any delta, and
    # on a research-heavy step that is tens of seconds to a couple of minutes of
    # silence — during which this loop only saw queue-empties / SSE keepalives and
    # emitted an invisible ZWSP, so the operator read it as a hang / timeout.
    # Now the wait is surfaced: a one-time notice, then a ~10 s elapsed trail.
    # Self-gating — a fast or cached step yields its first delta before the
    # keepalive timeout, so nothing below fires. Once content starts we revert to
    # the ZWSP so the trail never clutters the walkthrough.
    wait_started_at = None

    def _waiting_notice():
        nonlocal wait_started_at
        if started:
            return "​"
        if wait_started_at is None:
            wait_started_at = time.monotonic()
            return (
                "\n🔎 _Preparing this step — gathering references and drafting the "
                "walkthrough. A research-heavy step can take up to a minute…_\n"
            )
        return f"\n_…still working ({int(time.monotonic() - wait_started_at)}s elapsed)…_"

    try:
        while True:
            try:
                msg_type, f1, f2 = q.get(timeout=pipe.valves.keepalive_interval)
            except _q.Empty:
                yield _waiting_notice(); continue
            if msg_type == "connected":
                continue
            if msg_type == "heartbeat":
                yield _waiting_notice(); continue
            if msg_type == "http_error":
                yield f"❌ HTTP {f1}: {(f2 or '')[:200]}"; return
            if msg_type == "error":
                yield f"\n❌ Connection error: {f1}"; return
            if msg_type == "done":
                break
            event_type, data = f1, f2
            try:
                payload = json.loads(data)
            except Exception:
                continue
            if event_type == sse_const.ASSIST_GUIDE_DELTA:
                if not started:
                    # §17.644 — lead with a newline so the H2 starts at column 0
                    # of a fresh line. Keepalive ZWSPs (yielded while the research
                    # pre-pass runs, before the first delta) would otherwise sit
                    # on the same line as `##`, so the `#` is no longer the first
                    # char and OWUI renders it as literal text, not a heading.
                    yield "\n## 🧭 How to do this step\n\n"
                    started = True
                txt = payload.get("text", "")
                if txt:
                    got_text = True
                    yield txt
            elif event_type == sse_const.ASSIST_GUIDE_DONE:
                meta = payload.get("guidance_meta") or {}
                if payload.get("status") != "ready" and not got_text:
                    yield (
                        "⚠️ Couldn't generate a walkthrough right now. Work from "
                        "the raw task prompt above, or retry with `/assist guide`."
                    ); return
                banner = render_destructive_banner(meta)
                if banner:
                    yield "\n\n" + banner
                sources = meta.get("research_sources") or []
                if sources:
                    cites = ", ".join(
                        f"`{s.get('kind')}`: {s.get('query')}" for s in sources
                    )
                    yield f"\n_Confirmed via research — {cites}._\n"
                if payload.get("cached"):
                    yield "\n_(cached walkthrough — run `/assist guide` to regenerate)_\n"
                return
            elif event_type == sse_const.ERROR:
                yield f"\n❌ {payload.get('detail') or payload}\n"; return
    finally:
        stop_event.set()
        if r_holder:
            try:
                r_holder[0].close()
            except Exception:
                pass
        reader.join(timeout=5)


def assist_chat_turn(
    pipe, session_id: str, refine: str, *,
    node_key: str | None = None, chat_id: str | None = None,
    history: list[dict] | None = None,
) -> Generator[str, None, None]:
    """§17.537 — a plain-language chat turn inside an ACTIVE assist session.

    The router calls this when a chat with an active assist session receives
    bare (non-command) text. Rather than bouncing to the triage planner (the
    DeFruscio HomeLab symptom — frozen session + repeating Scope/Options/Gaps),
    the message is treated as a `refine` hint and answered with the current
    step's walkthrough. Mirrors the `/assist guide` dispatch: streamed when the
    `assist_stream` valve is on, blocking otherwise. A one-line banner orients
    the user (they typed a question and got step guidance, not a planner reply)
    and points at the commands to advance or step out."""
    yield (
        "_💬 In your active assist session — answering for the current step. "
        "Use `/assist next` to advance, `/assist pause` to step back to "
        "planning._\n\n"
    )
    _cmd = (assist_guide_stream_cmd
            if getattr(pipe.valves, "assist_stream", True) else assist_guide_cmd)
    yield from _cmd(
        pipe, session_id, node_key=node_key, refine=refine,
        research=pipe.valves.assist_guide_research, force=True,
        chat_id=chat_id, history=history,
    )


def record_turn_bg(pipe, session_id: str, content: str, *,
                   kind: str = "message", node_key: str | None = None) -> None:
    """§17.710a — best-effort raw-turn capture. Called for EVERY chat message
    before any client-side routing, so the transcript is lossless even when a
    message is fast-verb'd or the classifier mislabels it. Fire-and-forget: a
    capture hiccup must never affect the conversation, and the endpoint is a
    no-op server-side unless the unified-memory capture valve is on."""
    if not (content or "").strip():
        return
    try:
        _ss(pipe).post(
            f"{pipe.valves.orchestrator_url}/assist/{session_id}/turn",
            json={"role": "operator", "kind": kind, "content": content,
                  "node_key": node_key},
            headers=pipe._auth_headers(),
            timeout=10,
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# §17.626 — natural-language turns. Plain chat in an active session drives the
# whole flow (advance / skip / submit / fix / finalize / pause) without the
# operator typing a /assist subcommand. Obvious short verbs are matched here
# with no LLM; substantive/ambiguous messages hit the /interpret classifier.
# ---------------------------------------------------------------------------

_FAST_INTENT_PHRASES = {
    "advance": {
        "next", "next step", "next one", "continue", "go on", "keep going",
        "move on", "moving on", "proceed", "onward", "what's next", "whats next",
        "next please", "ok next", "okay next",
    },
    "skip": {
        "skip", "skip this", "skip it", "skip this step", "skip step",
        "skip this one", "pass", "pass on this",
    },
    "pause": {
        "pause", "pause this", "stop for now", "hold on", "take a break",
        "pause please",
    },
    "finalize": {
        "show me the result", "show the result", "show result", "show results",
        "show me the results", "compiled output", "wrap up", "wrap it up",
        "finish the job", "finish up", "we're done", "were done", "all done here",
        "that's everything", "thats everything",
    },
    "status": {
        "status", "where am i", "progress", "my progress", "how far am i",
        "how much is left", "how many left", "how many steps left",
    },
    "explain_plan": {
        "the plan", "show me the plan", "show the plan", "what's the plan",
        "whats the plan", "all the steps", "show all steps", "overview",
        "the big picture", "show me all the steps", "what are all the steps",
    },
    "handoff": {
        "you do it", "do it for me", "you do this one", "you do this",
        "handle it", "run it for me", "automate this", "you do the rest",
        "do the rest", "take over", "you handle it", "you take this one",
    },
}
_FAST_INTENT_LOOKUP = {
    phrase: intent
    for intent, phrases in _FAST_INTENT_PHRASES.items()
    for phrase in phrases
}


def fast_classify_turn(msg: str) -> str | None:
    """Intent for an unambiguous short verb phrase (whole-message match), else
    None. Deterministic — no LLM. 'done' is intentionally absent: it's ambiguous
    (submit-this-step vs finalize-the-job), so it goes to the classifier."""
    norm = (msg or "").strip().lower().strip(".!?,;: ").strip()
    return _FAST_INTENT_LOOKUP.get(norm)


def assist_interpret(
    pipe, session_id: str, message: str, *, node_key: str | None = None,
    history: list[dict] | None = None,
) -> dict:
    """POST /assist/{sid}/interpret → intent dict. Fail-soft → question so a
    classifier/endpoint hiccup degrades to the guide/refine turn."""
    fallback = {"intent": "question", "evidence": "", "error_text": "",
                "node_key": node_key}
    try:
        r = _ss(pipe).post(
            f"{pipe.valves.orchestrator_url}/assist/{session_id}/interpret",
            json={"message": message, "node_key": node_key,
                  "history": history or []},
            headers=pipe._auth_headers(),
            timeout=getattr(pipe.valves, "assist_guide_timeout", 180),
        )
        if r.status_code < 400:
            d = r.json()
            if isinstance(d, dict) and d.get("intent"):
                return d
    except (requests.exceptions.RequestException, ValueError) as e:
        pipe.logger.debug("assist_interpret failed: %s", e)
    return fallback


def _recall_node_key(pipe, chat_id: str | None, node_key: str | None) -> str | None:
    if node_key:
        return node_key
    return (assist_recall(pipe, chat_id) or {}).get("last_node_key")


# Verbosity keyword heuristics (§17.627) — used when the classifier routes a
# message to set_verbosity; we read the level from the raw words.
_TERSE_HINTS = (
    "terse", "just the command", "just commands", "just give me the command",
    "less detail", "too verbose", "too long", "shorter", "brief", "concise",
    "less wordy", "cut the",
)
# §17.643 — the beginner-language phrases ("beginner", "walk me through", "step
# by step", "eli5") were removed from this list: they signal a reader who wants
# a CLEAR, concise how-to, not more rationale. Mapping them to `detailed` made
# the walkthrough LONGER (more WHY, expanded verification) — the opposite of
# what a beginner asking for simple help wants. They now fall through to the
# beginner-clear `normal` default (which already assumes limited knowledge).
# Only phrases that genuinely ask for MORE depth remain here.
_DETAILED_HINTS = (
    "more detail", "detailed", "explain more", "more thorough", "verbose",
    "more context", "explain why", "in depth", "in-depth",
)


def _verbosity_from_message(msg: str) -> str:
    low = (msg or "").lower()
    if any(h in low for h in _TERSE_HINTS):
        return "terse"
    if any(h in low for h in _DETAILED_HINTS):
        return "detailed"
    return "normal"


_HANDOFF_ALL_HINTS = (
    "the rest", "everything else", "remaining", "all of them", "all the rest",
    "rest of", "whole thing", "everything", "all steps", "finish it all",
)


def _handoff_mode_from_message(msg: str) -> str:
    low = (msg or "").lower()
    return "all_remaining" if any(h in low for h in _HANDOFF_ALL_HINTS) else "single"


def assist_plan(
    pipe, session_id: str, *, chat_id: str | None = None,
) -> Generator[str, None, None]:
    """§17.627 — render the whole DAG as a natural plan overview (explain_plan).

    Pulls the session's job_id, then GET /dag/{job_id}, and lists every step
    with a status icon, marking the current one. Uses the full engine's DAG —
    the same node graph the autonomous executor runs — so the operator sees the
    big picture, not just the current step."""
    try:
        r = _ss(pipe).get(
            f"{pipe.valves.orchestrator_url}/assist/{session_id}",
            headers=pipe._auth_headers(), timeout=pipe.valves.request_timeout,
        )
    except requests.exceptions.RequestException as e:
        yield f"❌ Connection error: {e}"; return
    if r.status_code >= 400:
        yield f"❌ Couldn't load the plan (HTTP {r.status_code})."; return
    try:
        sess = r.json()
    except ValueError:
        yield "❌ Couldn't load the plan (bad reply)."; return
    job_id = sess.get("job_id")
    current = sess.get("current_node_key")
    if not job_id:
        yield "⚠️ This session has no plan yet."; return
    try:
        r2 = _ss(pipe).get(
            f"{pipe.valves.orchestrator_url}/dag/{job_id}",
            headers=pipe._auth_headers(), timeout=pipe.valves.request_timeout,
        )
        dag = r2.json() if r2.status_code < 400 else {}
    except (requests.exceptions.RequestException, ValueError):
        dag = {}
    nodes = dag.get("nodes") or []
    if not nodes:
        yield "⚠️ No steps found for this job yet."; return
    icons = {"done": "✅", "skipped": "⏭", "failed": "❌", "blocked": "⛔",
             "running": "🔄", "pending": "⚪", "presented": "⚪",
             "awaiting_input": "⚪"}
    done = sum(1 for n in nodes if n.get("status") == "done")
    lines = [f"### 🗺 The plan — {len(nodes)} step(s), {done} done\n"]
    for i, n in enumerate(nodes, 1):
        nk = n.get("node_key")
        icon = icons.get(n.get("status", ""), "⚪")
        title = n.get("title", "(untitled)")
        if nk and nk == current:
            lines.append(f"{i}. 👉 {icon} **{title}** ← you're here")
        else:
            lines.append(f"{i}. {icon} {title}")
    lines.append(
        "\n_Say **\"next\"** to work the current step, **\"you do the rest\"** to "
        "hand off, or ask me about any step._"
    )
    yield "\n".join(lines)


# §17.677 — a pending note-replan proposal is resolved by a plain yes/no. Kept
# deterministic (no LLM) so the confirm turn is cheap and unambiguous.
_REPLAN_YES = {
    "yes", "y", "yeah", "yep", "yup", "apply", "apply it", "apply them",
    "do it", "go ahead", "sure", "ok", "okay", "confirm", "confirmed",
    "make the changes", "update the plan", "fix the plan", "sounds good",
}
_REPLAN_NO = {
    "no", "n", "nope", "nah", "cancel", "discard", "skip it", "leave it",
    "leave it alone", "keep it", "keep as is", "don't", "dont", "no thanks",
    "never mind", "nevermind", "don't change it", "dont change it",
}


# §17.692 — smart-punctuation normalization. Phones / macOS auto-correct
# straight quotes to CURLY ones ("can't" → "can’t", U+2019), and the operator
# types on such a device. Every deterministic gate below matches straight
# apostrophes only (`can'?t`), so "can’t we just wipe it" silently missed the
# pivot detector (§17.691) and re-rendered the stale step — the recurring
# "it won't pivot" bug. Fold curly quotes / dashes / ellipsis to their ASCII
# forms up front so ALL the deterministic matchers (pivot, confirm, replan
# yes/no, fast-verb) see a normal apostrophe. Semantics are unchanged.
_SMART_PUNCT = str.maketrans({
    "’": "'", "‘": "'",   # ’ ‘  curly single quotes
    "“": '"', "”": '"',   # “ ”  curly double quotes
    "–": "-", "—": "-",   # – —  en / em dash
    "…": "...",                 # …    ellipsis
    " ": " ",                   #      non-breaking space
})


def _normalize_punct(s: str) -> str:
    return s.translate(_SMART_PUNCT) if s else s


def _replan_decision(msg: str) -> str | None:
    """'apply' / 'discard' for a bare yes/no, else None."""
    norm = _normalize_punct(msg or "").strip().lower().strip(".!?,;: ").strip()
    if norm in _REPLAN_YES:
        return "apply"
    if norm in _REPLAN_NO:
        return "discard"
    return None


def fetch_pending_replan(pipe, session_id: str) -> dict | None:
    """GET /assist/{sid}/replan → the pending proposal, or None. Fail-soft."""
    try:
        r = _ss(pipe).get(
            f"{pipe.valves.orchestrator_url}/assist/{session_id}/replan",
            headers=pipe._auth_headers(),
            timeout=pipe.valves.request_timeout,
        )
        if r.status_code < 400:
            d = r.json()
            if isinstance(d, dict):
                return d.get("pending")
    except (requests.exceptions.RequestException, ValueError):
        return None
    return None


def assist_replan_confirm(
    pipe, session_id: str, decision: str,
) -> Generator[str, None, None]:
    """POST /assist/{sid}/replan/apply and render the outcome."""
    try:
        r = _ss(pipe).post(
            f"{pipe.valves.orchestrator_url}/assist/{session_id}/replan/apply",
            json={"decision": decision},
            headers=pipe._auth_headers(),
            timeout=pipe.valves.request_timeout,
        )
    except requests.exceptions.RequestException as e:
        yield f"❌ Connection error: {e}"; return
    if r.status_code >= 400:
        yield f"❌ HTTP {r.status_code}: {r.text[:200]}"; return
    try:
        d = r.json()
    except ValueError:
        d = {}
    if decision == "discard" or not (isinstance(d, dict) and d.get("applied")):
        yield ("👍 Left the plan unchanged — the note is still recorded and "
               "carried forward. Say _\"next\"_ to continue.")
        return
    revised = d.get("revised") or []
    dropped = d.get("dropped") or []
    parts = ["✅ Plan updated."]
    if revised:
        parts.append(
            f"Revised {', '.join(revised)} (re-rendered when you next view "
            f"{'them' if len(revised) != 1 else 'it'})."
        )
    if dropped:
        parts.append(f"Dropped {', '.join(dropped)}.")
    if not revised and not dropped:
        parts.append("No pending steps needed changing after all.")
    parts.append("Say _\"next\"_ to continue.")
    yield " ".join(parts)


# §17.679 — a PIVOT / redirection message: the operator is changing direction
# or reshaping the plan, not asking about the current step. The LLM classifier
# routes some pivots to `note` (→ §17.677 re-plan, correct) but drops others to
# `question` (→ a bare re-render of the current step — the recurring "it repeated
# something now irrelevant" bug). This deterministic detector is the reliable
# backstop: a clear pivot is ALWAYS routed through the note→re-plan path so the
# engine surfaces which pending steps the pivot changes and offers to fix them,
# instead of repeating a now-stale step. Markers are specific to genuine
# direction/scope changes (not format nitpicks like "make the subject shorter").
_PIVOT_RE = re.compile(
    r"(^\s*actually\b)"                                        # opens with "actually"
    r"|\b(on second thought|scratch that|never ?mind|"
    r"changed? my mind|change of plan|different (direction|approach)|"
    r"start over|do it differently)\b"
    r"|\b(forget|drop|ditch|ignore|scrap) (the|that|this|all|everything|about|my)\b"
    r"|\b(switch|change|pivot|redo)\s+(it|this|them|the\s+\w+|everything|to|over to)\b"
    r"|\bmake (it|this|them|the whole \w+)\b.{0,60}\binstead\b"
    r"|\b(rather than|instead of)\s+\w+"
    r"|\b\w+\s+instead\b"                                      # "... do X instead"
    r"|\bno longer\b",
    re.IGNORECASE,
)
# A change phrased as applying to the WHOLE deliverable (not one step) is also a
# plan-reshaping pivot — it must fan out to every affected step, not re-render one.
_GLOBAL_CHANGE_RE = re.compile(
    r"\b(throughout|everywhere|across (all|the board)|"
    r"all (the )?(steps|emails|sections|parts|pages)|"
    r"every (step|email|section|part|page)|globally|"
    r"the (whole|entire) (thing|sequence|plan|project|document)|overall)\b",
    re.IGNORECASE,
)
# §17.691 — QUESTION-FRAMED pivots. The operator proposes a simpler / different
# approach as a QUESTION rather than a declaration ("can't I just erase the old
# containers and start fresh?", "why not just do it over the network?", "isn't
# it easier to clean the existing install?", "do I even need the USB step?").
# _PIVOT_RE only caught declarative pivots ("do X instead", "switch to Y"), so
# these fell to the `question` fallback and re-rendered the now-stale step (the
# reported "it wouldn't pivot from my references / instructions mid-assist"
# bug — the plan assumed a bare-metal reinstall, but the operator already had a
# working, reachable Proxmox and wanted to just clean it). Anchored on "just" /
# comparative / "need to" so a plain clarifying question ("what does step 2
# mean?") is NOT swept in. Only reached when the classifier already deemed the
# turn a `question`, so the blast radius is narrow.
_QUESTION_PIVOT_RE = re.compile(
    r"\b(?:can'?t|cant|could'?nt|couldn'?t|couldnt)\s+(?:i|we|you)\s+just\b"          # "can't I just <X>"
    r"|\bwhy\s+(?:not|don'?t|dont|do\s+not|can'?t|cant|wouldn'?t|shouldn'?t)\s+"
    r"(?:i\s+|we\s+|you\s+)?just\b"                                                    # "why not/don't we just"
    r"|\bwhy\s+not\s+just\b"
    r"|\b(?:isn'?t|wouldn'?t|won'?t)\s+it\s+(?:be\s+)?"
    r"(?:easier|simpler|better|faster|quicker|cleaner|nicer|safer|more\s+\w+)\b"       # "isn't it easier to"
    r"|\bdo\s+(?:i|we)\s+(?:(?:really|even|actually)\s+need\b|need\s+to\b)"            # "do I even need <X>" / "do I need to"
    r"|\bis\s+there\s+(?:any\s+)?(?:need|reason|point)\s+(?:to|in)\b",                 # "is there any need to"
    re.IGNORECASE,
)


def _looks_like_pivot(msg: str) -> bool:
    """§17.679/§17.691 — True when `msg` changes direction / reshapes the plan
    (as opposed to asking about, or refining, the current step). Deterministic
    (no LLM). Covers declarative pivots (_PIVOT_RE), whole-deliverable changes
    (_GLOBAL_CHANGE_RE), and question-framed alternatives (_QUESTION_PIVOT_RE).
    §17.692 — normalizes smart apostrophes first so "can’t we just" matches."""
    if not msg:
        return False
    msg = _normalize_punct(msg)
    return (bool(_PIVOT_RE.search(msg))
            or bool(_GLOBAL_CHANGE_RE.search(msg))
            or bool(_QUESTION_PIVOT_RE.search(msg)))


def _pivot_kind(msg: str) -> str:
    """A whole-deliverable change is a `preference` (fan out to all steps); a
    directional change is a `decision`. Both are plan-affecting → §17.677 runs."""
    return "preference" if _GLOBAL_CHANGE_RE.search(msg or "") else "decision"


# §17.705 — a pasted shell prompt line (root@pve:~# …). Anchored to the START of
# a line so an email-like `user@host` in prose doesn't match. Same shape as the
# server-side `_SHELL_PROMPT_RE` (assist_agent) that captures execution context.
_SHELL_PROMPT_LINE_RE = re.compile(
    r"(?m)^\s*[A-Za-z_][\w.-]*@[\w.-]+:[^\n#$]*[#$]"
)


def _looks_like_shell_evidence(msg: str) -> bool:
    """§17.705 — deterministic: is this message a pasted shell transcript the
    operator is reporting as the current step's RESULT (not a question)?

    Anchored on a real prompt line (`root@pve:~# …`) — the strongest, lowest-
    false-positive signal, and the same one the server uses to learn the
    execution context. This exists because the LLM turn-classifier misroutes a
    raw paste (it read the operator's Proxmox audit output as a `question`/`ask`
    and re-rendered the step, so the paste was never recorded and the operator's
    root@pve context was never captured — §17.679's lesson: prefer a
    deterministic gate over re-tuning the classifier).

    Conservative: if the paste ENDS on a question ("…here's the output, but which
    pool?") it's left to the classifier — that's a genuine ask/fix, not a plain
    submit."""
    if not msg or not _SHELL_PROMPT_LINE_RE.search(msg):
        return False
    lines = [ln for ln in msg.strip().splitlines() if ln.strip()]
    if lines and lines[-1].rstrip().endswith("?"):
        return False
    return True


# §17.707 — the operator asking what inputs/decisions the plan still needs from
# them. High-precision phrasing ("from me" / "to provide|decide" / "checklist")
# so a plain step question ("what do I need to do here?") is NOT swept in.
_CHECKLIST_RE = re.compile(
    r"\bcheck\s?list\b"
    r"|\bwhat\s+(?:do|does)\s+(?:you|it)\s+(?:still\s+)?need\s+(?:from\s+me|to\s+know)\b"
    r"|\bwhat\s+(?:else\s+)?do\s+(?:you|i)\s+need\s+to\s+(?:provide|decide|give|tell)\b"
    r"|\bwhat(?:'?s|\s+is|\s+are)\s+(?:still\s+)?(?:left|outstanding|remaining|needed)\s+(?:from|for)\s+me\b"
    r"|\bwhat\s+(?:inputs?|information|info|decisions?)\s+(?:do\s+you|are)\b",
    re.IGNORECASE,
)


def _looks_like_checklist_request(msg: str) -> bool:
    return bool(msg) and bool(_CHECKLIST_RE.search(_normalize_punct(msg)))


# §17.689 — deterministic backstop: a confirmation of a proposed decision the
# classifier read as a bare question still routes to submit (→ deliberation
# resolves + commits). Confirmations only — a made choice like "3 vlans" already
# classifies as submit; this catches the "looks good"/"yes" reply to a proposal.
_DECISION_CONFIRM_RE = re.compile(
    r"^\s*(?:"
    r"looks?\s+good|sounds?\s+good|that\s+works|works\s+for\s+me|"
    r"go\s+with\s+(?:that|it|those|this)|use\s+(?:that|those|this)|"
    r"perfect|confirm(?:ed|\s+it)?|lock\s+it\s+in|that'?s\s+(?:the\s+plan|it|right)|"
    r"approved?|do\s+that|yep|yeah|yes|correct|agreed?|great|ok(?:ay)?)\b",
    re.I,
)


def _looks_like_decision_confirm(msg: str) -> bool:
    return bool(msg) and bool(_DECISION_CONFIRM_RE.search(_normalize_punct(msg)))


def _word_count(msg: str) -> int:
    return len((msg or "").split())


def reroute_check(pipe, session_id: str, msg: str) -> list | None:
    """§17.693 — POST /assist/{sid}/reroute. Returns the affected-steps proposal
    list when the operator's message reshapes the plan (a reference to their real
    situation that invalidates pending steps), else None. Fail-soft — a hiccup
    must never block the turn (the caller just proceeds with skip/question)."""
    try:
        r = _ss(pipe).post(
            f"{pipe.valves.orchestrator_url}/assist/{session_id}/reroute",
            json={"message": msg},
            headers=pipe._auth_headers(),
            timeout=getattr(pipe.valves, "assist_guide_timeout", 180),
        )
        if r.status_code < 400:
            d = r.json()
            if isinstance(d, dict) and d.get("has_impact"):
                return (d.get("proposal") or {}).get("proposals") or None
    except (requests.exceptions.RequestException, ValueError) as e:
        pipe.logger.debug("assist reroute check failed: %s", e)
    return None


def assist_nl_turn(
    pipe, session_id: str, msg: str, *,
    node_key: str | None = None, chat_id: str | None = None,
    history: list[dict] | None = None,
) -> Generator[str, None, None]:
    """§17.626/§17.627 — route a plain-language message in an ACTIVE assist
    session to the right engine component.

    Fast-path the obvious verbs (no LLM); classify the rest via /interpret; then
    route: advance/skip/submit/fix/finalize/pause + handoff (autonomous executor,
    which brings RAG grounding, sim tools, sandbox + verifier), ask (RAG/web
    research), status/explain_plan (the DAG), set_env/set_verbosity (environment
    capture). Falls back to the step-guidance turn for questions/refinements.
    Slash commands bypass this entirely (dispatched earlier)."""
    # §17.692 — fold smart quotes/dashes to ASCII up front so every deterministic
    # gate (pivot, confirm, replan yes/no, fast-verb) sees a normal apostrophe.
    # The operator's device auto-curls "can't" → "can’t", which silently broke
    # pivot detection (§17.691) and re-rendered the stale step.
    msg = _normalize_punct(msg or "")
    # §17.710a — lossless capture: record the raw message BEFORE any classifier
    # or fast-verb routing, so the transcript never depends on the intent being
    # read correctly. Fire-and-forget; server no-ops unless the capture valve is
    # on (slash/curl paths are captured server-side at their endpoints instead).
    record_turn_bg(pipe, session_id, msg, kind="message", node_key=node_key)
    # §17.677 — a bare yes/no resolves a pending note-triggered plan fix before
    # any other classification. Only pays the GET when the message *looks* like a
    # confirm (deterministic phrase match), so normal turns are unaffected.
    decision = _replan_decision(msg)
    if decision and fetch_pending_replan(pipe, session_id):
        yield from assist_replan_confirm(pipe, session_id, decision)
        return
    intent = fast_classify_turn(msg)
    evidence, error_text, query, note_text, note_kind = "", "", "", "", "note"
    is_collect = False
    # §17.705 — deterministic pre-empt: a pasted shell transcript IS the operator
    # reporting this step's result. Recognize it BEFORE the (unreliable) LLM
    # classifier so it is always recorded as evidence — and so the submit path
    # captures/keeps the root@pve execution context (§17.703) and learns concrete
    # values from the output for later steps (§17.490). The reported failure was
    # exactly this: a Proxmox audit paste read as a `question` and re-rendered,
    # so nothing was tracked. §17.679 lesson: deterministic gate over the LLM.
    if intent is None and _looks_like_shell_evidence(msg):
        intent = "submit"
        evidence = msg.strip()
    # §17.707 — "what do you need from me?" → the live operator-input checklist.
    if intent is None and _looks_like_checklist_request(msg):
        yield from assist_checklist_cmd(pipe, session_id); return
    if intent is None:
        d = assist_interpret(pipe, session_id, msg, node_key=node_key, history=history)
        intent = d.get("intent") or "question"
        evidence = d.get("evidence") or ""
        error_text = d.get("error_text") or ""
        query = d.get("query") or ""
        note_text = d.get("note_text") or ""
        note_kind = d.get("note_kind") or "note"
        node_key = d.get("node_key") or node_key
        # §17.690 — is_collect covers decision AND gather steps (both deliberate).
        is_collect = bool(d.get("is_collect") or d.get("is_decision"))
    # §17.689/§17.690/§17.692 — on a COLLECT step (decision or gather), a
    # substantive turn the classifier read as a bare `question` is really the
    # operator WORKING the decision: a confirmation ("looks good"), a refinement
    # ("can we make the port random?"), a partial answer, or a clarification.
    # Route it to submit so the server-side deliberation incorporates it (adjust
    # the proposal / list what's missing / resolve) with the accumulated context,
    # instead of re-rendering the step and losing that context. A PIVOT is the
    # one exception — it must reshape the PLAN, so it falls through to the
    # note→re-plan gate below (§17.679/§17.691). Genuine external-lookup
    # questions classify as `ask` (→ research) and never reach here.
    if is_collect and intent == "question" and not _looks_like_pivot(msg):
        intent = "submit"

    # §17.693 — semantic pivot / re-plan detection. A `skip` or `question` here
    # may be a pivot the classifier MIS-ROUTED: a reference to the operator's
    # ACTUAL situation that invalidates PENDING steps ("I already have Proxmox
    # installed, we only need to remove the old containers and start new") —
    # lexically unremarkable, so no phrase gate catches it and a bare skip marches
    # the plan on with now-irrelevant steps. Cheap regex first (obvious pivots);
    # then a reliable impact check (the §17.677 analyzer) for substantive turns
    # the regex missed. Either surfaces a re-plan instead of skipping/re-rendering.
    if intent in ("skip", "question"):
        if _looks_like_pivot(msg):
            yield from assist_note_cmd(
                pipe, session_id, msg.strip(), kind=_pivot_kind(msg), node_key=node_key,
            )
            return
        if _word_count(msg) >= getattr(pipe.valves, "assist_pivot_min_words", 6):
            affected = reroute_check(pipe, session_id, msg)
            if affected:
                yield "🔀 Based on what you told me, this changes the plan.\n\n"
                yield _render_replan_surface(affected)
                return
        # not a pivot → fall through to the normal skip / question handling below

    if intent == "advance":
        yield from assist_next(pipe, session_id, chat_id=chat_id); return
    if intent == "pause":
        yield from assist_simple_post(pipe, session_id, "pause"); return
    if intent == "finalize":
        yield from assist_done(pipe, session_id, chat_id=chat_id); return
    if intent == "status":
        yield from assist_status(pipe, session_id); return
    if intent == "explain_plan":
        yield from assist_plan(pipe, session_id, chat_id=chat_id); return
    if intent == "skip":
        nk = _recall_node_key(pipe, chat_id, node_key)
        if not nk:
            yield ("Which step should I skip? Say _\"next\"_ to pull up the "
                   "current one first."); return
        yield from assist_skip(pipe, session_id, nk, chat_id=chat_id); return
    if intent == "submit":
        nk = _recall_node_key(pipe, chat_id, node_key)
        if not nk:
            # No step claimed yet — pull the next one instead of a dead-end.
            yield from assist_next(pipe, session_id, chat_id=chat_id); return
        ev = (evidence or msg).strip() or "Operator confirmed this step is complete."
        # §17.689/§17.690 — on a collect step (decision or gather) the server may
        # deliberate (assemble the deliverable across turns) rather than commit
        # outright, so use a neutral banner instead of "recording what you did".
        yield ("_🤔 Working through this step…_\n\n" if is_collect
               else "_📝 Recording what you did for this step…_\n\n")
        yield from assist_submit(
            pipe, session_id, nk, ev, chat_id=chat_id, history=history,
        ); return
    if intent == "fix":
        nk = _recall_node_key(pipe, chat_id, node_key)
        yield "_🔧 Sounds like something went wrong — let me help…_\n\n"
        yield from assist_fix_cmd(
            pipe, session_id, (error_text or msg), node_key=nk, chat_id=chat_id,
            history=history,
        ); return
    if intent == "handoff":
        nk = _recall_node_key(pipe, chat_id, node_key)
        if not nk:
            yield ("Which step should I take? Say _\"next\"_ first, then _\"you "
                   "do this one\"_."); return
        yield from assist_handoff(pipe, session_id, nk, _handoff_mode_from_message(msg))
        return
    if intent == "ask":
        nk = _recall_node_key(pipe, chat_id, node_key)
        yield from assist_research_cmd(
            pipe, session_id, (query or msg).strip(), node_key=nk, chat_id=chat_id,
            history=history,
        ); return
    if intent == "note":
        # §17.654 — capture a new requirement/constraint/decision and confirm it
        # back. It feeds forward into every later step's guidance context.
        nk = _recall_node_key(pipe, chat_id, node_key)
        yield from assist_note_cmd(
            pipe, session_id, (note_text or msg).strip(), kind=note_kind, node_key=nk,
        ); return
    if intent == "set_env":
        subs = dict(re.findall(r"([A-Za-z_]\w*)=(\S+)", msg))
        profile = re.sub(r"[A-Za-z_]\w*=\S+", "", msg).strip(" ,\t") or None
        yield from assist_env_cmd(
            pipe, session_id, profile=profile, substitutions=subs or None,
            chat_id=chat_id,
        ); return
    if intent == "set_verbosity":
        yield from assist_env_cmd(
            pipe, session_id, verbosity=_verbosity_from_message(msg), chat_id=chat_id,
        ); return
    # question (default) — the existing guide/refine turn. Pivots (regex or the
    # §17.693 semantic impact check) already returned above, so anything here is
    # a genuine question/refinement about the current step.
    yield from assist_chat_turn(
        pipe, session_id, msg, node_key=node_key, chat_id=chat_id, history=history,
    )


# ---------------------------------------------------------------------------
# §17.626 — natural-language START. When there's no active session and the
# message reads as assist intent, map it to an existing assistable job.
# ---------------------------------------------------------------------------

_START_STOPWORDS = {
    "the", "and", "for", "with", "you", "your", "our", "can", "please", "help",
    "assist", "let", "lets", "want", "need", "would", "like", "get", "got",
    "set", "setup", "install", "installation", "configure", "configuration",
    "deploy", "build", "run", "running", "implement", "finish", "complete",
    "step", "through", "walk", "using", "use", "job", "task", "project", "this",
    "that", "into", "onto", "over", "from", "out", "new", "make", "start",
    "work", "working", "system", "server", "one", "some", "any", "all",
}


def _start_tokens(s: str) -> set:
    return {
        w for w in re.findall(r"[a-z0-9]+", (s or "").lower())
        if len(w) > 2 and w not in _START_STOPWORDS
    }


def match_assist_candidate(msg: str, candidates: list, *, min_score: int = 2) -> tuple:
    """Score `candidates` (each {job_id,title,status,...}) against `msg` by
    distinctive-token overlap. Returns ``(best_candidate_or_None, ambiguous)``:

    - ``(None, False)``  — no signal (best overlap is 0); caller falls through
      to planning/triage instead of hijacking a new-idea message.
    - ``(cand, False)``  — one confident, unique match (start it).
    - ``(cand, True)``   — a weak or tied match (offer the list to choose).

    ``min_score`` is the confidence bar for a non-ambiguous match (default 2
    distinctive shared tokens). The disambiguation follow-up (§17.627) lowers it
    to 1: once a pick-list is already on screen, a unique single-token match
    ("the proxmox one") is enough to start.
    """
    mt = _start_tokens(msg)
    if not mt or not candidates:
        return None, False
    scored = sorted(
        ((len(mt & _start_tokens(c.get("title", ""))), c) for c in candidates),
        key=lambda x: x[0], reverse=True,
    )
    best_score, best = scored[0]
    second_score = scored[1][0] if len(scored) > 1 else 0
    if best_score == 0:
        return None, False
    ambiguous = best_score < min_score or second_score >= best_score
    return best, ambiguous


_ORDINAL_WORDS = {
    "first": 0, "1st": 0, "second": 1, "2nd": 1, "third": 2, "3rd": 2,
    "fourth": 3, "4th": 3, "fifth": 4, "5th": 4,
}


def resolve_candidate_pick(pipe, msg: str, pending_ids: list) -> str | None:
    """§17.627 — map a short selector reply to one of the pending candidate
    job_ids after a pick-list was shown, or None. Handles positional ("1",
    "the second one", "last") and name ("the proxmox one") selectors."""
    if not pending_ids:
        return None
    norm = (msg or "").strip().lower().strip(".!?,;: ")
    m = re.fullmatch(r"(?:number\s*|option\s*|#\s*)?(\d{1,2})", norm)
    if m:
        idx = int(m.group(1)) - 1
        return pending_ids[idx] if 0 <= idx < len(pending_ids) else None
    if norm in ("last", "the last one", "last one"):
        return pending_ids[-1]
    words = set(norm.split())
    for word, idx in _ORDINAL_WORDS.items():
        if word in words:
            return pending_ids[idx] if idx < len(pending_ids) else None
    # Name selector — restrict live candidates to the pending set and take a
    # unique single-token match (lower bar: we're already disambiguating).
    pend = set(pending_ids)
    cands = [c for c in fetch_assist_candidates(pipe) if c.get("job_id") in pend]
    if not cands:
        return None
    match, ambiguous = match_assist_candidate(msg, cands, min_score=1)
    return match.get("job_id") if (match and not ambiguous) else None


def fetch_assist_candidates(pipe) -> list:
    """GET /assist/candidates → list (fail-soft → []).

    §17.681 — always requests ``in_progress=true``: every live caller here is an
    AUTOMATIC continuity surface (reconnect / banner / pick-list follow-up), and
    those must never resurface a terminal (completed/cancelled) job on a bare
    "continue" or topic match. Deliberate terminal re-opens go through the
    explicit `/assist <job_id>` path, which doesn't touch this helper."""
    try:
        r = _ss(pipe).get(
            f"{pipe.valves.orchestrator_url}/assist/candidates",
            params={"in_progress": "true"},
            headers=pipe._auth_headers(),
            timeout=pipe.valves.request_timeout,
        )
        if r.status_code < 400:
            d = r.json()
            if isinstance(d, dict):
                return d.get("candidates") or []
    except (requests.exceptions.RequestException, ValueError) as e:
        pipe.logger.debug("fetch_assist_candidates failed: %s", e)
    return []


def render_candidate_list(candidates: list) -> str:
    """Offer the assistable jobs to choose from (ambiguous natural-start)."""
    lines = [
        "🤝 **I can walk you through one of these, step by step — which job?**",
        "",
    ]
    for c in candidates[:8]:
        lines.append(
            f"- **{c.get('title', '(untitled)')}** — `{c.get('status', '?')}` · "
            f"start with `/assist {c.get('job_id', '')}`"
        )
    if len(candidates) > 8:
        lines.append(f"- …and {len(candidates) - 8} more (`/here` lists all).")
    lines += [
        "",
        "_Just tell me which — the **number**, the **name** (\"the proxmox one\"), "
        "or paste its `/assist <id>`. Want to plan something new instead? Just "
        "describe it._",
    ]
    # §17.627 — hidden ordered-id marker so a short selector reply on the next
    # turn ("1", "the proxmox one") maps back to a job. §17.660 — a markdown
    # REFERENCE-LINK DEFINITION, not an HTML comment: OWUI's markdown renderer
    # shows `<!--…-->` as visible literal text (verified in-browser) but renders
    # an unused `[label]: dest` definition as NOTHING, while both survive in the
    # raw history OWUI replays. Payload base64url so the ids stay in the
    # (invisible) destination; `ASSIST_PICK:` token kept for grep/recovery.
    ids = ",".join(c.get("job_id", "") for c in candidates[:8])
    enc = base64.urlsafe_b64encode(ids.encode()).decode().rstrip("=")
    lines.append(f"\n\n[apick]: ASSIST_PICK:{enc}")
    return "\n".join(lines)


def try_natural_start(pipe, msg: str, chat_id: str | None):
    """§17.626 — attempt to START an assist session from a natural sentence.

    Returns a generator (start stream or candidate list) when it handles the
    message, or ``None`` to signal 'not an existing job — fall through to
    planning/triage'. Kept as a plain function (not a generator) so the caller
    can make the start-vs-list-vs-fallthrough decision before yielding."""
    candidates = fetch_assist_candidates(pipe)
    if not candidates:
        return None
    match, ambiguous = match_assist_candidate(msg, candidates)
    if match is None:
        return None  # no job matched — this is a new idea, let triage handle it.
    if not ambiguous:
        return assist_start(pipe, match["job_id"], chat_id=chat_id)
    return iter([render_candidate_list(candidates)])


def assist_research_cmd(
    pipe, session_id: str, question: str, *,
    node_key: str | None = None, chat_id: str | None = None,
    history: list[dict] | None = None,
) -> Generator[str, None, None]:
    """§17.486 — POST /assist/{sid}/research and render cited results."""
    try:
        r = _ss(pipe).post(
            f"{pipe.valves.orchestrator_url}/assist/{session_id}/research",
            json={"question": question, "node_key": node_key,
                  "history": history or []},
            headers=pipe._auth_headers(),
            timeout=getattr(pipe.valves, "assist_guide_timeout", 180),
        )
    except requests.exceptions.RequestException as e:
        yield f"❌ Connection error: {e}"; return
    if r.status_code >= 400:
        yield f"❌ HTTP {r.status_code}: {r.text[:200]}"; return
    try:
        d = r.json()
    except ValueError as e:
        yield f"❌ Assist research: orchestrator returned non-JSON body ({e}); raw: {r.text[:200]}"; return
    if not isinstance(d, dict):
        yield f"❌ Assist research: orchestrator reply not a dict; raw: {str(d)[:200]}"; return
    yield render_research(d)


def assist_env_cmd(
    pipe, session_id: str, *, profile: str | None = None,
    substitutions: dict | None = None, verbosity: str | None = None,
    show: bool = False, chat_id: str | None = None,
) -> Generator[str, None, None]:
    """§17.487 — GET/PUT the session's operator environment (+ §17.499 verbosity)."""
    base = f"{pipe.valves.orchestrator_url}/assist/{session_id}/env"
    try:
        if show:
            r = _ss(pipe).get(base, headers=pipe._auth_headers(),
                          timeout=pipe.valves.request_timeout)
        else:
            r = _ss(pipe).put(
                base,
                json={"profile": profile, "substitutions": substitutions or {},
                      "verbosity": verbosity},
                headers=pipe._auth_headers(),
                timeout=pipe.valves.request_timeout,
            )
    except requests.exceptions.RequestException as e:
        yield f"❌ Connection error: {e}"; return
    if r.status_code == 404:
        yield f"❌ Session `{session_id}` not found."; return
    if r.status_code >= 400:
        yield f"❌ HTTP {r.status_code}: {r.text[:200]}"; return
    try:
        d = r.json()
    except ValueError as e:
        yield f"❌ Assist env: orchestrator returned non-JSON body ({e}); raw: {r.text[:200]}"; return
    if not isinstance(d, dict):
        yield f"❌ Assist env: orchestrator reply not a dict; raw: {str(d)[:200]}"; return
    env_block = render_environment(d.get("environment"))
    if show:
        yield env_block
        return
    yield f"✅ Environment updated.\n\n{env_block}"
    # §17.706 — apply it immediately: re-render the CURRENT step so its commands
    # honor the just-changed environment. Previously the cached step stayed
    # stale, so an operator who stated "root@pve via the web console" kept seeing
    # hedged, generic guidance ("SSH or web shell / open a terminal") — the
    # reported "it couldn't tell I was on the Proxmox web console". force=True
    # bypasses the guidance cache; the one-time research pause is now visible
    # (§17.704). Only when a live step is resolvable from this chat — on the
    # curl/CLI path (no chat_id) there's nothing to recall, so it just confirms.
    nk = _recall_node_key(pipe, chat_id, None)
    if nk:
        yield "\n\n---\n\n_Applying that to this step…_\n"
        yield from assist_guide_stream_cmd(
            pipe, session_id, node_key=nk, force=True, chat_id=chat_id,
        )


def assist_fix_cmd(
    pipe, session_id: str, error_text: str, *,
    node_key: str | None = None, chat_id: str | None = None,
    history: list[dict] | None = None,
) -> Generator[str, None, None]:
    """§17.487 — POST /assist/{sid}/fix and render the diagnosis + fix."""
    try:
        r = _ss(pipe).post(
            f"{pipe.valves.orchestrator_url}/assist/{session_id}/fix",
            json={"error": error_text, "node_key": node_key,
                  "history": history or []},
            headers=pipe._auth_headers(),
            timeout=getattr(pipe.valves, "assist_guide_timeout", 180),
        )
    except requests.exceptions.RequestException as e:
        yield f"❌ Connection error: {e}"; return
    if r.status_code >= 400:
        yield f"❌ HTTP {r.status_code}: {r.text[:200]}"; return
    try:
        d = r.json()
    except ValueError as e:
        yield f"❌ Assist fix: orchestrator returned non-JSON body ({e}); raw: {r.text[:200]}"; return
    if not isinstance(d, dict):
        yield f"❌ Assist fix: orchestrator reply not a dict; raw: {str(d)[:200]}"; return
    yield render_fix(d)


def assist_simple_post(
    pipe, session_id: str, action: str,
) -> Generator[str, None, None]:
    try:
        r = _ss(pipe).post(
            f"{pipe.valves.orchestrator_url}/assist/{session_id}/{action}",
            headers=pipe._auth_headers(),
            timeout=pipe.valves.request_timeout,
        )
    except requests.exceptions.RequestException as e:
        yield f"❌ Connection error: {e}"; return
    if r.status_code >= 400:
        yield f"❌ HTTP {r.status_code}: {r.text[:200]}"; return
    # §17.275 — mirror §17.259's _assist_start guards.
    try:
        d = r.json()
    except ValueError as e:
        yield f"❌ Assist {action}: orchestrator returned non-JSON body ({e}); raw: {r.text[:200]}"; return
    if not isinstance(d, dict):
        yield f"❌ Assist {action}: orchestrator reply not a dict; raw: {str(d)[:200]}"; return
    yield f"✅ Session `{session_id}` -> `{d.get('status', action)}`."


def assist_done(
    pipe, session_id: str, *, chat_id: str | None = None,
) -> Generator[str, None, None]:
    # Pull session, then job's compiled_output via /exec/status.
    try:
        r = _ss(pipe).get(
            f"{pipe.valves.orchestrator_url}/assist/{session_id}",
            headers=pipe._auth_headers(),
            timeout=pipe.valves.request_timeout,
        )
    except requests.exceptions.RequestException as e:
        yield f"❌ Connection error: {e}"; return
    if r.status_code == 404:
        yield f"❌ Session `{session_id}` not found."; return
    if r.status_code >= 400:
        yield f"❌ HTTP {r.status_code}: {r.text[:200]}"; return
    # §17.613 (audit #25) — guard the JSON decode like every sibling handler
    # (§17.268/275); a non-JSON 200 body would raise mid-yield (the §17.505
    # TransferEncodingError surface).
    try:
        sess = r.json()
    except ValueError as e:
        yield f"❌ Assist done: orchestrator returned non-JSON body ({e}); raw: {r.text[:200]}"; return
    if not isinstance(sess, dict):
        yield f"❌ Assist done: orchestrator reply not a dict; raw: {str(sess)[:200]}"; return
    # Clear chat memory when a user explicitly invokes /assist done on a
    # terminal session — the next /assist <job_id> in this chat starts
    # cleanly. Pause/resume intentionally do NOT forget; user expects
    # mid-session pause to round-trip.
    if sess.get("status") in ("completed", "abandoned", "cancelled"):
        assist_forget(pipe, chat_id)
    job_id = sess.get("job_id")
    if not job_id:
        # §17.613 (audit #25) — avoid GET /exec/status/None when the session
        # has no job yet.
        yield "⚠️ Session has no associated job yet — no compiled output."; return
    try:
        r2 = _ss(pipe).get(
            f"{pipe.valves.orchestrator_url}/exec/status/{job_id}",
            headers=pipe._auth_headers(),
            timeout=pipe.valves.request_timeout,
        )
    except requests.exceptions.RequestException as e:
        yield f"❌ Connection error: {e}"; return
    if r2.status_code >= 400:
        yield f"⚠️ Compiled output not available (HTTP {r2.status_code})."; return
    try:
        d = r2.json()
    except ValueError as e:
        yield f"⚠️ Compiled output not available (non-JSON body: {e})."; return
    if not isinstance(d, dict):
        yield "⚠️ Compiled output not available (unexpected reply shape)."; return
    compiled = d.get("compiled_output") or "_(no compiled output yet)_"
    sess_status = sess.get("status")
    job_status = d.get("status", "?")
    # Reconciliation: session and job status come from two tables. They
    # can diverge if the assist branch left a step terminal while the
    # job stayed in an intermediate state. Surface the divergence so the
    # user sees an explicit cue rather than a confusing pairing.
    divergence = ""
    terminal_session = {"completed", "cancelled", "abandoned"}
    terminal_job = {"completed", "failed", "cancelled"}
    if sess_status in terminal_session and job_status not in terminal_job:
        divergence = (
            f"\n⚠️ Session is terminal (`{sess_status}`) but job is still "
            f"`{job_status}`. Run `/jobs` to inspect, or `/exec/retry` if "
            "a node needs another attempt.\n"
        )
    elif sess_status not in terminal_session and job_status in terminal_job:
        divergence = (
            f"\n⚠️ Job is terminal (`{job_status}`) but session is still "
            f"`{sess_status}`. Reload may be needed.\n"
        )
    yield (
        f"### Assist session `{session_id}` summary\n\n"
        f"- Status: `{sess_status}`\n"
        f"- Job: `{job_id}` → `{job_status}`\n"
        f"{divergence}"
        f"\n---\n\n## Compiled output\n\n{compiled}\n"
    )


def assist_friction(
    pipe, session_id: str, node_key: str, note: str,
) -> Generator[str, None, None]:
    try:
        r = _ss(pipe).post(
            f"{pipe.valves.orchestrator_url}/assist/{session_id}/friction",
            json={"node_key": node_key, "note": note},
            headers=pipe._auth_headers(),
            timeout=pipe.valves.request_timeout,
        )
    except requests.exceptions.RequestException as e:
        yield f"❌ Connection error: {e}"; return
    if r.status_code >= 400:
        yield f"❌ HTTP {r.status_code}: {r.text[:200]}"; return
    yield f"📝 Friction note recorded for `{node_key}` in session `{session_id}`."


def assist_note_cmd(
    pipe, session_id: str, note_text: str, *,
    kind: str = "note", node_key: str | None = None,
) -> Generator[str, None, None]:
    """§17.654 — record a session-level note/addition and confirm it back so the
    operator knows it landed. The note feeds forward into later steps' guidance."""
    if not (note_text or "").strip():
        yield "What should I note? Tell me the requirement, constraint, or decision to remember."
        return
    try:
        r = _ss(pipe).post(
            f"{pipe.valves.orchestrator_url}/assist/{session_id}/note",
            json={"text": note_text, "kind": kind, "node_key": node_key},
            headers=pipe._auth_headers(),
            timeout=pipe.valves.request_timeout,
        )
    except requests.exceptions.RequestException as e:
        yield f"❌ Connection error: {e}"; return
    if r.status_code >= 400:
        yield f"❌ HTTP {r.status_code}: {r.text[:200]}"; return
    label = kind if kind and kind != "note" else "note"
    # §17.677 — a plan-affecting note may come back with a proposed plan fix.
    proposal = None
    try:
        d = r.json()
        if isinstance(d, dict):
            proposal = d.get("replan_proposal")
    except ValueError:
        proposal = None
    yield f"📌 Noted ({label}): {note_text.strip()}\n\n"
    affected = (proposal or {}).get("proposals") if isinstance(proposal, dict) else None
    if not affected:
        yield "_I'll carry this forward into the remaining steps. Say _\"next\"_ to continue._"
        return
    yield _render_replan_surface(affected)


def _render_replan_surface(affected: list, *, lead: str | None = None) -> str:
    """§17.677/§17.693 — the surface-and-ask block: list the pending steps a
    plan change affects (drop/revise + why), then ask for a yes/no. Shared by the
    note path and the §17.693 semantic-pivot path so both read identically."""
    n = len([p for p in (affected or []) if isinstance(p, dict)])
    lines = [
        lead or (f"This affects **{n}** pending step{'s' if n != 1 else ''}:"),
        "",
    ]
    for p in (affected or []):
        if not isinstance(p, dict):
            continue
        nk = p.get("node_key", "?")
        act = "drop" if p.get("action") == "drop" else "revise"
        assumption = (p.get("current_assumption") or "").strip()
        change = (p.get("proposed_change") or "").strip()
        head = f"- **{nk}** ({act})"
        if assumption:
            head += f" — {assumption}"
        lines.append(head)
        if change:
            lines.append(f"    → {change}")
    lines += ["", "**Apply these plan changes?** (yes / no / edit)"]
    return "\n".join(lines)
