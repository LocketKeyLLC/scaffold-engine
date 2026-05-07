"""
scaffold_router.py — Open WebUI pipeline for Scaffold Engine.

Commands: see _help() for the full list.

Three timeout valves (consolidated from six hardcoded values):
  - request_timeout  (default 30s)    — quick JSON endpoints
  - stream_timeout   (default 3600s)  — SSE + long-poll LLM endpoints
  - triage_timeout   (default 3600s)  — direct Ollama calls for triage/synthesis

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
)

KNOWN_SUBCOMMANDS: dict = {
    "/model": ("list", "available", "set", "reset", "probe", "test", "help"),
    "/jobs": ("list", "find", "rename", "delete", "help"),
    "/schedule": ("list", "add", "delete", "run-now", "help"),
    "/assist": ("next", "submit", "skip", "handoff", "pause", "resume",
                "done", "friction", "help"),
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


# ─── SHARED: status icons — keep in sync across pipelines (#8.17) ───
# Pipelines load as isolated single-file modules; no shared imports possible.
# If you add/rename a status, update every pipeline file that has this block.
STATUS_ICONS = {
    "done":     "✅",
    "failed":   "❌",
    "running":  "🔄",
    "pending":  "⬜",
    "skipped":  "⏭️",
}
# ─── END SHARED ───

class Pipeline:
    class Valves(BaseModel):
        api_key: str = ""
        orchestrator_url: str = "http://scaffold-orchestrator:8000"

        # --- Consolidated timeouts (#8.8) ---
        request_timeout: int = 30     # quick JSON endpoints
        stream_timeout: int = 3600    # SSE + long-poll LLM endpoints
        triage_timeout: int = 3600    # direct Ollama calls
        # Legacy alias (migrated to stream_timeout on init if non-default)
        dag_timeout: int = 3600

        # SSE cadence & per-read timeout & stall threshold multiplier source
        keepalive_interval: int = 10

        # Triage
        triage_model: str = "qwen3:4b"
        triage_history_window: int = 8  # last N turns sent to triage; first user msg always pinned
        log_pipe_inputs: bool = False  # diagnostic: log body keys + message shape on every pipe() call
        ollama_url: str = "http://172.18.0.1:11434"

        # ── Assistant Mode ─────────────────────────────────────────────
        # When true, /confirm routes the job into Assist Mode (interactive
        # walk-through) instead of /execute/all (autonomous). Default off
        # to preserve existing UX.
        assist_after_confirm: bool = False
        assist_default_handoff_policy: str = "manual"           # manual | auto_on_skip | auto_all_remaining
        assist_default_replan_policy: str = "context_only"      # context_only | selective | full | disabled
        assist_max_evidence_chars: int = 200_000

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

    def _synthesize_idea(self, messages: List[dict]) -> str:
        clean = self._clean_messages(messages)
        if not any(m["role"] == "user" for m in clean):
            return ""

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
                    return cleaned
                self.logger.info("Synthesis cleaned to empty, using fallback")
            else:
                self.logger.error("Synthesis HTTP %s: %s", r.status_code, r.text[:300])
        except Exception as e:
            self.logger.error("Synthesis error: %s", e)

        user_texts = [m["content"] for m in clean if m["role"] == "user"]
        fallback = " ".join(user_texts)
        self.logger.info("Synthesis fallback (%d chars): %s", len(fallback), fallback[:200])
        return fallback

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
        for closing_tag in ("</context>", "</documents>", "</source>"):
            if closing_tag in msg:
                msg = msg.rsplit(closing_tag, 1)[-1].strip()
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
            yield from self._handle_assist(msg); return
        if self._is_cmd(msg, "/execute"):
            yield from self._handle_execute(msg); return
        if self._is_cmd(msg, "/confirm"):
            yield from self._handle_confirm(msg); return

        if msg.startswith("/"):
            result = self._handle_command(msg)
            if result:
                yield result
            return

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
        synthesized = self._synthesize_idea(chat_history)

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

        yield f"🔬 Researching: **{topic}** (depth: {args.depth})\n\n"
        yield from self._research_and_stream(topic, args.depth)

    def _handle_execute(self, msg: str) -> Generator[str, None, None]:
        parts = msg.split()
        if len(parts) < 2:
            yield "Usage: `/execute <job_id>`"
            return
        job_id = parts[1]
        yield f"Executing all nodes for job `{job_id}`...\n\n"
        yield from self._execute_and_stream(job_id, 0)

    def _handle_confirm(self, msg: str) -> Generator[str, None, None]:
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

        yield "🔬 Starting research and knowledge ingestion — this may take several minutes on CPU...\n\n"

        ok, res = yield from self._post_with_keepalive(
            f"{self.valves.orchestrator_url}/ideate/confirm",
            payload, self.valves.stream_timeout,
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
        if self.valves.assist_after_confirm:
            yield f"📋 Execution plan ready — entering Assist Mode for {num_nodes} steps...\n\n"
            yield from self._assist_start(job_id)
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
        "| Command | Description |\n"
        "|---|---|\n"
        "| `/assist <job_id>` | Start a session and render the first step. |\n"
        "| `/assist next <session_id>` | Fetch the next pending step. |\n"
        "| `` /assist submit <session_id> <node_key>\\n```evidence``` `` | Submit human evidence (multi-line via triple-backtick fence). |\n"
        "| `/assist skip <session_id> <node_key>` | Skip a node. |\n"
        "| `/assist handoff <session_id> <node_key> [single\\|all]` | Hand a node back to autonomous executor. |\n"
        "| `/assist pause <session_id>` | Pause; resume later. |\n"
        "| `/assist resume <session_id>` | Resume a paused session. |\n"
        "| `/assist done <session_id>` | Show the compiled output. |\n"
        "| `/assist friction <session_id> <node_key> <note>` | Log a friction note for post-mortem. |\n"
        "| `/assist help` | Show this message. |\n\n"
        "_Tip: paste multi-line evidence inside a triple-backtick fence; it will be captured intact._"
    )

    @staticmethod
    def _extract_fenced(msg: str) -> tuple[str, str]:
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

    def _render_step(self, step: dict) -> str:
        """Format a /assist/next response as markdown chat output."""
        if step.get("status") in ("completed", "abandoned", "cancelled"):
            return f"✅ **Session `{step['session_id']}` is {step['status']}.** Run `/assist done {step['session_id']}` to view the compiled output."
        if not step.get("node_key"):
            counts = step.get("step_counts", {})
            counts_str = ", ".join(f"{k}={v}" for k, v in counts.items()) or "n/a"
            return (
                f"⏳ **No claimable step right now.**\n\n"
                f"Step roll-up: {counts_str}\n\n"
                f"Some steps may already be presented to you and waiting on submit. "
                f"Use `/assist next {step['session_id']}` again after you submit."
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
            f"**Task prompt:**\n\n```\n{step.get('base_prompt', '')}\n```\n\n"
            f"**When done, submit your evidence:**\n"
            f"````\n"
            f"/assist submit {step['session_id']} {step['node_key']}\n"
            f"```\n"
            f"<your output here — command output, file diff, summary, anything>\n"
            f"```\n"
            f"````\n"
        )

    def _handle_assist(self, msg: str) -> Generator[str, None, None]:
        """Dispatch /assist subcommands. Stateless — session_id is echoed
        back to the user and accepted as the first arg of each follow-up,
        same pattern as `/research/reply <session_id>`."""
        head, fenced = self._extract_fenced(msg)
        parts = head.split(None, 4)
        cmd = parts[0] if parts else "/assist"

        # /assist help
        if cmd == "/assist/help" or (cmd == "/assist" and len(parts) > 1 and parts[1] == "help"):
            yield self._ASSIST_HELP; return

        # /assist <job_id> — start
        if cmd == "/assist":
            if len(parts) < 2:
                yield self._ASSIST_HELP; return
            arg1 = parts[1]
            # /assist <subcommand> ... — route to subcommand handler
            if arg1 in ("next", "submit", "skip", "handoff", "pause", "resume", "done", "friction"):
                # Re-prepend the subcommand and route via the slash-form below.
                yield from self._dispatch_assist_sub(arg1, parts[2:], fenced); return
            # Otherwise treat arg1 as job_id
            job_id = arg1
            yield from self._assist_start(job_id); return

        # Slash-form subcommands: /assist/next, /assist/submit, etc.
        if cmd.startswith("/assist/"):
            sub = cmd.split("/", 2)[2]  # "next" / "submit" / ...
            yield from self._dispatch_assist_sub(sub, parts[1:], fenced); return

        yield self._ASSIST_HELP

    def _dispatch_assist_sub(self, sub: str, args: list, fenced: str) -> Generator[str, None, None]:
        if sub == "next":
            if not args:
                yield "Usage: `/assist next <session_id>`"; return
            yield from self._assist_next(args[0]); return
        if sub == "submit":
            if len(args) < 2:
                yield "Usage: `/assist submit <session_id> <node_key>` followed by triple-backtick fenced evidence."; return
            yield from self._assist_submit(args[0], args[1], fenced or (" ".join(args[2:]) if len(args) > 2 else "")); return
        if sub == "skip":
            if len(args) < 2:
                yield "Usage: `/assist skip <session_id> <node_key>`"; return
            yield from self._assist_skip(args[0], args[1]); return
        if sub == "handoff":
            if len(args) < 2:
                yield "Usage: `/assist handoff <session_id> <node_key> [single|all]`"; return
            mode = (args[2] if len(args) > 2 else "single").lower()
            mode = "all_remaining" if mode in ("all", "all_remaining") else "single"
            yield from self._assist_handoff(args[0], args[1], mode); return
        if sub == "pause":
            if not args:
                yield "Usage: `/assist pause <session_id>`"; return
            yield from self._assist_simple_post(args[0], "pause"); return
        if sub == "resume":
            if not args:
                yield "Usage: `/assist resume <session_id>`"; return
            yield from self._assist_simple_post(args[0], "resume"); return
        if sub == "done":
            if not args:
                yield "Usage: `/assist done <session_id>`"; return
            yield from self._assist_done(args[0]); return
        if sub == "friction":
            if len(args) < 3:
                yield "Usage: `/assist friction <session_id> <node_key> <note>`"; return
            yield from self._assist_friction(args[0], args[1], " ".join(args[2:])); return
        yield self._ASSIST_HELP

    def _assist_start(self, job_id: str) -> Generator[str, None, None]:
        try:
            r = _HTTP_SESSION.post(
                f"{self.valves.orchestrator_url}/assist/start",
                json={
                    "job_id": job_id,
                    "handoff_policy": self.valves.assist_default_handoff_policy,
                    "replan_policy": self.valves.assist_default_replan_policy,
                },
                headers=self._auth_headers(),
                timeout=self.valves.request_timeout,
            )
        except requests.exceptions.RequestException as e:
            yield f"❌ Connection error: {e}"; return
        if r.status_code >= 400:
            yield f"❌ Could not start assist session: HTTP {r.status_code} {r.text[:200]}"; return
        d = r.json()
        sid = d["session_id"]
        yield (
            f"🤝 **Assist session started** — `{sid}`\n\n"
            f"Job `{d['job_id']}` is now in `assisted_executing` ({d['pending_steps']} pending step(s)).\n\n"
            f"Fetching first step...\n\n---\n\n"
        )
        yield from self._assist_next(sid)

    def _assist_next(self, session_id: str) -> Generator[str, None, None]:
        try:
            r = _HTTP_SESSION.get(
                f"{self.valves.orchestrator_url}/assist/{session_id}/next",
                headers=self._auth_headers(),
                timeout=self.valves.request_timeout,
            )
        except requests.exceptions.RequestException as e:
            yield f"❌ Connection error: {e}"; return
        if r.status_code == 404:
            yield f"❌ Session `{session_id}` not found."; return
        if r.status_code >= 400:
            yield f"❌ HTTP {r.status_code}: {r.text[:200]}"; return
        yield self._render_step(r.json())

    def _assist_submit(self, session_id: str, node_key: str, evidence: str) -> Generator[str, None, None]:
        if not evidence:
            yield "Empty evidence. Wrap your output in a triple-backtick fence and resend."; return
        if len(evidence) > self.valves.assist_max_evidence_chars:
            yield (f"❌ Evidence is {len(evidence)} chars; cap is "
                   f"{self.valves.assist_max_evidence_chars}. Trim and resend."); return
        try:
            r = _HTTP_SESSION.post(
                f"{self.valves.orchestrator_url}/assist/{session_id}/submit",
                json={
                    "node_key": node_key,
                    "output": evidence,
                    "evidence_kind": "text",
                    "action": "submit",
                },
                headers=self._auth_headers(),
                timeout=self.valves.request_timeout,
            )
        except requests.exceptions.RequestException as e:
            yield f"❌ Connection error: {e}"; return
        if r.status_code >= 400:
            yield f"❌ HTTP {r.status_code}: {r.text[:200]}"; return
        d = r.json()
        if d.get("no_op"):
            yield f"ℹ️ Step `{node_key}` already `{d['status']}`. No change."; return
        next_nk = d.get("next_node_key")
        msg = f"✅ Step `{node_key}` committed. "
        if next_nk:
            msg += f"Next: `{next_nk}`. Run `/assist next {session_id}` to fetch."
        else:
            msg += f"All steps terminal — run `/assist done {session_id}` to view compiled output."
        yield msg

    def _assist_skip(self, session_id: str, node_key: str) -> Generator[str, None, None]:
        try:
            r = _HTTP_SESSION.post(
                f"{self.valves.orchestrator_url}/assist/{session_id}/submit",
                json={"node_key": node_key, "output": "", "action": "skip"},
                headers=self._auth_headers(),
                timeout=self.valves.request_timeout,
            )
        except requests.exceptions.RequestException as e:
            yield f"❌ Connection error: {e}"; return
        if r.status_code >= 400:
            yield f"❌ HTTP {r.status_code}: {r.text[:200]}"; return
        d = r.json()
        next_nk = d.get("next_node_key")
        msg = f"⏭ Step `{node_key}` skipped. "
        if next_nk:
            msg += f"Next: `{next_nk}`."
        else:
            msg += f"All steps terminal — run `/assist done {session_id}`."
        yield msg

    def _assist_handoff(self, session_id: str, node_key: str, mode: str) -> Generator[str, None, None]:
        # SSE stream — reuse existing _stream_sse_to_queue plumbing.
        yield f"🤖 Handing `{node_key}` back to autonomous executor (mode: `{mode}`)...\n\n"
        url = f"{self.valves.orchestrator_url}/assist/{session_id}/handoff"
        body = {"node_key": node_key, "mode": mode}
        # Reuse the generic streaming runner used by /research.
        yield from self._stream_sse_with_keepalive(url, body)

    def _stream_sse_with_keepalive(self, url: str, body: dict) -> Generator[str, None, None]:
        """Minimal SSE consumer for assist handoff. Mirrors the queue loop
        used in _handle_research but emits assist_* events plus the
        standard execution events from the underlying executor."""
        import queue as _q
        import threading as _th
        q: _q.Queue = _q.Queue()
        reader = _th.Thread(
            target=self._stream_sse_to_queue,
            args=(url, body, q), daemon=True,
        )
        reader.start()
        while True:
            try:
                msg_type, f1, f2 = q.get(timeout=self.valves.keepalive_interval)
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
            if event_type == "assist_handoff_started":
                yield f"\n🟢 Autonomous executor took over `{payload.get('node_key', '?')}`.\n"
            elif event_type == "assist_handoff_done":
                yield f"\n✅ Handoff complete. Run `/assist next {payload.get('session_id', '?')}` to continue.\n"
            elif event_type == "node_started":
                yield f"  ▶ {payload.get('node_key', '?')} — {payload.get('title', '?')}\n"
            elif event_type == "node_completed":
                yield f"  ✓ {payload.get('node_key', '?')} (model: {payload.get('model', '?')})\n"
            elif event_type == "node_failed":
                yield f"  ✗ {payload.get('node_key', '?')}: {payload.get('error', '?')}\n"
            elif event_type == "error":
                yield f"\n⚠️ {payload.get('detail') or payload}\n"; return

    def _assist_simple_post(self, session_id: str, action: str) -> Generator[str, None, None]:
        try:
            r = _HTTP_SESSION.post(
                f"{self.valves.orchestrator_url}/assist/{session_id}/{action}",
                headers=self._auth_headers(),
                timeout=self.valves.request_timeout,
            )
        except requests.exceptions.RequestException as e:
            yield f"❌ Connection error: {e}"; return
        if r.status_code >= 400:
            yield f"❌ HTTP {r.status_code}: {r.text[:200]}"; return
        d = r.json()
        yield f"✅ Session `{session_id}` -> `{d.get('status', action)}`."

    def _assist_done(self, session_id: str) -> Generator[str, None, None]:
        # Pull session, then job's compiled_output via /exec/status.
        try:
            r = _HTTP_SESSION.get(
                f"{self.valves.orchestrator_url}/assist/{session_id}",
                headers=self._auth_headers(),
                timeout=self.valves.request_timeout,
            )
        except requests.exceptions.RequestException as e:
            yield f"❌ Connection error: {e}"; return
        if r.status_code == 404:
            yield f"❌ Session `{session_id}` not found."; return
        if r.status_code >= 400:
            yield f"❌ HTTP {r.status_code}: {r.text[:200]}"; return
        sess = r.json()
        job_id = sess.get("job_id")
        try:
            r2 = _HTTP_SESSION.get(
                f"{self.valves.orchestrator_url}/exec/status/{job_id}",
                headers=self._auth_headers(),
                timeout=self.valves.request_timeout,
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

    def _assist_friction(self, session_id: str, node_key: str, note: str) -> Generator[str, None, None]:
        try:
            r = _HTTP_SESSION.post(
                f"{self.valves.orchestrator_url}/assist/{session_id}/friction",
                json={"node_key": node_key, "note": note},
                headers=self._auth_headers(),
                timeout=self.valves.request_timeout,
            )
        except requests.exceptions.RequestException as e:
            yield f"❌ Connection error: {e}"; return
        if r.status_code >= 400:
            yield f"❌ HTTP {r.status_code}: {r.text[:200]}"; return
        yield f"📝 Friction note recorded for `{node_key}` in session `{session_id}`."

    # ------------------------------------------------------------------
    # Long-poll with keepalive (DRY helper, #8.7)
    # ------------------------------------------------------------------

    def _post_with_keepalive(
        self, url: str, payload: dict, timeout: int,
    ):
        """Generator: yields '\\u200b' every keepalive_interval until POST returns.
        Terminates with `return (ok, response_or_exception)`."""
        result = [None]
        error = [None]

        def _call():
            try:
                result[0] = _HTTP_SESSION.post(
                    url, json=payload, headers=self._auth_headers(), timeout=timeout,
                )
            except Exception as e:
                error[0] = e

        t = threading.Thread(target=_call, daemon=True)
        t.start()
        while t.is_alive():
            time.sleep(self.valves.keepalive_interval)
            if t.is_alive():
                yield "\u200b"
        t.join()

        if error[0] is not None:
            return (False, error[0])
        if result[0] is None:
            return (False, "no response")
        return (True, result[0])

    # ------------------------------------------------------------------
    # /go auto-chain
    # ------------------------------------------------------------------

    def _auto_chain(self, message: str) -> Generator[str, None, None]:
        yield "Let me think about this"
        ok, res = yield from self._post_with_keepalive(
            f"{self.valves.orchestrator_url}/ideate",
            {"idea": message, "model_overrides": self._model_overrides()},
            self.valves.stream_timeout,
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
            yield "---\n\n**What would you like to do?**\n\n"
            yield f"- **Proceed as-is:** Type `/confirm {job_id}`\n"
            yield f"- **Proceed with changes:** `/confirm {job_id} <your adjustments>`\n"
            yield f"- **Start over:** Describe a new idea and type `/go` again\n"
            return

        yield "Planning my approach"
        ok, res = yield from self._post_with_keepalive(
            f"{self.valves.orchestrator_url}/dag",
            {"job_id": job_id, "model_overrides": self._model_overrides()},
            self.valves.stream_timeout,
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
        url = f"{self.valves.orchestrator_url}{url_path}"
        reader = threading.Thread(
            target=self._stream_sse_to_queue,
            args=(url, body, q), daemon=True,
        )
        reader.start()

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

        reader.join(timeout=5)

    def _execute_and_stream(
        self, job_id: str, total_nodes: int,
    ) -> Generator[str, None, None]:
        q = queue.Queue()
        url = f"{self.valves.orchestrator_url}/execute/all"
        body = {"job_id": job_id, "model_overrides": self._model_overrides()}
        reader = threading.Thread(
            target=self._stream_sse_to_queue,
            args=(url, body, q), daemon=True,
        )
        reader.start()

        failed_nodes = []
        compiled_output = None
        compile_status = None
        stalled = False

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
                yield f"⚠️ Execution failed (HTTP {f1}). Please try again."
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
                    else:
                        yield f"✅ All steps completed. Use `/results {job_id}` for details."
                else:
                    yield f"✅ All steps completed. Use `/results {job_id}` for details."
            except Exception:
                yield f"✅ All steps completed. Use `/results {job_id}` for details."

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
            yield f"⏸️ Step {payload.get('node_key','?')} blocked (waiting on: {', '.join(payload.get('blocked_by', []))})\n"
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

    def _handle_command(self, msg: str) -> str:
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
                return self._handle_results(parts)
            if cmd == "/jobs":
                return self._handle_jobs(msg)
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
                return self._fmt(r)
            if cmd == "/dag":
                if len(parts) < 2:
                    return "Usage: /dag <job_id>"
                r = _HTTP_SESSION.post(
                    f"{self.valves.orchestrator_url}/dag",
                    json={"job_id": parts[1], "model_overrides": self._model_overrides()},
                    headers=self._auth_headers(),
                    timeout=self.valves.stream_timeout,
                )
                return self._fmt(r)
            if cmd == "/skip":
                if len(parts) < 3:
                    return "Usage: /skip <job_id> <node_key>"
                if _is_placeholder(parts[1]) or _is_placeholder(parts[2]):
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
                return self._fmt(r)
            if cmd == "/status":
                r = _HTTP_SESSION.get(
                    f"{self.valves.orchestrator_url}/status",
                    headers=self._auth_headers(),
                    timeout=self.valves.request_timeout,
                )
                if r.status_code >= 400:
                    return self._fmt(r)
                return self._render_status(r.json())

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
            return f"⚠️ Cannot reach orchestrator at {self.valves.orchestrator_url}. Is it running?"
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

    def _format_job_row(self, j: dict) -> str:
        icon = {
            "completed": "✅", "failed": "❌", "cancelled": "🚫",
            "blocked": "⛔", "awaiting_confirmation": "⏸️",
            "executing": "⏳", "running": "⏳", "planning": "🧠",
            "researching": "🔍", "refining": "✏️", "pending": "⏳",
        }.get(j.get("status", ""), "")
        short = (j.get("id") or "")[:8]
        upd = (j.get("updated_at") or "")[:16].replace("T", " ")
        return (
            f"| {icon} {j.get('status','')} | `{short}` | {j.get('title','')[:60]} "
            f"| {j.get('node_count', 0)} | {upd} |"
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

    def _handle_jobs(self, msg: str) -> str:
        """Top-level /jobs command dispatcher."""
        self._ensure_pending_deletes()
        parts = msg.split(None, 3)
        sub = parts[1] if len(parts) > 1 else ""

        # /jobs (no args) -> list
        if not sub:
            return self._jobs_list_action(status=None, query=None)

        if sub == "help":
            return self._jobs_help()
        if sub in self._VALID_JOB_STATUSES:
            return self._jobs_list_action(status=sub, query=None)
        if sub == "find":
            if len(parts) < 3:
                return "Usage: `/jobs find <text>`"
            return self._jobs_list_action(status=None, query=" ".join(parts[2:]))
        if sub == "rename":
            if len(parts) < 4:
                return "Usage: `/jobs rename <job_id> <new title>`"
            return self._jobs_rename_action(parts[2], parts[3])
        if sub == "delete":
            if len(parts) < 3:
                return "Usage: `/jobs delete <job_id>`"
            job_id = parts[2]
            confirm = (len(parts) > 3 and parts[3].strip().lower() == "confirm")
            return self._jobs_delete_action(job_id, confirm)
        close = difflib.get_close_matches(sub, ("help", "find", "rename", "delete") + tuple(self._VALID_JOB_STATUSES), n=2, cutoff=0.6)
        hint = ""
        if close:
            hint = "\n\nClosest matches:\n" + "\n".join(f"  - `/jobs {c}`" for c in close)
        return f"Unknown subcommand: `/jobs {sub}`{hint}\n\n" + self._jobs_help()

    def _jobs_list_action(self, status, query) -> str:
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
        data = r.json()
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
        if not jobs:
            return header + "\n\n_No matching jobs._"
        rows = ["", "| Status | ID | Title | Nodes | Updated |", "|---|---|---|---:|---|"]
        rows.extend(self._format_job_row(j) for j in jobs)
        return "\n".join([header] + rows)

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
            return f"Job not found: `{job_id}`"
        if r.status_code >= 400:
            return self._fmt(r)
        d = r.json()
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
                return f"Job not found: `{job_id}`"
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
            return f"Job not found: `{job_id}`"
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
        data = r.json()
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
            return f"Research session not found: `{session_id}`"
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
                return f"Research session not found: `{session_id}`"
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
            return f"Research session not found: `{session_id}`"
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
        the response carries no actions (e.g., older orchestrators)."""
        actions = data.get("next_actions") or []
        # Filter out wait-style actions for terminal states — "wait" only
        # makes sense when something is in-flight; emitting it for completed
        # jobs is noise.
        renderable = [a for a in actions if a.get("action") != "wait"]
        if not renderable:
            return ""
        lines = ["", "**Next steps:**"]
        for a in renderable:
            cmd = a.get("command")
            desc = a.get("description", "")
            if cmd:
                lines.append(f"• `{cmd}` — {desc}")
            elif a.get("endpoint"):
                lines.append(
                    f"• `{a.get('method','GET')} {a['endpoint']}` — {desc}"
                )
            else:
                lines.append(f"• {desc}")
        return "\n".join(lines)

    def _handle_results(self, parts: list) -> str:
        if len(parts) < 2:
            return "Usage: `/results <job_id>`"
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
            return f"⚠️ Cannot reach orchestrator at {self.valves.orchestrator_url}."

        if r.status_code == 404:
            return f"Job not found: `{job_id}`"
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
            return head + self._render_next_actions(data)

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
                return f"❌ Schedule #{sid} not found"
            r.raise_for_status()
            return f"🗑️ Deleted schedule #{sid}"
        except Exception as exc:
            return f"❌ Failed to delete: {exc}"

    # ------------------------------------------------------------------
    # Help
    # ------------------------------------------------------------------

    def _help(self) -> str:
        return """**Scaffold Router Commands**

**Workflow:** describe your idea → triage chat → `/go` → review → `/confirm` → execution.

**🗣 Scope & kickoff**
| Command | Description |
|---|---|
| *(plain message)* | Chat with the triage assistant to scope your idea. |
| `/go` or `/run` | Lock the scoped idea, run Phase 1, halt at confirmation gate. |
| `/idea <text>` | Submit an idea directly to Phase 1 (skips triage). |
| `/confirm <job_id> [feedback]` | Approve a refined idea; auto-chains research → DAG → execute. |

**⚙ Workflow control**
| Command | Description |
|---|---|
| `/execute <job_id>` | Run all pending DAG nodes (use after cancel or if auto-chain stalls). |
| `/skip <job_id> <node_key>` | Skip a specific node so downstream can proceed. |
| `/results <job_id>` | View output, in-flight progress, or failure details + recovery hints. |
| `/status` | List active jobs grouped by state. |

**📚 Knowledge base**
| Command | Description |
|---|---|
| `/rag <query>` | Search the Milvus knowledge base. |
| `/research <topic>` | Autonomous web research → distill → ingest. |
| `/research <url>` | Ingest a single web page. |
| `/research github:<owner>/<repo>` | Ingest a repo's README, docs, and module docstrings. |
| `/research openapi:<url>` | Ingest an OpenAPI spec, one entry per endpoint. |
| `/research/reply <session_id> <msg>` | Resume a paused research session. |
| `/research/pdf` | Upload a PDF — drag-drop at `GET /research/pdf` or `curl -F file=@x.pdf`. |

**🗂 Manage saved work**
| Command | Description |
|---|---|
| `/jobs <sub>` | List/filter/find/rename/delete jobs. `/jobs help` for details. |
| `/research/<sub>` | List/find/rename/delete research sessions. `/research/help` for details. |
| `/schedule <sub>` | Recurring research crons (list/add/delete). |

**🔧 Configuration & utilities**
| Command | Description |
|---|---|
| `/model <sub>` | Models per role — list/available/set/reset/probe. |
| `/optimize <prompt>` | Tighten and improve a prompt. |
| `/help` | Show this message."""
