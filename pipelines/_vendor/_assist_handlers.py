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

import json
import queue as _q
import re
import sys
import threading as _th
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
    return (
        f"{re_shown}"
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
                    "verbose", "status"):
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
    *, chat_id: str | None = None,
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
    msg = f"✅ Step `{node_key}` committed. "
    if next_nk:
        msg += (f"Moving on to `{next_nk}`…" if auto_advance
                else f"Next: `{next_nk}`. Run `/assist next` to fetch.")
    else:
        msg += f"All steps terminal — run `/assist done` to view compiled output."
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
    # (verdict/outcome/ran computed above — reused by the auto-advance gate.)
    if outcome == "failed":
        head = ("🛑 **Ran your code in the sandbox — it errored.**" if ran
                else "⚠️ **This may have failed.**")
        msg += (
            f"\n\n{head} {verdict.get('reason', '')}\n"
            f"Run `/assist fix <the error>` or re-do and resubmit."
        )
    elif outcome == "succeeded" and ran:
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
) -> Generator[str, None, None]:
    """§17.493 — stream the walkthrough from /assist/{sid}/guide/stream.

    Consumes the SSE stream (assist_guide_delta* → assist_guide_done) on the
    same thread/queue/keepalive skeleton as the handoff consumer. Yields the
    content live; the destructive banner + sources footnote are appended on
    `done` (trailing — we don't know them until generation completes). A cache
    hit arrives as one delta + done(cached) and renders instantly."""
    url = f"{pipe.valves.orchestrator_url}/assist/{session_id}/guide/stream"
    body = {"node_key": node_key, "refine": refine, "research": research, "force": force}
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
        chat_id=chat_id,
    )


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
) -> dict:
    """POST /assist/{sid}/interpret → intent dict. Fail-soft → question so a
    classifier/endpoint hiccup degrades to the guide/refine turn."""
    fallback = {"intent": "question", "evidence": "", "error_text": "",
                "node_key": node_key}
    try:
        r = _ss(pipe).post(
            f"{pipe.valves.orchestrator_url}/assist/{session_id}/interpret",
            json={"message": message, "node_key": node_key},
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


def assist_nl_turn(
    pipe, session_id: str, msg: str, *,
    node_key: str | None = None, chat_id: str | None = None,
) -> Generator[str, None, None]:
    """§17.626/§17.627 — route a plain-language message in an ACTIVE assist
    session to the right engine component.

    Fast-path the obvious verbs (no LLM); classify the rest via /interpret; then
    route: advance/skip/submit/fix/finalize/pause + handoff (autonomous executor,
    which brings RAG grounding, sim tools, sandbox + verifier), ask (RAG/web
    research), status/explain_plan (the DAG), set_env/set_verbosity (environment
    capture). Falls back to the step-guidance turn for questions/refinements.
    Slash commands bypass this entirely (dispatched earlier)."""
    intent = fast_classify_turn(msg)
    evidence, error_text, query = "", "", ""
    if intent is None:
        d = assist_interpret(pipe, session_id, msg, node_key=node_key)
        intent = d.get("intent") or "question"
        evidence = d.get("evidence") or ""
        error_text = d.get("error_text") or ""
        query = d.get("query") or ""
        node_key = d.get("node_key") or node_key

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
        yield "_📝 Recording what you did for this step…_\n\n"
        yield from assist_submit(pipe, session_id, nk, ev, chat_id=chat_id); return
    if intent == "fix":
        nk = _recall_node_key(pipe, chat_id, node_key)
        yield "_🔧 Sounds like something went wrong — let me help…_\n\n"
        yield from assist_fix_cmd(
            pipe, session_id, (error_text or msg), node_key=nk, chat_id=chat_id,
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
    # question (default) — the existing guide/refine turn.
    yield from assist_chat_turn(pipe, session_id, msg, node_key=node_key, chat_id=chat_id)


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
    """GET /assist/candidates → list (fail-soft → [])."""
    try:
        r = _ss(pipe).get(
            f"{pipe.valves.orchestrator_url}/assist/candidates",
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
    # turn ("1", "the proxmox one") maps back to a job. HTML comment → invisible
    # in the rendered chat but preserved in the raw history OWUI replays.
    ids = ",".join(c.get("job_id", "") for c in candidates[:8])
    lines.append(f"\n<!--ASSIST_PICK:{ids}-->")
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
) -> Generator[str, None, None]:
    """§17.486 — POST /assist/{sid}/research and render cited results."""
    try:
        r = _ss(pipe).post(
            f"{pipe.valves.orchestrator_url}/assist/{session_id}/research",
            json={"question": question, "node_key": node_key},
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
    else:
        yield f"✅ Environment updated.\n\n{env_block}"


def assist_fix_cmd(
    pipe, session_id: str, error_text: str, *,
    node_key: str | None = None, chat_id: str | None = None,
) -> Generator[str, None, None]:
    """§17.487 — POST /assist/{sid}/fix and render the diagnosis + fix."""
    try:
        r = _ss(pipe).post(
            f"{pipe.valves.orchestrator_url}/assist/{session_id}/fix",
            json={"error": error_text, "node_key": node_key},
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
