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


# §17.296 — lazy globals accessor. Captures scaffold_router's module
# globals each call so any unittest patch lands here too without the
# vendor module needing to re-import after monkeypatch.
def _scaffold_router():
    return sys.modules["scaffold_router"]


def _ss():
    """The shared ``requests.Session`` from scaffold_router."""
    return _scaffold_router()._HTTP_SESSION


def _sse_events_const():
    """The ``_SSE`` vendor module reference (event-name constants)."""
    return _scaffold_router()._SSE


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
        _ss().put(
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
        r = _ss().get(
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
        _ss().delete(
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
    """Format a /assist/next response as markdown chat output.

    Prompts use the short (no-session-id) form since chat memory is on
    by default. The explicit `<session_id>` form still works — users
    in a different chat or with `assist_session_memory_enabled=false`
    should paste it; see `/assist help`.
    """
    if step.get("status") in ("completed", "abandoned", "cancelled"):
        return (
            f"✅ **Session `{step['session_id']}` is {step['status']}.** "
            f"Run `/assist done` to view the compiled output."
        )
    if not step.get("node_key"):
        counts = step.get("step_counts", {})
        counts_str = ", ".join(f"{k}={v}" for k, v in counts.items()) or "n/a"
        return (
            f"⏳ **No claimable step right now.**\n\n"
            f"Step roll-up: {counts_str}\n\n"
            f"Some steps may already be presented to you and waiting on submit. "
            f"Use `/assist next` again after you submit."
        )
    upstream = step.get("upstream_outputs") or {}
    upstream_block = ""
    if upstream:
        upstream_block = "**Upstream outputs:**\n\n"
        for nk, txt in upstream.items():
            preview = txt if len(txt) <= 800 else txt[:800] + f"\n… [{len(txt) - 800} more chars]"
            upstream_block += f"_{nk}:_\n```\n{preview}\n```\n\n"
    deps = step.get("depends_on") or []
    deps_str = ", ".join(deps) if deps else "(none)"
    return (
        f"### Step `{step['node_key']}` — {step.get('title', '?')}\n\n"
        f"**Tool:** `{step.get('tool', 'LLM')}`  |  "
        f"**Domain:** `{step.get('domain') or 'n/a'}`  |  "
        f"**Depends on:** {deps_str}\n\n"
        f"{upstream_block}"
        # §17.486 — the human-facing walkthrough (generated separately) is now
        # the primary content; the raw LLM task prompt is demoted to a
        # collapsed block for operators who want the underlying instruction.
        f"<details>\n<summary>Raw task prompt (underlying instruction)</summary>\n\n"
        f"```\n{step.get('base_prompt', '')}\n```\n\n</details>\n\n"
        f"**When done, submit your evidence:**\n\n"
        f"```\n"
        f"/assist submit\n"
        f"<paste your output here — newlines and indentation preserved>\n"
        f"```\n\n"
        f"_Code fences (```) are optional. Content after the command "
        f"line is captured as-is._\n"
    )


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
    out = f"## 🧭 How to do this step\n\n{d['guidance']}\n"
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
    out = f"## 🔧 Troubleshooting `{node_key}`\n\n{d['fix']}\n"
    meta = d.get("guidance_meta") or {}
    sources = meta.get("research_sources") or []
    if sources:
        cites = ", ".join(f"`{s.get('kind')}`: {s.get('query')}" for s in sources)
        out += f"\n_Confirmed via research — {cites}._\n"
    return out


def render_environment(env: dict | None) -> str:
    """§17.487 — show the session's operator environment."""
    env = env or {}
    profile = (env.get("profile") or "").strip()
    subs = env.get("substitutions") or {}
    if not profile and not subs:
        return (
            "_No environment set._ Set one so walkthroughs use concrete commands:\n"
            "`/assist env Ubuntu 24.04, apt, bash` or `/assist env HOST_IP=10.0.0.5`."
        )
    out = "**Operator environment**\n\n"
    if profile:
        out += f"- Profile: {profile}\n"
    for k, v in subs.items():
        out += f"- `{k}` = `{v}`\n"
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
        out += f"_[{i}] ({s.get('kind')})_\n```\n{preview}\n```\n\n"
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
                    "done", "friction", "guide", "research", "env", "fix"):
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
        yield from assist_guide_cmd(
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
    try:
        r = _ss().post(
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
    sid = d.get("session_id") if isinstance(d, dict) else None
    if not sid:
        yield f"❌ Assist start: orchestrator reply missing `session_id`; raw: {str(d)[:200]}"; return
    assist_remember(pipe, chat_id, session_id=sid)
    resp_job_id = d.get("job_id", job_id)
    pending = d.get("pending_steps", "?")
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
        r = _ss().get(
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
    if step.get("node_key") and getattr(pipe.valves, "assist_auto_guide", True):
        yield "\n_Generating walkthrough…_\n\n"
        yield from assist_guide_cmd(
            pipe, session_id, node_key=step["node_key"],
            research=getattr(pipe.valves, "assist_guide_research", True),
            force=False, chat_id=chat_id,
        )


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
        r = _ss().post(
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
    msg = f"✅ Step `{node_key}` committed. "
    if next_nk:
        msg += f"Next: `{next_nk}`. Run `/assist next` to fetch."
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
    verdict = d.get("success_verdict") or {}
    outcome = verdict.get("outcome")
    ran = "sandbox" in (verdict.get("grounded_by") or "")
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


def assist_skip(
    pipe, session_id: str, node_key: str, *, chat_id: str | None = None,
) -> Generator[str, None, None]:
    try:
        r = _ss().post(
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
    msg = f"⏭ Step `{node_key}` skipped. "
    if next_nk:
        msg += f"Next: `{next_nk}`."
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
    sse_const = _sse_events_const()
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
        r = _ss().post(
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


def assist_research_cmd(
    pipe, session_id: str, question: str, *,
    node_key: str | None = None, chat_id: str | None = None,
) -> Generator[str, None, None]:
    """§17.486 — POST /assist/{sid}/research and render cited results."""
    try:
        r = _ss().post(
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
    substitutions: dict | None = None, show: bool = False,
    chat_id: str | None = None,
) -> Generator[str, None, None]:
    """§17.487 — GET/PUT the session's operator environment."""
    base = f"{pipe.valves.orchestrator_url}/assist/{session_id}/env"
    try:
        if show:
            r = _ss().get(base, headers=pipe._auth_headers(),
                          timeout=pipe.valves.request_timeout)
        else:
            r = _ss().put(
                base,
                json={"profile": profile, "substitutions": substitutions or {}},
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
        r = _ss().post(
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
        r = _ss().post(
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
        r = _ss().get(
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
    sess = r.json()
    # Clear chat memory when a user explicitly invokes /assist done on a
    # terminal session — the next /assist <job_id> in this chat starts
    # cleanly. Pause/resume intentionally do NOT forget; user expects
    # mid-session pause to round-trip.
    if sess.get("status") in ("completed", "abandoned", "cancelled"):
        assist_forget(pipe, chat_id)
    job_id = sess.get("job_id")
    try:
        r2 = _ss().get(
            f"{pipe.valves.orchestrator_url}/exec/status/{job_id}",
            headers=pipe._auth_headers(),
            timeout=pipe.valves.request_timeout,
        )
    except requests.exceptions.RequestException as e:
        yield f"❌ Connection error: {e}"; return
    if r2.status_code >= 400:
        yield f"⚠️ Compiled output not available (HTTP {r2.status_code})."; return
    d = r2.json()
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
        r = _ss().post(
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
