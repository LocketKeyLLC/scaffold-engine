"""
scaffold_router.py — Open WebUI pipeline for Scaffold Engine.

Commands: see _help() for the full list.

Three timeout valves (consolidated from six hardcoded values):
  - request_timeout  (default 30s)    — quick JSON endpoints
  - stream_timeout   (default 3600s)  — SSE + long-poll LLM endpoints
  - triage_timeout   (default 120s)   — direct Ollama calls for triage/synthesis (§17.199)

Legacy `dag_timeout` valve is preserved; if an admin customized it, the value
is migrated into stream_timeout on pipeline init.
"""

from typing import Generator, List
import json
import os
import logging
import queue
import re
import argparse
import difflib
import unicodedata
import shlex
import threading
import time

import requests
from pydantic import BaseModel

# §17.190 / §17.195: vendored modules from app/ + sdk/scaffold_client/
# (byte-equal copies — see ``make sync-sse-events`` / ``make sync-next-
# actions`` and the corresponding check-* gates). The OWUI pipelines
# runtime loads scaffold_router.py with /app/pipelines on sys.path,
# while the orchestrator test harness loads it via
# importlib.util.spec_from_file_location; loading by file path works in
# both environments.
import importlib.util as _importlib_util  # noqa: E402
import pathlib as _pathlib  # noqa: E402

def _load_vendor(modname: str, filename: str):
    # §17.212: vendor files live in `pipelines/_vendor/` rather than next to
    # this file. The OWUI loader scans every top-level `pipelines/*.py` and
    # quarantines any without a Pipeline class to `pipelines/failed/` — the
    # `:ro` overlay protects file content but not the directory entry, so the
    # rename succeeded and broke startup. A subdirectory is invisible to the
    # non-recursive loader scan.
    path = _pathlib.Path(__file__).parent / "_vendor" / filename
    spec = _importlib_util.spec_from_file_location(modname, path)
    mod = _importlib_util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

_SSE = _load_vendor("scaffold_router_sse_events", "_sse_events.py")
_next_actions = _load_vendor("scaffold_router_next_actions", "_next_actions.py")
# §17.296 — vendored /assist command handlers (~600 LOC lifted from
# scaffold_router.py). Mirrors the §17.190 + §17.195 vendor pattern.
# Loaded after `_SSE` because `_assist_handlers.stream_sse_with_keepalive`
# references its event-name constants; loaded before `_HTTP_SESSION` is
# fine because vendor functions resolve the session lazily via
# `sys.modules["scaffold_router"]._HTTP_SESSION`.
_assist = _load_vendor("scaffold_router_assist", "_assist_handlers.py")
# §17.297 — STATUS_ICONS hoisted into pipelines/_vendor/_status_icons.py
# so the dict has a single source of truth across all 5 pipeline files
# (pre-§17.297 each pipeline carried its own copy with a "keep in sync"
# comment). Module-level `STATUS_ICONS` alias preserved so existing
# call sites (`STATUS_ICONS.get(status, ...)`) keep working without a
# rewrite.
STATUS_ICONS = _load_vendor(
    "scaffold_router_status_icons", "_status_icons.py",
).STATUS_ICONS
del _importlib_util, _pathlib, _load_vendor

# Module-level Session for connection reuse across the many orchestrator
# HTTP calls a pipeline makes during a chat session. ``requests.X(...)``
# would open a fresh TCP connection per call; ``_HTTP_SESSION.X(...)``
# reuses the keep-alive pool. Tests patch ``_HTTP_SESSION.get`` /
# ``.post`` / ``.delete`` directly (see tests/_scaffold_router_setup.py).
_HTTP_SESSION = requests.Session()

logger = logging.getLogger("scaffold_router")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)


# --------------------------------------------------------------------
# Argument parsing helpers (Tier 1 #1 — shared parser foundation)
# --------------------------------------------------------------------

KNOWN_COMMANDS: tuple = (
    "/go", "/run", "/confirm", "/execute", "/idea", "/skip", "/optimize",
    "/rag", "/model", "/research", "/research/reply", "/research/list",
    "/research/find", "/research/rename", "/research/delete", "/research/help",
    "/research/pdf", "/jobs", "/schedule", "/status", "/results", "/help",
    "/assist", "/assist/next", "/assist/submit", "/assist/skip",
    "/assist/handoff", "/assist/pause", "/assist/resume", "/assist/done",
    "/assist/friction", "/assist/help",
    # U.8.D — chat parity for components reachable via CLI/SDK only.
    "/exec", "/cleanup", "/config", "/logs", "/health",
    # J.3.c — cost rollup for a job.
    "/cost",
    # §17.322 — operator-driven job cancel (the corollary to /go and
    # /confirm; replaces the SQL-only drain §17.321 had to use).
    "/cancel",
    # §17.479 — interactive node control (Phase 5 surfaces for the §17.478
    # /nodes CRUD API).
    "/node",
    # §17.562 — guided/minimal core verbs (DB-derived stateful defaults).
    "/here", "/next", "/resume", "/advanced",
    # §17.565 — artifacts (typed deliverables): fetch one or list a job's.
    "/artifacts",
)

# §17.562 — the guided/minimal CORE surface. When advanced_commands_enabled
# is off (default), only these pass dispatch; every other known command
# returns a one-line "enable with /advanced on" hint. The set covers the
# whole happy path: scope → launch → review → run/assist → resume → see-state.
# /assist/* subcommands resolve to the "/assist" base, so they're all core.
_CORE_COMMANDS: frozenset = frozenset({
    "/go", "/run", "/idea", "/confirm", "/execute",
    "/here", "/status", "/next", "/resume", "/results",
    "/assist", "/cancel", "/artifacts",
    "/help", "/advanced",
})

# §17.562 — lookup-class commands that get the "what's next" footer appended
# (non-streaming path only). Deliberately excludes commands that already
# render next steps (/idea, /confirm, /status, /here, /jobs) or are pure
# config/diagnostics (/help, /health, /model, /config, /advanced).
_FOOTER_COMMANDS: frozenset = frozenset({"/results", "/cost", "/logs", "/rag"})

KNOWN_SUBCOMMANDS: dict = {
    "/model": ("list", "available", "set", "reset", "probe", "test", "help"),
    "/jobs": ("list", "find", "rename", "delete", "help"),
    # U.8.D — `run-now` was advertised here but never had an orchestrator
    # endpoint or a chat handler. Removed; see audit follow-ups.
    "/schedule": ("list", "add", "delete", "help"),
    "/assist": ("next", "submit", "skip", "handoff", "pause", "resume",
                "done", "friction", "status", "help"),
    "/exec": ("retry", "help"),
    # §17.479 — node-control subcommands.
    "/node": ("reset", "del", "delete", "remove", "edit", "reorder", "help"),
}

# §17.628 — deterministic fast-path for engine-wide natural-language command
# routing. Whole-message phrase → read-only intent, no LLM, always treated as
# high-confidence. Only unambiguous, self-contained phrasings belong here; a
# message that needs a slot (a RAG query, a job name) or could be conversation
# is left to the /route classifier. Mirrors `_FAST_INTENT_PHRASES` in the
# in-session assist NL layer.
_FAST_COMMAND_PHRASES: dict = {
    "status": {
        "what's running", "whats running", "what is running", "what's going on",
        "whats going on", "what have i got running", "what jobs are running",
        "anything running", "any jobs running", "job status", "show status",
    },
    "jobs_list": {
        "list my jobs", "list jobs", "show my jobs", "show jobs", "my jobs",
        "what jobs do i have", "recent jobs",
    },
    "model_list": {
        "list models", "show models", "what models are set", "which models",
        "what models are routed", "current models",
    },
    "model_available": {
        "available models", "installed models", "what models do i have",
        "what's in ollama", "whats in ollama",
    },
    "model_probe": {
        "probe models", "are the models up", "ping the models", "check models",
    },
    "help": {
        "help", "what can you do", "what can i do", "how do i use this",
        "commands", "show commands", "what commands", "how does this work",
    },
}
_FAST_COMMAND_LOOKUP: dict = {
    phrase: intent
    for intent, phrases in _FAST_COMMAND_PHRASES.items()
    for phrase in phrases
}


def _fast_classify_command(msg: str) -> str | None:
    """Read-only intent for an unambiguous whole-message phrase, else None.
    Deterministic — no LLM. Only intents needing NO slot are eligible here
    (rag_query / jobs_find / results all require an argument, so they go to the
    /route classifier)."""
    norm = (msg or "").strip().lower().strip(".!?,;: ").strip()
    return _FAST_COMMAND_LOOKUP.get(norm)


_PLACEHOLDER_RE = re.compile(r"^[<\[(].+[>\])]$")
_PLACEHOLDER_TOKENS = frozenset({
    "query", "topic", "url", "message", "id", "session_id", "job_id",
    "node_key", "cron", "model", "prompt", "text", "feedback",
})

_DASH_VARIANTS = ("\u2014", "\u2013", "\u2012", "\u2212")  # em, en, figure, minus

_SINGLE_DASH_FLAG_RE = re.compile(r"(?<![\w-])-([a-zA-Z][a-zA-Z0-9-]+)(?=[\s=]|$)")


def _normalize_input(s: str):
    """NFKC-normalize and rewrite unicode dashes + single-dash long flags.

    Returns (normalized, rewrites). `rewrites` is a list of "before -> after"
    strings for any substitution made; empty if input was already canonical.
    Used by #13 to surface a confirmation when the parser silently rewrites.
    """
    if not s:
        return s, []
    norm = unicodedata.normalize("NFKC", s)
    rewrites = []

    for ch in _DASH_VARIANTS:
        if ch in norm:
            rewrites.append(f"`{ch}` -> `--`")
            norm = norm.replace(ch, "--")

    seen_flags = set()
    def _expand(m):
        flag = m.group(1)
        if flag not in seen_flags:
            rewrites.append(f"`-{flag}` -> `--{flag}`")
            seen_flags.add(flag)
        return f"--{flag}"
    norm = _SINGLE_DASH_FLAG_RE.sub(_expand, norm)

    return norm, rewrites


def _is_placeholder(value: str) -> bool:
    """True if value looks like an unfilled command placeholder."""
    if not value:
        return True
    v = value.strip()
    if not v:
        return True
    if _PLACEHOLDER_RE.match(v):
        return True
    if v.lower() in _PLACEHOLDER_TOKENS:
        return True
    return False


def _suggest_command(token: str, candidates=None):
    """Up to 3 close matches for an unknown command or subcommand."""
    pool = candidates if candidates is not None else KNOWN_COMMANDS
    return difflib.get_close_matches(token, pool, n=3, cutoff=0.6)


class _ChatArgError(Exception):
    """Raised by CommandParser when input is malformed; message is chat-ready."""


class CommandParser:
    """argparse wrapper with chat-friendly errors and residual-positional capture.

    Usage:
        p = CommandParser("research", "Autonomous web research")
        p.add_argument("--depth", choices=["shallow", "medium", "deep"], default="medium")
        p.add_example("/research kubernetes pods --depth=deep")
        args, residual = p.parse(raw_args_string)
    """

    def __init__(self, name: str, description: str = ""):
        self.name = name
        self.description = description
        self._parser = argparse.ArgumentParser(
            prog=f"/{name}",
            description=description,
            add_help=False,
            exit_on_error=False,
        )
        self._examples = []

    def add_argument(self, *args, **kwargs):
        return self._parser.add_argument(*args, **kwargs)

    def add_example(self, example: str) -> None:
        self._examples.append(example)

    def parse(self, raw: str):
        try:
            tokens = shlex.split(raw) if raw else []
        except ValueError as e:
            raise _ChatArgError(
                f"Could not parse `/{self.name}` input: {e}. Check for unmatched quotes."
            )
        try:
            ns, rest = self._parser.parse_known_args(tokens)
        except (argparse.ArgumentError, SystemExit) as e:
            raise _ChatArgError(self._format_parse_error(str(e)))
        # parse_known_args silently sweeps unknown flags into `rest`; surface them.
        unknown_flags = [t for t in rest if t.startswith("-") and len(t) > 1]
        if unknown_flags:
            bad = unknown_flags[0].split("=", 1)[0]
            valid_flags = [f for a in self._parser._actions for f in a.option_strings]
            close = difflib.get_close_matches(bad, valid_flags, n=1, cutoff=0.6)
            hint = f" Did you mean `{close[0]}`?" if close else ""
            raise _ChatArgError(
                f"Unknown flag `{bad}` for `/{self.name}`.{hint} "
                f"Run `/{self.name} --help` for options."
            )
        return ns, " ".join(rest).strip(), rest

    def help_text(self) -> str:
        lines = [f"**`/{self.name}`** - {self.description}".rstrip(" -"), ""]
        flag_lines = []
        for action in self._parser._actions:
            if not action.option_strings:
                continue
            flags = ", ".join(f"`{f}`" for f in action.option_strings)
            choices = f" ({'|'.join(map(str, action.choices))})" if action.choices else ""
            default = ""
            if action.default not in (None, argparse.SUPPRESS, False, ""):
                default = f" [default: {action.default}]"
            help_str = f" - {action.help}" if action.help else ""
            flag_lines.append(f"  {flags}{choices}{default}{help_str}")
        if flag_lines:
            lines.append("**Flags:**")
            lines.extend(flag_lines)
            lines.append("")
        if self._examples:
            lines.append("**Examples:**")
            for ex in self._examples:
                lines.append(f"  `{ex}`")
        return "\n".join(lines).rstrip()

    def _format_parse_error(self, err: str) -> str:
        m = re.search(r"unrecognized arguments?: (\S+)", err)
        if m:
            bad = m.group(1)
            valid_flags = [f for a in self._parser._actions for f in a.option_strings]
            close = difflib.get_close_matches(bad, valid_flags, n=1, cutoff=0.6)
            hint = f" Did you mean `{close[0]}`?" if close else ""
            return (f"Unknown flag `{bad}` for `/{self.name}`.{hint} "
                    f"Run `/{self.name} --help` for options.")
        return f"`/{self.name}`: {err} Run `/{self.name} --help` for options."


# --------------------------------------------------------------------
# Module-level prompts (#8.13)
# --------------------------------------------------------------------

TRIAGE_SYSTEM_PROMPT = """You are a hands-on project planning assistant for Scaffold Engine.
Respond ONLY in English.

Your job: turn vague ideas into specific, buildable scope by surfacing
options, naming gaps, and recommending defaults. Do not assume — ask,
list, recommend.

If the user provides a document, file, or specification, treat its
content as primary context. Do not ask the user to re-explain anything
already in the document.

EVERY RESPONSE includes ALL FOUR sections below, in this order, with
these exact headers — including the very first response in a new chat
(no prior assistant message exists yet — the 4 headers still apply),
when you are elaborating, giving examples, or answering a follow-up.
Do not drop "My pick" under any circumstance unless the scope is locked
and you are emitting the final summary. For a multi-part build, also include
the optional Components section (placed right after Scope so far) — it is the
only extra header allowed.

**Scope so far:**
One line summarizing what is clear about the build. If nothing is clear
yet, write "Not enough yet — see Gaps below."

**Components:** (OPTIONAL — include this section only when the build clearly
splits into 2-5 parts that could each be built on their own; OMIT the whole
section, header included, for a single-focus build)
List each part on one line as `name — one-clause scope`. These are the pieces
the user picks from at /go — each chosen part becomes its own job. Derive them
only from what the user stated or clearly implied; never invent parts. When you
show Components, push the user (in "My pick") on which parts are in scope for
this build before drilling into any one part's gaps.

**Options:**
When there is a real choice (architecture, technology, approach), list
2–3 options with a one-line tradeoff each. If scope is too vague for
options yet, write "Define WHAT first — see Gaps." If the direction is
genuinely settled, write "None — direction is settled" and skip to Gaps.

**Gaps:**
Always shown. For each bucket not yet "✓ covered", write the bucket name,
a colon, then ONE specific question the user can answer in a single
sentence. Never a category description like "needs definition of done" —
always a real question. The four buckets:
- WHAT specifically is being built
- HARDWARE / infrastructure (OS, CPU, RAM, storage, network)
- SUCCESS criteria (what "done" looks like)
- CONSTRAINTS (budget, timeline, equipment, skill)
Mark a bucket "✓ covered" only when the user has explicitly stated a value.
Parenthetical examples are answer shape only — never carry an example
value into "My pick" or "Scope so far".
INFORMATION VALUE — ask what matters, default the rest. Open buckets are not
equally important. Judge each as LOAD-BEARING (its answer would materially
change the plan, architecture, or tooling) or LOW-VALUE (a safe default exists
that won't change the recommendation). For a LOW-VALUE open bucket, append
"(can default: <value>)" to its question so the user can skip it instead of
answering. "My pick" pushes on the single highest-value open bucket only.

**My pick:**
Recommend ONE concrete default for the most important open decision.
State why in one sentence. End with: "Say so or override."
If the most important open decision depends on an unanswered Gap, do
NOT invent a value to recommend. Instead, name the blocking Gap and
push for that answer. Only recommend defaults you can derive from
values the user explicitly stated.

Worked example of a mid-conversation reply (after the user has answered
most of the Gaps but scope is not yet locked):

**Scope so far:**
A CLI tool on Pop!_OS that turns a folder of screenshots into one
searchable PDF. Evening project, no budget.

**Options:**
- OCR-first: Tesseract on each image, append text pages to PDF — text-searchable, lightweight.
- Image-with-OCR-layer: keep originals, overlay invisible OCR text — searchable AND visual, larger files.
- No OCR: just bundle images into a PDF — fastest, not searchable.

**Gaps:**
- WHAT specifically is being built: ✓ covered
- HARDWARE / infrastructure: ✓ covered
- SUCCESS criteria: should the PDF preserve the original screenshots, or be text-only?
- CONSTRAINTS: ✓ covered

**My pick:**
Image-with-OCR-layer — preserves what you screenshotted while staying searchable. Say so or override.

Worked example of an early reply (most buckets still open):

**Scope so far:**
A home lab on existing Proxmox VE hardware running media, AI,
game-server, and security workloads. Goals: security, ease, free.

**Components:**
- Media stack — Sonarr/Radarr/Jellyfin or similar, on the LAN.
- AI workload — local inference (which models TBD).
- Game server — one or more dedicated game hosts.
- Security layer — firewall/VPN/monitoring across the lab.

**Options:**
- VM per service: strongest isolation, more config overhead.
- LXC containers: lightest weight, shared kernel risk.
- Hybrid: critical workloads in VMs, rest in LXC.

**Gaps:**
- WHAT specifically is being built: which specific services in scope — Sonarr/Radarr/Jellyfin for media, which AI workloads, which games?
- HARDWARE / infrastructure: ✓ covered
- SUCCESS criteria: what does "done" look like — all services on LAN, or remote access too? Any uptime target?
- CONSTRAINTS: timeline for the build — a weekend, a month, open-ended?

**My pick:**
Hybrid — VMs for the AI workload (Tesla P40 passthrough is cleaner in a VM) and LXC for the rest. Say so or override.

Worked example of a FIRST-TURN response in a fresh chat (single user
message, zero prior assistant turns — same 4 headers, same rules):

User just sent: "I have a 2018 MacBook Pro with 16GB RAM and a 1TB SSD,
working from home. I want to do something with the spare cycles."

**Scope so far:**
A 2018 MacBook Pro (16 GB RAM, 1 TB SSD) used during work-from-home
hours, to be repurposed for some background workload during spare cycles.

**Options:**
Define WHAT first — see Gaps.

**Gaps:**
- WHAT specifically is being built: what kind of workload — local services (file/media server), background compute (LLM inference, encoding), or developer tooling (CI runner, build cache)?
- HARDWARE / infrastructure: ✓ covered
- SUCCESS criteria: what does "done" look like — does the workload need to be reliable (24/7), or opportunistic (run when you're idle)?
- CONSTRAINTS: any limits on power, noise, network bandwidth, or interference with your work day?

**My pick:**
None — the WHAT bucket is the load-bearing decision and it's open. Name a workload category and I'll recommend a specific implementation. Say so or override.


HISTORY TRACKING (critical):
Before writing your response, scan the entire conversation history above.
- If user stated WHAT in any prior message → mark "✓ covered"
- If user stated HARDWARE in any prior message → mark "✓ covered"
- If user stated SUCCESS in any prior message → mark "✓ covered"
- If user stated CONSTRAINTS in any prior message → mark "✓ covered"
Only list gaps that have NEW unknowns. Do NOT ask a question the user
already answered, even if phrased differently. Map implicit answers too:
- "1 month" = CONSTRAINTS (timeline)
- "Raspberry Pi" = HARDWARE
- "fully operational OS" = SUCCESS criteria
- "compiler" = WHAT


Rules:
- Keep each section to 1–3 short bullets or sentences.
- No markdown tables. No emoji. No fenced code blocks. No horizontal rules.
- No headers other than the four required ones (Scope so far / Options / Gaps / My pick) plus the optional Components header for multi-part builds.
- Plain bullets only. Bold only inside the required headers.
- One topic per response — pick the most important gap to push on.
- Do not invent requirements the user has not agreed to.
- Never invent a value the user did not state. If a bucket is open, the
  question goes in Gaps; it does not appear as a fact in Scope so far or
  as a chosen value in My pick.
- Never cite sources you weren't given. No invented studies, organizations,
  averages, or "real-world data" — no "USDA / NASA / industry research / 95%
  of users" appeals. Numerical specifics (costs, durations, percentages,
  benchmarks) must come from values the user stated; otherwise they are
  fabrication and must be omitted.
- Echo user-stated values verbatim in "Scope so far" — do not paraphrase
  specs (e.g., if the user said "Ryzen 9 7950X" do not write "Ryzen 9 7900X";
  if the user said "25GbE" do not write "10Gb"). Same rule for hardware
  model numbers, throughput figures, capacities, and named technologies.
- Do not execute anything. Do not write code. Do not propose scripts.
- Do not ask "should I write the script" or offer deliverables — that is the pipeline's job after /go.

STOP ASKING ONCE THE LOAD-BEARING GAPS ARE ANSWERED — don't drag the user
through low-value questions. When every LOAD-BEARING bucket is covered (even if
LOW-VALUE buckets remain open with safe defaults), replace the four sections
with a 2-4 sentence scope summary, state the defaults you will assume for any
remaining low-value gaps, and write: "Type `/go` to review the launch brief
(then `/go confirm` to start) — I'll use sensible defaults for the rest, or
answer the open points first to override them."
For a multi-part build, the summary names the components in scope and notes
that each becomes its own job at /go; otherwise it reads as a single build.
While ANY load-bearing gap is still open, keep emitting all four sections every
turn — even if the user answered everything else in their last message. (If all
four buckets read "✓ covered", the same summary-and-`/go` close applies with no
defaults to state.) The user decides when scope is locked and can always answer
more or override a default before /go, not you."""


SYNTHESIS_SYSTEM_PROMPT = (
    "You extract a project description from a planning conversation. "
    "Respond ONLY in English. "
    "Write 3-6 plain sentences describing what will be built, using only "
    "details the user confirmed. Be specific: include technologies, "
    "components, architecture, and goals. Write as a direct project "
    "description — not 'the user wants' but 'Build a...' or 'Set up a...'. "
    "No preamble, no markdown, no labels, no meta-text like 'type /go'."
)


# §17.297 — STATUS_ICONS is now loaded from pipelines/_vendor/_status_icons.py
# at module-init (see the `_load_vendor` block above). The "─── SHARED:
# keep in sync ───" inline block this comment replaces was the pre-§17.297
# convention; it's gone because the vendor module is the single source.


class Pipeline:
    class Valves(BaseModel):
        api_key: str = ""
        orchestrator_url: str = "http://scaffold-orchestrator:8000"

        # --- Consolidated timeouts (#8.8) ---
        request_timeout: int = 30     # quick JSON endpoints
        stream_timeout: int = 3600    # SSE + long-poll LLM endpoints
        # §17.199 — direct Ollama triage call default tightened
        # 3600s → 120s (AUDIT 2.8). Triage uses the 4b model which
        # responds in 5-30s on this host; the prior 1-hour default let a
        # wedged Ollama hang the SSE side for up to an hour before
        # surfacing the error. 120s is generous for the model and still
        # operator-overridable via SCAFFOLD_TRIAGE_TIMEOUT for a
        # deployment that genuinely needs longer.
        triage_timeout: int = 120
        # Legacy alias (migrated to stream_timeout on init if non-default)
        dag_timeout: int = 3600

        # SSE cadence & per-read timeout & stall threshold multiplier source
        keepalive_interval: int = 10
        # §17.173 — visible elapsed-time marker cadence for long blocking
        # POSTs (Phase 2 research, ~10-25 min on CPU). Zero-width keepalives
        # tick every keepalive_interval (above) to keep the SSE connection
        # alive; this separate cadence governs the *visible* "⏳ still
        # working" markers users see in chat. 120 s = ~10 markers across a
        # 25-min job, which surfaces progress without filling the chat with
        # redundant ticks. Set to 0 to disable visible markers.
        progress_marker_interval: int = 120

        # Triage
        triage_model: str = "qwen3:4b"
        triage_history_window: int = 8  # last N turns sent to triage; first user msg always pinned
        # §17.526 — when true, /go first tries POST /decompose: a multi-part
        # build fans out into an umbrella + one autonomously-run component job
        # per part. Default OFF — fanning out N full pipelines is heavy on
        # CPU-only inference, so it stays opt-in. A single-focus idea (or any
        # decompose failure) transparently falls back to the normal single-job
        # /ideate auto-chain.
        decompose_on_go: bool = False
        log_pipe_inputs: bool = False  # diagnostic: log body keys + message shape on every pipe() call
        # Sprint X.7 — diagnostic: one structured line per pipe() call with the
        # routing decision (which command branch / triage / unrecognized), the
        # wrapper-strip outcome, and content-type counts. Off by default to
        # avoid log volume on prod chats; flip on when debugging "why didn't
        # my command run / why did my file content disappear".
        log_routing_decisions: bool = False
        ollama_url: str = "http://172.18.0.1:11434"

        # ── Assistant Mode ─────────────────────────────────────────────
        # When true, /confirm routes the job into Assist Mode (interactive
        # walk-through) instead of /execute/all (autonomous). Default off
        # to preserve existing UX.
        assist_after_confirm: bool = False
        assist_default_handoff_policy: str = "manual"           # manual | auto_on_skip | auto_all_remaining
        assist_default_replan_policy: str = "context_only"      # context_only | selective | full | disabled
        assist_max_evidence_chars: int = 200_000
        # When true, the pipeline remembers `chat_id → session_id` via the
        # orchestrator's /assist/_chatmap endpoint so subcommands accept
        # an optional <session_id>. Default on; flip off to force users
        # back to explicit session IDs (debugging or shared-chat setups).
        # §17.265 — router-only by design. The other four pipelines
        # (execution_handler, dag_viewer, gt_browser, prompt_inspector)
        # do not handle /assist subcommands, so the valve would be inert
        # there. Do NOT replicate it for "consistency" — it would suggest
        # behavior that does not exist.
        assist_session_memory_enabled: bool = True

        # §17.486 — guidance layer. When on, /assist next auto-generates a
        # human walkthrough (copy-paste commands for shell/codegen work,
        # step-by-step instructions for non-coding work) for each claimed
        # step; /assist guide regenerates on demand. assist_guide_research
        # gates the SearXNG/Milvus confirm pre-pass. Generation is a slow
        # (8192-token) thinking-model call, so it gets its own timeout
        # separate from the fast-call request_timeout. Model role + token
        # budget are server-authoritative (app/config.py assist_guide_*).
        assist_auto_guide: bool = True
        assist_guide_research: bool = True
        assist_guide_timeout: int = 180
        # §17.537 — assist-aware chat routing. When a chat has an ACTIVE
        # assist session, plain (non-command) text is a conversational turn
        # ON that session — route it to the step guidance (refine=<text>)
        # instead of the triage planner. Without this, every bare message
        # mid-assist bounced to triage, freezing the session and repeating
        # the Scope/Options/Gaps blocks (the DeFruscio HomeLab symptom).
        # Requires assist_session_memory_enabled (the chatmap is the signal).
        # Flip off to force the old explicit-/assist-command flow.
        assist_chat_routing_enabled: bool = True
        # §17.493 — stream the walkthrough token-by-token (SSE) instead of a
        # blocking POST + full result. Off → the §17.486 non-streamed path.
        assist_stream: bool = True

        # §17.633 — cross-chat assist continuity. OWUI sends no chat_id and a
        # NEW chat has no session marker in history, so neither the chatmap nor
        # the history-recovery path can find in-progress assist work started in
        # another chat. With this on, a plain natural message that references an
        # in-progress job ("continue the proxmox setup") or reads as resuming
        # work ("what's next", "where were we") reconnects to it — re-presenting
        # the current step and re-emitting the marker so THIS chat tracks it. A
        # new chat's first turn also surfaces any in-progress work as a banner.
        # Off → the pre-§17.633 behavior (in-progress work is only reachable
        # from its original chat or via an explicit `/assist <job_id>`).
        assist_continuity_enabled: bool = True

        # §17.628 — engine-wide natural-language command routing. When a chat
        # has NO active assist session, a plain sentence that clearly names a
        # read-only engine action (status / results / RAG query / jobs+model
        # listing / help) is routed to that component instead of triage. Only
        # high-confidence, required-slot-satisfied classifications intercept;
        # anything ambiguous or idea-shaped falls through to the planner
        # untouched (triage stays the default). Flip off to force slash-command
        # -only access to those reads. Fail-soft: a classifier hiccup → triage.
        nl_command_routing_enabled: bool = True
        # Timeout (s) for the POST /route classifier call (short — one cheap
        # tool-call on the verifier role). Kept separate from request_timeout so
        # a slow classifier can be bounded without touching the read handlers.
        nl_command_route_timeout: int = 20

        # §17.300 — first-turn welcome preamble. When a brand-new chat
        # receives a natural-language message, the pipeline prepends a
        # small "here's how this works" block ahead of the triage
        # response so first-touch operators see the canonical flow
        # without typing `/help`. Slash commands skip the preamble
        # (operators using commands already know the surface). One
        # preamble per chat — subsequent turns are unaffected.
        show_welcome_on_first_turn: bool = True

        # §17.444 (Phase A / A5) — when true, `/go` shows the synthesized brief
        # and STOPS, requiring `/go confirm` to actually launch. Prevents
        # committing a 10–25 min CPU run to a bad synthesis before the user can
        # correct it. Set false to restore one-shot `/go` launch.
        confirm_before_launch: bool = True

        # §17.307 — active-job chat memory (pilot). When true, /idea
        # success caches `chat_id → job_id` in-pipeline; `/results`
        # and `/cost` invoked with NO args fall back to that cached
        # id and surface a 📌 hint instead of the bare Usage error.
        # Explicit args always override. Falls through to Usage error
        # when memory is empty (no surprise; behavior unchanged when
        # the cache is cold). Cache lives in-pipeline (not in the
        # orchestrator chatmap) — pilot is scoped to a single
        # pipeline replica; UUID re-type is the recovery if the
        # pipelines container restarts.
        active_job_memory_enabled: bool = True

        # §17.562 — guided/minimal command surface. When OFF (default), only
        # the _CORE_COMMANDS verbs are accepted; every other known command
        # returns a one-line "type /advanced on" hint and /help lists only the
        # core. Flip ON (via `/advanced on` or the valve) for the full ~50-
        # command surface. Keeps the surface small + low-stress for the common
        # user while leaving every power command one toggle away.
        advanced_commands_enabled: bool = False
        # §17.562 — append a compact "where you are / what's next" footer
        # (current job phase + top next action, from GET /work) to NON-
        # streaming command replies. Never interleaved into an SSE stream.
        status_footer_enabled: bool = True

        # Model overrides — §17.346 flipped router/coder/verifier defaults to
        # the same cloud model as §17.344's triage flip. Keep in sync with
        # app/config.py Settings defaults; the per-role rationale lives there.
        model_general: str = "deepseek-v4-pro:cloud"  # §17.632 — A/B'd 3.4× faster than qwen3.5 at equal synthesis quality; keep in sync w/ config.py + compose MODEL_GENERAL
        # §17.567 — A/B'd (model_ab.py --task verifier): kimi 30/30 @ 1.34s vs
        # qwen3.5 30/30 @ 6.12s. Keep in sync with app/config.py:model_verifier.
        model_verifier: str = "kimi-k2.7-code:cloud"
        model_coder: str = "kimi-k2.7-code:cloud"  # §17.498 — A/B'd coder-specialized model
        # §17.472 — synced to the orchestrator's real embedder. §17.83
        # switched the live embedder to nomic-embed-text (qwen3-embedding:8b
        # wedged deterministically on this host's Ollama --ollama-engine
        # path); app/config.py:model_embedder_pipeline followed, but this
        # pipeline default was missed and still read qwen3-embedding:8b. It's
        # cosmetic — model_embedder is in _SINGLETON_ROLES so _model_overrides
        # never sends it to the orchestrator — but it drove a misleading
        # `_probe_embedder_dim` startup log (`qwen3-embedding:8b native
        # dim=4096`) and the `/model list` display. nomic-embed-text probes at
        # native dim 768, truncated to 512 via MRL (same _EMBEDDER_EXPECTED_DIM).
        model_embedder: str = "nomic-embed-text"
        model_reranker: str = "tomaarsen/Qwen3-Reranker-0.6B-seq-cls"
        model_router: str = "qwen3.5:397b-cloud"
        model_fallback: str = "qwen3.5:latest"
        model_cloud_alt: str = "qwen3.5:397b-cloud"

    _MODEL_ROLES = (
        "model_general", "model_verifier", "model_coder",
        "model_embedder", "model_reranker", "model_router",
        "model_fallback", "model_cloud_alt",
    )
    _SINGLETON_ROLES = {"model_embedder", "model_reranker"}

    def __init__(self):
        self.id = "scaffold_router"
        self.name = "Scaffold Router"
        self._bootstrap_valves_from_template()
        self.valves = self.Valves()
        self._apply_env_fallbacks()
        self.logger = logger

        # Migrate legacy dag_timeout (#8.8 compat)
        if self.valves.dag_timeout != 3600 and self.valves.stream_timeout == 3600:
            self.logger.info(
                "Migrating legacy dag_timeout (%s) → stream_timeout",
                self.valves.dag_timeout,
            )
            self.valves.stream_timeout = self.valves.dag_timeout
        # Embedder dimension probe (strict)
        ok, msg = self._probe_embedder_dim()
        if not ok:
            raise RuntimeError(f"Embedder probe failed: {msg}")
        self.logger.info("Embedder probe OK: %s", msg)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _bootstrap_valves_from_template(self) -> None:
        """If live valves.json is missing or empty {}, seed from template.

        Pipelines main.py writes {} to valves.json whenever it is missing
        on container startup, which loses every saved value. We ship a
        valves.template.json next to the live file with sensible defaults
        (no secrets) and copy it in if the live file is empty.

        Fails closed on missing template — the pipeline cannot run without
        valid bootstrap state, and silent fall-through hides volume-mount
        misconfigurations.
        """
        here = os.path.dirname(os.path.abspath(__file__))
        sub = os.path.join(here, "scaffold_router")
        live = os.path.join(sub, "valves.json")
        tmpl = os.path.join(sub, "valves.template.json")
        if not os.path.exists(tmpl):
            raise RuntimeError(
                f"[scaffold_router] valves.template.json missing at {tmpl!r}; "
                f"verify ./pipelines volume mount in docker-compose.yml."
            )
        needs_seed = False
        if not os.path.exists(live):
            needs_seed = True
        else:
            with open(live, "r") as f:
                content = f.read().strip()
            if content in ("", "{}"):
                needs_seed = True
        if not needs_seed:
            return
        with open(tmpl, "r") as f:
            tmpl_data = f.read()
        with open(live, "w") as f:
            f.write(tmpl_data)
        print(  # noqa: T201
            f"[scaffold_router] Seeded {live!r} from template "
            f"(was missing or empty {{}})."
        )

    def _apply_env_fallbacks(self) -> None:
        """Fill empty string-valued valves from environment variables.

        Pipelines main.py rewrites valves.json to {} whenever the file
        is missing on container startup, which silently wipes saved
        config (most painfully: api_key). When a valve loads as empty,
        we look up the matching SCAFFOLD_* env var as a fallback so
        a wiped/regenerated valves.json does not block the pipeline.

        Precedence (default): valve > env > default. Setting the env
        var ``SCAFFOLD_VALVES_ENV_OVERRIDE`` to a truthy value (1/true/
        yes/on) flips the precedence to env > valve for the managed
        string fields (api_key, orchestrator_url, ollama_url). This is
        the recommended setting for prod: ``.env`` becomes the single
        source of truth for these and rotation drift is impossible.

        Int valves (timeouts) take env override only if the field was
        not present in valves.json at all — i.e. the user has not
        explicitly configured it via the OWUI valve UI. ENV_OVERRIDE
        does not apply to int valves (timeouts are tunable per-pipeline
        via the OWUI UI; we don't want a single env to clobber that).

        Env var naming: api_key -> SCAFFOLD_API_KEY, ollama_url ->
        SCAFFOLD_OLLAMA_URL, etc.
        """
        env_map = {
            "api_key": "SCAFFOLD_API_KEY",
            "orchestrator_url": "SCAFFOLD_ORCHESTRATOR_URL",
            "ollama_url": "SCAFFOLD_OLLAMA_URL",
        }
        env_int_map = {
            "request_timeout": "SCAFFOLD_REQUEST_TIMEOUT",
            "stream_timeout": "SCAFFOLD_STREAM_TIMEOUT",
            "triage_timeout": "SCAFFOLD_TRIAGE_TIMEOUT",
        }
        env_override = os.getenv("SCAFFOLD_VALVES_ENV_OVERRIDE", "").strip().lower() in (
            "1", "true", "yes", "on",
        )
        # Read live valves.json to distinguish "user-set default" from
        # "field absent" for int valves.
        here = os.path.dirname(os.path.abspath(__file__))
        live = os.path.join(here, "scaffold_router", "valves.json")
        try:
            with open(live, "r") as _fh:
                import json as _json
                saved = _json.load(_fh)
                if not isinstance(saved, dict):
                    saved = {}
        except Exception:
            saved = {}
        for valve_name, env_name in env_map.items():
            current = getattr(self.valves, valve_name, None)
            env_val = os.getenv(env_name, "")
            if not isinstance(current, str):
                continue
            if not current and env_val:
                setattr(self.valves, valve_name, env_val)
                print(  # noqa: T201
                    f"[scaffold_router] Valve {valve_name!r} empty; "
                    f"loaded from {env_name}."
                )
            elif env_override and env_val and current != env_val:
                setattr(self.valves, valve_name, env_val)
                print(  # noqa: T201
                    f"[scaffold_router] Valve {valve_name!r} OVERRIDE "
                    f"from {env_name} (env > valve precedence)."
                )
        for valve_name, env_name in env_int_map.items():
            if valve_name in saved:
                continue
            env_val = os.getenv(env_name, "")
            if not env_val:
                continue
            try:
                setattr(self.valves, valve_name, int(env_val))
                print(  # noqa: T201
                    f"[scaffold_router] Valve {valve_name!r} loaded from "
                    f"{env_name} (int)."
                )
            except (ValueError, TypeError):
                print(  # noqa: T201
                    f"[scaffold_router] {env_name}={env_val!r} not int; ignored."
                )
        # Drift warning: api_key in valves.json differs from env. The
        # print is captured in container logs (ops surface); set the
        # flag so user-facing 401s can include a UX hint as well.
        # In env_override mode, env already won above so drift is
        # already resolved — no warning, no flag.
        saved_key = saved.get("api_key", "")
        env_key = os.getenv("SCAFFOLD_API_KEY", "")
        if saved_key and env_key and saved_key != env_key and not env_override:
            print(  # noqa: T201
                "[scaffold_router] WARNING: api_key in valves.json differs "
                "from SCAFFOLD_API_KEY env. Using valves.json value. "
                "(Set SCAFFOLD_VALVES_ENV_OVERRIDE=true to make env win.)"
            )
            self._api_key_drift_detected = True
        else:
            self._api_key_drift_detected = False

    def _auth_headers(self) -> dict:
        key = self.valves.api_key or os.getenv("SCAFFOLD_API_KEY", "")
        return {"X-API-Key": key}

    def _drift_hint(self) -> str:
        """Markdown block to append on 401 responses when valves/env disagree.

        The print at init goes to container logs (ops surface) but not the
        OWUI chat (user surface). When the orchestrator rejects auth, the
        most likely cause is one of the two values being stale; this surfaces
        that hypothesis directly to the user without leaking either key.
        """
        if not getattr(self, "_api_key_drift_detected", False):
            return ""
        return (
            "\n\n⚠️ This pipeline detected that `api_key` in `valves.json` "
            "differs from `SCAFFOLD_API_KEY` in the environment. The 401 "
            "above is likely caused by one of those values being stale. "
            "Reconcile both sides and reload the pipeline."
        )

    _EMBEDDER_EXPECTED_DIM = 512

    def _probe_embedder_dim(self, model: str = None) -> tuple:
        """POST /api/embeddings; verify dim == 512. Returns (ok, msg)."""
        target = model or self.valves.model_embedder
        try:
            r = _HTTP_SESSION.post(
                f"{self.valves.ollama_url}/api/embeddings",
                json={"model": target, "prompt": "dimension probe"},
                timeout=self.valves.request_timeout,
            )
            r.raise_for_status()
            emb = r.json().get("embedding") or []
            n = len(emb)
            if n < self._EMBEDDER_EXPECTED_DIM:
                return (False, f"{target} returned dim={n}, < expected {self._EMBEDDER_EXPECTED_DIM} (post-truncation target)")
            return (True, f"{target} native dim={n}, will truncate to {self._EMBEDDER_EXPECTED_DIM}")
        except requests.exceptions.ConnectionError:
            return (False, f"cannot reach Ollama at {self.valves.ollama_url}")
        except Exception as e:
            return (False, f"{target}: {type(e).__name__}: {e}")

    def _model_overrides(self) -> dict:
        """Build overrides dict, filtering out empty strings (#8.10)."""
        out = {}
        for role in self._MODEL_ROLES:
            val = getattr(self.valves, role, "")
            if role not in self._SINGLETON_ROLES and val and val.strip():
                out[role] = val
        return out

    @staticmethod
    def _extract_text(content) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for c in content:
                if not isinstance(c, dict):
                    continue
                if c.get("type") == "text" and c.get("text"):
                    parts.append(c["text"])
                elif c.get("type") in ("file", "document") and c.get("text"):
                    parts.append(c["text"])
                elif c.get("type") in ("file", "document") and c.get("content"):
                    parts.append(c["content"])
                elif c.get("text") and c.get("type") not in ("image",):
                    parts.append(c["text"])
            return " ".join(parts)
        return str(content) if content else ""

    def _clean_messages(self, messages: List[dict]) -> List[dict]:
        cleaned = []
        for m in messages:
            text = self._extract_text(m.get("content", ""))
            text = text.replace("\u200b", "").strip()
            if text:
                cleaned.append({"role": m["role"], "content": text})
        return cleaned

    @staticmethod
    def _first_token(msg: str) -> str:
        if not msg:
            return ""
        parts = msg.split(None, 1)
        return parts[0].lower() if parts else ""

    def _is_cmd(self, msg: str, *commands: str) -> bool:
        """Word-boundary command match (#8.6): first token equals one of commands."""
        first = self._first_token(msg)
        return first in {c.lower() for c in commands}

    # ------------------------------------------------------------------
    # §17.300 — first-touch welcome
    # ------------------------------------------------------------------

    _WELCOME_PREAMBLE = (
        "👋 **Welcome to Scaffold Engine.**\n\n"
        "You can take either path:\n\n"
        "**A) Chat naturally to refine your idea** — describe what you "
        "want to build (you just did 👆), then type `/go` when the plan "
        "feels right.\n\n"
        "**B) Jump straight in with one command:**\n"
        "- `/idea Build a CLI that converts screenshots to PDF` — kick off "
        "Phase 1 directly\n"
        "- `/here` — see what's in progress and your next step\n"
        "- `/help` — the core commands\n\n"
        "_Power user? `/advanced on` unlocks the full surface — `/research`, "
        "`/jobs`, and ~45 more._\n\n"
        "---\n\n"
    )

    # §17.504 — assist-intent nudge. A free-text message that *asks the engine
    # to assist/help implement an existing build* (e.g. "assist with the
    # completion and implementation of the homelab") is NOT the `/assist`
    # command — the leading word "assist" is prose, so dispatch falls through
    # to the triage planner and the user gets 4-section planning replies while
    # believing they're in Assist Mode. This regex spots that intent so we can
    # point them at the real entry point. Anchored to imperative requests
    # ("assist …" at the start, "help me <do-verb>", "step/walk me through")
    # to avoid firing on project *descriptions* ("build an app that assists…").
    # NB: the verb group uses *stems* (complet, configur) so it matches
    # "complete"/"completion"/"configure"/"configuration" — therefore NO
    # trailing \b ("complet" is a prefix, not followed by a word boundary in
    # "complete"). The leading \b keeps the match word-anchored.
    _ASSIST_INTENT_RE = re.compile(
        r"^\s*(?:please\s+|can\s+you\s+)?assist\b"
        r"|\bhelp\s+me\b[^.]{0,40}?\b(?:implement|complet|finish|"
        r"set\s*up|setup|deploy|configur|install|build\s+out|run)"
        r"|\b(?:step\s+through|walk\s+me\s+through)\b",
        re.IGNORECASE,
    )

    # §17.539 — history-based active-session recovery anchor. chat_id is
    # structurally unavailable to an external pipe (OWUI pops `metadata` from
    # the body for OpenAI-compat endpoints AND the pipelines server passes no
    # request headers to pipe(), so X-OpenWebUI-Chat-Id never arrives), so
    # routing cannot rely on it. The conversation `messages` ARE reliably
    # delivered (full history — that is why _window_messages exists), and the
    # assist-start turn carries the session id ("🤝 Assist session started —
    # `<sid>`"). This regex recovers that id from history, mirroring §17.444's
    # _extract_pending_brief. \W+ consumes the "** — `" between the phrase and
    # the UUID (backtick included).
    _ASSIST_SESSION_MARKER_RE = re.compile(
        r"Assist session started\W+"
        r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
        r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
    )

    # §17.627 — hidden ordered-id marker rendered by the natural-start pick-list
    # (`render_candidate_list`), recovered on the next turn to resolve a short
    # selector reply ("1" / "the proxmox one") back to a job. Mirrors the
    # §17.444 pending-brief marker recovery pattern.
    _ASSIST_PICK_RE = re.compile(r"<!--ASSIST_PICK:([0-9a-fA-F,\-]+)-->")

    _ASSIST_NUDGE = (
        "💡 **Looking for Assist Mode?** Typing \"assist\" / \"help me "
        "implement\" in chat starts a *planning* conversation (below), not "
        "the interactive step-through executor. To run an existing job's "
        "steps yourself with the engine as co-pilot, use "
        "`/assist <job_id>` — find the id with `/here`. The job must still "
        "be in progress (`planning`/`executing`/`blocked`/`failed`), not "
        "already completed.\n\n"
        "---\n\n"
    )

    @classmethod
    def _looks_like_assist_intent(cls, msg: str) -> bool:
        """True when free-text `msg` reads as a request to use Assist Mode
        but isn't the `/assist` command (slash commands are dispatched
        before this is ever consulted)."""
        return bool(msg) and bool(cls._ASSIST_INTENT_RE.search(msg))

    @staticmethod
    def _is_first_turn(messages: list[dict]) -> bool:
        """True when the user has sent exactly one user-message in this chat.

        OWUI passes the full chat history to ``pipe()``; on a brand-new
        chat the user's first message is the only user-role entry. Any
        prior assistant turns (e.g., an OWUI greeting added by another
        pipeline) are ignored — we count user-role only.
        """
        user_count = sum(
            1 for m in (messages or [])
            if isinstance(m, dict) and m.get("role") == "user"
        )
        return user_count <= 1

    # ------------------------------------------------------------------
    # Triage / synthesis
    # ------------------------------------------------------------------

    def _window_messages(self, messages: List[dict]) -> List[dict]:
        """Cap triage history to last N turns; always pin the first user message.

        Mitigates qwen3:4b CPU latency growth on long conversations.
        Window size is set by valves.triage_history_window.
        """
        n = max(1, int(self.valves.triage_history_window))
        if len(messages) <= n:
            return messages
        first_user_idx = next(
            (i for i, m in enumerate(messages) if m.get("role") == "user"),
            None,
        )
        tail = messages[-n:]
        if first_user_idx is None or first_user_idx >= len(messages) - n:
            return tail
        return [messages[first_user_idx]] + tail

    @staticmethod
    def _strip_think(text: str) -> str:
        """§17.605 — remove <think>/<thinking> reasoning blocks (closed OR
        open/truncated) from a thinking-model response. triage_model is a
        thinking model, so without this the raw chain-of-thought leaked to chat
        (synthesis already stripped; triage didn't)."""
        text = re.sub(r"<think(?:ing)?>.*?</think(?:ing)?>", "", text, flags=re.DOTALL)
        text = re.sub(r"<think(?:ing)?>.*", "", text, flags=re.DOTALL)
        return text.strip()

    def _direct_completion(self, messages: List[dict]) -> str:
        """§17.634 — a raw LLM completion of the given messages with NO triage
        system prompt and NO routing. For OWUI background/task calls
        (title/tag/follow-up/…), whose prompt already carries its own
        instructions; must never touch the side-effectful assist/continuity/
        command paths. Fail-soft → empty string (OWUI falls back to a default
        title/tags)."""
        payload = {
            "model": self.valves.triage_model,
            "messages": self._clean_messages(messages),
            "stream": False,
        }
        try:
            r = _HTTP_SESSION.post(
                f"{self.valves.ollama_url}/v1/chat/completions",
                json=payload,
                timeout=self.valves.triage_timeout,
            )
            if r.status_code == 200:
                return self._strip_think(
                    r.json()["choices"][0]["message"]["content"]
                ) or ""
            self.logger.debug("direct_completion HTTP %s", r.status_code)
        except Exception as e:  # noqa: BLE001 — never break a task call
            self.logger.debug("direct_completion failed: %s", e)
        return ""

    def _call_triage(self, messages: List[dict]) -> str:
        clean = self._window_messages(self._clean_messages(messages))
        payload = {
            "model": self.valves.triage_model,
            "messages": [{"role": "system", "content": TRIAGE_SYSTEM_PROMPT}] + clean,
            "stream": False,
        }
        try:
            r = _HTTP_SESSION.post(
                f"{self.valves.ollama_url}/v1/chat/completions",
                json=payload,
                timeout=self.valves.triage_timeout,
            )
            if r.status_code == 200:
                content = self._strip_think(
                    r.json()["choices"][0]["message"]["content"]
                )
                # Guard empty-after-strip (reasoning consumed the whole reply).
                return content or (
                    "⚠️ Triage produced no visible output. "
                    "Type `/go` to launch directly."
                )
            return f"⚠️ Triage model error (HTTP {r.status_code}). Type `/go` to launch directly."
        except requests.exceptions.ConnectionError:
            return "⚠️ Cannot reach Ollama for triage. Type `/go` to launch directly."
        except Exception as e:
            self.logger.error("Triage call error: %s", e)
            return f"⚠️ Triage error: {e}. Type `/go` to launch directly."

    def _synthesize_idea(self, messages: List[dict]) -> tuple[str, bool]:
        """Synthesize a launch plan from the chat history.

        Returns ``(text, used_fallback)``:
          * ``text`` — the synthesized plan (or fallback concatenation
            of raw user messages when synthesis failed).
          * ``used_fallback`` — True when the Ollama call errored, HTTP-
            errored, or returned empty-after-think-strip, so the caller
            yielded a visible warning to the chat. §17.200 — pre-§17.200
            the fallback was silent (logged at INFO only); operators saw
            the orchestrator launch with a plan they couldn't reconcile
            against what they typed.
        """
        clean = self._clean_messages(messages)
        if not any(m["role"] == "user" for m in clean):
            return "", False

        transcript = "\n\n".join(
            f"{'User' if m['role']=='user' else 'Assistant'}: {m['content']}"
            for m in clean
        )
        payload = {
            "model": self.valves.triage_model,
            "messages": [
                {"role": "system", "content": SYNTHESIS_SYSTEM_PROMPT},
                {"role": "user", "content":
                    f"Here is the planning conversation. Extract the final agreed-upon plan:\n\n{transcript}"},
            ],
            "stream": False,
        }
        try:
            r = _HTTP_SESSION.post(
                f"{self.valves.ollama_url}/v1/chat/completions",
                json=payload,
                timeout=self.valves.triage_timeout,
            )
            if r.status_code == 200:
                raw = r.json()["choices"][0]["message"]["content"].strip()
                self.logger.info("Synthesis raw (%d chars): %s", len(raw), raw[:200])
                cleaned = self._strip_think(raw)  # §17.605 — shared helper
                if cleaned:
                    return cleaned, False
                self.logger.info("Synthesis cleaned to empty, using fallback")
            else:
                self.logger.error("Synthesis HTTP %s: %s", r.status_code, r.text[:300])
        except Exception as e:
            self.logger.error("Synthesis error: %s", e)

        user_texts = [m["content"] for m in clean if m["role"] == "user"]
        fallback = " ".join(user_texts)
        self.logger.info("Synthesis fallback (%d chars): %s", len(fallback), fallback[:200])
        return fallback, True

    # ------------------------------------------------------------------
    # Main entry
    # ------------------------------------------------------------------

    def _log_pipe_inputs(
        self, user_message: str, messages: List[dict], body: dict
    ) -> None:
        """One-shot diagnostic log of pipe() inputs.

        Gated on valves.log_pipe_inputs. Captures the shape of OWUI
        payloads to diagnose intermittent file-routing failures (cases
        where uploaded file content does not appear in user_message).
        """
        try:
            body_keys = sorted(body.keys()) if isinstance(body, dict) else []
            meta = body.get("metadata") if isinstance(body, dict) else None
            meta_keys = sorted(meta.keys()) if isinstance(meta, dict) else []
            files_field = body.get("files") if isinstance(body, dict) else None
            files_meta = meta.get("files") if isinstance(meta, dict) else None
            files_count = (
                len(files_field) if isinstance(files_field, list)
                else len(files_meta) if isinstance(files_meta, list)
                else 0
            )
            file_ids = body.get("file_ids") if isinstance(body, dict) else None
            last_role = messages[-1].get("role") if messages else None
            um_len = len(user_message)
            head = user_message[:80].replace("\n", "\\n")
            tail = user_message[-80:].replace("\n", "\\n") if um_len > 80 else ""
            print(  # noqa: T201
                f"[scaffold_router] PIPE_INPUTS body_keys={body_keys} "
                f"metadata_keys={meta_keys} files_count={files_count} "
                f"file_ids={file_ids!r} messages_n={len(messages)} "
                f"last_role={last_role!r} user_message_len={um_len} "
                f"head={head!r} tail={tail!r}"
            )
        except Exception as e:
            print(f"[scaffold_router] PIPE_INPUTS log failed: {e}")  # noqa: T201

    def _classify_dispatch(self, msg: str) -> tuple[str, str | None]:
        """Sprint X.7 — mirror the dispatch chain in pipe() to compute a
        decision string + matched command for diagnostic logging.

        Returns ``(decision, matched_command)``:
          - ``("command:<name>", "<name>")`` when a command branch will match
          - ``("command:unrecognized", "<first-token>")`` when slash-prefixed
            but no handler matches
          - ``("triage", None)`` when the message falls through to triage

        The dispatch chain in ``pipe()`` is the canonical source of truth;
        this function intentionally duplicates its predicates so the
        logging path is a pure side-channel that can't accidentally change
        routing behavior. If you add a new command branch in ``pipe()``,
        add the matching predicate here too.
        """
        if self._is_cmd(msg, "/go", "/run"):
            return ("command:/go", "/go")
        if self._is_cmd(msg, "/research/reply"):
            return ("command:/research/reply", "/research/reply")
        if self._is_cmd(msg, "/research/list", "/research/find",
                        "/research/rename", "/research/delete", "/research/help"):
            return ("command:/research/mgmt", self._first_token(msg))
        if self._is_cmd(msg, "/research"):
            return ("command:/research", "/research")
        if self._is_cmd(
            msg,
            "/assist", "/assist/next", "/assist/submit", "/assist/skip",
            "/assist/handoff", "/assist/pause", "/assist/resume",
            "/assist/done", "/assist/friction", "/assist/help",
        ):
            return ("command:/assist", self._first_token(msg))
        if self._is_cmd(msg, "/execute"):
            return ("command:/execute", "/execute")
        if self._is_cmd(msg, "/confirm"):
            return ("command:/confirm", "/confirm")
        if msg.startswith("/"):
            return ("command:unrecognized", self._first_token(msg) or "/")
        return ("triage", None)

    def _log_routing_decision(
        self,
        decision: str,
        msg_len: int,
        *,
        command: str | None = None,
        wrapper_stripped: str | None = None,
        files_count: int = 0,
        normalize_rewrites: int = 0,
        body: dict | None = None,
    ) -> None:
        """Sprint X.7 — one structured line per pipe() call with the routing
        decision and the context that drove it.

        Gated on ``valves.log_routing_decisions``. Distinct from
        ``_log_pipe_inputs`` (which dumps raw input shape) — this one
        captures *what the router did with it* so an operator can answer
        "why didn't my /research command run" or "why did my uploaded PDF
        content not appear in triage" by reading a single line.

        ``decision`` is one of:
          - ``command:<name>`` — dispatched to a specific command handler
          - ``command:unrecognized`` — slash-prefixed but no handler
          - ``triage`` — fell through to the LLM triage path
        """
        try:
            files_field = body.get("files") if isinstance(body, dict) else None
            file_ids = body.get("file_ids") if isinstance(body, dict) else None
            file_ids_count = len(file_ids) if isinstance(file_ids, list) else 0
            print(  # noqa: T201
                f"[scaffold_router] ROUTING_DECISION decision={decision!r} "
                f"command={command!r} wrapper_stripped={wrapper_stripped!r} "
                f"msg_len={msg_len} files_count={files_count} "
                f"file_ids_count={file_ids_count} "
                f"normalize_rewrites={normalize_rewrites} "
                f"has_files_field={files_field is not None}"
            )
        except Exception as e:
            print(f"[scaffold_router] ROUTING_DECISION log failed: {e}")  # noqa: T201

    def pipe(
        self,
        user_message: str,
        model_id: str,
        messages: List[dict],
        body: dict,
    ) -> Generator[str, None, None]:
        if self.valves.log_pipe_inputs:
            self._log_pipe_inputs(user_message, messages, body)
        msg = user_message.strip()
        # Strip Open WebUI context wrapper. Apr 26 2026: hardened from a
        # single-tag regex (only matched </context>) to a multi-wrapper sweep
        # plus a heuristic warning when a long message looks like it carries
        # an unrecognized wrapper. Closes overview "Known Open Issues" #9.
        wrapper_stripped: str | None = None  # X.7: surface which tag matched
        for closing_tag in ("</context>", "</documents>", "</source>"):
            if closing_tag in msg:
                msg = msg.rsplit(closing_tag, 1)[-1].strip()
                wrapper_stripped = closing_tag
                break
        else:
            # No known wrapper matched. If the message looks like it might
            # contain one (long + leading angle bracket), warn so format
            # drift is visible instead of silently feeding a context dump
            # into triage.
            if len(msg) > 2000 and msg.startswith("<"):
                # Pipelines runs in its own container — use print() since the
                # Open WebUI Pipelines logger isn't always wired up.
                print(  # noqa: T201
                    f"[scaffold_router] WARN: message starts with '<' and is "
                    f"{len(msg)} chars but no known closing tag matched. "
                    f"Open WebUI may have changed wrapper format. "
                    f"First 80 chars: {msg[:80]!r}"
                )

        body["stream"] = True

        # §17.634 — OWUI background/task calls (title / tag / follow-up / query /
        # emoji / autocomplete generation) arrive through THIS same pipe, marked
        # with `body.metadata.task`. They are NOT user turns: routing them
        # through triage/assist/continuity/command paths produces garbage titles
        # AND — since §17.633 — has real side effects (continuity reconnection
        # calls assist_start, spuriously starting other assist sessions; caught
        # in a live OWUI browser test). Short-circuit to a raw, routing-free,
        # side-effect-free completion so OWUI still gets its title/tags/etc.
        _meta = body.get("metadata") if isinstance(body, dict) else None
        _task = (_meta or {}).get("task") if isinstance(_meta, dict) else None
        if _task:
            self.logger.info(
                "owui task call task=%s → direct completion (no routing)", _task,
            )
            yield self._direct_completion(messages)
            return

        # Normalize input (Tier 1 #1): NFKC + unicode-dash -> `--` + `-flag` -> `--flag`.
        # Surface rewrites so the parser's behavior is visible (Tier 1 #13).
        msg, _rewrites = _normalize_input(msg)
        if _rewrites:
            yield f"_Note: interpreted {', '.join(_rewrites)}._\n\n"

        # X.7 — emit a single routing-decision log line just before dispatch.
        # The decision string mirrors the dispatch chain below; intentional
        # duplication so the logging stays a pure side-channel and the
        # dispatch flow itself is untouched.
        if self.valves.log_routing_decisions:
            decision, matched_cmd = self._classify_dispatch(msg)
            files_field = body.get("files") if isinstance(body, dict) else None
            meta = body.get("metadata") if isinstance(body, dict) else None
            meta_files = meta.get("files") if isinstance(meta, dict) else None
            files_count = (
                len(files_field) if isinstance(files_field, list)
                else len(meta_files) if isinstance(meta_files, list)
                else 0
            )
            self._log_routing_decision(
                decision, len(msg),
                command=matched_cmd,
                wrapper_stripped=wrapper_stripped,
                files_count=files_count,
                normalize_rewrites=len(_rewrites or []),
                body=body,
            )

        # §17.562 — guided/minimal surface + streaming core verbs. The
        # /advanced toggle is handled first (it controls the gate). Then non-
        # core commands are blocked with a one-line hint when advanced mode is
        # off. Then the streaming /resume verb dispatches. Plain text and core
        # verbs fall through to the normal chain untouched.
        if msg.startswith("/"):
            if self._is_cmd(msg, "/advanced"):
                yield self._handle_advanced(msg); return
            gate = self._gate_advanced(msg)
            if gate is not None:
                yield gate; return
            if self._is_cmd(msg, "/resume"):
                yield from self._handle_resume(body=body); return

        # Word-boundary command dispatch (#8.6)
        if self._is_cmd(msg, "/go", "/run"):
            yield from self._handle_go(msg, messages); return
        if self._is_cmd(msg, "/research/reply"):
            yield from self._handle_research_reply(msg); return
        if self._is_cmd(msg, "/research/list", "/research/find", "/research/rename", "/research/delete", "/research/help"):
            yield from self._handle_research_mgmt(msg); return
        if self._is_cmd(msg, "/research"):
            yield from self._handle_research(msg); return
        if self._is_cmd(
            msg,
            "/assist", "/assist/next", "/assist/submit", "/assist/skip",
            "/assist/handoff", "/assist/pause", "/assist/resume",
            "/assist/done", "/assist/friction", "/assist/help",
        ):
            yield from self._handle_assist(msg, body=body); return
        if self._is_cmd(msg, "/execute"):
            # §17.314 — pass chat_id to support the confirmation-
            # friction recall path. State-altering recall: show 📌
            # + require explicit `/execute confirm` to fire.
            yield from self._handle_execute(
                msg, chat_id=self._chat_id_from_body(body),
            ); return
        if self._is_cmd(msg, "/confirm"):
            yield from self._handle_confirm(msg, body=body); return

        if msg.startswith("/"):
            # §17.307 — extract chat_id for active-job memory. Same
            # source as the /assist chatmap path.
            result = self._handle_command(
                msg, chat_id=self._chat_id_from_body(body),
            )
            if result:
                # §17.562 — append the "what's next" footer to lookup-class
                # replies that don't already render next steps. Non-streaming
                # path only — never inside an SSE stream.
                _, base = self._command_base(msg)
                if base in _FOOTER_COMMANDS:
                    result = result + self._status_footer()
                yield result
            return

        # §17.300 / §17.633 — first-touch orientation is deferred to JUST BEFORE
        # the planner (see the end of pipe()) so it only shows when the turn is
        # actually a new idea — not when the message reconnects to in-progress
        # work or drives a command. Prevents the "👋 Welcome, describe what you
        # want to build" preamble from prefacing a resume.

        # §17.627 — natural-start disambiguation follow-up. If the previous turn
        # offered an assist candidate pick-list, a short selector reply ("1",
        # "the proxmox one", "second") starts that job. Checked BEFORE the noise
        # guard so a bare "1" isn't swallowed. A non-matching reply falls through
        # to normal routing (maybe it's a new idea after all).
        if not msg.startswith("/"):
            pending = self._extract_pending_candidates(messages)
            if pending:
                picked = _assist.resolve_candidate_pick(self, msg, pending)
                if picked:
                    yield from _assist.assist_start(
                        self, picked, chat_id=self._chat_id_from_body(body),
                    )
                    return

        # §17.629 — pending NL-command confirm follow-up. If the previous turn
        # rendered a confirm card for an expensive write (research / schedule)
        # and this turn is an affirmative ("go"/"yes"), fire the stashed action.
        # Checked BEFORE the noise guard (a bare "go" mustn't be swallowed) and
        # gated on the valve. A non-affirmative reply discards the pending
        # action and falls through to normal routing (the operator changed
        # their mind or is refining).
        if not msg.startswith("/") and self.valves.nl_command_routing_enabled:
            pend = self._extract_pending_nl_confirm(messages)
            if pend and self._is_affirmative(msg):
                yield from self._execute_nl_action(
                    pend, chat_id=self._chat_id_from_body(body),
                )
                return

        # §17.349 — guard against single-char / noise input (e.g. the
        # bare "a" case from the §17.342 transcript). The triage prompt
        # is expensive (cloud roundtrip, ~7-10 s) and unhelpful on input
        # the user clearly typed by accident or while mid-thought. Cut
        # the LLM call entirely; respond with a short clarifying nudge
        # instead. Bar is intentionally low: 2 chars after strip — catches
        # "a", "?", "x" while preserving "hi" / "ok" as real (if terse)
        # input. The first user message is excluded from this check on
        # purpose: a brand-new chat may not have history yet, and the
        # welcome preamble above already gives the operator orientation.
        last_user = msg.strip() if msg else ""
        if len(last_user) < 2 and not self._is_first_turn(messages):
            yield (
                "I didn't catch that — could you describe what you're "
                "trying to build or change? A sentence or two is enough."
            )
            return

        # §17.537 — assist-aware chat routing. When THIS chat has an ACTIVE
        # assist session, plain text is a conversational turn on that session,
        # not a new triage idea: route it to the current step's guidance so the
        # user gets grounded, step-by-step help instead of the planner's
        # repeating Scope/Options/Gaps blocks. Falls through to triage on a
        # recall miss or a paused/terminal session. Placed BEFORE the §17.504
        # nudge — that nudge points at `/assist <job_id>` and is wrong/confusing
        # once the user is already inside an active session.
        # §17.539 — resolve the active session from chat_id (fast path, when
        # OWUI delivers one) OR from the conversation history (robust path —
        # the confirmed reality is OWUI does NOT deliver chat_id to an external
        # pipe, so the history marker is the load-bearing signal). Either way
        # the routing decision no longer depends on OWUI's metadata/header quirks.
        cid = self._chat_id_from_body(body)
        active = (
            self._active_assist_session(cid)
            or self._active_assist_session_via_history(messages)
        )
        if active:
            # §17.626 — plain text in an active session is an intent, not just a
            # refine hint: classify it and route (submit/skip/next/fix/…).
            yield from self._assist_nl_turn(
                active["session_id"], msg,
                node_key=active.get("last_node_key"), chat_id=cid,
            )
            return

        # §17.633 — cross-chat continuity (supersedes the narrow §17.626 start).
        # No session is bound to THIS chat, but the operator may be continuing
        # IN-PROGRESS assist work from another chat (the common case: OWUI sent
        # no chat_id AND a new chat has no session marker, so the two discovery
        # paths above both no-op). Reconnect on a topic reference ("continue the
        # proxmox setup", "help me finish the firewall") OR a resume phrasing
        # ("what's next", "where were we"): a strong/unique match resumes the
        # session immediately (re-presenting the step + re-emitting the marker so
        # THIS chat tracks it), ambiguous → pick-list, no signal → None so a
        # genuinely-new idea falls through to the planner untouched.
        if self.valves.assist_continuity_enabled:
            reconnect = self._reconnect_in_progress(msg, cid)
            if reconnect is not None:
                yield from reconnect
                return
        # §17.504 — assist-intent phrasing but nothing to reconnect to: surface
        # the entry point, then let the planner handle the apparently-new idea.
        if self._looks_like_assist_intent(msg):
            yield self._ASSIST_NUDGE

        # §17.628 — engine-wide natural-language command routing. Before falling
        # through to the planner, check whether this plain message clearly names
        # a read-only engine action (status / results / RAG query / jobs+model
        # listing / help) and, if so, drive that component. High-confidence
        # intercept only: ambiguous or idea-shaped input returns None here and
        # continues to triage untouched. Placed AFTER the assist paths (an
        # active session or a "help me do X" start both take precedence) and
        # BEFORE triage (so a clear read isn't misread as a new idea).
        handled = self._nl_command_route(msg, messages, chat_id=cid)
        if handled is not None:
            yield from handled
            return

        # §17.300 / §17.633 — first-turn orientation, shown only now that we've
        # decided this turn is a new idea (not a reconnect / command). If the
        # operator has in-progress assist work from another chat, the
        # in-progress banner takes precedence (more relevant + how to resume);
        # otherwise the first-touch welcome preamble orients a brand-new user.
        if self._is_first_turn(messages):
            banner = (
                self._in_progress_banner()
                if self.valves.assist_continuity_enabled else ""
            )
            if banner:
                yield banner
            elif self.valves.show_welcome_on_first_turn:
                yield self._WELCOME_PREAMBLE

        yield self._call_triage(messages)

    # ------------------------------------------------------------------
    # Generator command handlers
    # ------------------------------------------------------------------

    # §17.444 (Phase A / A5) — marker that lets `/go confirm` recover the EXACT
    # brief shown on the prior `/go` turn from chat history (stateless, no
    # re-synthesis drift between what was shown and what launches).
    _PENDING_BRIEF_MARKER = "📋 **Proposed launch brief:**"

    def _extract_pending_brief(self, messages: List[dict]) -> str | None:
        """Recover the most recent gated brief from a prior assistant turn."""
        for m in reversed(messages):
            if m.get("role") != "assistant":
                continue
            content = m.get("content", "")
            if isinstance(content, str) and self._PENDING_BRIEF_MARKER in content:
                after = content.split(self._PENDING_BRIEF_MARKER, 1)[1]
                brief = after.split("\n---", 1)[0].strip()
                if brief:
                    return brief
        return None

    def _handle_go(self, msg: str, messages: List[dict]) -> Generator[str, None, None]:
        tokens = msg.split()
        is_confirm = len(tokens) >= 2 and tokens[1].lower() == "confirm"

        # `/go confirm` — launch the exact brief shown on the previous `/go`.
        if is_confirm:
            pending = self._extract_pending_brief(messages)
            if pending:
                yield f"> **Launching with:** {pending}\n\n---\n\n"
                yield from self._auto_chain(pending)
                return
            yield ("_(No pending brief found — re-synthesizing from the "
                   "conversation and launching.)_\n\n")

        chat_history = [
            m for m in messages
            if not (
                m["role"] == "user"
                and isinstance(m.get("content"), str)
                and self._is_cmd(m["content"], "/go", "/run")
            )
        ]
        user_msgs = [m for m in chat_history if m["role"] == "user"]
        if not user_msgs:
            yield "Nothing to launch yet — describe your idea first, then type `/go`."
            return

        yield f"📋 Synthesizing from {len(user_msgs)} user message(s)...\n\n"
        synthesized, used_fallback = self._synthesize_idea(chat_history)

        if not synthesized or len(synthesized.strip()) < 10:
            yield ("⚠️ Synthesis produced an empty or too-short result. "
                   "Here's what I captured from your messages:\n\n")
            for i, m in enumerate(user_msgs, 1):
                content = m.get("content", "")
                if isinstance(content, list):
                    content = " ".join(c.get("text", "") for c in content if c.get("type") == "text")
                yield f"{i}. {content[:200]}\n"
            yield "\nPlease try rephrasing your idea in a single message, then type `/go`."
            return

        # §17.200 — when the LLM synthesis path failed (transport / HTTP
        # error / empty-after-think-strip) but the fallback "join user
        # messages" produced something usable, surface a visible warning
        # so the user knows their plan wasn't actually LLM-refined. Pre-
        # §17.200 this fallback was silent (logged at INFO only) and
        # operators saw the orchestrator launch with a plan they
        # couldn't reconcile against what they typed.
        if used_fallback:
            yield ("⚠️ Couldn't synthesize a plan from this conversation "
                   "(triage LLM failed); using your raw messages instead. "
                   "Consider rephrasing in a single message if the launch "
                   "doesn't match your intent.\n\n")

        # §17.444 (Phase A / A5) — correction gate. Show the brief and stop;
        # the user reviews it and types `/go confirm` to commit the long run,
        # or keeps chatting to refine. Skipped when this turn IS the confirm
        # (re-synthesis fallback above) or the valve is disabled.
        if self.valves.confirm_before_launch and not is_confirm:
            yield (
                f"{self._PENDING_BRIEF_MARKER}\n\n{synthesized}\n\n---\n\n"
                "Type `/go confirm` to launch this (≈10–25 min on this host), "
                "or keep chatting to refine it first."
            )
            return

        yield f"> **Launching with:** {synthesized}\n\n---\n\n"
        yield from self._auto_chain(synthesized)

    def _handle_research_reply(self, msg: str) -> Generator[str, None, None]:
        parts = msg.split(None, 2)
        if len(parts) < 3:
            yield "Usage: `/research/reply <session_id> <your reply>`"
            return
        session_id = parts[1].strip()
        user_reply = parts[2].strip()
        if _is_placeholder(session_id):
            yield (
                "Looks like a placeholder slipped through. Replace "
                f"`{session_id}` with the actual session ID from the "
                "research-paused message."
            )
            return
        if not user_reply or _is_placeholder(user_reply):
            yield "Reply cannot be empty (or a placeholder)."
            return
        yield f"▶️ Resuming session `{session_id}` ...\n\n"
        yield from self._research_reply_and_stream(session_id, user_reply)

    def _handle_research(self, msg: str) -> Generator[str, None, None]:
        # Parser shared by /research and /schedule add (Tier 1 #1, #2, #3, #5).
        parser = CommandParser("research", "Autonomous web research")
        parser.add_argument(
            "--depth", choices=["shallow", "medium", "deep"], default="medium",
            help="Research iteration count",
        )
        parser.add_example("/research kubernetes pods --depth=deep")
        parser.add_example("/research https://example.com/article")
        parser.add_example("/research github:owner/repo")
        parser.add_example("/research openapi:https://api.example.com/openapi.json")

        parts = msg.split(None, 1)
        raw_args = parts[1] if len(parts) > 1 else ""

        # §17.310 — `/research` (no args) and `/research --help` both
        # surface the rich modes panel. Pre-§17.310 they dumped the
        # parser's plain help_text (4 example lines, no purpose
        # column). The panel teaches WHEN to use each mode, not just
        # WHAT they look like.
        if raw_args.strip() in ("", "--help", "-h", "help"):
            yield self._research_modes_panel()
            return

        # §17.215 E2 — /research is autonomous (20-60 min wall time) and
        # easy to mistake for /rag (instant lookup). Detect a `--confirm`
        # flag and strip it before parsing; without it, short plain-text
        # queries get a disambiguation prompt rather than booting Phase 2.
        # Long topics, URL/github:/hf:/arxiv:/openapi: prefixes, and
        # scripted callers that pass --confirm bypass the prompt.
        confirm_explicit = False
        stripped_tokens = []
        for tok in raw_args.split():
            if tok == "--confirm":
                confirm_explicit = True
                continue
            stripped_tokens.append(tok)
        raw_args = " ".join(stripped_tokens)

        try:
            args, topic, _ = parser.parse(raw_args)
        except _ChatArgError as e:
            yield str(e)
            return

        # Placeholder rejection (#3).
        if _is_placeholder(topic):
            yield ("It looks like the topic is missing or a placeholder. "
                   "Try `/research what changed in the codebase last week`.\n\n"
                   + parser.help_text())
            return

        if not confirm_explicit and self._looks_like_rag_query(topic):
            yield (
                "**`/research` runs 20-60 min of autonomous web research.** "
                "It looks like a short query — did you mean `/rag`?\n\n"
                f"- **Quick lookup (seconds):** `/rag {topic}`\n"
                f"- **Autonomous research (20-60 min):** "
                f"`/research {topic} --confirm`\n"
                "\n💡 If you have a URL / repo / spec instead, "
                "`/research` accepts modes: `<url>`, `github:owner/repo`, "
                "`openapi:<url>`. See `/research --help`.\n"
            )
            return

        yield f"🔬 Researching: **{topic}** (depth: {args.depth})\n\n"
        yield from self._research_and_stream(topic, args.depth)

    # §17.215 E2 — heuristic used by _handle_research to decide whether
    # to surface the disambiguation prompt (vs. firing Phase 2). Returns
    # True for "looks more like a /rag query than a research topic":
    # ≤4 tokens AND no URL / github: / hf: / arxiv: / openapi: / pdf:
    # prefix. URLs and source-prefixed forms always pass through (they
    # are unambiguously research-mode inputs).
    _RESEARCH_PREFIX_RE = re.compile(
        r"^(?:https?://|github:|hf:|arxiv:|openapi:|pdf:)",
        re.IGNORECASE,
    )

    def _looks_like_rag_query(self, topic: str) -> bool:
        t = topic.strip()
        if not t:
            return False
        if self._RESEARCH_PREFIX_RE.match(t):
            return False
        return len(t.split()) <= 4

    @staticmethod
    def _research_modes_panel() -> str:
        """§17.310 — rich mode-discovery panel surfaced by `/research`
        (no args) and `/research --help`. Pre-§17.310 both paths dumped
        the parser's plain help_text — 4 example lines with no purpose
        column. The panel teaches WHEN to use each mode, not just WHAT
        they look like.

        Each row uses a short id-shape match (`abc1234e`-style fragment
        for any /research/<sub> references is unnecessary here — modes
        don't carry job_ids).
        """
        return (
            "**`/research` — Autonomous web research**\n\n"
            "Pick a mode based on what you have:\n\n"
            "| Mode | When to use | Example |\n"
            "|---|---|---|\n"
            "| **Topic** | Open-ended question; let the agent discover sources | `/research kubernetes best practices` |\n"
            "| **URL** | Specific page you want ingested verbatim | `/research https://example.com/article` |\n"
            "| **GitHub** | Repo's README, docs, and module docstrings | `/research github:owner/repo` |\n"
            "| **OpenAPI** | OpenAPI/Swagger spec — one entry per endpoint | `/research openapi:https://api.example.com/openapi.json` |\n"
            "| **PDF** | Local PDF (drag-drop UI or `curl -F`) | `/research/pdf` |\n"
            "\n"
            "**Flags:**\n"
            "- `--depth shallow | medium | deep` — iteration count "
            "(default: medium)\n"
            "- `--confirm` — bypass the short-query disambiguation "
            "prompt (scripted callers)\n"
            "\n"
            "**Manage saved sessions:** `/research/help` "
            "(list / find / rename / delete / reply / schedule)\n"
        )

    def _handle_execute(
        self, msg: str, *, chat_id: str | None = None,
    ) -> Generator[str, None, None]:
        parts = msg.split()

        # §17.314 — confirmation-friction recall pilot for state-
        # altering commands. /execute kicks off all pending DAG nodes,
        # so we can't auto-substitute the recalled id like /results
        # (§17.307) or /logs (§17.311) — a muscle-memory `/execute`
        # alone could fire on the wrong job. Instead: show the recalled
        # job via 📌, list 3 options, require explicit `/execute
        # confirm` to fire on the recalled id.
        if len(parts) == 2 and parts[1].lower() == "confirm":
            recalled = self._active_job_recall(chat_id)
            if not recalled or not recalled.get("job_id"):
                yield (
                    "❌ `/execute confirm` requires an active job in chat "
                    "memory, but none is set.\n\n"
                    "Pass an explicit job_id: `/execute <job_id>`. "
                    "Use `/jobs` to list active jobs."
                )
                return
            rid = recalled["job_id"]
            yield self._active_job_hint(rid, recalled.get("title"))
            yield f"Executing all nodes for job `{rid}`...\n\n"
            yield from self._execute_and_stream(rid, 0)
            return

        if len(parts) < 2:
            recalled = self._active_job_recall(chat_id)
            if recalled and recalled.get("job_id"):
                rid = recalled["job_id"]
                short = rid[:8] if len(rid) >= 8 else rid
                title = recalled.get("title")
                title_part = f" — _{title}_" if title else ""
                yield (
                    f"📌 Active job in this chat: `{short}`{title_part}.\n\n"
                    f"⚠️ `/execute` runs ALL pending DAG nodes — "
                    f"state-altering.\n\n"
                    f"- Type `/execute confirm` to run on `{short}`\n"
                    f"- Type `/execute <other_job_id>` to target a "
                    f"different job\n"
                    f"- Or check the job first: `/results {short}`"
                )
                return
            yield (
                "Usage: `/execute <job_id>`\n"
                "Example: `/execute 01ab243e`\n\n"
                "💡 Use `/jobs` to list your active jobs and copy a job_id."
            )
            return
        # §17.301 — placeholder check
        if _is_placeholder(parts[1]):
            yield (
                "It looks like job_id is missing or a placeholder. "
                "Try `/execute 01ab243e` (use `/jobs` to find a real id)."
            )
            return
        job_id = parts[1]
        yield f"Executing all nodes for job `{job_id}`...\n\n"
        yield from self._execute_and_stream(job_id, 0)

    def _handle_confirm(
        self, msg: str, *, body: dict | None = None,
    ) -> Generator[str, None, None]:
        parts = msg.split(None, 2)
        if len(parts) < 2:
            yield "Usage: `/confirm <job_id> [feedback]`"
            return
        job_id = parts[1]
        if _is_placeholder(job_id):
            yield (
                "Looks like a placeholder slipped through. Replace "
                f"`{job_id}` with the actual job_id from the analysis output."
            )
            return
        payload = {"job_id": job_id, "model_overrides": self._model_overrides()}
        if len(parts) > 2:
            feedback = parts[2]
            if _is_placeholder(feedback):
                yield (
                    "Feedback looks like a placeholder. Either rerun without "
                    f"feedback (`/confirm {job_id}`) or replace `{feedback}` "
                    "with your actual adjustments."
                )
                return
            payload["feedback"] = feedback

        yield "🔬 Starting research and knowledge ingestion — this may take 10-25 minutes on CPU. Progress markers will appear roughly every 2 minutes.\n\n"

        ok, res = yield from self._post_with_keepalive(
            f"{self.valves.orchestrator_url}/ideate/confirm",
            payload, self.valves.stream_timeout,
            progress_label="Phase 2 — researching + ingesting",
        )
        if not ok:
            yield (
                f"\n⚠️ Research phase error: {res}\n\n"
                f"Retry options:\n"
                f"- `/confirm {job_id}` — re-run research and planning\n"
                f"- `/jobs` — check job status\n"
            )
            return
        r = res
        if r.status_code >= 400:
            try:
                err = r.json().get("message") or r.json().get("detail") or r.text[:200]
            except Exception:
                err = r.text[:200]
            drift = self._drift_hint() if r.status_code == 401 else ""
            yield (
                f"\n⚠️ Research phase failed: {err}\n\n"
                f"Retry options:\n"
                f"- `/confirm {job_id}` — re-run research and planning\n"
                f"- `/jobs` — check job status"
                f"{drift}\n"
            )
            return

        yield "\n✅ Research complete — generating execution plan...\n\n"

        ok, res = yield from self._post_with_keepalive(
            f"{self.valves.orchestrator_url}/dag",
            {"job_id": job_id, "model_overrides": self._model_overrides()},
            self.valves.stream_timeout,
            progress_label="Phase 3 — planning DAG",
        )
        if not ok:
            yield (
                f"\n⚠️ DAG generation error: {res}\n\n"
                f"Research finished — only the plan step failed. Retry options:\n"
                f"- `/dag {job_id}` — regenerate the execution plan\n"
                f"- `/jobs` — check job status\n"
            )
            return
        r = res
        if r.status_code >= 400:
            drift = self._drift_hint() if r.status_code == 401 else ""
            yield (
                f"\n⚠️ DAG generation failed (HTTP {r.status_code}).\n\n"
                f"Research finished — only the plan step failed. Retry options:\n"
                f"- `/dag {job_id}` — regenerate the execution plan\n"
                f"- `/jobs` — check job status"
                f"{drift}\n"
            )
            return
        try:
            dag_data = r.json()
            num_nodes = dag_data.get("task_count", len(dag_data.get("tasks", [])))
        except (ValueError, KeyError):
            yield "\n⚠️ Unexpected response from DAG generation."
            return

        # If the operator opted into Assist Mode auto-routing, hand off to
        # /assist/start instead of /execute/all. Default valve is False.
        # Plumb chat_id from body so the auto-into-assist flow gets W.9
        # session memory just like an explicit /assist <job_id> would.
        if self.valves.assist_after_confirm:
            yield f"📋 Execution plan ready — entering Assist Mode for {num_nodes} steps...\n\n"
            yield from self._assist_start(
                job_id, chat_id=self._chat_id_from_body(body),
            )
            return

        # §17.562 — ALWAYS surface the autonomous-vs-assist choice after
        # planning, with a recommendation (was §17.508: only shown when the
        # plan had Shell steps, else it silently auto-executed). Silent
        # auto-run was a top "assist-vs-autonomous confusion" complaint — the
        # user never learned the option existed. Shell-step detection now
        # drives the *recommendation*, not whether the choice appears. A DAG
        # with hands-on/Shell steps recommends Assist (auto-running them only
        # writes runbooks marked done → a "completed" job that built nothing,
        # §17.506); a pure text/code/research DAG recommends Autonomous.
        yield self._execution_choice(job_id, dag_data, num_nodes)
        return

    # ------------------------------------------------------------------
    # Backward-compat aliases (older tests reference these method names)
    # ------------------------------------------------------------------

    def _research_and_stream(self, topic: str, depth: str = "medium"):
        yield from self._research_and_stream_raw(
            "/research",
            {"topic": topic, "depth": depth,
             "model_overrides": self._model_overrides()},
        )

    def _research_reply_and_stream(self, session_id: str, user_reply: str):
        yield from self._research_and_stream_raw(
            "/research/reply",
            {"session_id": session_id, "reply": user_reply,
             "model_overrides": self._model_overrides()},
        )

    # ------------------------------------------------------------------
    # /assist — Assistant Mode chat surface
    # ------------------------------------------------------------------

    _ASSIST_HELP = (
        "**Assistant Mode** — walk through a job's DAG step-by-step with human evidence.\n\n"
        "After `/assist <job_id>`, this chat remembers the active session, so "
        "`<session_id>` is **optional** in every follow-up. Pass an explicit "
        "`<session_id>` to override (e.g. resume a session from a different chat).\n\n"
        "| Command | Description |\n"
        "|---|---|\n"
        "| `/assist <job_id>` | Start a session and render the first step. |\n"
        "| `/assist next [<session_id>]` | Fetch the next pending step (auto-generates a walkthrough). |\n"
        "| `/assist guide [<session_id>] [refine…]` | (Re)generate the step's walkthrough; add a hint like `redo for macOS`. |\n"
        "| `/assist research [<session_id>] <question>` | Look up + confirm a fact (versions, flags, package names). |\n"
        "| `/assist env [<session_id>] [<text> / KEY=value]` | Set your OS/shell/tools (or concrete values) so commands are real, not placeholders. No args shows current. |\n"
        "| `/assist fix [<session_id>] <error>` | Hit an error? Get a diagnosis + corrected copy-paste commands. |\n"
        "| `/assist verbose [<session_id>] terse\\|normal\\|detailed` | Tune walkthrough detail — terse for experts, detailed for step-by-step. |\n"
        "| `` /assist submit [<session_id>] [<node_key>]\\n```evidence``` `` | Submit human evidence. Both args optional after `/assist next`. |\n"
        "| `/assist skip [<session_id>] [<node_key>]` | Skip a node. |\n"
        "| `/assist handoff [<session_id>] <node_key> [single\\|all]` | Hand a node back to autonomous executor. |\n"
        "| `/assist pause [<session_id>]` | Pause; resume later. |\n"
        "| `/assist resume [<session_id>]` | Resume a paused session. |\n"
        "| `/assist status [<session_id>]` | Show session status, current step, and per-status step counts. |\n"
        "| `/assist done [<session_id>]` | Show the compiled output (clears chat memory). |\n"
        "| `/assist friction [<session_id>] [<node_key>] <note>` | Log a friction note. |\n"
        "| `/assist help` | Show this message. |\n\n"
        "_Tip: paste multi-line evidence inside a triple-backtick fence; it will be captured intact._"
    )

    # UUID4-ish: matches a Postgres `uuid` rendered as a string. Used to
    # decide whether the user's first arg is an explicit session_id or
    # the start of the per-subcommand args (node_key / mode / note).
    _UUID_RE = re.compile(
        r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
        re.IGNORECASE,
    )

    # §17.316 — job_id token detector for state-altering recall paths
    # (§17.315 /exec retry + §17.316 /skip). Matches either a full
    # UUID OR an 8-char hex short_id (the canonical orchestrator-
    # accepted shortened form used throughout the §-doc examples,
    # e.g., "Example: /skip 01ab243e T2"). Anything else is treated
    # as a node_key for auto-substitute via §17.307 active-job recall.
    _JOB_ID_TOKEN_RE = re.compile(
        r"^[0-9a-f]{8}(?:-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})?$",
        re.IGNORECASE,
    )

    @staticmethod
    def _chat_id_from_body(body: dict | None) -> str | None:
        if not isinstance(body, dict):
            return None
        meta = body.get("metadata")
        if not isinstance(meta, dict):
            return None
        cid = meta.get("chat_id")
        return cid if isinstance(cid, str) and cid else None

    # §17.296 — the /assist command surface lives in
    # ``pipelines/_vendor/_assist_handlers.py``. The methods below are
    # thin delegates so existing callers (chat dispatch, tests patching
    # `pipe._assist_*`) keep working unchanged. Behavior is byte-for-
    # byte preserved; only the file boundary moved. Add new /assist
    # logic to the vendor module + add a delegate here.

    def _assist_remember(
        self, chat_id: str | None, *, session_id: str, last_node_key: str | None = None,
    ) -> None:
        return _assist.assist_remember(
            self, chat_id, session_id=session_id, last_node_key=last_node_key,
        )

    def _assist_recall(self, chat_id: str | None) -> dict | None:
        return _assist.assist_recall(self, chat_id)

    def _assist_forget(self, chat_id: str | None) -> None:
        return _assist.assist_forget(self, chat_id)

    def _active_assist_session(self, chat_id: str | None) -> dict | None:
        """§17.537 — recalled assist session for this chat IF it's active.

        The signal for assist-aware chat routing. Returns the chatmap entry
        (`{session_id, last_node_key, status}`) only when the mapped session
        is `active`; a recall miss, a paused session, or a terminal session
        returns None so plain text falls through to the triage planner. Gated
        by `assist_chat_routing_enabled` (and, transitively, by the chatmap's
        own `assist_session_memory_enabled`)."""
        # §17.539 — observability at the silent fork. The §17.537/538
        # assist-vs-triage decision was previously invisible: a missing
        # chat_id or an unwritten chatmap looked identical to "no session"
        # and dropped the user back to triage with no signal (it took
        # Redis/Postgres forensics + two wrong root causes to diagnose). Each
        # skip branch now logs its precise reason. chat_id is an OWUI chat
        # UUID, not user content.
        if not self.valves.assist_chat_routing_enabled:
            return None
        if not chat_id:
            self.logger.info(
                "assist_routing skip reason=chat_id_missing "
                "(OWUI sent no chat_id — unsaved/temporary chat?)"
            )
            return None
        recalled = self._assist_recall(chat_id)
        if not recalled or not recalled.get("session_id"):
            self.logger.info(
                "assist_routing skip reason=no_session_for_chat chat_id=%s "
                "(chatmap never written or no active session)", chat_id,
            )
            return None
        if recalled.get("status") != "active":
            self.logger.info(
                "assist_routing skip reason=session_not_active "
                "chat_id=%s session=%s status=%s",
                chat_id, recalled.get("session_id"), recalled.get("status"),
            )
            return None
        self.logger.info(
            "assist_routing match chat_id=%s session=%s node=%s",
            chat_id, recalled.get("session_id"), recalled.get("last_node_key"),
        )
        return recalled

    def _assist_chat_turn(
        self, session_id: str, refine: str, *,
        node_key: str | None = None, chat_id: str | None = None,
    ) -> Generator[str, None, None]:
        """§17.537 — delegate a plain-language assist-session turn to guidance."""
        yield from _assist.assist_chat_turn(
            self, session_id, refine, node_key=node_key, chat_id=chat_id,
        )

    def _assist_nl_turn(
        self, session_id: str, msg: str, *,
        node_key: str | None = None, chat_id: str | None = None,
    ) -> Generator[str, None, None]:
        """§17.626 — route a plain-language assist-session turn to the right
        action (advance/skip/submit/fix/finalize/pause/question). Supersedes the
        §17.537 always-guide behavior so the operator drives the flow by talking."""
        yield from _assist.assist_nl_turn(
            self, session_id, msg, node_key=node_key, chat_id=chat_id,
        )

    def _assist_try_natural_start(self, msg: str, chat_id: str | None):
        """§17.626 — attempt to START assist from a natural sentence. Returns a
        generator (start stream / candidate list) or None to fall through to
        planning/triage."""
        return _assist.try_natural_start(self, msg, chat_id)

    # §17.633 — resume phrasings: a plain message that reads as "let's carry on"
    # even with no job named. Substring-matched (lowercased). Kept reasonably
    # specific so a genuinely-new idea isn't mistaken for a resume.
    _RESUME_PHRASES = (
        "continue", "keep going", "keep working", "carry on", "resume",
        "where were we", "where did we leave", "left off", "pick up where",
        "pick up from", "what's next", "whats next", "what is next",
        "next step", "back to work", "back to the", "get back to",
        "let's finish", "lets finish", "finish setting up", "finish up",
        "let's keep", "lets keep", "pick back up",
    )

    def _looks_like_resume(self, msg: str) -> bool:
        low = (msg or "").lower()
        return any(p in low for p in self._RESUME_PHRASES)

    def _reconnect_in_progress(self, msg: str, chat_id: str | None):
        """§17.633 — reconnect a chat with no bound session to IN-PROGRESS assist
        work started elsewhere. Returns a generator (resume stream / pick-list)
        or None to fall through to the planner.

        `/assist/candidates` returns in-progress jobs (assisted_executing +
        awaiting_assist). `assist_start` is idempotent on an active session
        (re-presents the current step + re-emits the marker so THIS chat tracks
        it) and starts an awaiting_assist one — so reconnection is just picking
        the right job and calling it."""
        cands = _assist.fetch_assist_candidates(self)
        if not cands:
            self.logger.info("continuity: no in-progress candidates → fall through")
            return None
        # Strong, unique topic match (≥2 distinctive shared tokens) → resume now.
        match, ambiguous = _assist.match_assist_candidate(msg, cands)
        if match and not ambiguous:
            self.logger.info("continuity: reconnect (strong topic match) job=%s",
                             match.get("job_id"))
            return _assist.assist_start(self, match["job_id"], chat_id=chat_id)
        if self._looks_like_resume(msg):
            # Resume intent present → a single distinctive topic token is enough
            # to pick one ("continue proxmox" shares just "proxmox" with the long
            # title). Unique → resume; else offer the in-progress list.
            m2, amb2 = _assist.match_assist_candidate(msg, cands, min_score=1)
            if m2 and not amb2:
                self.logger.info("continuity: reconnect (resume+topic) job=%s",
                                 m2.get("job_id"))
                return _assist.assist_start(self, m2["job_id"], chat_id=chat_id)
            if len(cands) == 1:
                self.logger.info("continuity: reconnect (resume, single) job=%s",
                                 cands[0].get("job_id"))
                return _assist.assist_start(self, cands[0]["job_id"], chat_id=chat_id)
            self.logger.info("continuity: resume phrasing, %d candidates → pick-list",
                             len(cands))
            return iter([_assist.render_candidate_list(cands)])
        # Topic matched but ambiguous (tied / weak) → let the operator choose.
        if match and ambiguous:
            self.logger.info("continuity: ambiguous topic match → pick-list")
            return iter([_assist.render_candidate_list(cands)])
        self.logger.info("continuity: no reconnect signal → fall through to planner")
        return None

    def _in_progress_banner(self) -> str:
        """§17.633 — a brief, additive reminder of in-progress assist work for a
        new chat's first turn. Empty string when there is none (no banner)."""
        cands = _assist.fetch_assist_candidates(self)
        if not cands:
            return ""
        lines = [f"📌 **You have {len(cands)} task(s) in progress:**", ""]
        for c in cands[:5]:
            lines.append(
                f"- **{c.get('title', '(untitled)')}** — `{c.get('status', '?')}`"
            )
        if len(cands) > 5:
            lines.append(f"- …and {len(cands) - 5} more.")
        lines += [
            "",
            "_Say **\"continue <name>\"** (e.g. \"continue proxmox\") to pick up "
            "where you left off — or just describe something new._\n\n---\n",
        ]
        return "\n".join(lines)

    def _session_id_from_history(self, messages: List[dict]) -> str | None:
        """§17.539 — most-recent assist session id named in an assistant turn.

        The assist-start message ("🤝 Assist session started — `<sid>`") is in
        the conversation history OWUI delivers, so we can recover the session
        even when chat_id is absent. Scans newest-first; returns None if no
        assist session was ever started in this chat."""
        for m in reversed(messages or []):
            if m.get("role") != "assistant":
                continue
            content = m.get("content")
            if not isinstance(content, str):
                continue
            match = self._ASSIST_SESSION_MARKER_RE.search(content)
            if match:
                return match.group(1)
        return None

    def _extract_pending_candidates(self, messages: List[dict]) -> list[str]:
        """§17.627 — ordered candidate job_ids from the most-recent assist
        pick-list in history (the `<!--ASSIST_PICK:…-->` marker), or []."""
        for m in reversed(messages or []):
            if m.get("role") != "assistant":
                continue
            content = m.get("content")
            if not isinstance(content, str):
                continue
            match = self._ASSIST_PICK_RE.search(content)
            if match:
                return [x for x in match.group(1).split(",") if x]
        return []

    def _get_assist_session(self, session_id: str) -> dict | None:
        """§17.539 — GET /assist/{sid} → session dict (status, current_node_key,
        …), or None on miss/error. Used to confirm a history-recovered session
        is still live before routing to it."""
        try:
            r = _HTTP_SESSION.get(
                f"{self.valves.orchestrator_url}/assist/{session_id}",
                headers=self._auth_headers(),
                timeout=self.valves.request_timeout,
            )
            if r.status_code == 200:
                d = r.json()
                return d if isinstance(d, dict) else None
        except requests.exceptions.RequestException as e:
            self.logger.warning("get_assist_session failed: %s", e)
        return None

    def _active_assist_session_via_history(
        self, messages: List[dict],
    ) -> dict | None:
        """§17.539 — recover an ACTIVE assist session from conversation history
        when chat_id is unavailable (the confirmed real failure: OWUI never
        delivers chat_id to the pipe). Confirms liveness via GET /assist/{sid}.

        Returns the same `{session_id, last_node_key, status}` shape as
        `_active_assist_session` so the routing fork is signal-agnostic."""
        if not self.valves.assist_chat_routing_enabled:
            return None
        sid = self._session_id_from_history(messages)
        if not sid:
            return None
        sess = self._get_assist_session(sid)
        if not sess:
            self.logger.info(
                "assist_routing history: session %s not found/unreachable", sid,
            )
            return None
        if sess.get("status") != "active":
            self.logger.info(
                "assist_routing history: session %s status=%s (not active)",
                sid, sess.get("status"),
            )
            return None
        self.logger.info(
            "assist_routing history-match session=%s node=%s",
            sid, sess.get("current_node_key"),
        )
        return {
            "session_id": sid,
            "last_node_key": sess.get("current_node_key"),
            "status": "active",
        }

    # §17.307 — active-job chat memory. Mirrors the assist chatmap
    # shape (remember / recall) but in-pipeline (no orchestrator
    # roundtrip; the chatmap endpoint exists for /assist's cross-
    # request session continuity — active-job memory is a UX cache
    # that survives within a single pipeline replica). Pilot scope:
    # /idea writes; /results and /cost read.
    def _active_job_remember(
        self, chat_id: str | None, job_id: str, *, title: str | None = None,
    ) -> None:
        if not chat_id or not self.valves.active_job_memory_enabled:
            return
        if not hasattr(self, "_active_jobs_by_chat"):
            self._active_jobs_by_chat = {}
        self._active_jobs_by_chat[chat_id] = {
            "job_id": job_id,
            "title": title,
            "remembered_at": time.time(),
        }

    def _fetch_work(self) -> dict | None:
        """GET /work — the user's non-terminal jobs + active assist sessions.

        §17.562 — the single-user "you-are-here" primitive backing /here,
        /next, /resume, and the status footer. Returns the parsed dict, or
        None on any error so callers degrade gracefully. This is the DB-
        derived path that does NOT depend on chat_id (OWUI doesn't deliver
        one) — distinct from _active_job_recall, which stays the per-chat
        cache for the existing no-arg id-taking commands (/results, /cost,
        /cancel, …) and their state-altering confirm-friction.
        """
        try:
            r = _HTTP_SESSION.get(
                f"{self.valves.orchestrator_url}/work",
                headers=self._auth_headers(),
                timeout=self.valves.request_timeout,
            )
            if r.status_code >= 400:
                return None
            return r.json()
        except (requests.exceptions.RequestException, ValueError):
            return None

    def _active_job_recall(self, chat_id: str | None) -> dict | None:
        # §17.539 — chat_id is NOT delivered to the pipe in this OWUI setup
        # (see _active_assist_session_via_history), so this recall returns None
        # in practice and no-arg commands ask for an explicit job id. That is a
        # deliberate SAFE degradation: unlike assist routing (§17.539), this
        # feeds state-altering commands (/cancel, /skip, /execute confirm), and
        # "which job is active" is ambiguous from history — a wrong-job
        # auto-recall on a no-arg /cancel is worse than asking for an explicit
        # id. So this is intentionally NOT made history-based. §17.562 — for
        # the read-only "where am I / resume" case, /here, /next and /resume
        # use _fetch_work (DB-derived, chat_id-independent) instead.
        if not chat_id or not self.valves.active_job_memory_enabled:
            return None
        return getattr(self, "_active_jobs_by_chat", {}).get(chat_id)

    @staticmethod
    def _active_job_hint(job_id: str, title: str | None) -> str:
        """§17.307 — render the 📌 hint prepended to recalled-id
        responses. Operator must always be able to see WHICH job was
        recalled so they can recognize a stale cache (e.g., across
        long chat gaps) and pass an explicit id instead."""
        short = job_id[:8] if len(job_id) >= 8 else job_id
        title_part = f" — _{title}_" if title else ""
        return (
            f"📌 Using active job `{short}`{title_part} (most recent "
            f"in this chat). Pass an explicit job_id to override.\n\n"
        )

    # ------------------------------------------------------------------
    # §17.562 — guided/minimal surface + DB-derived core verbs
    # ------------------------------------------------------------------
    @staticmethod
    def _command_base(msg: str) -> tuple[str, str]:
        """(token, base) for a slash command. `/assist next`→('/assist','/assist');
        `/assist/next`→('/assist/next','/assist'); `/research/list`→(…,'/research')."""
        tok = (msg.split(None, 1)[0] or "").lower()
        base = "/" + tok.lstrip("/").split("/", 1)[0]
        return tok, base

    def _gate_advanced(self, msg: str) -> str | None:
        """Return a hint when `msg` is a non-core command and advanced mode is
        off; None when it should dispatch. Unknown commands fall through (None)
        so the existing did-you-mean suggester still runs."""
        if self.valves.advanced_commands_enabled:
            return None
        tok, base = self._command_base(msg)
        if tok in _CORE_COMMANDS or base in _CORE_COMMANDS:
            return None
        if tok not in KNOWN_COMMANDS and base not in KNOWN_COMMANDS:
            return None  # unknown → let _handle_command's suggester handle it
        return (
            f"🔒 `{tok}` is an advanced command. The guided surface keeps "
            f"things simple — type `/advanced on` to enable the full command "
            f"set, then re-run it. (`/help` lists the core verbs.)"
        )

    def _handle_advanced(self, msg: str) -> str:
        parts = msg.split()
        arg = parts[1].lower() if len(parts) > 1 else ""
        if arg in ("on", "true", "1", "yes", "enable"):
            self.valves.advanced_commands_enabled = True
            self._persist_advanced()
            return ("🔓 **Advanced commands enabled.** `/help` now lists the "
                    "full surface. Turn back off with `/advanced off`.")
        if arg in ("off", "false", "0", "no", "disable"):
            self.valves.advanced_commands_enabled = False
            self._persist_advanced()
            return ("🔒 **Advanced commands disabled** — back to the guided "
                    "core. `/advanced on` to re-enable.")
        state = "on" if self.valves.advanced_commands_enabled else "off"
        return (
            f"Advanced commands are **{state}**.\n\n"
            f"- `/advanced on` — enable the full ~50-command surface\n"
            f"- `/advanced off` — guided/minimal core only\n"
        )

    def _persist_advanced(self) -> None:
        """§17.562 — best-effort persist of the advanced toggle to valves.json
        so it survives a pipelines-container restart (OWUI reloads valves.json
        on init). Merge-write to avoid clobbering other valves; silent on any
        error — the in-memory value still applies for this session."""
        try:
            here = os.path.dirname(os.path.abspath(__file__))
            live = os.path.join(here, "scaffold_router", "valves.json")
            data: dict = {}
            if os.path.exists(live):
                with open(live, "r") as f:
                    content = f.read().strip()
                if content and content != "{}":
                    loaded = json.loads(content)
                    if isinstance(loaded, dict):
                        data = loaded
            data["advanced_commands_enabled"] = (
                self.valves.advanced_commands_enabled
            )
            with open(live, "w") as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass

    @staticmethod
    def _top_action_cmd(actions: list | None, job_id: str | None) -> str:
        """First actionable, fully-filled command from a next_actions list.

        §17.562 — skips node-specific actions whose `{node_key}` placeholder is
        still unfilled (no node context here): a bare `/skip <id> {node_key}`
        reads as broken in /here·/next. Those degrade to "—" → the user opens
        `/results` for node detail.
        """
        for a in (actions or []):
            if not isinstance(a, dict):
                continue
            if a.get("action") == "wait":
                continue
            cmd = a.get("command")
            if cmd and "{" not in cmd:
                return f"`{cmd}`"
        return "—"

    def _render_here(self, work: dict) -> str:
        """The 'you-are-here' surface: active jobs + assist sessions."""
        jobs = work.get("jobs") or []
        sessions = work.get("assist_sessions") or []
        if not jobs and not sessions:
            return (
                "✨ **Nothing in progress right now.**\n\n"
                "Start something: just describe what you want to build and "
                "then `/go` — or `/idea <your idea>` to jump straight in."
            )
        out: list[str] = ["**📍 Your active work**\n"]
        if jobs:
            out.append("| Job | Phase | Next step |")
            out.append("|---|---|---|")
            for j in jobs:
                jid = (j.get("id") or "")[:8]
                title = j.get("title") or "_(untitled)_"
                phase = j.get("phase") or j.get("status") or ""
                nxt = self._top_action_cmd(j.get("next_actions"), j.get("id"))
                out.append(f"| `{jid}` {title} | {phase} | {nxt} |")
            out.append("")
        if sessions:
            out.append("**🤝 Assist sessions** (you're walking these yourself)\n")
            for s in sessions:
                sid = (s.get("session_id") or "")[:8]
                title = s.get("job_title") or ""
                node = s.get("current_node_key") or "start"
                out.append(
                    f"- `{sid}` {title} — at **{node}** → `/resume` or "
                    f"`/assist next`"
                )
            out.append("")
        out.append("_Tip: `/next` for your single next step, `/resume` to jump "
                   "back in._")
        return "\n".join(out)

    def _render_next(self, work: dict) -> str:
        """The single highest-priority next step for the user's current work."""
        jobs = work.get("jobs") or []
        sessions = work.get("assist_sessions") or []
        if sessions:
            s = sessions[0]
            node = s.get("current_node_key") or "the first step"
            return (
                f"👉 Resume your assist session with `/resume` (or "
                f"`/assist next`) — job _{s.get('job_title','')}_, at **{node}**."
            )
        if not jobs:
            return (
                "✨ Nothing in progress. Describe an idea and `/go`, or "
                "`/idea <your idea>`."
            )
        j = jobs[0]
        cmd = self._top_action_cmd(j.get("next_actions"), j.get("id"))
        title = j.get("title") or ""
        phase = j.get("phase") or ""
        if cmd == "—":
            return (
                f"📍 _{title}_ is in **{phase}** — nothing to do right now. "
                f"Check progress with `/results`."
            )
        return f"👉 Next for _{title}_ (**{phase}**): {cmd}"

    def _handle_resume(self, body: dict | None = None):
        """Jump back into in-progress work with no UUID. One item → resume it;
        many → show the list to pick; none → friendly empty state."""
        work = self._fetch_work()
        if work is None:
            yield ("⚠️ Couldn't reach the orchestrator to find your work. "
                   "Try `/health`.")
            return
        jobs = work.get("jobs") or []
        sessions = work.get("assist_sessions") or []
        if not jobs and not sessions:
            yield ("✨ Nothing to resume. Describe an idea and `/go`, or "
                   "`/idea <your idea>`.")
            return
        if len(jobs) + len(sessions) > 1:
            yield "You have more than one thing in progress — pick one:\n\n"
            yield self._render_here(work)
            return
        if sessions:
            sid = sessions[0].get("session_id")
            yield (f"▶️ Resuming assist session `{(sid or '')[:8]}`…\n\n"
                   f"---\n\n")
            yield from self._assist_next(
                sid, chat_id=self._chat_id_from_body(body),
            )
            return
        # exactly one non-terminal job → point at its next step (don't auto-fire
        # a 10–25 min run; the user chooses).
        yield self._render_next(work)

    def _status_footer(self) -> str:
        """Compact 'where you are / what's next' footer for NON-streaming
        replies. Empty string when disabled, on error, or nothing in progress.
        MUST NOT be appended inside an SSE stream (corrupts the auto-chain)."""
        if not self.valves.status_footer_enabled:
            return ""
        work = self._fetch_work()
        if not work:
            return ""
        jobs = work.get("jobs") or []
        sessions = work.get("assist_sessions") or []
        if not jobs and not sessions:
            return ""
        nxt = self._render_next(work)
        return f"\n\n---\n{nxt}"

    def _resolve_session_id(
        self, args: list, chat_id: str | None,
    ) -> tuple[str | None, list]:
        return _assist.resolve_session_id(self, args, chat_id)

    @staticmethod
    def _no_session_msg(sub: str) -> str:
        return _assist.no_session_msg(sub)

    @staticmethod
    def _extract_fenced(msg: str) -> tuple[str, str]:
        return _assist.extract_fenced(msg)

    def _render_step(self, step: dict) -> str:
        return _assist.render_step(step)

    def _handle_assist(
        self, msg: str, *, body: dict | None = None,
    ) -> Generator[str, None, None]:
        yield from _assist.handle_assist(self, msg, body=body)

    def _dispatch_assist_sub(
        self, sub: str, args: list, fenced: str, *, chat_id: str | None,
    ) -> Generator[str, None, None]:
        yield from _assist.dispatch_assist_sub(
            self, sub, args, fenced, chat_id=chat_id,
        )

    def _assist_start(
        self, job_id: str, *, chat_id: str | None = None,
    ) -> Generator[str, None, None]:
        yield from _assist.assist_start(self, job_id, chat_id=chat_id)

    def _assist_next(
        self, session_id: str, *, chat_id: str | None = None,
    ) -> Generator[str, None, None]:
        yield from _assist.assist_next(self, session_id, chat_id=chat_id)

    def _assist_submit(
        self, session_id: str, node_key: str, evidence: str,
        *, chat_id: str | None = None,
    ) -> Generator[str, None, None]:
        yield from _assist.assist_submit(
            self, session_id, node_key, evidence, chat_id=chat_id,
        )

    def _assist_skip(
        self, session_id: str, node_key: str, *, chat_id: str | None = None,
    ) -> Generator[str, None, None]:
        yield from _assist.assist_skip(
            self, session_id, node_key, chat_id=chat_id,
        )

    def _assist_handoff(
        self, session_id: str, node_key: str, mode: str,
    ) -> Generator[str, None, None]:
        yield from _assist.assist_handoff(self, session_id, node_key, mode)

    def _stream_sse_with_keepalive(
        self, url: str, body: dict,
    ) -> Generator[str, None, None]:
        yield from _assist.stream_sse_with_keepalive(self, url, body)

    def _assist_simple_post(
        self, session_id: str, action: str,
    ) -> Generator[str, None, None]:
        yield from _assist.assist_simple_post(self, session_id, action)

    def _assist_done(
        self, session_id: str, *, chat_id: str | None = None,
    ) -> Generator[str, None, None]:
        yield from _assist.assist_done(self, session_id, chat_id=chat_id)

    def _assist_friction(
        self, session_id: str, node_key: str, note: str,
    ) -> Generator[str, None, None]:
        yield from _assist.assist_friction(self, session_id, node_key, note)

    def _assist_guide(
        self, session_id: str, *, node_key: str | None = None,
        refine: str | None = None, research: bool | None = None,
        force: bool = True, chat_id: str | None = None,
    ) -> Generator[str, None, None]:
        yield from _assist.assist_guide_cmd(
            self, session_id, node_key=node_key, refine=refine,
            research=research, force=force, chat_id=chat_id,
        )

    def _assist_research(
        self, session_id: str, question: str, *,
        node_key: str | None = None, chat_id: str | None = None,
    ) -> Generator[str, None, None]:
        yield from _assist.assist_research_cmd(
            self, session_id, question, node_key=node_key, chat_id=chat_id,
        )

    def _assist_env(
        self, session_id: str, *, profile: str | None = None,
        substitutions: dict | None = None, show: bool = False,
        chat_id: str | None = None,
    ) -> Generator[str, None, None]:
        yield from _assist.assist_env_cmd(
            self, session_id, profile=profile, substitutions=substitutions,
            show=show, chat_id=chat_id,
        )

    def _assist_fix(
        self, session_id: str, error_text: str, *,
        node_key: str | None = None, chat_id: str | None = None,
    ) -> Generator[str, None, None]:
        yield from _assist.assist_fix_cmd(
            self, session_id, error_text, node_key=node_key, chat_id=chat_id,
        )

    # ------------------------------------------------------------------
    # Long-poll with keepalive (DRY helper, #8.7)
    # ------------------------------------------------------------------

    def _post_with_keepalive(
        self, url: str, payload: dict, timeout: int,
        *, progress_label: str | None = None,
    ):
        """Generator: yields '\\u200b' every keepalive_interval until POST returns.

        \u00a717.173 \u2014 when ``progress_label`` is set, additionally yields a
        visible "\u23f3 <label>... (Nm SSs elapsed)" marker every
        ``progress_marker_interval`` seconds. This surfaces progress to
        the OWUI chat for long blocking POSTs (Phase 2 research can
        take 10-25 min on CPU; without visible markers the chat appears
        frozen). Zero-width keepalives continue ticking in between to
        maintain SSE connection.

        \u00a717.201 \u2014 the worker thread's result/exception are passed
        through a ``concurrent.futures.Future`` rather than the pre-fix
        single-element-list pattern. The list pattern relied on the
        CPython GIL plus ``Thread.join``'s implicit barrier for safe
        cross-thread reads \u2014 which works today but is the kind of
        thread-safety-by-CPython-quirk pattern that breaks under PyPy
        or no-GIL CPython 3.13+. ``Future`` makes the synchronization
        point explicit and survives any interpreter.

        Terminates with ``return (ok, response_or_exception)``.
        """
        from concurrent.futures import Future

        future: Future = Future()

        def _call():
            try:
                future.set_result(_HTTP_SESSION.post(
                    url, json=payload, headers=self._auth_headers(), timeout=timeout,
                ))
            except BaseException as e:
                # BaseException (not Exception) so KeyboardInterrupt /
                # SystemExit don't slip past silently \u2014 Future raises
                # whatever exception was set when .result() is called.
                future.set_exception(e)

        t = threading.Thread(target=_call, daemon=True)
        t.start()

        # \u00a717.173 \u2014 track elapsed time so visible markers stay synchronized
        # to wall-clock progress regardless of keepalive_interval. Marker
        # interval of 0 disables visible markers entirely (back-compat for
        # tests that count zero-width ticks).
        start = time.monotonic()
        marker_interval = self.valves.progress_marker_interval
        last_marker = start

        while not future.done():
            time.sleep(self.valves.keepalive_interval)
            if future.done():
                break
            now = time.monotonic()
            if (
                progress_label
                and marker_interval > 0
                and (now - last_marker) >= marker_interval
            ):
                elapsed = int(now - start)
                mm, ss = elapsed // 60, elapsed % 60
                yield f"\n\u23f3 {progress_label}\u2026 ({mm}m {ss:02d}s elapsed)\n"
                last_marker = now
            else:
                yield "\u200b"
        # The loop exited because future.done() is True; result() returns
        # immediately or raises the set exception.
        try:
            return (True, future.result())
        except BaseException as e:
            return (False, e)

    # ------------------------------------------------------------------
    # /go auto-chain
    # ------------------------------------------------------------------

    def _try_decompose(self, message: str) -> Generator[str, None, bool]:
        """§17.526 — POST /decompose; if the idea splits into ≥2 components, launch
        an umbrella + one autonomous component job each and report. Returns True
        when handled, False to fall back to the single-job /ideate path."""
        yield "Checking whether this splits into independent components"
        ok, res = yield from self._post_with_keepalive(
            f"{self.valves.orchestrator_url}/decompose",
            {"idea": message, "model_overrides": self._model_overrides()},
            self.valves.stream_timeout,
            progress_label="Decomposing into components",
        )
        if not ok or getattr(res, "status_code", 500) >= 400:
            return False  # error → fall back to normal flow
        try:
            data = res.json()
        except ValueError:
            return False
        if not data.get("decomposed"):
            return False  # single-focus build → normal single-job flow
        umbrella = data.get("umbrella_job_id", "")
        children = data.get("children", []) or []
        yield f"\n\n**Launched {len(children)} components** under umbrella `{umbrella}`:\n\n"
        for c in children:
            yield f"- `{c.get('job_id','')}` — {c.get('label','')}\n"
        yield (
            f"\nEach component runs autonomously through its own pipeline "
            f"(research → DAG → execute). Track all of them with "
            f"`/results {umbrella}`.\n"
        )
        return True

    def _auto_chain(self, message: str) -> Generator[str, None, None]:
        if self.valves.decompose_on_go:
            handled = yield from self._try_decompose(message)
            if handled:
                return
        yield "Let me think about this"
        ok, res = yield from self._post_with_keepalive(
            f"{self.valves.orchestrator_url}/ideate",
            {"idea": message, "model_overrides": self._model_overrides()},
            self.valves.stream_timeout,
            progress_label="Phase 1 — refining idea",
        )
        if not ok:
            err = res
            if isinstance(err, requests.exceptions.ConnectionError):
                yield "\nI couldn't reach the analysis engine. It may be restarting — please try again."
            elif isinstance(err, requests.exceptions.Timeout):
                yield "\n⚠️ The analysis engine timed out. Please try again."
            else:
                yield f"\n⚠️ Error: {err}"
            return

        r = res
        if r.status_code >= 400:
            try:
                err = r.json().get("message") or r.json().get("detail") or r.text[:200]
            except Exception:
                err = r.text[:200]
            yield (
                f"\n⚠️ I had trouble with that request (HTTP {r.status_code}): {err}\n"
                f"Could you rephrase it?"
            )
            return
        try:
            data = r.json()
            job_id = data["job_id"]
        except (ValueError, KeyError) as e:
            yield f"\n⚠️ Unexpected response: {e}"
            return

        brief = data.get("refined_brief", {})
        title = brief.get("title", "") if isinstance(brief, dict) else ""
        yield (f"\n**{title}**\n\n" if title else "\n\n")

        if data.get("status") == "awaiting_confirmation":
            feas = data.get("feasibility", {})
            is_feasible = feas.get("feasible", True)
            confidence = feas.get("confidence", 0)
            desc = brief.get("description", "") if isinstance(brief, dict) else ""
            if desc:
                yield f"{desc}\n\n"
            yield f"**Feasibility:** {'✅' if is_feasible else '⚠️'} ({confidence:.0%} confidence)\n\n"
            risks = feas.get("risks", []) or []
            if risks:
                yield "**Risks to consider:**\n"
                for risk in risks:
                    yield f"- {risk}\n"
                yield "\n"
            clar = feas.get("clarifications_needed", []) or []
            if clar:
                yield "**A few things that could be more specific:**\n"
                for c in clar:
                    yield f"- **{c}**\n"
                yield "\n"
            # §17.305 — harmonize /go's awaiting_confirmation pause with
            # §17.303's /idea Next-block. Same 4 canonical commands so
            # operators landing here from EITHER entry point see the
            # same discovery surface. "Start over" stays as a 5th line
            # — unique to /go's chat-history-driven entry path.
            yield "---\n\n**What would you like to do?**\n\n"
            yield (
                f"- `/confirm {job_id}` — auto-chain Phase 2 "
                f"(research → compile → DAG → execute)\n"
            )
            yield (
                f"- `/confirm {job_id} <your adjustments>` — adjust the "
                f"brief before proceeding\n"
            )
            yield f"- `/results {job_id}` — peek at current state\n"
            yield (
                f"- `/cost {job_id}` — see refinement costs so far\n"
            )
            yield "\n_Or start over: describe a new idea and type `/go` again._\n"
            return

        yield "Planning my approach"
        ok, res = yield from self._post_with_keepalive(
            f"{self.valves.orchestrator_url}/dag",
            {"job_id": job_id, "model_overrides": self._model_overrides()},
            self.valves.stream_timeout,
            progress_label="Phase 3 — planning DAG",
        )
        if not ok:
            err = res
            if isinstance(err, requests.exceptions.Timeout):
                yield "\n\nPlanning is taking longer than expected. Please simplify."
            elif isinstance(err, requests.exceptions.ConnectionError):
                yield "\n\nCouldn't reach the engine. Try again in a moment."
            else:
                yield f"\n\n⚠️ Error during planning: {err}"
            yield (
                f"\n\nResearch finished — only the plan step failed. "
                f"Retry with `/dag {job_id}` or check status with `/jobs`.\n"
            )
            return
        r = res
        if r.status_code >= 400:
            try:
                err = r.json().get("message") or r.json().get("detail") or r.text[:200]
            except Exception:
                err = r.text[:200]
            yield (
                f"\n\n❌ Planning failed (HTTP {r.status_code}): {err}\n"
                f"**Research is done — retry just this step with `/dag {job_id}`** "
                f"(or check status with `/jobs`).\n"
            )
            return
        try:
            dag_data = r.json()
            num_nodes = dag_data.get("task_count", len(dag_data.get("tasks", [])))
        except (ValueError, KeyError):
            yield "\n\n⚠️ Unexpected response from DAG generation."
            return

        # §17.562 — always-ask: present the autonomous-vs-assist choice rather
        # than silently auto-executing (consistent with /confirm). This tail is
        # the edge path where /ideate did NOT pause at awaiting_confirmation.
        yield "\n"
        yield self._execution_choice(job_id, dag_data, num_nodes)
        return

    def _execution_choice(
        self, job_id: str, dag_data: dict, num_nodes: int,
    ) -> str:
        """§17.562 — the always-ask autonomous-vs-assist prompt after planning.
        Shared by /confirm and the /go auto-chain so the choice is identical.
        Shell/hands-on steps drive the *recommendation*, not the visibility."""
        tasks = dag_data.get("tasks", []) if isinstance(dag_data, dict) else []
        shell_steps = sum(
            1 for t in tasks
            if isinstance(t, dict) and str(t.get("tool", "")).lower() == "shell"
        )
        if shell_steps:
            rec = (
                f"This plan has **{shell_steps} hands-on step(s)** you run on "
                f"real systems — **Assist is recommended** (the engine can't "
                f"perform those for you)."
            )
        else:
            rec = (
                "This is a text/code/research plan the engine can run on its "
                "own — **Autonomous is recommended**."
            )
        return (
            f"📋 **Execution plan ready — {num_nodes} steps.**\n\n"
            f"{rec} How do you want to proceed?\n\n"
            f"- `/execute {job_id}` — **autonomous**: the engine runs every "
            f"step itself.\n"
            f"- `/assist {job_id}` — **assisted**: you run each step, the "
            f"engine guides and verifies.\n"
        )

    # ------------------------------------------------------------------
    # SSE reader helper (#8.7, #8.12)
    # ------------------------------------------------------------------

    def _stream_sse_to_queue(
        self, url: str, payload: dict, event_queue: queue.Queue,
        *,
        stop_event: "threading.Event | None" = None,
        r_holder: "list | None" = None,
    ) -> None:
        """POST `url` and stream SSE events into event_queue.

        Queue messages (tuples of length 3):
          ("connected",    None, None)
          ("http_error",   status_code, body_text)
          ("event",        event_type, data_string)
          ("heartbeat",    None, None)                  # on per-read timeout
          ("event", "stream_stalled", json_payload)     # after 5x keepalive silent
          ("error",        exception_str, None)
          ("done",         None, None)

        §17.262 — Optional early-exit plumbing. Pass ``stop_event`` and
        ``r_holder=[]`` to allow the consumer to signal shutdown when its
        generator exits early (e.g. client disconnect → GeneratorExit).
        The consumer's finally block sets ``stop_event`` and calls
        ``r_holder[0].close()`` to force ``iter_lines`` to raise; this
        function observes ``stop_event`` on each ReadTimeout cycle.
        """
        keep = self.valves.keepalive_interval
        max_idle = max(300, 5 * keep)
        # The read-timeout drives how often a silent server triggers a
        # ReadTimeout and we emit a heartbeat. §17.609 — each ReadTimeout
        # cycle covers ``read_timeout`` wall-clock seconds, NOT ``keep``:
        # the 30s lower bound means a small keepalive_interval (default 10)
        # still waits 30s per silent cycle. The stall accumulator below must
        # therefore add ``read_timeout``; adding ``keep`` undercounted elapsed
        # time 3× at the default, firing the stall guard at ~900s not ~300s.
        read_timeout = max(30, keep)

        try:
            r = _HTTP_SESSION.post(
                url, json=payload, headers=self._auth_headers(),
                stream=True, timeout=(30, read_timeout),
            )
        except requests.exceptions.ConnectionError as e:
            event_queue.put(("error", f"cannot reach orchestrator: {e}", None)); return
        except Exception as e:
            event_queue.put(("error", str(e), None)); return

        # §17.262 — expose the live Response to the consumer so it can
        # close() on early exit. Must run BEFORE the status-code check
        # so a 4xx still gets the close-path treatment (no-op since the
        # body has already been read in r.text[:400]).
        if r_holder is not None:
            r_holder.append(r)

        if r.status_code >= 400:
            try:
                body_text = r.text[:400]
            except Exception:
                body_text = ""
            event_queue.put(("http_error", r.status_code, body_text))
            r.close(); return

        event_queue.put(("connected", None, None))

        event_type = None
        data_buffer = ""
        idle_seconds = 0
        malformed_lines = 0  # SSE lines we don't recognize (no event: / data: prefix)

        try:
            while True:
                try:
                    for raw_line in r.iter_lines(decode_unicode=True):
                        if raw_line is None:
                            continue
                        # Reset idle timer only after consuming a real line.
                        idle_seconds = 0
                        line = raw_line.strip() if raw_line else ""
                        if line == "":
                            if event_type and data_buffer:
                                event_queue.put(("event", event_type, data_buffer))
                            event_type = None
                            data_buffer = ""
                            continue
                        if line.startswith("event:"):
                            event_type = line[6:].strip()
                        elif line.startswith("data:"):
                            # SSE spec: multi-line data fields join with newline.
                            data_buffer += ("\n" if data_buffer else "") + line[5:].lstrip()
                        elif line.startswith(":"):
                            # SSE comment frames (e.g. ": keepalive") — ignore.
                            pass
                        else:
                            malformed_lines += 1
                    # Iterator exhausted — server closed cleanly
                    if event_type and data_buffer:
                        event_queue.put(("event", event_type, data_buffer))
                    if malformed_lines:
                        # Surface to operator logs (not user UX). A non-zero
                        # count signals an upstream SSE producer that's not
                        # framing per spec (event:/data:/comment).
                        print(  # noqa: T201
                            f"[scaffold_router] SSE: dropped "
                            f"{malformed_lines} unrecognized line(s) from {url}",
                            flush=True,
                        )
                    event_queue.put(("done", None, None))
                    return
                except requests.exceptions.ReadTimeout:
                    # §17.262 — observe early-exit signal here. The consumer's
                    # finally sets stop_event when its generator closes; we
                    # bail without emitting heartbeat to drain cleanly.
                    if stop_event is not None and stop_event.is_set():
                        event_queue.put(("done", None, None))
                        return
                    idle_seconds += read_timeout  # §17.609 — true per-cycle wall time
                    if idle_seconds >= max_idle:
                        event_queue.put((
                            "event", "stream_stalled",
                            json.dumps({"idle_seconds": idle_seconds,
                                        "max_idle": max_idle}),
                        ))
                        event_queue.put(("done", None, None))
                        return
                    event_queue.put(("heartbeat", None, None))
                    continue
        except Exception as e:
            event_queue.put(("error", str(e), None))
        finally:
            try:
                r.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # SSE consumers
    # ------------------------------------------------------------------

    def _research_and_stream_raw(
        self, url_path: str, body: dict,
    ) -> Generator[str, None, None]:
        q = queue.Queue()
        # §17.262 — early-exit plumbing so a GeneratorExit (client
        # disconnect) tears down the daemon reader within reader.join's
        # 5s window instead of leaving it alive until the 24h SSE
        # timeout expires.
        stop_event = threading.Event()
        r_holder: list = []
        url = f"{self.valves.orchestrator_url}{url_path}"
        reader = threading.Thread(
            target=self._stream_sse_to_queue,
            args=(url, body, q),
            kwargs={"stop_event": stop_event, "r_holder": r_holder},
            daemon=True,
        )
        reader.start()

        try:
            while True:
                try:
                    msg_type, f1, f2 = q.get(timeout=self.valves.keepalive_interval)
                except queue.Empty:
                    yield "\u200b"; continue

                if msg_type == "connected":
                    continue
                if msg_type == "heartbeat":
                    yield "\u200b"; continue
                if msg_type == "http_error":
                    try:
                        err = json.loads(f2).get("detail", f2[:200])
                    except Exception:
                        err = (f2 or "")[:200]
                    yield f"⚠️ Research failed (HTTP {f1}): {err}"
                    return
                if msg_type == "error":
                    yield f"\n⚠️ Connection error during research: {f1}"
                    return
                if msg_type == "done":
                    break

                event_type, data = f1, f2
                try:
                    payload = json.loads(data)
                except json.JSONDecodeError:
                    continue

                if event_type == "heartbeat":
                    yield "\u200b"
                elif event_type == "stream_stalled":
                    yield (f"\n⚠️ **Stream stalled** — no data in {payload.get('idle_seconds','?')}s. "
                           f"Research may still be running in the background — "
                           f"check `/jobs` for current status.\n")
                    return
                elif event_type == "error":
                    yield from self._render_error_event(payload); return
                elif event_type == "research_started":
                    yield f"📊 Depth: {payload.get('depth','?')} | Max iterations: {payload.get('max_iterations','?')}\n"
                    # §17.502 — surface the verification path up-front. On a
                    # long run the SSE stream can drop before research_complete
                    # (where the /rag hint also lives), leaving the user unsure
                    # anything was ingested. This early, reliable event ensures
                    # they always know how to check.
                    yield (
                        "_Ingests into the shared knowledge base — when done, "
                        "check with `/rag <query>` (searches all domains) or "
                        "`/research/list`._\n\n"
                    )
                elif event_type == "decomposition_complete":
                    facets = payload.get("facets", [])
                    yield f"🧩 Decomposed into {len(facets)} facets: {', '.join(facets)}\n"
                    yield f"   Complexity: {payload.get('complexity','?')} | Queries: {payload.get('query_count','?')}\n\n"
                elif event_type == "iteration_started":
                    yield f"--- **Iteration {payload.get('iteration','?')}** ---\n"
                elif event_type == "search_complete":
                    yield f"🔍 Found {payload.get('results_found',0)} new results ({payload.get('total_urls',0)} URLs searched)\n"
                elif event_type == "extraction_complete":
                    yield f"📝 Extracted {payload.get('entries_extracted',0)} entries\n"
                elif event_type == "ingestion_complete":
                    yield f"💾 Ingested {payload.get('entries_ingested',0)} entries ({payload.get('total_rejected',0)} duplicates rejected)\n"
                elif event_type == "iteration_complete":
                    yield "\n"
                elif event_type == "gap_analysis":
                    yield f"📈 Coverage: {payload.get('coverage_pct','?')}%"
                    gaps = payload.get("gap_facets", [])
                    if gaps:
                        yield f" | Gaps: {', '.join(gaps)}"
                    yield "\n"
                    if payload.get("assessment"):
                        yield f"   {payload['assessment']}\n"
                    yield "\n"
                elif event_type == "convergence":
                    yield f"✅ Converged: {payload.get('reason','')}\n\n"
                elif event_type == "awaiting_reply":
                    sid = payload.get("session_id", "?")
                    mins = payload.get("expires_in_seconds", 3600) // 60
                    yield "---\n\n⏸️ **Research paused — need your input**\n\n"
                    yield f"**Question:** {payload.get('question','')}\n\n"
                    yield f"**Session:** `{sid}` (expires in {mins} min)\n\n"
                    yield f"**To continue:** type `/research/reply {sid} <your answer>`\n"
                    yield "**To abandon:** do nothing — the session auto-cancels on expiry.\n\n"
                    return
                elif event_type == "research_resumed":
                    yield f"▶️ Resuming session `{payload.get('session_id','?')}` from iteration {payload.get('iteration','?')}\n"
                    yield f"   Reply injected: _{payload.get('reply','')}_\n\n"
                elif event_type == "research_complete":
                    total = payload.get("total_ingested", 0)
                    entries = payload.get("total_entries", 0)
                    iterations = payload.get("iterations", 0)
                    mins = payload.get("duration_ms", 0) / 60000
                    yield "---\n\n**Research Complete**\n\n"
                    yield f"- **Topic:** {payload.get('topic','?')}\n"
                    yield f"- **Entries extracted:** {entries}\n"
                    yield f"- **Ingested:** {total}\n"
                    yield f"- **Iterations:** {iterations}\n"
                    yield f"- **Duration:** {mins:.1f} min\n\n"
                    if payload.get("summary"):
                        yield f"**Summary:**\n\n{payload['summary']}\n\n"
                    yield "---\n\n**Next steps:**\n\n"
                    # §17.510 — honest about the research→build link. `/go`
                    # synthesizes a brief from your CHAT, not the KB; the
                    # ingested knowledge is used automatically as grounding when
                    # a build's nodes execute (same-domain retrieval). Don't
                    # imply `/go` reads this research directly.
                    yield "- `/rag <query>` to retrieve what was ingested (this knowledge also grounds builds at execution time)\n"
                    yield "- `/research <subtopic> --depth deep` to explore further\n"
                    yield "- `/go` to start a new build from your chat description\n"

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

    def _execute_and_stream(
        self, job_id: str, total_nodes: int,
    ) -> Generator[str, None, None]:
        q = queue.Queue()
        url = f"{self.valves.orchestrator_url}/execute/all"
        body = {"job_id": job_id, "model_overrides": self._model_overrides()}
        # §17.262 — early-exit plumbing so a GeneratorExit (client
        # disconnect) tears down the daemon reader within reader.join's
        # 5s window instead of leaving it alive until the 24h SSE timeout.
        stop_event = threading.Event()
        r_holder: list = []
        reader = threading.Thread(
            target=self._stream_sse_to_queue,
            args=(url, body, q),
            kwargs={"stop_event": stop_event, "r_holder": r_holder},
            daemon=True,
        )
        reader.start()

        failed_nodes = []
        compiled_output = None
        compile_status = None
        stalled = False

        try:
            while True:
                try:
                    msg_type, f1, f2 = q.get(timeout=self.valves.keepalive_interval)
                except queue.Empty:
                    yield "\u200b"; continue

                if msg_type == "connected":
                    continue
                if msg_type == "heartbeat":
                    yield "\u200b"; continue
                if msg_type == "http_error":
                    if f1 == 409:
                        yield (f"Job `{job_id}` is already being processed. "
                               f"Check progress with `/results {job_id}`, "
                               f"or wait a moment before retrying.")
                        return
                    hint = self._drift_hint() if f1 == 401 else ""
                    yield f"⚠️ Execution failed (HTTP {f1}). Please try again.{hint}"
                    return
                if msg_type == "error":
                    yield from self._recover_from_disconnect(job_id)
                    return
                if msg_type == "done":
                    break

                event_type, data = f1, f2
                if event_type == "stream_stalled":
                    try:
                        p = json.loads(data) if data else {}
                    except json.JSONDecodeError:
                        p = {}
                    yield (f"\n⚠️ **Stream stalled** — no data for {p.get('idle_seconds','?')}s. "
                           f"Execution may still be running; use `/results {job_id}` to check.\n")
                    stalled = True
                    continue

                yield from self._handle_sse_event(event_type, data, failed_nodes)

                if event_type == "pipeline_complete":
                    try:
                        payload = json.loads(data)
                        compiled_output = payload.get("compiled_output", "")
                        if not compiled_output and payload.get("compiled_output_available"):
                            compiled_output = self._poll_compiled_output(job_id)
                        compile_status = payload.get("compile_status", "complete")
                        # §17.611 (audit #18) — the real node count lives in the
                        # pipeline_complete payload; both callers pass total_nodes=0
                        # and never reassign it, so the partial-results banner read
                        # "N of 0 steps". Read it here.
                        total_nodes = int(payload.get("total_nodes") or total_nodes)
                        for fn in payload.get("failed_nodes", []) or []:
                            failed_nodes.append(fn)
                    except json.JSONDecodeError:
                        pass

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
        if stalled:
            return

        if compiled_output:
            if compile_status == "partial" and failed_nodes:
                yield f"\n⚠️ **Partial results** — {len(failed_nodes)} of {total_nodes} steps could not be completed:\n"
                for fn in failed_nodes:
                    if isinstance(fn, dict):
                        yield f"- **{fn.get('title', fn.get('node_key','?'))}**: {fn.get('reason','unknown')}\n"
                    else:
                        yield f"- {fn}\n"
                yield "\n---\n\n"
                yield compiled_output
            else:
                yield compiled_output
            # §17.304 — post-completion Next-block: operator just saw the
            # output; surface the canonical follow-on commands so they
            # don't have to remember `/cost` / `/results` from /help.
            yield self._render_completion_next_block(job_id, failed_nodes)
        else:
            yield "\n⏳ Fetching final output...\n"
            time.sleep(3)
            try:
                sr = _HTTP_SESSION.get(
                    f"{self.valves.orchestrator_url}/exec/status/{job_id}",
                    headers=self._auth_headers(),
                    timeout=self.valves.request_timeout,
                )
                if sr.status_code == 200:
                    fallback = sr.json().get("compiled_output", "")
                    if fallback:
                        yield fallback
                        yield self._render_completion_next_block(job_id, failed_nodes)
                    else:
                        yield f"✅ All steps completed."
                        yield self._render_completion_next_block(job_id, failed_nodes)
                else:
                    yield f"✅ All steps completed."
                    yield self._render_completion_next_block(job_id, failed_nodes)
            except Exception:
                yield f"✅ All steps completed."
                yield self._render_completion_next_block(job_id, failed_nodes)

    # §17.304 — post-execution Next-block. Mirrors §17.303's /idea
    # Next-block at the OTHER end of the operator journey: after
    # /execute/all (or the /confirm auto-chain) yields its compiled
    # output. Pre-§17.304 operators saw the result + "Use `/results`
    # for details" as the sole signpost. Now: a small bulleted list
    # of the canonical follow-on commands, with the real job_id
    # pre-filled per §17.303's pattern, plus an `/exec retry` row
    # per failed node when the compile was partial.
    def _render_completion_next_block(
        self, job_id: str, failed_nodes: list,
    ) -> str:
        adv = self.valves.advanced_commands_enabled
        lines: list[str] = ["\n\n---\n\n**Next steps:**"]
        # `/exec retry` rows first when there are failures — operator
        # action is highest-leverage on those (advanced surface only).
        if failed_nodes and adv:
            for fn in failed_nodes:
                if isinstance(fn, dict):
                    nk = fn.get("node_key", "?")
                    title = fn.get("title", "")
                    suffix = f" (\"{title}\")" if title else ""
                    lines.append(
                        f"- `/exec retry {job_id} {nk}`{suffix} — retry "
                        f"this failed step"
                    )
        lines.append(f"- `/results {job_id}` — full status + node-by-node detail")
        lines.append("- `/here` — your active work + next step")
        if adv:
            lines.append(
                f"- `/cost {job_id}` — see total LLM cost + latency rollup"
            )
            if not failed_nodes:
                lines.append(
                    f"- `/jobs rename {job_id} <new title>` — set a memorable "
                    f"title for later lookup"
                )
        else:
            # §17.562 — guided: the retry/cost/rename commands are gated; point
            # at /advanced rather than naming a command that would 🔒-block.
            if failed_nodes:
                lines.append(
                    "- `/advanced on` — unlock `/exec retry` to re-run failed "
                    "steps (plus `/cost`, job management)"
                )
            else:
                lines.append(
                    "- `/advanced on` — unlock `/cost`, `/jobs rename`, and the "
                    "full surface"
                )
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # SSE event renderers
    # ------------------------------------------------------------------

    def _render_error_event(self, payload: dict) -> Generator[str, None, None]:
        """Render an SSE `error` event (#8.2)."""
        message = (payload.get("error") or payload.get("message")
                   or payload.get("detail") or "unknown error")
        tb = payload.get("traceback") or payload.get("stack_trace")
        yield "\n❌ **Execution error:** "
        yield f"{message}\n\n"
        if tb:
            yield f"```traceback\n{tb}\n```\n"

    def _handle_sse_event(
        self, event_type: str, data: str, failed_nodes: list,
    ) -> Generator[str, None, None]:
        try:
            payload = json.loads(data)
        except json.JSONDecodeError as e:
            self.logger.debug(
                "SSE JSON decode failed (event=%s): %s | data=%r",
                event_type, e, data[:200],
            )
            return

        if event_type == "node_start":
            yield f"🔄 Step {payload.get('node_key','?')}: {payload.get('title','')} ({payload.get('tool','')})...\n"
        elif event_type == "node_done":
            # §17.509 — don't claim "complete" for unexecuted runbook steps.
            if payload.get("runbook_only"):
                yield (
                    f"📋 Step {payload.get('node_key','?')} — runbook generated "
                    f"(not executed; you perform it).\n"
                )
            else:
                yield f"✅ Step {payload.get('node_key','?')} complete.\n"
        elif event_type == "node_failed":
            reason = payload.get("error") or payload.get("verification_reason") or "unknown"
            yield f"❌ Step {payload.get('node_key','?')} failed: {reason}\n"
            failed_nodes.append(payload)
        elif event_type == "node_retry":
            title = payload.get("title", "")
            yield f"🔄 Step {payload.get('node_key','?')}: Retrying{' — ' + title if title else ''} (attempt {payload.get('retry_count',0)})...\n"
        elif event_type == "blocked":
            # §17.295 — render the cause-aware blocked payload. Pre-§17.295
            # this read top-level `node_key` + `blocked_by` (a list of
            # strings) — but the actual terminal blocked event from
            # execute_all_nodes carries `blocked_nodes` (a list of
            # `{node_key, title, blocked_by: [{node_key, status}], cause}`)
            # so the pre-fix render produced "Step ? blocked (waiting on: )"
            # — empty fields. Split by cause so operators see actionable
            # vs waiting separately, with a copy-pasteable retry hint for
            # the actionable bucket.
            blocked_nodes_list = payload.get("blocked_nodes") or []
            actionable = [b for b in blocked_nodes_list if b.get("cause") == "failed"]
            waiting = [b for b in blocked_nodes_list if b.get("cause") == "waiting"]
            msg = payload.get("message", "Pipeline blocked")
            yield f"⏸️ {msg}\n"
            for b in actionable:
                deps_failed = [
                    d.get("node_key", "?") for d in (b.get("blocked_by") or [])
                    if d.get("status") in ("failed", "blocked")
                ]
                key = b.get("node_key", "?")
                yield (
                    f"  • `{key}` blocked by failed upstream "
                    f"({', '.join(deps_failed)}) — try `/exec retry "
                    f"<job_id> {deps_failed[0] if deps_failed else key}`\n"
                )
            for b in waiting:
                deps_pending = [
                    d.get("node_key", "?") for d in (b.get("blocked_by") or [])
                    if d.get("status") in ("pending", "running")
                ]
                yield (
                    f"  • `{b.get('node_key', '?')}` waiting on "
                    f"({', '.join(deps_pending)})\n"
                )
        elif event_type == "error":
            # #8.2 — bubble orchestrator error events to chat
            yield from self._render_error_event(payload)
            failed_nodes.append(payload)
        elif event_type == "pipeline_complete":
            pass  # final output handled after loop

    # ------------------------------------------------------------------
    # Recovery / polling
    # ------------------------------------------------------------------

    _TERMINAL_STATES = {"completed", "done", "failed", "cancelled", "canceled", "blocked"}

    def _recover_from_disconnect(self, job_id: str) -> Generator[str, None, None]:
        yield "\n⏳ Connection interrupted — checking job status...\n"
        last_status = None
        for attempt in range(3):
            time.sleep(5 if attempt == 0 else 10)
            try:
                r = _HTTP_SESSION.get(
                    f"{self.valves.orchestrator_url}/exec/status/{job_id}",
                    headers=self._auth_headers(),
                    timeout=self.valves.request_timeout,
                )
                if r.status_code != 200:
                    continue
                data = r.json()
                status = (data.get("status") or data.get("job_status") or "").lower()
                last_status = status or last_status
                if status in ("completed", "done"):
                    compiled = data.get("compiled_output", "")
                    if compiled:
                        yield "✅ Job completed successfully.\n\n"
                        yield compiled
                        return
                    yield "✅ Job completed but no output was generated."
                    return
                if status == "failed":
                    reason = data.get("error") or data.get("reason") or "see server logs"
                    yield f"❌ Job `{job_id}` failed: {reason}\n"
                    yield f"Run `/results {job_id}` for full diagnostic output."
                    return
                if status in ("cancelled", "canceled"):
                    yield f"🛑 Job `{job_id}` was cancelled.\n"
                    return
                if status == "blocked":
                    blocked_by = data.get("blocked_by") or []
                    detail = f" (waiting on: {', '.join(blocked_by)})" if blocked_by else ""
                    yield f"⏸️ Job `{job_id}` is blocked{detail}.\n"
                    yield f"Use `/results {job_id}` once unblocked."
                    return
            except Exception as e:
                self.logger.debug("recover poll attempt %d: %s", attempt, e)
                continue
        # All polls exhausted without a terminal state.
        last = last_status or "unknown"
        yield (f"⚠️ Connection lost; orchestrator unreachable or job `{job_id}` still running "
               f"(last status: `{last}`).\n"
               f"Run `/results {job_id}` to retrieve output once available, "
               f"or `/cancel {job_id}` to abort.")

    def _poll_compiled_output(self, job_id: str) -> str:
        try:
            r = _HTTP_SESSION.get(
                f"{self.valves.orchestrator_url}/exec/status/{job_id}",
                headers=self._auth_headers(),
                timeout=self.valves.request_timeout,
            )
            if r.status_code == 200:
                return r.json().get("compiled_output", "")
        except Exception:
            pass
        return ""

    # ------------------------------------------------------------------
    # Single-string command dispatcher
    # ------------------------------------------------------------------

    def _handle_command(self, msg: str, chat_id: str | None = None) -> str:
        parts = msg.split(None, 2)
        cmd = parts[0].lower()

        try:
            if cmd == "/help":
                return self._help()
            if cmd == "/model":
                return self._handle_model(msg)
            if cmd == "/schedule":
                return self._handle_schedule(msg)
            if cmd == "/results":          # #8.1
                return self._handle_results(parts, chat_id=chat_id)
            if cmd == "/artifacts":        # §17.565
                return self._handle_artifacts(parts)
            if cmd == "/jobs":
                # §17.309 — pass chat_id for the active-job 📌 marker.
                return self._handle_jobs(msg, chat_id=chat_id)
            if cmd == "/idea":
                if len(parts) < 2:
                    return "Usage: /idea <description>"
                text = " ".join(parts[1:])
                if _is_placeholder(text):
                    return "It looks like the description is missing or a placeholder. Try `/idea Build a CLI that converts screenshots to a searchable PDF`."
                r = _HTTP_SESSION.post(
                    f"{self.valves.orchestrator_url}/ideate",
                    json={"idea": text, "model_overrides": self._model_overrides()},
                    headers=self._auth_headers(),
                    timeout=self.valves.stream_timeout,
                )
                # §17.303 — render success with a pre-filled Next-block
                # so operators don't have to scan JSON for the job_id.
                # §17.307 — pass chat_id so successful /idea seeds
                # active-job memory for this chat.
                return self._render_ideate_response(r, chat_id=chat_id)
            if cmd == "/dag":
                if len(parts) < 2:
                    return (
                        "Usage: `/dag <job_id>`\n"
                        "Example: `/dag 01ab243e`\n\n"
                        "💡 Use `/jobs` to list your active jobs and copy a job_id."
                    )
                # §17.301 — placeholder check, mirror /idea + /skip pattern
                if _is_placeholder(parts[1]):
                    return (
                        "It looks like job_id is missing or a placeholder. "
                        "Try `/dag 01ab243e` (use `/jobs` to find a real id)."
                    )
                r = _HTTP_SESSION.post(
                    f"{self.valves.orchestrator_url}/dag",
                    json={"job_id": parts[1], "model_overrides": self._model_overrides()},
                    headers=self._auth_headers(),
                    timeout=self.valves.stream_timeout,
                )
                return self._fmt(r)
            if cmd == "/skip":
                # §17.316 — extracted into _handle_skip for the tiered
                # confirmation-friction recall model (closes the §17.307
                # cohort: /skip is the 6th and final id-taker).
                return self._handle_skip(parts, chat_id=chat_id)
            if cmd == "/node":
                # §17.479 — interactive node control (reset/del/edit/reorder)
                # over the §17.478 /nodes CRUD API.
                return self._handle_node(parts, chat_id=chat_id)
            if cmd == "/cancel":
                # §17.322 — operator-driven job cancel. Mirrors §17.314
                # /execute's confirmation-friction pattern: state-
                # altering, so 0-args with recall yields options
                # instead of auto-firing.
                return self._handle_cancel(parts, chat_id=chat_id)
            if cmd == "/optimize":
                if len(parts) < 2:
                    return "Usage: /optimize <prompt text>"
                text = " ".join(parts[1:])
                if _is_placeholder(text):
                    return "It looks like the prompt is missing or a placeholder. Try `/optimize Write a function that returns the nth Fibonacci number`."
                r = _HTTP_SESSION.post(
                    f"{self.valves.orchestrator_url}/optimize",
                    json={"prompt": text, "skip_verify": False,
                          "model_overrides": self._model_overrides()},
                    headers=self._auth_headers(),
                    timeout=self.valves.stream_timeout,
                )
                return self._fmt(r)
            if cmd == "/rag":
                if len(parts) < 2:
                    return "Usage: /rag <query>"
                text = " ".join(parts[1:])
                if _is_placeholder(text):
                    return "It looks like the query is missing or a placeholder. Try `/rag what changed in the codebase last week`."
                r = _HTTP_SESSION.post(
                    f"{self.valves.orchestrator_url}/rag",
                    json={"query": text, "top_k": 5},
                    headers=self._auth_headers(),
                    timeout=self.valves.request_timeout,
                )
                return self._render_rag_results(r, query=text)
            if cmd == "/status":
                r = _HTTP_SESSION.get(
                    f"{self.valves.orchestrator_url}/status",
                    headers=self._auth_headers(),
                    timeout=self.valves.request_timeout,
                )
                if r.status_code >= 400:
                    return self._fmt(r)
                # §17.313 — pass chat_id for the 📌 active-job marker
                # (synergy with §17.307).
                return self._render_status(r.json(), chat_id=chat_id)
            # §17.562 — guided core verbs (DB-derived, no UUID needed).
            if cmd == "/here":
                work = self._fetch_work()
                if work is None:
                    return ("⚠️ Couldn't reach the orchestrator. Try "
                            "`/health`.")
                return self._render_here(work)
            if cmd == "/next":
                work = self._fetch_work()
                if work is None:
                    return ("⚠️ Couldn't reach the orchestrator. Try "
                            "`/health`.")
                return self._render_next(work)

            # ----- U.8.D — diagnostics + admin parity -------------------
            if cmd == "/exec":
                # §17.315 — pass chat_id for /exec retry's tiered
                # confirmation-friction recall paths.
                return self._handle_exec(parts, chat_id=chat_id)
            if cmd == "/cleanup":
                return self._handle_cleanup()
            if cmd == "/config":
                return self._handle_config(parts)
            if cmd == "/logs":
                # §17.311 — extend §17.307 active-job recall to /logs.
                return self._handle_logs(parts, chat_id=chat_id)
            if cmd == "/health":
                return self._handle_health()

            # ----- J.3.c — cost rollup for a job ------------------------
            if cmd == "/cost":
                return self._handle_cost(parts, chat_id=chat_id)

            close = _suggest_command(cmd)
            if close:
                hint = "\n".join(f"  - `{c}`" for c in close)
                return (f"Unknown command: `{cmd}`\n\n"
                        f"Closest matches:\n{hint}\n\n"
                        f"Type `/help` for the full list.")
            return f"Unknown command: `{cmd}`\nType `/help` for available commands."

        except requests.exceptions.Timeout:
            return "⚠️ Request timed out. The orchestrator is still processing — check back shortly."
        except requests.exceptions.ConnectionError:
            return (
                f"⚠️ Cannot reach orchestrator at {self.valves.orchestrator_url}. "
                f"Is it running?\n\n"
                f"💡 Try `/health` to probe each subsystem (Postgres + Ollama + Milvus + Redis)."
            )
        except Exception as e:
            self.logger.exception("Command `%s` failed", cmd)
            return ("⚠️ Internal error processing command. "
                    "See server logs for details.")

    # ------------------------------------------------------------------
    # §17.628 / §17.629 — engine-wide natural-language command routing
    #
    # Sibling to the §17.626/§17.627 in-session assist NL flow, for the TOP
    # level (no active session). A plain sentence that clearly names an engine
    # action is translated to the canonical slash string and dispatched through
    # the EXISTING `_handle_command`/`_handle_*` — no duplicated logic.
    # Two-tier, mirroring `fast_classify_turn` + `/interpret`:
    #   1. `_fast_classify_command` — deterministic whole-message phrase match,
    #      no LLM, always high-confidence.
    #   2. `POST /route` — the classifier; only intercepts on confidence='high'
    #      AND a satisfied required slot. Everything else → None (triage).
    # §17.629 (Phase 2) adds mutating/expensive intents: the two that commit
    # real cost (research_topic, schedule_add) render a confirm card and fire
    # only on an affirmative follow-up; the cheap/reversible ones (model_set/
    # reset, optimize, jobs_rename) run directly.
    # ------------------------------------------------------------------

    # Required slots per intent — an intent with an unsatisfied slot falls
    # through to triage rather than intercepting into an empty action.
    _NL_REQUIRED_SLOTS: dict = {
        "rag_query": ("query",),
        "jobs_find": ("query",),
        "research_topic": ("topic",),
        "schedule_add": ("cron", "topic"),
        "model_set": ("model_role", "model_name"),
        "optimize": ("prompt",),
        "jobs_rename": ("job_ref", "new_name"),
        "jobs_delete": ("target_ref",),
        "schedule_delete": ("target_ref",),
        "research_delete": ("target_ref",),
    }

    # §17.629 — pending-confirm marker for the expensive writes. A confirm card
    # embeds this HTML comment carrying the JSON action; on the next turn an
    # affirmative reply ("go"/"yes") recovers + fires it. HTML comment → hidden
    # in the rendered chat but preserved in the raw history OWUI replays (same
    # mechanism as the §17.627 `<!--ASSIST_PICK-->` pick-list marker).
    _NL_CONFIRM_MARKER_RE = re.compile(r"<!--NL_CONFIRM:(\{.*?\})-->", re.DOTALL)
    _NL_AFFIRMATIVE: frozenset = frozenset({
        "go", "yes", "y", "yep", "yeah", "yup", "ok", "okay", "sure",
        "do it", "run it", "run", "confirm", "confirmed", "proceed",
        "go ahead", "yes please", "launch", "start", "start it", "go for it",
    })

    def _nl_command_route(
        self, msg: str, messages: List[dict], *, chat_id: str | None = None,
    ):
        """Decide whether a plain message is an engine command and, if so,
        return a generator that yields the handled reply. Returns ``None`` to
        fall through to triage (the caller then runs the planner).

        The decision is made BEFORE yielding — mirrors ``try_natural_start`` —
        so triage is never partially pre-empted by a command that turns out not
        to apply."""
        if not self.valves.nl_command_routing_enabled:
            return None

        intent = _fast_classify_command(msg)
        data: dict = {}
        if intent is None:
            d = self._classify_command(msg)
            intent = d.get("intent") or "none"
            # High-confidence intercept: an ambiguous read still goes to the
            # planner. This is the "won't hijack a conversation" guarantee.
            if intent == "none" or d.get("confidence") != "high":
                return None
            data = d

        # Required-slot gate — never intercept into an empty query/find/action.
        for slot in self._NL_REQUIRED_SLOTS.get(intent, ()):
            if not (data.get(slot) or "").strip():
                return None

        return self._dispatch_nl_command(intent, data, msg, chat_id=chat_id)

    def _classify_command(self, msg: str) -> dict:
        """POST /route → intent dict. Fail-soft → intent='none' so a classifier
        or endpoint hiccup degrades to triage rather than misfiring."""
        fallback = {"intent": "none", "confidence": "low"}
        try:
            r = _HTTP_SESSION.post(
                f"{self.valves.orchestrator_url}/route",
                json={"message": msg},
                headers=self._auth_headers(),
                timeout=getattr(self.valves, "nl_command_route_timeout", 20),
            )
            if r.status_code < 400:
                d = r.json()
                if isinstance(d, dict) and d.get("intent"):
                    return d
        except (requests.exceptions.RequestException, ValueError) as e:
            self.logger.debug("nl _classify_command failed: %s", e)
        return fallback

    def _dispatch_nl_command(
        self, intent: str, data: dict, msg: str, *, chat_id: str | None = None,
    ):
        """Translate a resolved intent to its canonical slash string and run it
        through the existing handler. Generator: yields the rendered reply.

        Reads + cheap/reversible writes run immediately. The two expensive
        writes (research_topic, schedule_add) instead render a confirm card
        (`_render_nl_confirm`) and fire only on the affirmative follow-up
        handled in `pipe()` — so a one-sentence misfire never launches a 40-min
        run or a recurring schedule."""
        query = (data.get("query") or "").strip()

        # (intent → canonical slash command) for the direct, single-shot cases.
        simple = {
            "status": "/status",
            "help": "/help",
            "jobs_list": "/jobs list",
            "model_list": "/model list",
            "model_available": "/model available",
            "model_probe": "/model probe",
            "model_reset": "/model reset",
            "rag_query": f"/rag {query}",
            "jobs_find": f"/jobs find {query}",
            "optimize": f"/optimize {(data.get('prompt') or '').strip()}",
            "model_set": (
                f"/model set {(data.get('model_role') or '').strip()} "
                f"{(data.get('model_name') or '').strip()}"
            ),
        }
        if intent in simple:
            yield self._handle_command(simple[intent], chat_id=chat_id)
            return

        if intent == "results":
            yield from self._nl_results(data, chat_id=chat_id)
            return
        if intent == "jobs_rename":
            yield from self._nl_rename(data, chat_id=chat_id)
            return
        if intent == "research_topic":
            yield self._confirm_research(data)
            return
        if intent == "schedule_add":
            yield self._confirm_schedule(data)
            return
        if intent in ("jobs_delete", "schedule_delete", "research_delete"):
            yield from self._nl_delete(intent, data, chat_id=chat_id)
            return

        # Unknown/unsupported intent slipped through — degrade gracefully.
        yield self._call_triage_from_msg(msg)

    def _nl_results(self, data: dict, *, chat_id: str | None = None):
        """Resolve a job reference for `results` and dispatch `/results`.

        - explicit/uniquely-matched job → `/results <id>`
        - ambiguous name → a plain disambiguation list (ids + `/results <id>`);
          NOT the assist pick-list, whose "1" follow-up starts a session
        - no ref → `/results` (falls back to active-job recall)
        - named but no match → clarify, don't silently show the wrong job."""
        ref = (data.get("job_ref") or "").strip()
        if not ref:
            yield self._handle_command("/results", chat_id=chat_id)
            return
        match, ambiguous, cands = self._resolve_job_ref(ref)
        if match and not ambiguous:
            yield self._handle_command(
                f"/results {match['job_id']}", chat_id=chat_id,
            )
            return
        if match and ambiguous and cands:
            yield self._render_job_disambiguation(cands, "see results for", "/results")
            return
        yield (
            f"I couldn't find a job matching “{ref}”. Try `/jobs list` to see "
            f"recent jobs, or `/jobs find {ref}` to search."
        )

    def _nl_rename(self, data: dict, *, chat_id: str | None = None):
        """Resolve a job reference for `jobs_rename` and dispatch `/jobs rename`.
        Rename is reversible, so a unique match runs directly; ambiguity lists
        the candidates (the new title is preserved in the hint)."""
        ref = (data.get("job_ref") or "").strip()
        new_name = (data.get("new_name") or "").strip()
        match, ambiguous, cands = self._resolve_job_ref(ref)
        if match and not ambiguous:
            yield self._handle_command(
                f"/jobs rename {match['job_id']} {new_name}", chat_id=chat_id,
            )
            return
        if match and ambiguous and cands:
            yield self._render_job_disambiguation(
                cands, "rename", f"/jobs rename", suffix=f" {new_name}",
            )
            return
        yield (
            f"I couldn't find a job matching “{ref}” to rename. Try `/jobs list` "
            f"to see recent jobs."
        )

    def _render_job_disambiguation(
        self, cands: list, verb: str, slash: str, *, suffix: str = "",
    ) -> str:
        """Plain (non-stateful) job disambiguation for results/rename. Unlike
        the assist pick-list, this carries NO hidden marker — a bare "1" reply
        would otherwise be captured by the assist-start pick resolver. Operators
        pick by pasting the explicit `<slash> <id>` line."""
        lines = [
            f"I found a few jobs that could match — which one do you want to "
            f"{verb}?", "",
        ]
        for c in cands[:8]:
            lines.append(
                f"- `{c.get('job_id', '')}` — {c.get('title', '(untitled)')} "
                f"(`{c.get('status', '?')}`)"
            )
        lines += ["", f"Reply with `{slash} <id>{suffix}` using an id above."]
        return "\n".join(lines)

    def _resolve_job_ref(self, ref: str):
        """Token-match a job name/topic against recent jobs (GET /jobs).

        Returns ``(match_or_None, ambiguous, candidates)`` where candidates are
        normalized ``{job_id,title,status}`` dicts — the same shape
        `_assist.match_assist_candidate` / `render_candidate_list` expect, so
        the assist pick-list machinery is reused verbatim."""
        cands: List[dict] = []
        try:
            r = _HTTP_SESSION.get(
                f"{self.valves.orchestrator_url}/jobs",
                params={"limit": 25},
                headers=self._auth_headers(),
                timeout=self.valves.request_timeout,
            )
            if r.status_code < 400:
                jobs = (r.json() or {}).get("jobs") or []
                cands = [
                    {"job_id": j.get("id"), "title": j.get("title", ""),
                     "status": j.get("status", "")}
                    for j in jobs if j.get("id")
                ]
        except (requests.exceptions.RequestException, ValueError) as e:
            self.logger.debug("nl _resolve_job_ref failed: %s", e)
        if not cands:
            return None, False, []
        match, ambiguous = _assist.match_assist_candidate(ref, cands)
        return match, ambiguous, cands

    # ---- §17.630 destructive intents (always confirmed) ------------------

    # (intent → resolver attr name, human noun, the slash+token that actually
    # deletes). The confirm card is the gate; the slash below runs only on the
    # affirmative follow-up via `_execute_nl_action`.
    _NL_DELETE_SPEC = {
        "jobs_delete": ("_resolve_job_ref", "job", "/jobs delete"),
        "schedule_delete": ("_resolve_schedule_ref", "schedule", "/schedule delete"),
        "research_delete": ("_resolve_research_ref", "research session", "/research/delete"),
    }

    def _nl_delete(self, intent: str, data: dict, *, chat_id: str | None = None):
        """Resolve the named delete target and render a stark confirm card.

        Nothing is removed here — the card embeds an `_NL_CONFIRM` marker that
        `_execute_nl_action` fires only on an affirmative follow-up. A unique
        match → confirm; ambiguous → marker-less disambiguation; no match →
        clarify (never delete the wrong thing)."""
        resolver_name, noun, slash = self._NL_DELETE_SPEC[intent]
        ref = (data.get("target_ref") or "").strip()
        match, ambiguous, cands = getattr(self, resolver_name)(ref)
        if match and not ambiguous:
            label = match.get("title") or match["job_id"]
            summary = (
                f"⚠️ **Permanently delete this {noun}?**\n\n"
                f"- **{label}** (`{match['job_id']}`)\n\n"
                f"This can't be undone."
            )
            yield self._render_nl_confirm(
                intent, {"id": match["job_id"], "label": label, "noun": noun},
                summary,
            )
            return
        if match and ambiguous and cands:
            yield self._render_job_disambiguation(cands, f"delete", slash)
            return
        yield (
            f"I couldn't find a {noun} matching “{ref}” to delete. Try "
            f"`{slash.rsplit(' ', 1)[0]} list` to see what's there."
        )

    def _resolve_named_ref(
        self, url: str, list_key: str, id_field: str, title_field: str,
        ref: str, *, params: dict | None = None,
    ):
        """Generic token-match of `ref` against a named list endpoint. Returns
        ``(match_or_None, ambiguous, candidates)`` in the normalized
        ``{job_id,title,status}`` shape `match_assist_candidate` expects."""
        cands: List[dict] = []
        try:
            r = _HTTP_SESSION.get(
                url, params=params or {}, headers=self._auth_headers(),
                timeout=self.valves.request_timeout,
            )
            if r.status_code < 400:
                items = (r.json() or {}).get(list_key) or []
                cands = [
                    {"job_id": str(it.get(id_field)),
                     "title": it.get(title_field, ""),
                     "status": it.get("status", "")}
                    for it in items if it.get(id_field) is not None
                ]
        except (requests.exceptions.RequestException, ValueError) as e:
            self.logger.debug("nl _resolve_named_ref(%s) failed: %s", list_key, e)
        if not cands:
            return None, False, []
        match, ambiguous = _assist.match_assist_candidate(ref, cands)
        return match, ambiguous, cands

    def _resolve_schedule_ref(self, ref: str):
        """Match a schedule by topic against GET /schedule."""
        return self._resolve_named_ref(
            f"{self.valves.orchestrator_url}/schedule", "schedules", "id", "topic",
            ref,
        )

    def _resolve_research_ref(self, ref: str):
        """Match a research session by topic against GET /research/sessions."""
        return self._resolve_named_ref(
            f"{self.valves.orchestrator_url}/research/sessions", "sessions", "id",
            "topic", ref, params={"limit": 25},
        )

    # ---- §17.629 confirm cards for the expensive writes ------------------

    _DEPTH_ETA = {"shallow": "~20–30 min", "medium": "~40–60 min", "deep": "60+ min"}

    def _render_nl_confirm(self, intent: str, slots: dict, summary: str) -> str:
        """A confirm card: human summary + a hidden action marker recovered on
        the affirmative follow-up. `slots` must be JSON-serializable + minimal
        (only what `_execute_nl_action` needs)."""
        marker = f"<!--NL_CONFIRM:{json.dumps({'intent': intent, 'slots': slots})}-->"
        return (
            f"{summary}\n\n"
            "_Reply **go** (or **yes**) to run it, or tell me what to change._\n"
            f"{marker}"
        )

    def _confirm_research(self, data: dict) -> str:
        topic = (data.get("topic") or "").strip()
        depth = (data.get("depth") or "").strip() or "medium"
        eta = self._DEPTH_ETA.get(depth, "~40–60 min")
        summary = (
            f"🔬 **Research this?**\n\n"
            f"- **Topic:** {topic}\n"
            f"- **Depth:** {depth} ({eta} on this host, CPU-only)"
        )
        return self._render_nl_confirm(
            "research_topic", {"topic": topic, "depth": depth}, summary,
        )

    def _confirm_schedule(self, data: dict) -> str:
        topic = (data.get("topic") or "").strip()
        depth = (data.get("depth") or "").strip() or "medium"
        cron = (data.get("cron") or "").strip()
        tz = (data.get("tz") or "").strip() or "UTC"
        summary = (
            f"🗓 **Schedule recurring research?**\n\n"
            f"- **Topic:** {topic}\n"
            f"- **Cron:** `{cron}` ({tz})\n"
            f"- **Depth:** {depth}"
        )
        return self._render_nl_confirm(
            "schedule_add",
            {"topic": topic, "depth": depth, "cron": cron, "tz": tz},
            summary,
        )

    def _extract_pending_nl_confirm(self, messages: List[dict]) -> dict | None:
        """Recover the pending NL action from the most recent assistant turn's
        confirm marker, or None. Only the immediately-preceding assistant turn
        counts — a stale marker further back does not re-fire."""
        for m in reversed(messages):
            if m.get("role") != "assistant":
                continue
            content = m.get("content", "")
            if isinstance(content, str):
                mt = self._NL_CONFIRM_MARKER_RE.search(content)
                if mt:
                    try:
                        d = json.loads(mt.group(1))
                        if isinstance(d, dict) and d.get("intent"):
                            return d
                    except ValueError:
                        return None
            return None  # most recent assistant turn had no confirm marker
        return None

    def _is_affirmative(self, msg: str) -> bool:
        norm = (msg or "").strip().lower().strip(".!,;: ").strip()
        return norm in self._NL_AFFIRMATIVE

    def _execute_nl_action(
        self, pending: dict, *, chat_id: str | None = None,
    ):
        """Fire a confirmed expensive write. Generator — research streams SSE."""
        intent = pending.get("intent")
        slots = pending.get("slots") or {}
        if intent == "research_topic":
            topic = (slots.get("topic") or "").strip()
            depth = (slots.get("depth") or "medium").strip()
            yield from self._handle_research(f"/research {topic} --depth={depth}")
            return
        if intent == "schedule_add":
            topic = (slots.get("topic") or "").strip()
            depth = (slots.get("depth") or "medium").strip()
            cron = (slots.get("cron") or "").strip()
            tz = (slots.get("tz") or "UTC").strip()
            cmd = f'/schedule add "{cron}" --depth={depth} --tz={tz} {topic}'
            yield self._handle_schedule(cmd)
            return
        # §17.630 — destructive: fire the underlying delete WITH its confirm
        # token (the NL confirm card was the human gate).
        if intent == "jobs_delete":
            yield self._handle_command(
                f"/jobs delete {slots.get('id', '')} confirm", chat_id=chat_id,
            )
            return
        if intent == "schedule_delete":
            yield self._handle_schedule(f"/schedule delete {slots.get('id', '')}")
            return
        if intent == "research_delete":
            yield from self._handle_research_mgmt(
                f"/research/delete {slots.get('id', '')} confirm",
            )
            return
        yield "⚠️ That confirmation expired — please ask again."

    def _call_triage_from_msg(self, msg: str) -> str:
        """Fallback triage call from a raw string (no message list). Used only
        on the defensive unknown-intent branch; the normal path uses the full
        message history via `_call_triage`."""
        return self._call_triage([{"role": "user", "content": msg}])

    # ------------------------------------------------------------------
    # /status renderer
    # ------------------------------------------------------------------
    def _render_status(
        self, data: dict, *, chat_id: str | None = None,
    ) -> str:
        counts = data.get("status_counts") or {}
        total = data.get("total_jobs", 0)
        recent = data.get("recent_jobs") or []

        # §17.313 — friendly empty state. Pre-§17.313 `/status` with
        # nothing in the system rendered just the header. Match the
        # §17.309 /jobs empty-state pattern: surface the welcome
        # starters so brand-new operators have a copy-pasteable path
        # forward.
        if total == 0 and not recent and not any(counts.values()):
            return self._status_empty_state()

        # Active = anything not in a terminal state
        terminal = {"completed", "failed", "cancelled", "blocked"}
        active_total = sum(v for k, v in counts.items() if k not in terminal and v)

        lines = [f"## 📊 Job Status — {total} total, {active_total} active"]

        # Counts table (drop zero rows for noise reduction)
        nonzero = [(k, v) for k, v in counts.items() if v]
        if nonzero:
            lines.append("")
            lines.append("| Status | Count |")
            lines.append("|---|---:|")
            for k, v in sorted(nonzero, key=lambda kv: -kv[1]):
                lines.append(f"| {k} | {v} |")

        # §17.313 — active-job recall for 📌 marker (synergy with
        # §17.307 / §17.309). Match on FULL id only (no short-id
        # collisions). chat_id may be None for curl-only callers.
        active_id = None
        recalled = self._active_job_recall(chat_id)
        if recalled:
            active_id = recalled.get("job_id")

        if recent:
            icon = {
                "completed": "✅", "failed": "❌", "cancelled": "🚫",
                "blocked": "⛔", "awaiting_confirmation": "⏸️",
                "executing": "⏳", "running": "⏳", "planning": "🧠",
                "researching": "🔍", "refining": "✏️", "pending": "⏳",
            "awaiting_assist": "🙋",  # §17.624 hands-on plan → /assist
            }
            lines.append("")
            lines.append(f"**Recent jobs (last {len(recent)}):**")
            lines.append("")
            lines.append("| Status | ID | Title | Nodes | Updated |")
            lines.append("|---|---|---|---:|---|")
            for j in recent:
                st = j.get("status", "?")
                jid = j.get("id", "?")
                short = jid[:8] if isinstance(jid, str) else "?"
                # §17.313 — 📌 prefix on the §17.307-recalled row.
                prefix = "📌 " if active_id and jid == active_id else ""
                title = (j.get("title") or "")[:60]
                nc = j.get("node_count", 0)
                upd = (j.get("updated_at") or "")[:16].replace("T", " ")
                lines.append(
                    f"| {icon.get(st, '')} {st} | {prefix}`{short}` "
                    f"| {title} | {nc} | {upd} |"
                )

            actionable = next(
                (j for j in recent if j.get("next_actions")
                 and j.get("status") not in ("completed", "cancelled")),
                None,
            )
            if actionable:
                lines.append("")
                lines.append("**Next steps:**")
                for a in (actionable.get("next_actions") or [])[:2]:
                    if a.get("action") == "wait":
                        continue
                    cmd = a.get("command")
                    desc = a.get("description", "")
                    if cmd:
                        lines.append(f"• `{cmd}` — {desc}")
                    elif a.get("endpoint"):
                        lines.append(f"• `{a.get('method','GET')} {a['endpoint']}` — {desc}")

        # §17.313 — cross-reference footer. /status is the at-a-glance
        # dashboard; /jobs is the management list. Help operators pick
        # the right surface for the action they want.
        lines.append("")
        lines.append("---")
        lines.append("")
        lines.append(
            "💡 `/here` shows your active work + next step. "
            "`/advanced on` unlocks job management (`/jobs`: rename / delete)."
        )
        return "\n".join(lines)

    @staticmethod
    def _status_empty_state() -> str:
        """§17.313 — empty state for `/status` (no jobs in the system).
        §17.562 — guided/minimal: surface core verbs only; `/research` lives
        behind the `/advanced` pointer so a brand-new operator isn't sent at a
        gated command."""
        return (
            "## 📊 Job Status\n\n"
            "_No jobs yet._\n\n"
            "**Get started:**\n"
            "- `/idea Build a CLI that converts screenshots to PDF` "
            "— kick off Phase 1 directly\n"
            "- _Or describe an idea in the chat and type `/go`._\n"
            "- `/advanced on` — unlock `/research` (web research → knowledge "
            "base) and the full surface"
        )


    # ------------------------------------------------------------------
    # Phase D — /jobs and /research management subcommands
    # ------------------------------------------------------------------


    def _ensure_pending_deletes(self):
        if not hasattr(self, "_pending_deletes"):
            self._pending_deletes = {}

    def _format_job_row(
        self, j: dict, *, active_id: str | None = None,
    ) -> str:
        icon = {
            "completed": "✅", "failed": "❌", "cancelled": "🚫",
            "blocked": "⛔", "awaiting_confirmation": "⏸️",
            "executing": "⏳", "running": "⏳", "planning": "🧠",
            "researching": "🔍", "refining": "✏️", "pending": "⏳",
            "awaiting_assist": "🙋",  # §17.624 hands-on plan → /assist
        }.get(j.get("status", ""), "")
        full_id = j.get("id") or ""
        short = full_id[:8]
        # §17.309 — 📌 prefix on the §17.307-remembered active job.
        active_prefix = "📌 " if active_id and full_id == active_id else ""
        upd = (j.get("updated_at") or "")[:16].replace("T", " ")
        return (
            f"| {icon} {j.get('status','')} | {active_prefix}`{short}` "
            f"| {j.get('title','')[:60]} | {j.get('node_count', 0)} | {upd} |"
        )

    def _format_session_row(self, sess: dict) -> str:
        icon = {
            "completed": "✅", "failed": "❌", "cancelled": "🚫",
            "running": "⏳", "pending": "⏳",
            "paused_awaiting_reply": "⏸️",
        }.get(sess.get("status", ""), "")
        short = (sess.get("id") or "")[:8]
        upd = (sess.get("updated_at") or "")[:16].replace("T", " ")
        return (
            f"| {icon} {sess.get('status','')} | `{short}` | {sess.get('topic','')[:60]} "
            f"| {sess.get('depth','')} | {sess.get('total_entries_ingested', 0)} | {upd} |"
        )

    def _jobs_help(self) -> str:
        return (
            "**`/jobs` Commands**\n\n"
            "| Command | Description |\n|---|---|\n"
            "| `/jobs` | List recent jobs (latest 25) |\n"
            "| `/jobs <status>` | Filter by status (completed, failed, blocked, ...) |\n"
            "| `/jobs find <text>` | Search by title |\n"
            "| `/jobs rename <id> <new title>` | Rename a job |\n"
            "| `/jobs delete <id>` | Preview what will be deleted |\n"
            "| `/jobs delete <id> confirm` | Permanently delete (within 5 min of preview) |\n"
            "| `/jobs help` | Show this message |"
        )

    def _research_mgmt_help(self) -> str:
        return (
            "**`/research` Management Subcommands**\n\n"
            "| Command | Description |\n|---|---|\n"
            "| `/research/list` | List recent research sessions (latest 25) |\n"
            "| `/research/find <text>` | Search by topic |\n"
            "| `/research/rename <id> <new topic>` | Rename a session |\n"
            "| `/research/delete <id>` | Preview what will be deleted |\n"
            "| `/research/delete <id> confirm` | Permanently delete |\n\n"
            "_Autonomous research:_ `/research <topic>`, `/research <url>`, "
            "`/research github:owner/repo`, `/research openapi:<url>`."
        )

    _VALID_JOB_STATUSES = {
        "pending", "refining", "awaiting_confirmation", "researching",
        "planning", "executing", "running", "completed", "failed",
        "cancelled", "blocked",
    }

    def _handle_jobs(self, msg: str, *, chat_id: str | None = None) -> str:
        """Top-level /jobs command dispatcher."""
        self._ensure_pending_deletes()
        parts = msg.split(None, 3)
        sub = parts[1] if len(parts) > 1 else ""

        # /jobs (no args) -> list
        if not sub:
            return self._jobs_list_action(
                status=None, query=None, chat_id=chat_id,
            )

        if sub == "help":
            return self._jobs_help()
        if sub in self._VALID_JOB_STATUSES:
            return self._jobs_list_action(
                status=sub, query=None, chat_id=chat_id,
            )
        if sub == "find":
            if len(parts) < 3:
                return "Usage: `/jobs find <text>`"
            return self._jobs_list_action(
                status=None, query=" ".join(parts[2:]), chat_id=chat_id,
            )
        if sub == "rename":
            if len(parts) < 4:
                return (
                    "Usage: `/jobs rename <job_id> <new title>`\n"
                    "Example: `/jobs rename 01ab243e My Improved Title`\n\n"
                    "💡 Use `/jobs` to list active jobs and copy a job_id."
                )
            # §17.301 — placeholder checks on both args
            if _is_placeholder(parts[2]):
                return (
                    "It looks like job_id is missing or a placeholder. "
                    "Try `/jobs rename 01ab243e My Improved Title`."
                )
            if _is_placeholder(parts[3]):
                return (
                    "It looks like the new title is missing or a placeholder. "
                    "Try `/jobs rename 01ab243e My Improved Title`."
                )
            return self._jobs_rename_action(parts[2], parts[3])
        if sub == "delete":
            if len(parts) < 3:
                return (
                    "Usage: `/jobs delete <job_id>`\n"
                    "Example: `/jobs delete 01ab243e`\n\n"
                    "💡 Use `/jobs` to list active jobs and copy a job_id.\n"
                    "Add `confirm` to skip the confirmation prompt: "
                    "`/jobs delete 01ab243e confirm`."
                )
            # §17.301 — placeholder check
            if _is_placeholder(parts[2]):
                return (
                    "It looks like job_id is missing or a placeholder. "
                    "Try `/jobs delete 01ab243e` (use `/jobs` to find a real id)."
                )
            job_id = parts[2]
            confirm = (len(parts) > 3 and parts[3].strip().lower() == "confirm")
            return self._jobs_delete_action(job_id, confirm)
        close = difflib.get_close_matches(sub, ("help", "find", "rename", "delete") + tuple(self._VALID_JOB_STATUSES), n=2, cutoff=0.6)
        hint = ""
        if close:
            hint = "\n\nClosest matches:\n" + "\n".join(f"  - `/jobs {c}`" for c in close)
        return f"Unknown subcommand: `/jobs {sub}`{hint}\n\n" + self._jobs_help()

    def _jobs_list_action(
        self, status, query, *, chat_id: str | None = None,
    ) -> str:
        params = {"limit": 25}
        if status:
            params["status"] = status
        if query:
            params["q"] = query
        try:
            r = _HTTP_SESSION.get(
                f"{self.valves.orchestrator_url}/jobs",
                params=params,
                headers=self._auth_headers(),
                timeout=self.valves.request_timeout,
            )
        except requests.exceptions.RequestException as e:
            return f"⚠️ {e}"
        if r.status_code >= 400:
            return self._fmt(r)
        # §17.275 — non-JSON 200 body would have crashed pre-fix.
        try:
            data = r.json()
        except ValueError as e:
            return f"⚠️ Jobs list: orchestrator returned non-JSON body ({e}); raw: {r.text[:200]}"
        if not isinstance(data, dict):
            return f"⚠️ Jobs list: orchestrator reply not a dict; raw: {str(data)[:200]}"
        jobs = data.get("jobs", [])
        total = data.get("total", 0)
        header_bits = []
        if status:
            header_bits.append(f"status=`{status}`")
        if query:
            header_bits.append(f"title~`{query}`")
        header = "## 📋 Jobs"
        if header_bits:
            header += f" — filtered ({', '.join(header_bits)})"
        header += f" — {len(jobs)} of {total}"
        # §17.309 — friendlier empty state. When no jobs match, surface
        # starter commands (mirror §17.300 welcome's exemplars) so the
        # operator has a copy-pasteable path forward instead of a
        # terse "No matching jobs." line.
        if not jobs:
            if status or query:
                # Filtered miss: keep terse — the operator typed a
                # specific filter, suggest broadening rather than
                # restarting.
                return (
                    header + "\n\n_No matching jobs._\n\n"
                    "💡 Try `/jobs` (no filter) to see everything, "
                    "or `/jobs find <text>` to search by title."
                )
            # Unfiltered empty: brand-new user or all jobs cleaned up.
            return self._jobs_empty_state(header)
        # §17.309 — active-job 📌 marker. If §17.307 has a remembered
        # job for this chat and it's in the displayed list, prefix
        # its row so the operator can spot "what was I working on?"
        # at a glance.
        active_id = None
        recalled = self._active_job_recall(chat_id)
        if recalled:
            active_id = recalled.get("job_id")
        rows = ["", "| Status | ID | Title | Nodes | Updated |", "|---|---|---|---:|---|"]
        rows.extend(self._format_job_row(j, active_id=active_id) for j in jobs)
        # §17.309 — next-actions hint footer. Copy-pasteable commands
        # an operator typically wants after scanning the list.
        # Mirror the Next-block shape from §17.303 / §17.305.
        # §17.313 — added /status as the 4th line to disambiguate the
        # two job-overview surfaces (/status = dashboard with counts;
        # /jobs = management list).
        footer = (
            "\n\n---\n\n"
            "💡 **Next:**\n"
            "- `/results <id>` — view output / progress / failure detail\n"
            "- `/cost <id>` — cost + latency rollup\n"
            "- `/status` — at-a-glance dashboard with counts by state\n"
            "- `/jobs help` — find / rename / delete / filter"
        )
        return "\n".join([header] + rows) + footer

    def _jobs_empty_state(self, header: str) -> str:
        """§17.309 — friendly empty state for unfiltered /jobs. Surface
        the §17.300 welcome's starter commands so a brand-new operator
        (or one who just cleared their job history) has a path forward."""
        return (
            f"{header}\n\n"
            "_No jobs yet._\n\n"
            "**Get started:**\n"
            "- `/idea Build a CLI that converts screenshots to PDF` "
            "— kick off Phase 1 directly\n"
            "- `/research kubernetes best practices` — "
            "autonomous web research + ingest\n"
            "- _Or describe an idea in the chat and type `/go`._"
        )

    def _jobs_rename_action(self, job_id: str, title: str) -> str:
        try:
            r = _HTTP_SESSION.patch(
                f"{self.valves.orchestrator_url}/jobs/{job_id}",
                json={"title": title.strip()},
                headers=self._auth_headers(),
                timeout=self.valves.request_timeout,
            )
        except requests.exceptions.RequestException as e:
            return f"⚠️ {e}"
        if r.status_code == 404:
            return (
                f"Job not found: `{job_id}`.\n\n"
                f"💡 Use `/jobs` to list active jobs and copy a real job_id."
            )
        if r.status_code >= 400:
            return self._fmt(r)
        # §17.275 — non-JSON 200 body would have crashed pre-fix.
        try:
            d = r.json()
        except ValueError as e:
            return f"⚠️ Job rename: orchestrator returned non-JSON body ({e}); raw: {r.text[:200]}"
        if not isinstance(d, dict):
            return f"⚠️ Job rename: orchestrator reply not a dict; raw: {str(d)[:200]}"
        return f"✅ Renamed `{(d.get('id') or '')[:8]}`: **{d.get('title')}**"

    def _jobs_delete_action(self, job_id: str, confirm: bool) -> str:
        self._ensure_pending_deletes()
        if confirm:
            pending = self._pending_deletes.get(("job", job_id))
            import time
            if not pending or time.time() - pending > 300:
                return (f"⚠️ No recent preview for `{job_id[:8]}`. "
                        f"Run `/jobs delete {job_id}` first.")
            try:
                r = _HTTP_SESSION.delete(
                    f"{self.valves.orchestrator_url}/jobs/{job_id}",
                    headers=self._auth_headers(),
                    timeout=self.valves.request_timeout,
                )
            except requests.exceptions.RequestException as e:
                return f"⚠️ {e}"
            if r.status_code == 404:
                return (
                    f"Job not found: `{job_id}`.\n\n"
                    f"💡 Use `/jobs` to list active jobs and copy a real job_id."
                )
            if r.status_code >= 400:
                return self._fmt(r)
            self._pending_deletes.pop(("job", job_id), None)
            return f"🗑️ Deleted job `{job_id[:8]}`."
        # Preview path
        try:
            r = _HTTP_SESSION.get(
                f"{self.valves.orchestrator_url}/exec/status/{job_id}",
                headers=self._auth_headers(),
                timeout=self.valves.request_timeout,
            )
        except requests.exceptions.RequestException as e:
            return f"⚠️ {e}"
        if r.status_code == 404:
            return (
                f"Job not found: `{job_id}`.\n\n"
                f"💡 Use `/jobs` to list active jobs and copy a real job_id."
            )
        if r.status_code >= 400:
            return self._fmt(r)
        d = r.json()
        title = d.get("job_title") or "(untitled)"
        status = d.get("job_status") or "?"
        total = d.get("total_nodes") or 0
        import time
        self._pending_deletes[("job", job_id)] = time.time()
        return (
            f"⚠️ **About to delete job** `{job_id[:8]}`\n\n"
            f"- Title: **{title}**\n"
            f"- Status: `{status}`\n"
            f"- DAG nodes that will cascade: {total}\n\n"
            f"This is irreversible. To proceed, type:\n"
            f"`/jobs delete {job_id} confirm`"
        )

    # /research subcommand handling -------------------------------------

    def _handle_research_mgmt(self, msg: str) -> Generator[str, None, None]:
        """Handle /research/<sub> management commands."""
        self._ensure_pending_deletes()
        parts = msg.split(None, 2)
        sub = parts[0].strip().lower()[len("/research/"):]
        if sub == "help":
            yield self._research_mgmt_help(); return
        if sub == "list":
            yield self._research_list_action(query=None); return
        if sub == "find":
            if len(parts) < 2:
                yield "Usage: `/research/find <text>`"; return
            yield self._research_list_action(query=" ".join(parts[1:])); return
        if sub == "rename":
            if len(parts) < 3:
                yield "Usage: `/research/rename <session_id> <new topic>`"; return
            yield self._research_rename_action(parts[1], parts[2]); return
        if sub == "delete":
            if len(parts) < 2:
                yield "Usage: `/research/delete <session_id>`"; return
            session_id = parts[1]
            confirm = (len(parts) > 2 and parts[2].strip().lower() == "confirm")
            yield self._research_delete_action(session_id, confirm); return
        # Unknown subcommand fuzzy fallback (Tier 1 #4).
        close = difflib.get_close_matches(sub, ("help", "list", "find", "rename", "delete"), n=2, cutoff=0.6)
        hint = ""
        if close:
            hint = "\n\nClosest matches:\n" + "\n".join(f"  - `/research/{c}`" for c in close)
        yield f"Unknown subcommand: `/research/{sub}`{hint}\n\n" + self._research_mgmt_help()

    def _research_list_action(self, query) -> str:
        params = {"limit": 25}
        if query:
            params["q"] = query
        try:
            r = _HTTP_SESSION.get(
                f"{self.valves.orchestrator_url}/research/sessions",
                params=params,
                headers=self._auth_headers(),
                timeout=self.valves.request_timeout,
            )
        except requests.exceptions.RequestException as e:
            return f"⚠️ {e}"
        if r.status_code >= 400:
            return self._fmt(r)
        # §17.275 — non-JSON 200 body would have crashed pre-fix.
        try:
            data = r.json()
        except ValueError as e:
            return f"⚠️ Research list: orchestrator returned non-JSON body ({e}); raw: {r.text[:200]}"
        if not isinstance(data, dict):
            return f"⚠️ Research list: orchestrator reply not a dict; raw: {str(data)[:200]}"
        sessions = data.get("sessions", [])
        total = data.get("total", 0)
        header = "## 🔍 Research Sessions"
        if query:
            header += f" — topic~`{query}`"
        header += f" — {len(sessions)} of {total}"
        if not sessions:
            return header + "\n\n_No matching sessions._"
        rows = ["", "| Status | ID | Topic | Depth | Entries | Updated |",
                "|---|---|---|---|---:|---|"]
        rows.extend(self._format_session_row(s) for s in sessions)
        return "\n".join([header] + rows)

    def _research_rename_action(self, session_id: str, topic: str) -> str:
        try:
            r = _HTTP_SESSION.patch(
                f"{self.valves.orchestrator_url}/research/sessions/{session_id}",
                json={"topic": topic.strip()},
                headers=self._auth_headers(),
                timeout=self.valves.request_timeout,
            )
        except requests.exceptions.RequestException as e:
            return f"⚠️ {e}"
        if r.status_code == 404:
            return (
                f"Research session not found: `{session_id}`.\n\n"
                f"💡 Use `/research/list` to see active sessions and copy a real session_id."
            )
        if r.status_code >= 400:
            return self._fmt(r)
        d = r.json()
        return f"✅ Renamed `{(d.get('id') or '')[:8]}`: **{d.get('topic')}**"

    def _research_delete_action(self, session_id: str, confirm: bool) -> str:
        self._ensure_pending_deletes()
        import time
        if confirm:
            pending = self._pending_deletes.get(("research", session_id))
            if not pending or time.time() - pending > 300:
                return (f"⚠️ No recent preview for `{session_id[:8]}`. "
                        f"Run `/research/delete {session_id}` first.")
            try:
                r = _HTTP_SESSION.delete(
                    f"{self.valves.orchestrator_url}/research/sessions/{session_id}",
                    headers=self._auth_headers(),
                    timeout=self.valves.request_timeout,
                )
            except requests.exceptions.RequestException as e:
                return f"⚠️ {e}"
            if r.status_code == 404:
                return (
                    f"Research session not found: `{session_id}`.\n\n"
                    f"💡 Use `/research/list` to see active sessions and copy a real session_id."
                )
            if r.status_code >= 400:
                return self._fmt(r)
            self._pending_deletes.pop(("research", session_id), None)
            return f"🗑️ Deleted research session `{session_id[:8]}`."
        # Preview
        try:
            r = _HTTP_SESSION.get(
                f"{self.valves.orchestrator_url}/research/sessions",
                params={"limit": 100},
                headers=self._auth_headers(),
                timeout=self.valves.request_timeout,
            )
        except requests.exceptions.RequestException as e:
            return f"⚠️ {e}"
        if r.status_code >= 400:
            return self._fmt(r)
        sessions = r.json().get("sessions", [])
        match = next((s for s in sessions if s.get("id") == session_id), None)
        if not match:
            return (
                f"Research session not found: `{session_id}`.\n\n"
                f"💡 Use `/research/list` to see active sessions and copy a real session_id."
            )
        self._pending_deletes[("research", session_id)] = time.time()
        return (
            f"⚠️ **About to delete research session** `{session_id[:8]}`\n\n"
            f"- Topic: **{match.get('topic','')}**\n"
            f"- Status: `{match.get('status','')}`\n"
            f"- Entries ingested into KB: {match.get('total_entries_ingested', 0)} "
            f"(KB entries are NOT deleted)\n\n"
            f"This drops only the session metadata. To proceed:\n"
            f"`/research/delete {session_id} confirm`"
        )

    # ------------------------------------------------------------------
    # /results handler (#8.1)
    # ------------------------------------------------------------------

    def _render_next_actions(self, data: dict) -> str:
        """Format the orchestrator-supplied `next_actions` list (audit
        item 10) into a markdown 'Next steps' block. Returns "" when
        the response carries no actions (e.g., older orchestrators).

        §17.195 — delegates filter + field-selection to the shared helper
        in ``pipelines/_vendor/_next_actions.py`` (byte-equal vendor of
        ``sdk/scaffold_client/next_actions.py``). Output is byte-identical
        to the pre-§17.195 inline implementation.
        """
        return _next_actions.format_block(
            data.get("next_actions") or [], style="markdown",
        )

    def _render_rag_results(
        self, r: requests.Response, *, query: str,
    ) -> str:
        """§17.215 E4 — render `/rag` results with provenance.

        The orchestrator's ``/rag`` endpoint returns a JSON envelope
        ``{"results": [{"text": str, "source_type": str,
        "confidence_score": float, ...}, ...]}``. ``source_type`` +
        ``confidence_score`` are populated since Phase-7 wrap
        (§17.104 + §17.120) but the pre-§17.215 renderer dropped them
        on the floor by returning a raw ``json.dumps`` blob via
        ``_fmt``. This renderer surfaces both per result so operators
        can judge whether a hit is from a high-confidence tech_docs
        chunk vs. a low-confidence chat-log snippet without round-tripping
        through `/results` or the Milvus UI.

        Falls back to the raw JSON dump (via ``_fmt``) on any envelope
        shape we don't recognize, to stay safe against orchestrator
        version drift.
        """
        # Error path: defer to the existing formatter, which already
        # handles non-JSON responses + HTTP >=400 + drift hints.
        if r.status_code >= 400:
            return self._fmt(r)
        try:
            data = r.json()
        except Exception:
            return self._fmt(r)

        results = data.get("results")
        if not isinstance(results, list) or not results:
            # Empty hit list: explicit message + escalation path rather than a
            # dead end (§17.444 / A3) — nothing in the KB means the user should
            # be pointed at the one command that can fix that.
            return (
                f"No matches in the knowledge base for `{query}`.\n\n"
                f"💡 Nothing ingested on this yet — `/research {query}` to fetch "
                "and ingest it, then re-run your search."
            )

        # All results need the dict shape; otherwise fall back to raw
        # JSON to avoid masking server-side changes.
        if not all(isinstance(rr, dict) for rr in results):
            return self._fmt(r)

        lines = [f"**RAG results for `{query}`** ({len(results)} hit(s)):\n"]
        # §17.444 (Phase A / A3) — surface retrieval uncertainty the pipeline
        # already computes but the renderer used to drop. A top-N fallback below
        # the confidence threshold, or an RRF-only ranking when the reranker is
        # unavailable, otherwise looks identical to a high-confidence hit.
        meta = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
        if meta.get("below_threshold") or meta.get("fell_back_to_top3"):
            thr = meta.get("confidence_threshold")
            thr_txt = f" (< {thr:.2f})" if isinstance(thr, (int, float)) else ""
            lines.append(
                f"> ⚠️ **Low confidence{thr_txt}** — nothing cleared the threshold, "
                "so these are a best-effort top-N fallback. Treat them as weak matches "
                f"(`/research {query}` to fetch better sources).\n"
            )
        if meta.get("skipped_rerank"):
            lines.append(
                "> ⚠️ **Ranking: RRF-only** — the reranker was unavailable, so these "
                "weren't relevance-checked by the cross-encoder.\n"
            )
        for i, hit in enumerate(results, start=1):
            text_field = (
                hit.get("text")
                or hit.get("content")
                or hit.get("chunk")
                or ""
            )
            source_type = hit.get("source_type")
            confidence = hit.get("confidence_score")
            meta_parts = []
            if source_type:
                meta_parts.append(f"source_type={source_type}")
            if isinstance(confidence, (int, float)):
                # §17.447 (Phase B / B2) — label provenance: this is the
                # RETRIEVAL relevance score (cosine/rerank), not a
                # factual-correctness or verifier judgement.
                meta_parts.append(f"confidence={confidence:.2f} (retrieval)")
            meta = (" · " + " · ".join(meta_parts)) if meta_parts else ""
            # Header line per result; matches the spec format
            # `· source_type=tech_docs · confidence=0.82`.
            lines.append(f"\n### Result {i}{meta}\n")
            if text_field:
                lines.append(f"{text_field}\n")
            # Optional provenance fields are surfaced as a footer if
            # present. We keep this terse — chat real estate is scarce.
            extras = []
            if "source_url" in hit and hit["source_url"]:
                extras.append(f"source: <{hit['source_url']}>")
            elif "source_ref" in hit and hit["source_ref"]:
                extras.append(f"source: {hit['source_ref']}")
            if extras:
                lines.append("_(" + " · ".join(extras) + ")_\n")
        return "".join(lines)

    def _handle_cancel(
        self, parts: list, *, chat_id: str | None = None,
    ) -> str:
        """§17.322 — `/cancel <job_id>` operator-driven cancel.

        Mirrors the §17.314 /execute confirmation-friction pattern
        because /cancel is state-altering (flips status →
        ``cancelled``) and operating on the wrong job is the worst-
        case failure. Idempotent at the orchestrator (`POST /jobs/
        {id}/cancel` returns 200 + ``was_already_cancelled=True``
        on a no-op), so duplicate clicks don't break anything.

        Argument shapes:
          - 0 args, recall hit → 📌 + 3 options (require explicit
            ``/cancel confirm`` to fire on the recalled id)
          - 0 args, no recall → Usage error
          - 1 arg = ``confirm`` (recall hit) → POST on recalled id
          - 1 arg = ``confirm`` (no recall) → error pointing at
            explicit form
          - 1 arg, job_id-shaped → POST (no friction; operator
            deliberately typed the id)
          - 1 arg, neither → invalid-id error
          - 2+ args → warn extras ignored; treat as 1-arg case
        """
        # 0 args after /cancel — confirmation-friction with recall.
        if len(parts) < 2:
            recalled = self._active_job_recall(chat_id)
            if recalled and recalled.get("job_id"):
                rid = recalled["job_id"]
                short = rid[:8] if len(rid) >= 8 else rid
                title = recalled.get("title")
                title_part = f" — _{title}_" if title else ""
                return (
                    f"📌 Active job in this chat: `{short}`{title_part}.\n\n"
                    f"⚠️ `/cancel` flips the job to `cancelled` — "
                    f"state-altering (but reversible via `/jobs/{short}"
                    f"/resume`).\n\n"
                    f"- Type `/cancel confirm` to cancel `{short}`\n"
                    f"- Type `/cancel <other_job_id>` to target a "
                    f"different job\n"
                    f"- Or inspect first: `/status {short}`"
                )
            return (
                "Usage: `/cancel <job_id>`\n"
                "Example: `/cancel 01ab243e`\n\n"
                "💡 Use `/jobs` to list your active jobs and copy a job_id."
            )

        # 1-arg = "confirm" — fire on recalled id (§17.314 pattern).
        if parts[1].lower() == "confirm" and len(parts) == 2:
            recalled = self._active_job_recall(chat_id)
            if not recalled or not recalled.get("job_id"):
                return (
                    "❌ `/cancel confirm` requires an active job in chat "
                    "memory, but none is set.\n\n"
                    "Pass an explicit job_id: `/cancel <job_id>`. "
                    "Use `/jobs` to list active jobs."
                )
            rid = recalled["job_id"]
            hint = self._active_job_hint(rid, recalled.get("title"))
            return hint + self._post_cancel(rid)

        # 1-or-more args — first arg should be a job_id.
        if _is_placeholder(parts[1]):
            return (
                "It looks like job_id is missing or a placeholder. "
                "Try `/cancel 01ab243e` (use `/jobs` to find a real id)."
            )
        if not self._JOB_ID_TOKEN_RE.match(parts[1]):
            return (
                f"❌ `{parts[1]}` doesn't look like a job_id (expected "
                f"a UUID or 8-hex-char short id).\n\n"
                f"Pass an explicit job_id: `/cancel <job_id>`. "
                f"Use `/jobs` to list active jobs."
            )
        return self._post_cancel(parts[1])

    def _post_cancel(self, job_id: str) -> str:
        """POST /jobs/{id}/cancel and render the result.

        Three render shapes corresponding to the three CancelJobResult
        outcomes (plus error shapes for 404/409/422 surfaced via _fmt).
        """
        r = _HTTP_SESSION.post(
            f"{self.valves.orchestrator_url}/jobs/{job_id}/cancel",
            headers=self._auth_headers(),
            timeout=self.valves.request_timeout,
        )
        if r.status_code != 200:
            return self._fmt(r)
        try:
            body = r.json()
        except Exception:
            return self._fmt(r)
        short = job_id[:8] if len(job_id) >= 8 else job_id
        if body.get("was_already_cancelled"):
            return (
                f"ℹ️ Job `{short}` was already cancelled — no change.\n\n"
                f"💡 To restart it: `POST /jobs/{job_id}/resume`."
            )
        prior = body.get("status_before", "active")
        return (
            f"🛑 Cancelled job `{short}` (was `{prior}` → now `cancelled`).\n\n"
            f"💡 Reversible: `POST /jobs/{job_id}/resume` to restart from "
            f"the last pending node."
        )

    # ------------------------------------------------------------------
    # §17.479 — interactive node control (Phase 5 surface for /nodes CRUD)
    # ------------------------------------------------------------------

    def _node_help(self) -> str:
        return (
            "**`/node` — interactive node control**\n\n"
            "| Command | Effect |\n|---|---|\n"
            "| `/node reset <job_id> <node_key>` | Re-run a node + its downstream (any status) |\n"
            "| `/node del <job_id> <node_key>` | Delete a node (dependents rewired) |\n"
            "| `/node edit <job_id> <node_key> <field> <value>` | Edit `title` / `tool` / `deliverable` |\n"
            "| `/node reorder <job_id> T1,T2,T3` | Renumber execution order |\n"
            "| `/node help` | This list |\n\n"
            "💡 The `job_id` can be omitted to use the active job from this "
            "chat. Insert + `depends_on` edits: use the `/nodes` API directly."
        )

    def _resolve_job_node(
        self, rest: list, chat_id: str | None,
    ) -> tuple:
        """Return (job_id, node_key, hint, error). Supports
        `<job_id> <node_key>` (explicit) and `<node_key>` (recall job_id)."""
        if not rest:
            return None, None, "", (
                "Usage: `/node <reset|del|edit|reorder> ...` — see `/node help`."
            )
        if len(rest) >= 2 and self._JOB_ID_TOKEN_RE.match(rest[0]):
            return rest[0], rest[1], "", None
        node_key = rest[0]
        recalled = self._active_job_recall(chat_id)
        if not recalled or not recalled.get("job_id"):
            return None, None, "", (
                f"❌ No active job in chat memory. Pass an explicit id: "
                f"`/node <op> <job_id> {node_key}` (use `/jobs` to find one)."
            )
        rid = recalled["job_id"]
        return rid, node_key, self._active_job_hint(rid, recalled.get("title")), None

    def _handle_node(self, parts: list, *, chat_id: str | None = None) -> str:
        if len(parts) < 2:
            return self._node_help()
        sub = parts[1].lower()
        rest = parts[2:]
        url = self.valves.orchestrator_url
        if sub == "help":
            return self._node_help()
        if sub == "reset":
            job_id, node_key, hint, err = self._resolve_job_node(rest, chat_id)
            if err:
                return err
            r = _HTTP_SESSION.post(
                f"{url}/nodes/{job_id}/{node_key}/reset", json={},
                headers=self._auth_headers(), timeout=self.valves.request_timeout,
            )
            return hint + self._fmt(r)
        if sub in ("del", "delete", "remove"):
            job_id, node_key, hint, err = self._resolve_job_node(rest, chat_id)
            if err:
                return err
            r = _HTTP_SESSION.delete(
                f"{url}/nodes/{job_id}/{node_key}",
                headers=self._auth_headers(), timeout=self.valves.request_timeout,
            )
            return hint + self._fmt(r)
        if sub == "edit":
            return self._handle_node_edit(rest, chat_id)
        if sub == "reorder":
            return self._handle_node_reorder(rest, chat_id)
        return self._node_help()

    def _handle_node_edit(self, rest: list, chat_id: str | None) -> str:
        hint = ""
        if rest and self._JOB_ID_TOKEN_RE.match(rest[0]):
            if len(rest) < 4:
                return ("Usage: `/node edit <job_id> <node_key> <field> <value>` "
                        "(field: title | tool | deliverable)")
            job_id, node_key, field, value = (
                rest[0], rest[1], rest[2].lower(), " ".join(rest[3:]),
            )
        else:
            recalled = self._active_job_recall(chat_id)
            if not recalled or not recalled.get("job_id"):
                return ("❌ No active job in chat memory. Use "
                        "`/node edit <job_id> <node_key> <field> <value>`.")
            if len(rest) < 3:
                return ("Usage: `/node edit <node_key> <field> <value>` "
                        "(field: title | tool | deliverable)")
            job_id = recalled["job_id"]
            hint = self._active_job_hint(job_id, recalled.get("title"))
            node_key, field, value = rest[0], rest[1].lower(), " ".join(rest[2:])
        body: dict = {}
        if field == "title":
            body["title"] = value
        elif field == "tool":
            body["tool"] = value
        elif field in ("deliverable", "is_deliverable"):
            body["is_deliverable"] = value.strip().lower() in ("true", "yes", "1", "on")
        else:
            return (f"Unknown field `{field}`. Editable via chat: "
                    f"title | tool | deliverable. (depends_on / insert: use the API.)")
        r = _HTTP_SESSION.patch(
            f"{self.valves.orchestrator_url}/nodes/{job_id}/{node_key}",
            json=body, headers=self._auth_headers(),
            timeout=self.valves.request_timeout,
        )
        return hint + self._fmt(r)

    def _handle_node_reorder(self, rest: list, chat_id: str | None) -> str:
        hint = ""
        if rest and self._JOB_ID_TOKEN_RE.match(rest[0]):
            if len(rest) < 2:
                return "Usage: `/node reorder <job_id> T1,T2,T3`"
            job_id, keys_str = rest[0], rest[1]
        else:
            recalled = self._active_job_recall(chat_id)
            if not recalled or not recalled.get("job_id"):
                return "❌ No active job in chat memory. Use `/node reorder <job_id> T1,T2,T3`."
            if not rest:
                return "Usage: `/node reorder T1,T2,T3`"
            job_id = recalled["job_id"]
            hint = self._active_job_hint(job_id, recalled.get("title"))
            keys_str = rest[0]
        ordered = [k.strip() for k in keys_str.split(",") if k.strip()]
        r = _HTTP_SESSION.post(
            f"{self.valves.orchestrator_url}/nodes/{job_id}/reorder",
            json={"ordered_keys": ordered}, headers=self._auth_headers(),
            timeout=self.valves.request_timeout,
        )
        return hint + self._fmt(r)

    def _handle_skip(
        self, parts: list, *, chat_id: str | None = None,
    ) -> str:
        """§17.316 — `/skip` tiered confirmation-friction recall.

        Sixth and final cohort member to adopt §17.307's active-job
        memory. /skip has DUAL semantics that map cleanly onto the
        §17.315 tiered model:

          - 0 args, recall hit → 📌 + list candidates (informational,
            §17.307 auto-substitute pattern — safe to recall because
            listing isn't state-altering)
          - 0 args, no recall → pre-§17.316 Usage error
          - 1 arg, UUID-shaped → list candidates (existing §17.215 E1
            behavior — informational, already job_id-specific)
          - 1 arg, non-UUID, recall hit → 📌 + auto-skip on recalled
            job_id (state-altering but operator deliberate per §17.315
            pattern; failure mode is visible 404, not destructive)
          - 1 arg, non-UUID, no recall → friendly error pointing at
            2-arg form, pre-filling typed node_key
          - 2 args → existing explicit skip POST (unchanged)

        The 0-args informational path is the §17.316 contribution
        that distinguishes /skip from §17.314's /execute (where 0
        args is state-altering) and §17.315's /exec retry (where 0
        args needs a node_key to disambiguate). /skip's 0-args is
        safe to auto-recall because the action it produces (list
        candidates) is read-only.
        """
        # 0 args after /skip — list candidates from recall, or Usage.
        if len(parts) < 2:
            recalled = self._active_job_recall(chat_id)
            if recalled and recalled.get("job_id"):
                rid = recalled["job_id"]
                hint = self._active_job_hint(rid, recalled.get("title"))
                return hint + self._render_skip_candidates(rid)
            return (
                "Usage: `/skip <job_id> <node_key>`\n"
                "Example: `/skip 01ab243e T2`\n\n"
                "💡 Use `/jobs` to list your active jobs and copy a job_id, "
                "or bare `/skip <job_id>` to list candidate nodes."
            )

        if _is_placeholder(parts[1]):
            return ("It looks like job_id or node_key is missing or a "
                    "placeholder. Try `/skip 01ab243e T2`.")

        # 1 arg branch — job_id-shaped lists candidates; non-job_id-
        # shaped is a node_key.
        if len(parts) < 3:
            only = parts[1]
            if self._JOB_ID_TOKEN_RE.match(only):
                # Existing §17.215 E1 behavior — bare `/skip <id>`
                # lists candidates. Matches full UUID OR 8-hex-char
                # short_id (the canonical operator-typed form).
                return self._render_skip_candidates(only)
            # Non-UUID single arg: operator specified node_key but not
            # job_id. Auto-substitute from recall (§17.315 pattern).
            recalled = self._active_job_recall(chat_id)
            if not recalled or not recalled.get("job_id"):
                return (
                    f"❌ No active job in chat memory to skip "
                    f"`{only}` on.\n\n"
                    f"Pass an explicit job_id: "
                    f"`/skip <job_id> {only}`. "
                    f"Use `/jobs` to list active jobs, or "
                    f"`/skip <job_id>` (with id alone) to list "
                    f"candidate nodes."
                )
            rid = recalled["job_id"]
            hint = self._active_job_hint(rid, recalled.get("title"))
            r = _HTTP_SESSION.post(
                f"{self.valves.orchestrator_url}/skip",
                json={"job_id": rid, "node_key": only},
                headers=self._auth_headers(),
                timeout=self.valves.request_timeout,
            )
            return hint + self._fmt(r)

        # 2 args — existing explicit path, unchanged.
        if _is_placeholder(parts[2]):
            return ("It looks like job_id or node_key is missing or a "
                    "placeholder. Try `/skip 01ab243e T2`.")
        r = _HTTP_SESSION.post(
            f"{self.valves.orchestrator_url}/skip",
            json={"job_id": parts[1], "node_key": parts[2]},
            headers=self._auth_headers(),
            timeout=self.valves.request_timeout,
        )
        return self._fmt(r)

    def _render_skip_candidates(self, job_id: str) -> str:
        """§17.215 E1 — render a markdown hint listing skippable nodes
        for `job_id` when the user types bare `/skip <job_id>` with no
        node_key. Fetches `/exec/status/{job_id}` and surfaces failed,
        blocked, and pending nodes with copy-pasteable
        ``/skip <job_id> <node_key>`` lines.

        Mirrors the affordance pattern of ``_render_next_actions``
        (§17.195): the user does not have to remember (or look up) the
        node_key — the chat surfaces it. If the job is not reachable or
        has no candidates, we fall back to the original usage hint so
        scripted callers and operator muscle-memory still get a clear
        message.
        """
        try:
            r = _HTTP_SESSION.get(
                f"{self.valves.orchestrator_url}/exec/status/{job_id}",
                headers=self._auth_headers(),
                timeout=self.valves.request_timeout,
            )
        except requests.exceptions.Timeout:
            return (
                "Usage: /skip <job_id> <node_key>\n\n"
                "_(also tried to list candidate nodes, but the request "
                "to the orchestrator timed out.)_"
            )
        except requests.exceptions.ConnectionError:
            return (
                "Usage: /skip <job_id> <node_key>\n\n"
                "_(also tried to list candidate nodes, but the "
                "orchestrator is unreachable.)_"
            )

        if r.status_code == 404:
            return (
                f"Job not found: `{job_id}`.\n\n"
                f"💡 Use `/jobs` to list active jobs and copy a real job_id."
            )
        if r.status_code >= 400:
            return (
                "Usage: /skip <job_id> <node_key>\n\n"
                f"_(also tried to list candidate nodes, but the "
                f"orchestrator returned HTTP {r.status_code}.)_"
            )

        try:
            data = r.json()
        except ValueError:
            return (
                "Usage: /skip <job_id> <node_key>\n\n"
                "_(also tried to list candidate nodes, but the "
                "orchestrator response was not JSON.)_"
            )

        # Bucket nodes by status. The orchestrator emits a `nodes` array
        # of dicts each with `node_key`, `title`, `status`. We surface
        # failed first (most likely to need a skip), then blocked, then
        # pending. `done` / `skipped` / `running` are intentionally
        # excluded — skipping those is a no-op or destructive.
        nodes = [n for n in (data.get("nodes") or []) if isinstance(n, dict)]
        buckets: dict = {"failed": [], "blocked": [], "pending": []}
        for n in nodes:
            status = (n.get("status") or "").lower()
            if status in buckets:
                buckets[status].append(n)

        candidates = buckets["failed"] + buckets["blocked"] + buckets["pending"]
        if not candidates:
            job_status = data.get("status") or data.get("job_status") or "unknown"
            return (
                f"Usage: `/skip <job_id> <node_key>`\n\n"
                f"Job `{job_id}` (status: **{job_status}**) has no "
                f"skippable nodes (no failed / blocked / pending nodes "
                f"found).\n"
            )

        lines = [
            f"Usage: `/skip <job_id> <node_key>`\n",
            f"Candidate nodes for job `{job_id}`:\n",
        ]
        for status_key in ("failed", "blocked", "pending"):
            group = buckets[status_key]
            if not group:
                continue
            lines.append(f"\n**{status_key.capitalize()}:**\n")
            for n in group:
                node_key = n.get("node_key", "?")
                title = n.get("title", "")
                title_part = f" — {title}" if title else ""
                lines.append(f"- `/skip {job_id} {node_key}`{title_part}\n")
        return "".join(lines)

    def _render_umbrella(self, job_id: str, data: dict) -> str:
        """§17.528 — rollup view for a decomposition umbrella: each component
        child + its status, with a drill-in hint."""
        children = data.get("children") or []
        total = data.get("children_total", len(children))
        done = data.get("children_completed", 0)
        status = data.get("job_status", "aggregating")
        head_icon = {"completed": "✅", "failed": "❌", "awaiting_assist": "🙋"}.get(status, "⏳")
        child_icon = {
            "completed": "✅", "failed": "❌", "cancelled": "🛑", "blocked": "⏸️",
            "awaiting_assist": "🙋",  # §17.624 hands-on plan → /assist
        }
        lines = [
            f"{head_icon} **Umbrella** `{job_id}` — {status} "
            f"({done}/{total} components completed)\n"
        ]
        for c in children:
            cs = c.get("status", "?")
            cid = c.get("job_id", "")
            line = (
                f"- {child_icon.get(cs, '⏳')} `{cid}` — {c.get('title', '')} ({cs})"
            )
            # §17.532 — surface a recovery path for stuck components inline.
            if cs in ("failed", "blocked"):
                line += f" → `/results {cid}` to inspect failed nodes & retry"
            lines.append(line)
        lines.append("\n_Drill into any component with `/results <its job_id>`._")
        # §17.533 — once the umbrella finalizes, show the assembled deliverable.
        compiled = data.get("compiled_output")
        if compiled:
            lines.append("\n---\n\n" + compiled)
        return "\n".join(lines)

    # §17.565 — artifacts (typed deliverables)
    def _artifacts_section(self, job_id: str) -> str:
        """Best-effort '📦 Artifacts' block appended to /results output.
        Returns '' on any error or when the job has no artifacts."""
        try:
            r = _HTTP_SESSION.get(
                f"{self.valves.orchestrator_url}/jobs/{job_id}/artifacts",
                headers=self._auth_headers(),
                timeout=self.valves.request_timeout,
            )
            if r.status_code >= 400:
                return ""
            arts = (r.json() or {}).get("artifacts") or []
        except (requests.exceptions.RequestException, ValueError):
            return ""
        if not arts:
            return ""
        lines = ["\n\n---\n\n**📦 Artifacts**\n"]
        for a in arts:
            atype = a.get("artifact_type", "?")
            title = a.get("title") or "(untitled)"
            size = a.get("size_bytes") or 0
            aid = a.get("id", "")
            lines.append(f"- `[{atype}]` {title} ({size} bytes) — `/artifacts {aid}`")
        return "\n".join(lines)

    def _handle_artifacts(self, parts: list) -> str:
        """`/artifacts <artifact_id>` fetches one artifact's content;
        `/artifacts <job_id>` lists a job's artifacts. The id is tried as an
        artifact first, then as a job (both are UUIDs)."""
        if len(parts) < 2 or _is_placeholder(parts[1]):
            return (
                "Usage: `/artifacts <artifact_id>` (fetch one) or "
                "`/artifacts <job_id>` (list a job's artifacts).\n\n"
                "💡 `/results <job_id>` shows the 📦 Artifacts list with ids."
            )
        oid = parts[1].strip()
        base = self.valves.orchestrator_url
        try:
            r = _HTTP_SESSION.get(
                f"{base}/artifacts/{oid}",
                headers=self._auth_headers(),
                timeout=self.valves.request_timeout,
            )
        except requests.exceptions.RequestException:
            return (f"⚠️ Cannot reach orchestrator at {base}. Try `/health`.")
        if r.status_code == 200:
            try:
                a = r.json()
            except ValueError:
                return "⚠️ Unexpected response from orchestrator."
            atype = a.get("artifact_type", "?")
            title = a.get("title") or "(untitled)"
            content = a.get("content") or ""
            fence = "python" if atype == "code" else ""
            return (
                f"**📦 {title}** `[{atype}]` · {a.get('size_bytes', 0)} bytes\n\n"
                f"```{fence}\n{content}\n```"
            )
        # Not an artifact id — try it as a job id (list).
        if r.status_code in (404, 422):
            section = self._artifacts_section(oid)
            if section:
                return "**📦 Artifacts**" + section.split("**📦 Artifacts**", 1)[-1]
            return (
                f"No artifact or job artifacts found for `{oid}`.\n\n"
                f"💡 Run `/results <job_id>` to see a job's artifacts, or check the id."
            )
        return f"⚠️ Error {r.status_code}: {r.text[:200]}"

    def _handle_results(
        self, parts: list, *, chat_id: str | None = None,
    ) -> str:
        # §17.307 — when no explicit id, try active-job memory before
        # falling back to the §17.301 Usage error. Empty cache + no
        # arg = unchanged Usage error (no surprise). The recursive
        # call passes an explicit id so this branch is not re-entered.
        if len(parts) < 2:
            recalled = self._active_job_recall(chat_id)
            if recalled and recalled.get("job_id"):
                rid = recalled["job_id"]
                hint = self._active_job_hint(rid, recalled.get("title"))
                return hint + self._handle_results([parts[0], rid])
            return (
                "Usage: `/results <job_id>`\n"
                "Example: `/results 01ab243e`\n\n"
                "💡 Use `/jobs` to list your active jobs and copy a job_id."
            )
        # §17.301 — placeholder check
        if _is_placeholder(parts[1]):
            return (
                "It looks like job_id is missing or a placeholder. "
                "Try `/results 01ab243e` (use `/jobs` to find a real id)."
            )
        job_id = parts[1].strip()

        # §17.471 — `/results <job_id> nodes` (aliases: full / all / detail)
        # routes to the per-node output view. The default `/results` body
        # returns only the compiled deliverable, which Strategy 0 limits to
        # the DAG's is_output_node leaves — so on a multi-leaf job most node
        # outputs (T1..Tn interior) never appear. This subcommand pulls up
        # every node's full work product.
        if len(parts) >= 3 and parts[2].strip().lower() in (
            "nodes", "full", "all", "detail",
        ):
            return self._handle_node_outputs(job_id)

        try:
            r = _HTTP_SESSION.get(
                f"{self.valves.orchestrator_url}/exec/status/{job_id}",
                headers=self._auth_headers(),
                timeout=self.valves.request_timeout,
            )
        except requests.exceptions.Timeout:
            return "⚠️ Request timed out."
        except requests.exceptions.ConnectionError:
            return (
                f"⚠️ Cannot reach orchestrator at {self.valves.orchestrator_url}.\n\n"
                f"💡 Try `/health` to probe each subsystem (Postgres + Ollama + Milvus + Redis)."
            )

        if r.status_code == 404:
            return (
                f"Job not found: `{job_id}`.\n\n"
                f"💡 Use `/jobs` to list active jobs and copy a real job_id."
            )
        if r.status_code >= 400:
            return f"⚠️ Error {r.status_code}: {r.text[:200]}"
        try:
            data = r.json()
        except ValueError:
            return "⚠️ Unexpected response from orchestrator."

        status = data.get("status") or data.get("job_status") or "unknown"

        # §17.528 — an umbrella (task decomposition) has no DAG; show the
        # component-children rollup instead of an empty node view.
        if data.get("job_type") == "umbrella":
            return self._render_umbrella(job_id, data)

        if status in ("completed", "done"):
            compiled = data.get("compiled_output", "")
            # §17.471 — the compiled deliverable is assembled from the DAG's
            # is_output_node leaves only (execution_compile Strategy 0), so
            # interior nodes' work is not shown here. Point operators at the
            # per-node view so they can pull up every node T1..Tn.
            total = data.get("total_nodes") or 0
            nodes_hint = (
                f"\n\n---\n_Showing the compiled deliverable. To see every "
                f"node's full output ({total} nodes), run "
                f"`/results {job_id} nodes`._"
                if total else ""
            )
            if compiled:
                return compiled + nodes_hint + self._artifacts_section(job_id)
            return (
                f"✅ Job `{job_id}` completed, but no compiled output is "
                f"available.{nodes_hint}{self._artifacts_section(job_id)}"
            )

        if status in ("running", "executing", "planning", "researching", "refining"):
            total = data.get("total_nodes") or data.get("task_count") or 0
            # #1: orchestrator returns per-status `counts` and a `nodes` array.
            # Earlier code asked for `completed_nodes`/`current_node` which were
            # never emitted, so progress always read "0/N". Derive from counts.
            counts = data.get("counts") or {}
            done = int(counts.get("done", 0)) + int(counts.get("skipped", 0))
            failed = int(counts.get("failed", 0))
            running_node = next(
                (n for n in (data.get("nodes") or [])
                 if isinstance(n, dict) and n.get("status") == "running"),
                None,
            )
            cur_str = (
                f", currently running: {running_node.get('node_key','?')} "
                f"({running_node.get('title','?')})"
                if running_node else ""
            )
            fail_str = f", {failed} failed" if failed else ""
            head = f"⏳ Status: **{status}** — {done}/{total} nodes complete{fail_str}{cur_str}"
            # §17.288 — provide a path forward when the orchestrator
            # omits next_actions on an in-progress status (older
            # orchestrator, or transient-empty during a status flip).
            # Pre-§17.288 the operator got just the progress line with
            # nowhere to go; now they always see at least a copy-pasteable
            # next step.
            actions_block = self._render_next_actions(data)
            if actions_block:
                return head + actions_block
            return (
                head
                + f"\n\n_No next steps suggested yet — re-run "
                + f"`/results {job_id}` after the next node completes._"
            )

        if status in ("failed", "blocked", "cancelled"):
            err = (data.get("error_summary") or data.get("error")
                   or data.get("message") or "")
            counts = data.get("counts") or {}
            total = data.get("total_nodes") or 0
            done = int(counts.get("done", 0)) + int(counts.get("skipped", 0))
            failed_n = int(counts.get("failed", 0))
            failed_nodes = [
                n for n in (data.get("nodes") or [])
                if isinstance(n, dict) and n.get("status") == "failed"
            ]
            lines = [f"⚠️ Status: **{status}** — {done}/{total} nodes complete, {failed_n} failed"]
            if err:
                lines.append(f"_{err}_")
            if failed_nodes:
                lines.append("")
                lines.append("**Failed nodes:**")
                lines.append("")
                lines.append("| Node | Title | Model |")
                lines.append("|---|---|---|")
                for n in failed_nodes:
                    lines.append(
                        f"| `{n.get('node_key','?')}` | {n.get('title','?')} "
                        f"| `{n.get('assigned_model') or 'default'}` |"
                    )
            # Audit item 10: the orchestrator now supplies a structured
            # next_actions list with the failed node_key already filled
            # in. Render that instead of hardcoded retry/skip lines so
            # the source of truth lives server-side.
            actions_block = self._render_next_actions(data)
            if actions_block:
                lines.append(actions_block)
            return "\n".join(lines)

        if status == "awaiting_confirmation":
            head = f"⏸️ Status: **{status}** — job is waiting for your review."
            return head + self._render_next_actions(data)

        head = f"Status: **{status}**"
        actions_block = self._render_next_actions(data)
        return head + (actions_block if actions_block else " (no further details available)")

    # ------------------------------------------------------------------
    # §17.471 — per-node output view (`/results <job_id> nodes`)
    # ------------------------------------------------------------------

    # Per-node body cap. Generous enough to show real work product, but
    # bounded so a 10-node job doesn't emit a 50k-char chat message. The
    # untruncated text lives in the DB / web detail page.
    _NODE_OUTPUT_PREVIEW_CHARS = 3000

    def _handle_node_outputs(self, job_id: str) -> str:
        """Render every node's output for a completed/in-progress job.

        Backs `/results <job_id> nodes`. Calls `GET /exec/nodes/{job_id}`
        (which returns full `output_text` per node) and renders each node
        T1..Tn with its status, output-node marker, and a capped body.
        """
        try:
            r = _HTTP_SESSION.get(
                f"{self.valves.orchestrator_url}/exec/nodes/{job_id}",
                headers=self._auth_headers(),
                timeout=self.valves.request_timeout,
            )
        except requests.exceptions.Timeout:
            return "⚠️ Request timed out."
        except requests.exceptions.ConnectionError:
            return (
                f"⚠️ Cannot reach orchestrator at {self.valves.orchestrator_url}.\n\n"
                f"💡 Try `/health` to probe each subsystem (Postgres + Ollama + Milvus + Redis)."
            )

        if r.status_code == 404:
            return (
                f"Job not found: `{job_id}`.\n\n"
                f"💡 Use `/jobs` to list active jobs and copy a real job_id."
            )
        if r.status_code >= 400:
            return f"⚠️ Error {r.status_code}: {r.text[:200]}"
        try:
            data = r.json()
        except ValueError:
            return "⚠️ Unexpected response from orchestrator."

        nodes = data.get("nodes") or []
        if not nodes:
            return (
                f"Job `{job_id}` has no DAG nodes yet.\n\n"
                f"💡 If it's still planning, re-run `/results {job_id}` shortly."
            )

        title = data.get("job_title") or job_id
        jstatus = data.get("job_status") or "unknown"
        # §17.475 — prefer the explicit deliverable marker; fall back to the
        # is_output_node leaves for pre-§17.475 jobs (matching compile
        # Strategy 0's own fallback).
        out_keys = [
            n.get("node_key", "?") for n in nodes if n.get("is_deliverable")
        ] or [
            n.get("node_key", "?") for n in nodes if n.get("is_output_node")
        ]
        out_set = set(out_keys)
        lines = [
            f"## Node outputs — {title}",
            f"_Job status: **{jstatus}** · {len(nodes)} nodes_",
        ]
        if out_keys:
            # Make explicit which nodes fed the compiled deliverable so the
            # gap (interior nodes omitted from `/results`) is legible.
            lines.append(
                f"_Compiled deliverable is built from: "
                f"{', '.join('`' + k + '`' for k in out_keys)}._"
            )
        lines.append("")

        cap = self._NODE_OUTPUT_PREVIEW_CHARS
        for n in nodes:
            nk = n.get("node_key", "?")
            n_status = n.get("status", "unknown")
            icon = STATUS_ICONS.get(n_status, "")
            marker = " ⭐" if n.get("node_key") in out_set else ""
            lines.append(f"### {nk} — {n.get('title', '')} · {icon} {n_status}{marker}")
            body = n.get("output_text") or ""
            if not body:
                lines.append("_(no output)_\n")
                continue
            total = len(body)
            if total > cap:
                body = body[:cap] + f"\n\n… [{total - cap} more chars — see /web/jobs/{job_id}]"
            lines.append(body)
            lines.append("")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Formatter
    # ------------------------------------------------------------------

    def _fmt(self, r: requests.Response) -> str:
        try:
            data = r.json()
        except Exception:
            return f"HTTP {r.status_code}: {r.text[:500]}"
        if r.status_code >= 400:
            return f"⚠️ Error {r.status_code}: {data.get('message') or data.get('detail') or r.text[:200]}"
        return f"```json\n{json.dumps(data, indent=2)}\n```"

    # ------------------------------------------------------------------
    # §17.303 — focused renderers for job-id-producing success paths
    # ------------------------------------------------------------------

    def _render_ideate_response(
        self, r: requests.Response, *, chat_id: str | None = None,
    ) -> str:
        """§17.303 — render Phase 1 (`/idea` → /ideate) success with a
        pre-filled Next-block instead of a raw JSON dump.

        Falls back to ``_fmt(r)`` for:
          * HTTP ≥ 400 (existing error rendering)
          * Non-JSON 200 body
          * 200 body without ``job_id`` (defensive)

        The orchestrator's pre-§17.303 ``message`` field already
        suggests ``Reply /confirm <job_id> to proceed`` — but the
        placeholder is the LITERAL string ``<job_id>``, not the real
        id. The renderer fills in the actual id so operators can
        copy-paste straight to the next turn.

        §17.307 — on success, seed active-job memory so subsequent
        `/results` and `/cost` invocations without an explicit id
        recall this job_id automatically.
        """
        if r.status_code >= 400:
            return self._fmt(r)
        try:
            data = r.json()
        except Exception:
            return self._fmt(r)
        if not isinstance(data, dict) or not data.get("job_id"):
            return self._fmt(r)

        job_id = data["job_id"]
        status = data.get("status", "?")
        brief = data.get("refined_brief") or {}
        feasibility = data.get("feasibility") or {}

        # §17.307 — seed active-job memory. Best-effort + gated on the
        # valve + chat_id. Runs before rendering so a render exception
        # doesn't strand the cache.
        brief_title_for_cache = (
            brief.get("title") if isinstance(brief, dict) else None
        )
        self._active_job_remember(
            chat_id, job_id, title=brief_title_for_cache,
        )

        lines: list[str] = [
            f"✅ **Job created** `{job_id}` (status: `{status}`)\n",
        ]
        # Refined brief summary — title + 1-line description if present.
        brief_title = brief.get("title") if isinstance(brief, dict) else None
        if brief_title:
            lines.append(f"**Refined brief:** {brief_title}")
        # Feasibility verdict (a 2026-04 audit added structured fields).
        if isinstance(feasibility, dict):
            feasible = feasibility.get("feasible")
            confidence = feasibility.get("confidence")
            if feasible is not None:
                verdict = "✅ feasible" if feasible else "⚠️ infeasible"
                conf_str = (
                    f" (confidence: {confidence:.2f})"
                    if isinstance(confidence, (int, float)) else ""
                )
                lines.append(f"**Feasibility:** {verdict}{conf_str}")
        # Surface the feasibility-fallback warning if the orchestrator
        # set it (pre-§17.303 it lived in the ``message`` field; we
        # extract it explicitly so the next-block stays clean).
        msg = data.get("message", "")
        if isinstance(msg, str) and "Feasibility check failed" in msg:
            lines.append(
                "\n⚠️ Feasibility check failed; using best-effort defaults. "
                "Review the brief above."
            )

        lines.append("\n**Next steps:**")
        lines.append(
            f"- `/confirm {job_id}` — auto-chain Phase 2 "
            f"(research → compile → DAG → execute)"
        )
        lines.append(
            f"- `/confirm {job_id} <your feedback>` — adjust the brief "
            f"before proceeding"
        )
        lines.append(f"- `/results {job_id}` — peek at current state")
        if self.valves.advanced_commands_enabled:
            lines.append(f"- `/cost {job_id}` — see costs so far")

        # Append the raw JSON as a smaller footer so operators who want
        # the full payload still have it.
        lines.append(
            f"\n<details><summary>Full Phase 1 response</summary>\n\n"
            f"```json\n{json.dumps(data, indent=2)}\n```\n\n</details>"
        )
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # /model command system
    # ------------------------------------------------------------------

    def _handle_model(self, msg: str) -> str:
        parts = msg.split()
        if len(parts) < 2:
            return self._model_help()
        sub = parts[1].lower()
        if sub == "list":      return self._model_list()
        if sub == "available": return self._model_available()
        if sub == "set":       return self._model_set(parts)
        if sub == "reset":     return self._model_reset()
        if sub == "probe":     return self._model_probe()
        if sub == "help":      return self._model_help()
        close = difflib.get_close_matches(sub, ("list", "available", "set", "reset", "probe", "help"), n=2, cutoff=0.6)
        hint = ""
        if close:
            hint = "\n\nClosest matches:\n" + "\n".join(f"  - `/model {c}`" for c in close)
        return f"Unknown subcommand: `/model {sub}`{hint}\n\n{self._model_help()}"

    def _model_list(self) -> str:
        lines = ["| Role | Current Model | Default? |", "|---|---|---|"]
        default_valves = self.Valves()
        for role in self._MODEL_ROLES:
            current = getattr(self.valves, role, "")
            default = getattr(default_valves, role, "")
            is_default = "yes" if current == default else "no"
            lines.append(f"| `{role.replace('model_','')}` | `{current}` | {is_default} |")
        return "**Current Model Assignments**\n\n" + "\n".join(lines)

    def _model_available(self) -> str:
        try:
            r = _HTTP_SESSION.get(f"{self.valves.ollama_url}/api/tags",
                             timeout=self.valves.request_timeout)
            r.raise_for_status()
            models = r.json().get("models", [])
            if not models:
                return "No models found on Ollama."
            names = sorted(m["name"] for m in models)
            return f"**Available Ollama Models** ({len(names)}):\n\n" + "\n".join(f"- `{n}`" for n in names)
        except requests.exceptions.ConnectionError:
            return f"Cannot reach Ollama at `{self.valves.ollama_url}`."
        except Exception as e:
            return f"Error querying Ollama: {e}"

    def _model_set(self, parts: list) -> str:
        if len(parts) < 4:
            return "Usage: `/model set <role> <model>`\nExample: `/model set general qwen3:8b`"

        role_input = parts[2].lower()
        model_tag = parts[3]
        role_key = role_input if role_input.startswith("model_") else f"model_{role_input}"

        if role_key not in self._MODEL_ROLES:
            valid = ", ".join(r.replace("model_", "") for r in self._MODEL_ROLES)
            return f"Unknown role: `{role_input}`\nValid roles: {valid}"

        if role_key in self._SINGLETON_ROLES:
            env_var = role_key.upper()
            return (f"Role `{role_input}` is config-locked; set `{env_var}` env var "
                    f"and restart the container.")

        if role_key != "model_reranker":
            try:
                r = _HTTP_SESSION.get(f"{self.valves.ollama_url}/api/tags",
                                 timeout=self.valves.request_timeout)
                r.raise_for_status()
                available = {m["name"] for m in r.json().get("models", [])}
                available_bare = {n.replace(":latest", "") for n in available}
                if model_tag not in available and model_tag not in available_bare:
                    return (f"Model `{model_tag}` not found on Ollama.\n"
                            f"Run `/model available` to see available models.")
            except requests.exceptions.ConnectionError:
                return "Cannot reach Ollama to validate. Model not set."
            except Exception as e:
                return f"Validation error: {e}"

        old = getattr(self.valves, role_key)
        setattr(self.valves, role_key, model_tag)
        result = f"**Updated `{role_key.replace('model_','')}`**\n`{old}` -> `{model_tag}`"
        result += "\n\n_(session-only; container restart reverts to env/defaults)_"
        return result

    def _model_reset(self) -> str:
        default_valves = self.Valves()
        changes = []
        for role in self._MODEL_ROLES:
            current = getattr(self.valves, role)
            default = getattr(default_valves, role)
            if current != default:
                setattr(self.valves, role, default)
                changes.append(f"- `{role.replace('model_','')}`: `{current}` -> `{default}`")
        if not changes:
            return "All roles are already at default values."
        return "**Reset to defaults:**\n\n" + "\n".join(changes)

    def _model_probe(self) -> str:
        ok, msg = self._probe_embedder_dim()
        status = "OK" if ok else "FAIL"
        return f"**Embedder probe: {status}**\n`{msg}`"

    def _model_help(self) -> str:
        return """**Model Commands**
| Command | Description |
|---|---|
| `/model list` | Show current model assignments |
| `/model available` | List models available on Ollama |
| `/model set <role> <model>` | Assign a model to a role |
| `/model reset` | Reset all roles to defaults |
| `/model probe` | Probe embedder dimension (must equal 512) |
| `/model help` | Show this message |

**Roles:** general, verifier, coder, embedder, reranker, router, fallback, cloud_alt
**Example:** `/model set general qwen3:8b`"""

    # ------------------------------------------------------------------
    # /schedule command system (#8.9)
    # ------------------------------------------------------------------

    @staticmethod
    def _schedule_help() -> str:
        """§17.312 — richer /schedule help. Mirror of §17.310's
        /research mode panel + §17.309's /jobs UX patterns: table of
        subcommands + Examples block teaching cron flavors + Flags
        section calling out `--depth` and `--tz` explicitly."""
        return (
            "**`/schedule` — Recurring research crons**\n\n"
            "| Command | What it does |\n"
            "|---|---|\n"
            "| `/schedule list` | Show all schedules |\n"
            "| `/schedule add \"<cron>\" <topic>` | Create a recurring research |\n"
            "| `/schedule add \"<cron>\" --depth=<lvl> --tz=<IANA> <topic>` "
            "| Create with flags |\n"
            "| `/schedule delete <id>` | Remove a schedule |\n"
            "\n"
            "**Cron format:** `minute hour day month weekday` (UTC by default)\n\n"
            "**Examples:**\n"
            "- `/schedule add \"0 9 * * 1\" kubernetes news` "
            "— Mondays at 9am UTC\n"
            "- `/schedule add \"0 0 * * *\" daily AI roundup` "
            "— every day at midnight UTC\n"
            "- `/schedule add \"0 9 * * 1\" --tz=America/New_York "
            "kubernetes news` — 9am ET\n"
            "\n"
            "**Flags:**\n"
            "- `--depth shallow | medium | deep` — research iteration "
            "count (default: medium)\n"
            "- `--tz <IANA>` — timezone for the cron expression "
            "(default: UTC)\n"
        )

    @staticmethod
    def _schedule_empty_state() -> str:
        """§17.312 — friendly empty state for `/schedule list`. Pre-fix
        was a single-line starter; post-fix surfaces 3 cron flavors
        + the `--tz` tip so non-UTC operators discover the flag."""
        return (
            "## 🗓 Schedules\n\n"
            "_No schedules yet._\n\n"
            "**Get started — pick a cron shape:**\n"
            "- `/schedule add \"0 9 * * 1\" kubernetes news` "
            "— Mondays at 9am UTC\n"
            "- `/schedule add \"0 0 * * *\" daily AI roundup` "
            "— every day at midnight UTC\n"
            "- `/schedule add \"0 9 * * 1\" --tz=America/New_York "
            "kubernetes news` — 9am ET\n"
            "\n"
            "_Cron format: `minute hour day month weekday`. "
            "Run `/schedule help` for flags and more shapes._"
        )

    def _handle_schedule(self, msg: str) -> str:
        parts = msg.split(None, 2)
        sub = parts[1].lower() if len(parts) > 1 else "help"
        base = self.valves.orchestrator_url
        hdr = self._auth_headers()

        valid_subs = ("list", "add", "delete", "help")
        if sub != "help" and sub not in valid_subs:
            close = difflib.get_close_matches(sub, valid_subs, n=2, cutoff=0.6)
            hint = ""
            if close:
                hint = "\n\nClosest matches:\n" + "\n".join(f"  - `/schedule {c}`" for c in close)
            return f"Unknown subcommand: `/schedule {sub}`{hint}\n\nRun `/schedule help` for the full list."
        if sub == "help":
            # §17.312 — richer help with table + Examples + Flags
            # (mirror of §17.310's /research mode panel shape).
            return self._schedule_help()

        if sub == "list":
            try:
                r = _HTTP_SESSION.get(f"{base}/schedule", headers=hdr,
                                 timeout=self.valves.request_timeout)
                r.raise_for_status()
                rows = r.json().get("schedules", [])
            except Exception as exc:
                return f"❌ Failed to list schedules: {exc}"
            # §17.312 — friendlier empty state (3 cron flavors + tz tip)
            # and a next-actions footer on populated results (mirror of
            # §17.309's /jobs UX).
            if not rows:
                return self._schedule_empty_state()
            lines = ["| ID | Topic | Cron | Depth | Next Run | Runs | Failures |",
                     "|----|-------|------|-------|----------|------|----------|"]
            for s in rows:
                lines.append(
                    f"| {s['id']} | {s['topic']} | `{s['cron_expression']}` | "
                    f"{s['depth']} | {s.get('next_run_at') or '—'} | "
                    f"{s['run_count']} | {s['failure_count']} |"
                )
            footer = (
                "\n\n---\n\n"
                "💡 **Next:**\n"
                "- `/schedule add \"<cron>\" <topic>` — create another\n"
                "- `/schedule delete <id>` — remove a schedule\n"
                "- `/schedule help` — full reference (cron syntax + flags)"
            )
            return "\n".join(lines) + footer

        if sub == "add":
            # CommandParser shared with /research (Tier 1 #1, #2, #3, #5).
            parser = CommandParser("schedule add", "Create a recurring research schedule")
            parser.add_argument(
                "--depth", choices=["shallow", "medium", "deep"], default="medium",
                help="Research iteration count",
            )
            parser.add_argument(
                "--tz", default="UTC",
                help="IANA timezone for the cron schedule (default UTC)",
            )
            parser.add_example('/schedule add "0 9 * * 1" --depth=medium kubernetes news')
            parser.add_example('/schedule add "0 9 * * 1" --tz=America/New_York kubernetes news')

            raw_args = parts[2] if len(parts) >= 3 else ""
            if raw_args.strip() in ("--help", "-h", "help"):
                return parser.help_text() + "\n\nCron: `minute hour day month weekday`."
            if not raw_args.strip():
                return "Usage: `/schedule add <cron> [--depth=<level>] [--tz=<IANA>] <topic>`"

            try:
                args, _, positional = parser.parse(raw_args)
            except _ChatArgError as exc:
                return str(exc)
            if len(positional) < 2:
                return "Usage: `/schedule add \"<cron>\" [--depth=<level>] [--tz=<IANA>] <topic>`"

            cron_expr = positional[0]
            topic = " ".join(positional[1:])
            if _is_placeholder(topic):
                return ("It looks like the topic is missing or a placeholder. "
                        "Try `/schedule add \"0 9 * * 1\" kubernetes news`.")
            depth = args.depth
            try:
                r = _HTTP_SESSION.post(
                    f"{base}/schedule", headers=hdr,
                    timeout=self.valves.request_timeout,
                    json={"topic": topic, "cron_expression": cron_expr,
                          "depth": depth,
                          "timezone": args.tz,
                          "model_overrides": self._model_overrides()},
                )
                if r.status_code == 422:
                    return f"❌ {r.json().get('detail', 'validation failed')}"
                r.raise_for_status()
                s = r.json()
                return (f"✅ Scheduled **#{s['id']}**: {s['topic']}\n"
                        f"Cron: `{s['cron_expression']}` ({s.get('timezone','UTC')}) — depth: {s['depth']}")
            except Exception as exc:
                return f"❌ Failed to create schedule: {exc}"

        # delete
        if len(parts) < 3 or not parts[2].strip().isdigit():
            return "Usage: `/schedule delete <id>`"
        sid = int(parts[2].strip())
        try:
            r = _HTTP_SESSION.delete(f"{base}/schedule/{sid}", headers=hdr,
                                timeout=self.valves.request_timeout)
            if r.status_code == 404:
                return (
                    f"❌ Schedule #{sid} not found.\n\n"
                    f"💡 Use `/schedule list` to see active schedules and copy a real id."
                )
            r.raise_for_status()
            return f"🗑️ Deleted schedule #{sid}"
        except Exception as exc:
            return f"❌ Failed to delete: {exc}"

    # ------------------------------------------------------------------
    # U.8.D — chat parity for /exec, /cleanup, /config, /logs, /health
    # ------------------------------------------------------------------

    def _handle_exec(
        self, parts: list, *, chat_id: str | None = None,
    ) -> str:
        # _handle_command splits with maxsplit=2 so parts[2] (if present) is
        # the post-subcommand tail as one string. Re-split it here.
        if len(parts) < 2 or parts[1] == "help":
            return ("Usage: `/exec retry <job_id> <node_key>`\n\n"
                    "Resets a failed/blocked node to `pending` so it can run again.\n"
                    "Run `/results <job_id>` for a failure report with prefilled args.")
        sub = parts[1].lower()
        tail = parts[2].split() if len(parts) > 2 else []
        if sub == "retry":
            # §17.315 — tiered confirmation-friction recall for the
            # state-altering /exec retry. Three friction tiers based on
            # operator specificity:
            #   0 args  → 3 options (operator typed nothing concrete)
            #   1 arg, UUID-shaped → Usage error (ambiguous — could be
            #     job_id-with-missing-node OR node_key-named-like-uuid)
            #   1 arg, non-UUID → auto-substitute job_id from recall +
            #     fire (operator specified the node — intent is clear)
            #   2+ args → existing path (unchanged)
            if len(tail) == 0:
                recalled = self._active_job_recall(chat_id)
                if recalled and recalled.get("job_id"):
                    rid = recalled["job_id"]
                    short = rid[:8] if len(rid) >= 8 else rid
                    title = recalled.get("title")
                    title_part = f" — _{title}_" if title else ""
                    return (
                        f"📌 Active job: `{short}`{title_part}.\n\n"
                        f"⚠️ `/exec retry` re-runs a failed/blocked "
                        f"node — state-altering.\n\n"
                        f"- Type `/exec retry <node_key>` "
                        f"(uses active job)\n"
                        f"- Type `/exec retry <other_job_id> <node_key>` "
                        f"to target a different job\n"
                        f"- Or check the job first: `/results {short}`"
                    )
                return "Usage: `/exec retry <job_id> <node_key>`"
            if len(tail) == 1:
                only = tail[0]
                if self._JOB_ID_TOKEN_RE.match(only):
                    # job_id-shaped single arg is ambiguous (operator
                    # typed job_id but forgot node_key). Matches full
                    # UUID OR 8-hex short_id. Refuse to guess — point
                    # at the 2-arg form.
                    return (
                        "Usage: `/exec retry <job_id> <node_key>`\n\n"
                        f"Looks like you typed a job_id (`{only[:8]}`) "
                        f"without a node_key. Add the node: "
                        f"`/exec retry {only[:8]} <node_key>`.\n\n"
                        "💡 Use `/results <job_id>` to see failed nodes "
                        "with prefilled retry commands."
                    )
                # Non-UUID single arg: operator specified node_key but
                # not job_id. Auto-substitute from recall.
                if _is_placeholder(only):
                    return ("It looks like node_key is a placeholder. "
                            "Try `/exec retry T2` (active job will be "
                            "used) or `/exec retry 01ab243e T2`.")
                recalled = self._active_job_recall(chat_id)
                if not recalled or not recalled.get("job_id"):
                    return (
                        f"❌ No active job in chat memory to retry "
                        f"`{only}` on.\n\n"
                        f"Pass an explicit job_id: "
                        f"`/exec retry <job_id> {only}`. "
                        f"Use `/jobs` to list active jobs."
                    )
                rid = recalled["job_id"]
                hint = self._active_job_hint(rid, recalled.get("title"))
                r = _HTTP_SESSION.post(
                    f"{self.valves.orchestrator_url}/exec/retry",
                    json={"job_id": rid, "node_key": only},
                    headers=self._auth_headers(),
                    timeout=self.valves.request_timeout,
                )
                return hint + self._fmt(r)
            # 2+ args — existing explicit path, unchanged.
            job_id, node_key = tail[0], tail[1]
            if _is_placeholder(job_id) or _is_placeholder(node_key):
                return ("It looks like job_id or node_key is a placeholder. "
                        "Try `/exec retry 01ab243e T2`.")
            r = _HTTP_SESSION.post(
                f"{self.valves.orchestrator_url}/exec/retry",
                json={"job_id": job_id, "node_key": node_key},
                headers=self._auth_headers(),
                timeout=self.valves.request_timeout,
            )
            return self._fmt(r)
        return f"Unknown `/exec` subcommand: `{sub}`. Try `/exec help`."

    def _handle_cleanup(self) -> str:
        r = _HTTP_SESSION.post(
            f"{self.valves.orchestrator_url}/jobs/cleanup",
            json={},
            headers=self._auth_headers(),
            timeout=self.valves.request_timeout,
        )
        if r.status_code >= 400:
            return self._fmt(r)
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if not isinstance(data, dict):
            return self._fmt(r)
        counts = {k: v for k, v in data.items() if isinstance(v, int)}
        if not counts:
            return f"```json\n{json.dumps(data, indent=2)}\n```"
        lines = ["**🧹 Stale-job reaper run**", "", "| Action | Count |", "|---|---:|"]
        for k, v in sorted(counts.items()):
            lines.append(f"| {k} | {v} |")
        return "\n".join(lines)

    def _handle_config(self, parts: list) -> str:
        """`/config [substring] [--non-defaults]` — render Settings table.

        Without args, lists every field. Substring filters by name. Flag
        `--non-defaults` shows only fields whose live value differs from
        the default. Values for sensitive fields are server-redacted.
        """
        non_defaults_only = False
        substr = None
        for arg in parts[1:]:
            if arg == "--non-defaults":
                non_defaults_only = True
            elif arg.startswith("--"):
                return f"Unknown flag: `{arg}`. Supported: `--non-defaults`."
            else:
                substr = arg.lower()
        r = _HTTP_SESSION.get(
            f"{self.valves.orchestrator_url}/config",
            headers=self._auth_headers(),
            timeout=self.valves.request_timeout,
        )
        if r.status_code >= 400:
            return self._fmt(r)
        data = r.json()
        fields = data.get("fields", []) if isinstance(data, dict) else []
        if substr:
            fields = [f for f in fields if substr in str(f.get("name", "")).lower()]
        if non_defaults_only:
            fields = [f for f in fields if not f.get("is_default")]
        if not fields:
            return "(no settings match those filters)"
        lines = [
            f"## ⚙️ Config — {len(fields)} field(s)"
            + (f", filter=`{substr}`" if substr else "")
            + (", non-default only" if non_defaults_only else ""),
            "",
            "| Setting | Value | Default? |",
            "|---|---|---|",
        ]
        # Cap at 60 rows so chat doesn't choke on /config with no filter.
        for f in fields[:60]:
            name = str(f.get("name", ""))[:40]
            value = str(f.get("value", ""))[:60]
            is_default = "✓" if f.get("is_default") else "—"
            lines.append(f"| `{name}` | `{value}` | {is_default} |")
        if len(fields) > 60:
            lines.append("")
            lines.append(f"_…{len(fields) - 60} more (filter to narrow)._")
        return "\n".join(lines)

    def _handle_logs(
        self, parts: list, *, chat_id: str | None = None,
    ) -> str:
        # §17.311 — extend §17.307 active-job memory pattern to /logs
        # (third read-only id-taker after /results + /cost). Same
        # contract: cache hit + no arg = recall + 📌 hint; cache miss
        # = the §17.301-style Usage error (richer than the pre-§17.311
        # terse one-liner).
        if len(parts) < 2:
            recalled = self._active_job_recall(chat_id)
            if recalled and recalled.get("job_id"):
                rid = recalled["job_id"]
                hint = self._active_job_hint(rid, recalled.get("title"))
                return hint + self._handle_logs([parts[0], rid])
            return (
                "Usage: `/logs <job_id>`\n"
                "Example: `/logs 01ab243e`\n\n"
                "💡 Use `/jobs` to list your active jobs and copy a job_id."
            )
        if _is_placeholder(parts[1]):
            return "It looks like job_id is missing or a placeholder. Try `/logs 01ab243e`."
        job_id = parts[1]
        r = _HTTP_SESSION.get(
            f"{self.valves.orchestrator_url}/logs/{job_id}",
            params={"limit": 50, "offset": 0},
            headers=self._auth_headers(),
            timeout=self.valves.request_timeout,
        )
        if r.status_code >= 400:
            return self._fmt(r)
        try:  # §17.611 (audit #22) — parse once (requests doesn't cache .json())
            data = r.json()
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        nodes = data.get("nodes") or []
        total = data.get("node_count", len(nodes))
        job_status = data.get("job_status", "?")
        header = (f"## 🪵 Logs — `{job_id[:8]}`  status: `{job_status}`  "
                  f"({len(nodes)}/{total} nodes)")
        if not nodes:
            return header + "\n\n_(no DAG nodes — job may not have been planned yet)_"
        # §17.447 (Phase B / B2) — "Verify" labels the column as the verifier's
        # confidence in each node's output (vs a retrieval or feasibility score).
        lines = [header, "", "| Key | Status | Verify | Tool | Output preview |",
                 "|---|---|---:|---|---|"]
        failures = []
        for n in nodes:
            key = str(n.get("node_key", ""))[:10]
            st = str(n.get("status", ""))[:10]
            conf = n.get("confidence")
            conf_s = f"{conf:.2f}" if isinstance(conf, (int, float)) else "—"
            tool = str(n.get("tool", ""))[:10]
            # §17.445 — API field is `output_preview` (was reading the absent
            # `output_text`, so the preview column was always blank).
            preview = (n.get("output_preview") or "").replace("\n", " ").replace("|", "\\|")[:60]
            lines.append(f"| `{key}` | {st} | {conf_s} | {tool} | {preview} |")
            # §17.445 (Phase A / A1) — collect the "why" for failed/blocked nodes.
            reason = n.get("failure_reason")
            if reason and n.get("status") in ("failed", "blocked"):
                failures.append((n.get("node_key", "?"), str(reason)))
        if failures:
            lines.append("\n**Why these failed/blocked:**")
            for k, reason in failures:
                lines.append(f"- `{k}` — {reason[:240]}")
        return "\n".join(lines)

    def _handle_health(self) -> str:
        try:
            r = _HTTP_SESSION.get(
                f"{self.valves.orchestrator_url}/health",
                headers=self._auth_headers(),
                timeout=self.valves.request_timeout,
            )
        except requests.exceptions.Timeout:
            # §17.317 — friendly recovery shape when /health itself is
            # unreachable. Operators arrive at /health from §17.302's
            # "cannot reach orchestrator → try /health" recovery hints,
            # so the next layer down also needs a recovery surface.
            return self._render_health_unreachable("timed out")
        except requests.exceptions.ConnectionError:
            return self._render_health_unreachable("refused")
        if r.status_code >= 400:
            # §17.317 — pair the raw error with a recovery footer
            # (rather than the bare _fmt JSON dump). The orchestrator
            # is reachable but the /health endpoint errored — still
            # actionable.
            return self._fmt(r) + self._render_health_recovery_footer()
        try:  # §17.611 (audit #22) — parse once (requests doesn't cache .json())
            data = r.json()
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        checks = data.get("checks", {})
        if not checks:
            return f"```json\n{json.dumps(data, indent=2)}\n```"
        UP = {"up", "ok", "healthy", "true"}
        DOWN = {"down", "fail", "error", "unhealthy"}
        lines = ["## 🩺 Health", "", "| Subsystem | Status | Latency |",
                 "|---|---|---:|"]
        # §17.317 — track down subsystems for the verdict header and
        # recovery footer.
        down_names: list[str] = []
        up_count = 0
        for name, info in checks.items():
            if isinstance(info, dict):
                status = str(info.get("status", "?"))
                latency = info.get("latency_ms")
            else:
                status = str(info)
                latency = None
            lower = status.lower()
            icon = "✅" if lower in UP else ("❌" if lower in DOWN else "ℹ️")
            lat = f"{latency} ms" if latency is not None else "—"
            lines.append(f"| {name} | {icon} {status} | {lat} |")
            if lower in UP:
                up_count += 1
            elif lower in DOWN:
                down_names.append(name)
        # §17.317 — single-line verdict above the table for at-a-glance
        # scan. Operators landing here from §17.302's recovery hint want
        # "is it broken or not?" first, details second. Insert verdict
        # at index 2 (between the title and the empty line that the
        # original "##" header expected).
        total = len(checks)
        if not down_names:
            verdict = f"✅ **All {total} subsystems up.**"
        else:
            names_fmt = ", ".join(f"`{n}`" for n in down_names)
            verdict = (
                f"⚠️ **{len(down_names)} of {total} subsystems down:** "
                f"{names_fmt}."
            )
        lines.insert(1, "")
        lines.insert(2, verdict)
        # §17.317 — recovery footer only when something's down. All-up
        # path stays clean.
        if down_names:
            lines.append(self._render_health_recovery_footer())
        return "\n".join(lines)

    @staticmethod
    def _render_health_recovery_footer() -> str:
        """§17.317 — recovery footer for /health when subsystems are
        down. Generic across deployments (no hardcoded service names
        beyond `scaffold-orchestrator`, which is always known)."""
        return (
            "\n\n---\n\n"
            "💡 **Recovery:**\n"
            "- Inspect: `docker compose ps` "
            "(verify each subsystem's container is running)\n"
            "- Restart a service: "
            "`docker compose restart <service>` "
            "(use the name from `ps`)\n"
            "- Logs: `docker compose logs --tail=50 <service>`\n"
            "- Retry: `/health` once the container is healthy "
            "(milvus boots slowly — give it ~30 s)"
        )

    def _render_health_unreachable(self, reason: str) -> str:
        """§17.317 — friendly recovery shape when the orchestrator's
        /health endpoint itself is unreachable. Mirror of §17.302's
        connection-error pattern but with diagnostic-specific
        commands."""
        url = self.valves.orchestrator_url
        return (
            f"⚠️ Cannot reach orchestrator `/health` at `{url}` "
            f"({reason}).\n\n"
            f"💡 **Recovery:**\n"
            f"- Verify: `docker compose ps` (is `scaffold-orchestrator` "
            f"running?)\n"
            f"- Restart: `docker compose restart scaffold-orchestrator`\n"
            f"- Logs: `docker compose logs --tail=50 scaffold-orchestrator`\n"
            f"- Retry: `/health` once the container is healthy."
        )

    def _handle_cost(
        self, parts: list, *, chat_id: str | None = None,
    ) -> str:
        """J.3.c — render the per-job cost rollup from /jobs/{id}/costs.

        Shows totals + a per-(provider, model) breakdown table. Falls
        through to a friendly "no calls logged yet" message when the
        job has no telemetry (call_count == 0), which is also the
        zero-shape J.3.b returns for jobs that ran before the
        migration was applied.

        §17.307 — when no explicit id, try active-job memory before
        falling back to Usage error.
        """
        if len(parts) < 2:
            recalled = self._active_job_recall(chat_id)
            if recalled and recalled.get("job_id"):
                rid = recalled["job_id"]
                hint = self._active_job_hint(rid, recalled.get("title"))
                return hint + self._handle_cost([parts[0], rid])
            return "Usage: `/cost <job_id>`"
        if _is_placeholder(parts[1]):
            return "It looks like job_id is missing or a placeholder. Try `/cost 01ab243e`."
        job_id = parts[1]
        r = _HTTP_SESSION.get(
            f"{self.valves.orchestrator_url}/jobs/{job_id}/costs",
            headers=self._auth_headers(),
            timeout=self.valves.request_timeout,
        )
        if r.status_code >= 400:
            return self._fmt(r)
        try:  # §17.611 (audit #22) — parse once (requests doesn't cache .json())
            data = r.json()
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        cost = float(data.get("total_cost_usd") or 0.0)
        calls = int(data.get("call_count") or 0)
        prompt_tokens = int(data.get("total_prompt_tokens") or 0)
        completion_tokens = int(data.get("total_completion_tokens") or 0)
        latency_ms = int(data.get("total_latency_ms") or 0)
        breakdown = data.get("by_provider") or []
        # §17.289 — `data_source` was added in §17.284 so callers can
        # distinguish "no calls yet" (data_source="ok") from "the rollup
        # query failed and these zeros are a fallback" (data_source=
        # "error"). UX-3 surfaces the flag here so a zero-cost rollup on
        # a busy job is no longer indistinguishable from a green run.
        data_source = data.get("data_source", "ok")

        header = (
            f"## 💰 Cost — `{job_id[:8]}`  "
            f"${cost:.4f}  ({calls} call{'s' if calls != 1 else ''})"
        )
        # §17.289 — error-source warning prepended to BOTH the zero-calls
        # branch and the populated-data branch. The numbers below may
        # be a fail-open fallback; tell the operator that explicitly.
        error_banner = (
            "\n\n⚠️ **Telemetry query failed** — figures may be stale or "
            f"incomplete. Re-run `/cost {job_id}` or check orchestrator logs."
            if data_source == "error" else ""
        )
        if calls == 0:
            zero_reason = (
                "_(no LLM calls logged for this job yet — either it hasn't "
                "run, or it was created before telemetry was enabled)_"
            )
            return header + error_banner + "\n\n" + zero_reason

        latency_s = latency_ms / 1000.0
        lines = [
            header + error_banner, "",
            f"**Tokens:** prompt={prompt_tokens:,} · completion={completion_tokens:,}",
            f"**Latency:** {latency_ms:,} ms ({latency_s:.1f} s total LLM time)",
        ]
        if breakdown:
            lines.append("")
            lines.append("| Provider | Model | Calls | Cost | Latency |")
            lines.append("|---|---|---:|---:|---:|")
            for row in breakdown:
                provider = str(row.get("provider", ""))[:20]
                model = str(row.get("model", ""))[:30]
                row_calls = int(row.get("calls") or 0)
                row_cost = float(row.get("cost_usd") or 0.0)
                row_latency = int(row.get("latency_ms") or 0)
                lines.append(
                    f"| {provider} | `{model}` | {row_calls} | "
                    f"${row_cost:.4f} | {row_latency:,} ms |"
                )
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Help
    # ------------------------------------------------------------------

    def _help(self) -> str:
        # §17.562 — guided/minimal help. When advanced mode is off (default),
        # show only the small core surface; the full ~50-command reference is
        # one `/advanced on` away.
        if not self.valves.advanced_commands_enabled:
            return self._help_core()
        return self._help_full()

    @staticmethod
    def _help_core() -> str:
        return """**Scaffold Engine — quick start**

Describe what you want to build, then **`/go`**. That's the whole loop.

**The core commands**
| Command | What it does |
|---|---|
| *(just chat)* | Describe an idea; the engine asks questions until you `/go`. |
| `/go` | Turn the conversation into a plan and launch. |
| `/idea <text>` | Skip the chat — send an idea straight in. |
| `/confirm <job_id>` | Approve a refined idea → research + plan. |
| `/here` | **Where am I?** Everything in progress + the next step for each. |
| `/next` | The single next thing to do. |
| `/resume` | Jump back into whatever you were doing — no IDs needed. |
| `/results` | See the output (or progress) of your current job. |
| `/assist <job_id>` | Walk through a hands-on plan step-by-step, guided. |
| `/cancel` | Stop the current job. |

After a plan is ready you'll be asked to pick **autonomous** (`/execute`) or
**assisted** (`/assist`) — with a recommendation.

_Lost? Just type `/here`. Want the full ~50-command surface? `/advanced on`._"""

    @staticmethod
    def _help_full() -> str:
        return """**Scaffold Engine — full command reference**

**Canonical flow:** chat naturally to scope an idea → `/go` to launch → review the plan → `/confirm <job_id>` to execute.

**Try one of these to start:**
- `/idea Build a CLI that converts screenshots to PDF` — jump straight to Phase 1
- `/research kubernetes best practices` — autonomous web research + ingest
- `/jobs` — see what's already running

---

**🗣 Scope & kickoff**
| Command | What it does |
|---|---|
| *(plain message)* | Chat to scope an idea. Triage asks clarifying questions until you `/go`. |
| `/go` or `/run` | Synthesize chat → Phase 1, pause at the confirmation gate. |
| `/idea <text>` | Skip triage; send an idea directly to Phase 1. |
| `/confirm <job_id> [feedback]` | Approve a refined idea — auto-chains research → DAG → execute. |

**⚙ Workflow control**
| Command | What it does |
|---|---|
| `/execute <job_id>` | Run all pending DAG nodes (resume after cancel or stall). |
| `/skip <job_id> [<node_key>]` | Skip a node; bare `/skip <job_id>` lists candidates. |
| `/node <sub> <job_id> <node_key>` | Edit a node: `reset` (re-run + downstream), `del`, `edit`, `reorder`. `/node help`. |
| `/cancel <job_id>` | Cancel a running or queued job. |
| `/assist <sub>` | Human-in-the-loop step-through of a job's DAG. `/assist help` for the full session flow. |
| `/results <job_id>` | View output, in-flight progress, or failure detail + recovery hints. |
| `/status` | List active jobs grouped by state. |

**📚 Knowledge base**
| Command | What it does |
|---|---|
| `/rag <query>` | Search the Milvus knowledge base. |
| `/research <topic>` | Autonomous web research → distill → ingest. |
| `/research <url>` | Ingest a single web page. |
| `/research github:<owner>/<repo>` | Ingest a repo's README + docs + docstrings. |
| `/research openapi:<url>` | Ingest an OpenAPI spec, one entry per endpoint. |
| `/research/reply <session_id> <msg>` | Resume a paused research session. |
| `/research/pdf` | Upload a PDF — drag-drop at `GET /research/pdf` or `curl -F file=@x.pdf`. |

**🗂 Manage saved work**
| Command | What it does |
|---|---|
| `/jobs <sub>` | list / find / rename / delete jobs. `/jobs help` for details. |
| `/research/<sub>` | list / find / rename / delete research sessions. `/research/help`. |
| `/schedule <sub>` | Recurring research crons. `/schedule help`. |

**🔧 Configuration & utilities**
| Command | What it does |
|---|---|
| `/model <sub>` | Models per role — list / available / set / reset / probe. |
| `/optimize <prompt>` | Tighten and improve a prompt. |
| `/config [substring] [--non-defaults]` | List settings with values + redaction. |
| `/help` | Show this message. |

**🩺 Diagnostics & admin**
| Command | What it does |
|---|---|
| `/health` | Probe Postgres + Ollama + Milvus + Redis + sidecars. |
| `/logs <job_id>` | Per-node DAG state with output preview. |
| `/exec retry <job_id> <node_key>` | Retry a failed/blocked node. |
| `/cleanup` | Sweep stale jobs (reset orphans, cancel long-idle). |
| `/cost <job_id>` | Cost + latency rollup — totals + per-(provider, model). |

---

**Common scenarios**

- **Launch from a conversation** — describe what you want for a few turns, then `/go`. The triage LLM asks clarifying questions; when you `/go`, it synthesizes your turns into a brief and Phase 1 runs.
- **One-shot launch** — `/idea <your idea>` — skips triage entirely. Phase 1 pauses at the confirmation gate; you `/confirm <id>` to proceed.
- **Check what a `/research` run ingested** — research feeds the *global* knowledge base, not a specific job, so `/dag` and `/results` won't show it. Use `/rag <query>` (searches all domains) to retrieve the content, or `/research/list` to see each session's ingest count.
- **Recover from a failed node** — `/results <job_id>` shows what broke; `/exec retry <job_id> <node_key>` re-runs just that node and re-flows downstream.
- **Inspect cost mid-flight** — `/cost <job_id>` shows LLM spend so far. `/jobs` lists every active job; `/jobs find <text>` filters by title.

---

*Native web UI: `http://<host>:8000/web/jobs`. Full reference: README.md + USER_GUIDE.md in the repo.*"""
