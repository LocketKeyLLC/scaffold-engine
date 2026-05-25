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
)

KNOWN_SUBCOMMANDS: dict = {
    "/model": ("list", "available", "set", "reset", "probe", "test", "help"),
    "/jobs": ("list", "find", "rename", "delete", "help"),
    # U.8.D — `run-now` was advertised here but never had an orchestrator
    # endpoint or a chat handler. Removed; see audit follow-ups.
    "/schedule": ("list", "add", "delete", "help"),
    "/assist": ("next", "submit", "skip", "handoff", "pause", "resume",
                "done", "friction", "help"),
    "/exec": ("retry", "help"),
}

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
these exact headers — including when you are elaborating, giving
examples, or answering a follow-up. Do not drop "My pick" under any
circumstance unless the scope is locked and you are emitting the
final summary.

**Scope so far:**
One line summarizing what is clear about the build. If nothing is clear
yet, write "Not enough yet — see Gaps below."

**Options:**
When there is a real choice (architecture, technology, approach), list
2–3 options with a one-line tradeoff each. If scope is too vague for
options yet, write "Define WHAT first — see Gaps." If the direction is
genuinely settled, write "None — direction is settled" and skip to Gaps.

**Gaps:**
Always shown. List every detail still missing from these four buckets:
- WHAT specifically is being built
- HARDWARE / infrastructure (OS, CPU, RAM, storage, network)
- SUCCESS criteria (what "done" looks like)
- CONSTRAINTS (budget, timeline, equipment, skill)
If a bucket is fully covered, mark it "✓ covered" on its own line.

**My pick:**
Recommend ONE concrete default for the most important open decision.
State why in one sentence. End with: "Say so or override."

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
- SUCCESS criteria: needs one detail — should the PDF preserve the original screenshots, or text only?
- CONSTRAINTS: ✓ covered

**My pick:**
Image-with-OCR-layer — preserves what you screenshotted while staying searchable. Say so or override.


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
- No headers other than the four required ones (Scope so far / Options / Gaps / My pick).
- Plain bullets only. Bold only inside the four required headers.
- One topic per response — pick the most important gap to push on.
- Do not invent requirements the user has not agreed to.
- Do not execute anything. Do not write code. Do not propose scripts.
- Do not ask "should I write the script" or offer deliverables — that is the pipeline's job after /go.

When AND ONLY WHEN all four Gaps buckets read "✓ covered" with nothing
else open, replace the four sections with a 2-4 sentence scope summary
and write: "Type `/go` when you're ready to launch."
Until that condition is met, keep emitting all four sections every turn —
even if the user has answered everything in their last message. The user
decides when scope is locked, not you."""


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

        # §17.300 — first-turn welcome preamble. When a brand-new chat
        # receives a natural-language message, the pipeline prepends a
        # small "here's how this works" block ahead of the triage
        # response so first-touch operators see the canonical flow
        # without typing `/help`. Slash commands skip the preamble
        # (operators using commands already know the surface). One
        # preamble per chat — subsequent turns are unaffected.
        show_welcome_on_first_turn: bool = True

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

        # Model overrides
        model_general: str = "qwen3-vl:235b-instruct-cloud"
        model_verifier: str = "qwen2.5:7b"
        model_coder: str = "qwen2.5-coder:7b"
        model_embedder: str = "qwen3-embedding:8b"
        model_reranker: str = "tomaarsen/Qwen3-Reranker-0.6B-seq-cls"
        model_router: str = "qwen3:4b"
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
        "- `/research kubernetes best practices` — autonomous web "
        "research + ingest\n"
        "- `/jobs` — see what's already running\n"
        "- `/help` — full command surface (22 commands)\n\n"
        "---\n\n"
    )

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
                return r.json()["choices"][0]["message"]["content"]
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
                cleaned = re.sub(
                    r"<think(?:ing)?>.*?</think(?:ing)?>",
                    "", raw, flags=re.DOTALL,
                ).strip()
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
            yield from self._handle_execute(msg); return
        if self._is_cmd(msg, "/confirm"):
            yield from self._handle_confirm(msg, body=body); return

        if msg.startswith("/"):
            # §17.307 — extract chat_id for active-job memory. Same
            # source as the /assist chatmap path.
            result = self._handle_command(
                msg, chat_id=self._chat_id_from_body(body),
            )
            if result:
                yield result
            return

        # §17.300 — first-touch welcome preamble. Natural-language input
        # AND brand-new chat AND valve enabled → prepend the canonical
        # flow + jump-in commands so the operator sees the surface
        # without typing `/help` first. Triage still runs and answers
        # their actual question; the preamble is additive.
        if (
            self.valves.show_welcome_on_first_turn
            and self._is_first_turn(messages)
        ):
            yield self._WELCOME_PREAMBLE

        yield self._call_triage(messages)

    # ------------------------------------------------------------------
    # Generator command handlers
    # ------------------------------------------------------------------

    def _handle_go(self, msg: str, messages: List[dict]) -> Generator[str, None, None]:
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

        # Per-command help (#5).
        if raw_args.strip() in ("--help", "-h", "help"):
            yield parser.help_text() + "\n\nManage sessions: `/research/help`"
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

    def _handle_execute(self, msg: str) -> Generator[str, None, None]:
        parts = msg.split()
        if len(parts) < 2:
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

        yield f"📋 Execution plan ready — running {num_nodes} steps...\n\n"
        yield from self._execute_and_stream(job_id, num_nodes)

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
        "| `/assist next [<session_id>]` | Fetch the next pending step. |\n"
        "| `` /assist submit [<session_id>] [<node_key>]\\n```evidence``` `` | Submit human evidence. Both args optional after `/assist next`. |\n"
        "| `/assist skip [<session_id>] [<node_key>]` | Skip a node. |\n"
        "| `/assist handoff [<session_id>] <node_key> [single\\|all]` | Hand a node back to autonomous executor. |\n"
        "| `/assist pause [<session_id>]` | Pause; resume later. |\n"
        "| `/assist resume [<session_id>]` | Resume a paused session. |\n"
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

    def _active_job_recall(self, chat_id: str | None) -> dict | None:
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

    def _auto_chain(self, message: str) -> Generator[str, None, None]:
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
            yield "\nI had trouble with that request. Could you rephrase it?"
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
            yield "\n\nI wasn't able to plan that. Please rephrase or simplify."
            yield (
                f"\n\nResearch finished — only the plan step failed. "
                f"Retry with `/dag {job_id}` or check status with `/jobs`.\n"
            )
            return
        try:
            dag_data = r.json()
            num_nodes = dag_data.get("task_count", len(dag_data.get("tasks", [])))
        except (ValueError, KeyError):
            yield "\n\n⚠️ Unexpected response from DAG generation."
            return

        yield f"\nReady — executing {num_nodes} steps...\n\n"
        yield from self._execute_and_stream(job_id, num_nodes)

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
        # ReadTimeout and we emit a heartbeat. Tying it to ``keep`` keeps
        # the heartbeat cadence honest: each ReadTimeout cycle covers
        # exactly ``keep`` wall-clock seconds, so ``idle_seconds += keep``
        # below counts real elapsed time. Lower bound 30s to avoid
        # thrashing on tiny keep values.
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
                    idle_seconds += keep
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
                    yield f"\n⚠️ **Stream stalled** — no data in {payload.get('idle_seconds','?')}s. Closing.\n"
                    return
                elif event_type == "error":
                    yield from self._render_error_event(payload); return
                elif event_type == "research_started":
                    yield f"📊 Depth: {payload.get('depth','?')} | Max iterations: {payload.get('max_iterations','?')}\n\n"
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
                    yield "- `/go` to build a project plan from this research\n"
                    yield "- `/research <subtopic> --depth deep` to explore further\n"
                    yield "- `/rag <query>` to query what was ingested\n"

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
                        yield "That question is already being processed. Please wait."
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
        lines: list[str] = ["\n\n---\n\n**Next steps:**"]
        # `/exec retry` rows first when there are failures — operator
        # action is highest-leverage on those.
        if failed_nodes:
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
        lines.append(f"- `/cost {job_id}` — see total LLM cost + latency rollup")
        if not failed_nodes:
            # Only suggest the friction commands when there's nothing
            # broken to fix first — operator decision tree is "fix vs
            # tune", not all four at once.
            lines.append(
                f"- `/jobs rename {job_id} <new title>` — set a memorable "
                f"title for later lookup"
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
                if len(parts) < 2:
                    return "Usage: /skip <job_id> <node_key>"
                if _is_placeholder(parts[1]):
                    return "It looks like job_id or node_key is missing or a placeholder. Try `/skip 01ab243e T2`."
                # §17.215 E1 — bare `/skip <job_id>` now lists candidate nodes
                # (failed / blocked / pending) with copy-pasteable /skip lines,
                # instead of erroring out. Mirrors the _render_next_actions
                # affordance (§17.195). If a node_key is supplied, behave as
                # before.
                if len(parts) < 3:
                    return self._render_skip_candidates(parts[1])
                if _is_placeholder(parts[2]):
                    return "It looks like job_id or node_key is missing or a placeholder. Try `/skip 01ab243e T2`."
                r = _HTTP_SESSION.post(
                    f"{self.valves.orchestrator_url}/skip",
                    json={"job_id": parts[1], "node_key": parts[2]},
                    headers=self._auth_headers(),
                    timeout=self.valves.request_timeout,
                )
                return self._fmt(r)
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
                return self._render_status(r.json())

            # ----- U.8.D — diagnostics + admin parity -------------------
            if cmd == "/exec":
                return self._handle_exec(parts)
            if cmd == "/cleanup":
                return self._handle_cleanup()
            if cmd == "/config":
                return self._handle_config(parts)
            if cmd == "/logs":
                return self._handle_logs(parts)
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
    # /status renderer
    # ------------------------------------------------------------------
    def _render_status(self, data: dict) -> str:
        counts = data.get("status_counts") or {}
        total = data.get("total_jobs", 0)
        recent = data.get("recent_jobs") or []

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

        if recent:
            icon = {
                "completed": "✅", "failed": "❌", "cancelled": "🚫",
                "blocked": "⛔", "awaiting_confirmation": "⏸️",
                "executing": "⏳", "running": "⏳", "planning": "🧠",
                "researching": "🔍", "refining": "✏️", "pending": "⏳",
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
                title = (j.get("title") or "")[:60]
                nc = j.get("node_count", 0)
                upd = (j.get("updated_at") or "")[:16].replace("T", " ")
                lines.append(f"| {icon.get(st, '')} {st} | `{short}` | {title} | {nc} | {upd} |")

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
        return "\n".join(lines)


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
        # §17.309 — next-actions hint footer. Three copy-pasteable
        # commands an operator typically wants after scanning the list.
        # Mirror the Next-block shape from §17.303 / §17.305.
        footer = (
            "\n\n---\n\n"
            "💡 **Next:**\n"
            "- `/results <id>` — view output / progress / failure detail\n"
            "- `/cost <id>` — cost + latency rollup\n"
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
            # Empty hit list: explicit message rather than empty JSON.
            return f"No matches for `{query}`."

        # All results need the dict shape; otherwise fall back to raw
        # JSON to avoid masking server-side changes.
        if not all(isinstance(rr, dict) for rr in results):
            return self._fmt(r)

        lines = [f"**RAG results for `{query}`** ({len(results)} hit(s)):\n"]
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
                meta_parts.append(f"confidence={confidence:.2f}")
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

        if status in ("completed", "done"):
            compiled = data.get("compiled_output", "")
            if compiled:
                return compiled
            return f"✅ Job `{job_id}` completed, but no compiled output is available."

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
            return (
                "**Schedule commands:**\n"
                "- `/schedule list` — show all schedules\n"
                "- `/schedule add <cron> [--depth=<shallow|medium|deep>] <topic>`\n"
                "  Example: `/schedule add \"0 9 * * 1\" --depth=medium kubernetes news`\n"
                "- `/schedule delete <id>`\n\n"
                "Cron format: `minute hour day month weekday` (UTC)\n"
                "Depth defaults to `medium`."
            )

        if sub == "list":
            try:
                r = _HTTP_SESSION.get(f"{base}/schedule", headers=hdr,
                                 timeout=self.valves.request_timeout)
                r.raise_for_status()
                rows = r.json().get("schedules", [])
            except Exception as exc:
                return f"❌ Failed to list schedules: {exc}"
            if not rows:
                return "No schedules yet. Try `/schedule add \"0 9 * * 1\" kubernetes news`"
            lines = ["| ID | Topic | Cron | Depth | Next Run | Runs | Failures |",
                     "|----|-------|------|-------|----------|------|----------|"]
            for s in rows:
                lines.append(
                    f"| {s['id']} | {s['topic']} | `{s['cron_expression']}` | "
                    f"{s['depth']} | {s.get('next_run_at') or '—'} | "
                    f"{s['run_count']} | {s['failure_count']} |"
                )
            return "\n".join(lines)

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

    def _handle_exec(self, parts: list) -> str:
        # _handle_command splits with maxsplit=2 so parts[2] (if present) is
        # the post-subcommand tail as one string. Re-split it here.
        if len(parts) < 2 or parts[1] == "help":
            return ("Usage: `/exec retry <job_id> <node_key>`\n\n"
                    "Resets a failed/blocked node to `pending` so it can run again.\n"
                    "Run `/results <job_id>` for a failure report with prefilled args.")
        sub = parts[1].lower()
        tail = parts[2].split() if len(parts) > 2 else []
        if sub == "retry":
            if len(tail) < 2:
                return "Usage: `/exec retry <job_id> <node_key>`"
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

    def _handle_logs(self, parts: list) -> str:
        if len(parts) < 2:
            return "Usage: `/logs <job_id>`"
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
        data = r.json() if isinstance(r.json(), dict) else {}
        nodes = data.get("nodes") or []
        total = data.get("node_count", len(nodes))
        job_status = data.get("job_status", "?")
        header = (f"## 🪵 Logs — `{job_id[:8]}`  status: `{job_status}`  "
                  f"({len(nodes)}/{total} nodes)")
        if not nodes:
            return header + "\n\n_(no DAG nodes — job may not have been planned yet)_"
        lines = [header, "", "| Key | Status | Conf | Tool | Output preview |",
                 "|---|---|---:|---|---|"]
        for n in nodes:
            key = str(n.get("node_key", ""))[:10]
            st = str(n.get("status", ""))[:10]
            conf = n.get("confidence")
            conf_s = f"{conf:.2f}" if isinstance(conf, (int, float)) else "—"
            tool = str(n.get("tool", ""))[:10]
            preview = (n.get("output_text") or "").replace("\n", " ").replace("|", "\\|")[:60]
            lines.append(f"| `{key}` | {st} | {conf_s} | {tool} | {preview} |")
        return "\n".join(lines)

    def _handle_health(self) -> str:
        r = _HTTP_SESSION.get(
            f"{self.valves.orchestrator_url}/health",
            headers=self._auth_headers(),
            timeout=self.valves.request_timeout,
        )
        if r.status_code >= 400:
            return self._fmt(r)
        data = r.json() if isinstance(r.json(), dict) else {}
        checks = data.get("checks", {})
        if not checks:
            return f"```json\n{json.dumps(data, indent=2)}\n```"
        UP = {"up", "ok", "healthy", "true"}
        DOWN = {"down", "fail", "error", "unhealthy"}
        lines = ["## 🩺 Health", "", "| Subsystem | Status | Latency |",
                 "|---|---|---:|"]
        for name, info in checks.items():
            if isinstance(info, dict):
                status = str(info.get("status", "?"))
                latency = info.get("latency_ms")
            else:
                status = str(info)
                latency = None
            icon = "✅" if status.lower() in UP else ("❌" if status.lower() in DOWN else "ℹ️")
            lat = f"{latency} ms" if latency is not None else "—"
            lines.append(f"| {name} | {icon} {status} | {lat} |")
        return "\n".join(lines)

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
        data = r.json() if isinstance(r.json(), dict) else {}
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
- **Recover from a failed node** — `/results <job_id>` shows what broke; `/exec retry <job_id> <node_key>` re-runs just that node and re-flows downstream.
- **Inspect cost mid-flight** — `/cost <job_id>` shows LLM spend so far. `/jobs` lists every active job; `/jobs find <text>` filters by title.

---

*Native web UI: `http://<host>:8000/web/jobs`. Full reference: README.md + USER_GUIDE.md in the repo.*"""
