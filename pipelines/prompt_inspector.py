"""
title: Prompt Inspector
author: scaffold-engine
version: 0.1.0
description: View and edit optimized prompts for DAG nodes.
"""

import requests
from typing import Optional
from pydantic import BaseModel


# Module-level Session for connection reuse. Tests patch
# ``_HTTP_SESSION.get`` / ``.post`` directly.
_HTTP_SESSION = requests.Session()


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

# ---------------------------------------------------------------------------
# Valves persistence helpers (inlined per-pipeline — OWUI Pipelines auto-
# discovers every sibling .py as a pipeline candidate, so a shared module
# cannot live alongside).
# ---------------------------------------------------------------------------
import os as _vp_os


def _bootstrap_valves(pipeline_id: str) -> None:
    here = _vp_os.path.dirname(_vp_os.path.abspath(__file__))
    sub = _vp_os.path.join(here, pipeline_id)
    live = _vp_os.path.join(sub, "valves.json")
    tmpl = _vp_os.path.join(sub, "valves.template.json")
    if not _vp_os.path.exists(tmpl):
        raise RuntimeError(
            f"[{pipeline_id}] valves.template.json missing at {tmpl!r}; "
            f"the pipeline cannot bootstrap. Verify the ./pipelines volume "
            f"mount in docker-compose.yml is present."
        )
    needs_seed = False
    if not _vp_os.path.exists(live):
        needs_seed = True
    else:
        try:
            with open(live, "r") as f:
                content = f.read().strip()
        except OSError as e:
            raise RuntimeError(f"[{pipeline_id}] cannot read valves.json: {e}")
        if content in ("", "{}"):
            needs_seed = True
    if not needs_seed:
        return
    with open(tmpl, "r") as f:
        tmpl_data = f.read()
    with open(live, "w") as f:
        f.write(tmpl_data)
    print(f"[{pipeline_id}] Seeded valves.json from template.")


_VP_ENV_MAP = {
    "api_key": "SCAFFOLD_API_KEY",
    "orchestrator_url": "SCAFFOLD_ORCHESTRATOR_URL",
}
_VP_ENV_INT_MAP = {
    "request_timeout": "SCAFFOLD_REQUEST_TIMEOUT",
}


def _apply_env_fallbacks(pipeline_id: str, valves) -> bool:
    """Returns True iff valves.json's api_key differs from the environment.
    Caller stores the bool on the Pipeline so 401 paths can append a
    drift hint to the user-visible response."""
    import json as _vp_json
    here = _vp_os.path.dirname(_vp_os.path.abspath(__file__))
    live = _vp_os.path.join(here, pipeline_id, "valves.json")
    try:
        with open(live, "r") as _fh:
            saved = _vp_json.load(_fh)
            if not isinstance(saved, dict):
                saved = {}
    except Exception:
        saved = {}
    changed = False
    for valve_name, env_name in _VP_ENV_MAP.items():
        current = getattr(valves, valve_name, None)
        if isinstance(current, str) and not current:
            env_val = _vp_os.getenv(env_name, "")
            if env_val:
                setattr(valves, valve_name, env_val)
                changed = True
                print(f"[{pipeline_id}] Valve {valve_name!r} loaded from {env_name}.", flush=True)
    for valve_name, env_name in _VP_ENV_INT_MAP.items():
        if valve_name in saved:
            continue
        env_val = _vp_os.getenv(env_name, "")
        if not env_val:
            continue
        try:
            setattr(valves, valve_name, int(env_val))
            changed = True
            print(f"[{pipeline_id}] Valve {valve_name!r} loaded from {env_name} (int).", flush=True)
        except (ValueError, TypeError):
            print(f"[{pipeline_id}] {env_name}={env_val!r} is not an int; ignoring.", flush=True)
    saved_key = saved.get("api_key", "")
    env_key = _vp_os.getenv("SCAFFOLD_API_KEY", "")
    drift_detected = bool(saved_key and env_key and saved_key != env_key)
    if drift_detected:
        print(f"[{pipeline_id}] WARNING: api_key in valves.json differs from SCAFFOLD_API_KEY env. Using valves.json value.", flush=True)
    if changed:
        try:
            here = _vp_os.path.dirname(_vp_os.path.abspath(__file__))
            live = _vp_os.path.join(here, pipeline_id, "valves.json")
            payload = {k: getattr(valves, k) for k in valves.model_dump().keys()}
            with open(live, "w") as f:
                _vp_json.dump(payload, f)
            print(f"[{pipeline_id}] Persisted env-fallback values to {live!r}.", flush=True)
        except Exception as e:
            print(f"[{pipeline_id}] Persist failed: {e}", flush=True)
    return drift_detected


class Pipeline:
    class Valves(BaseModel):
        api_key: str = ""
        orchestrator_url: str = "http://scaffold-orchestrator:8000"
        request_timeout: int = 30

    def __init__(self):
        self.id = "prompt_inspector"
        self.name = "prompt_inspector"
        _bootstrap_valves("prompt_inspector")
        self.valves = self.Valves()
        self._api_key_drift_detected = _apply_env_fallbacks(
            "prompt_inspector", self.valves,
        )

    def _drift_hint(self) -> str:
        """Markdown line appended to user-visible 401 responses so the
        valves.json / SCAFFOLD_API_KEY env mismatch surfaces in the OWUI
        UI rather than only in the container logs."""
        if not getattr(self, "_api_key_drift_detected", False):
            return ""
        return (
            "\n\n⚠️ This pipeline detected that `api_key` in `valves.json` "
            "differs from `SCAFFOLD_API_KEY` in the environment. The 401 "
            "above is likely caused by one of those values being stale. "
            "Reconcile both sides and reload the pipeline."
        )

    async def on_startup(self):
        pass

    async def on_shutdown(self):
        pass

    def _help(self) -> str:
        return (
            "## 🔍 Prompt Inspector\n\n"
            "| Command | Description |\n"
            "|---|---|\n"
            "| `/prompt list <job_id>` | List all node prompts for a job |\n"
            "| `/prompt view <job_id> <node_key>` | View full prompt for a node |\n"
            "| `/prompt edit <job_id> <node_key> <new prompt>` | Edit prompt in a single message (newlines preserved) |\n"
            "| `/prompt help` | Show this help |\n"
        )

    def _list(self, parts: list) -> str:
        if len(parts) < 3:
            return "❌ Usage: `/prompt list <job_id>`"

        job_id = parts[2]
        try:
            resp = _HTTP_SESSION.get(
                f"{self.valves.orchestrator_url}/prompts/{job_id}",
                headers={"X-API-Key": self.valves.api_key},
                timeout=self.valves.request_timeout
            )
            if resp.status_code != 200:
                hint = self._drift_hint() if resp.status_code == 401 else ""
                return f"❌ Error: {resp.json().get('detail', resp.text)}{hint}"

            data = resp.json()
            nodes = data.get("nodes", [])
            lines = [
                f"## 📋 Prompts for Job `{job_id[:8]}...`\n",
                f"**{data.get('node_count', len(nodes))} nodes**\n",
                "| # | Node | Status | Template | Optimized |",
                "|---|---|---|---|---|",
            ]
            for n in nodes:
                t_icon = "✅" if n.get("has_template") else "❌"
                o_icon = "✅" if n.get("has_optimized") else "⬜"
                status = n.get("status", "?")
                s_icon = STATUS_ICONS.get(status, status)
                lines.append(
                    f"| {n.get('execution_order', '?')} | `{n.get('node_key', '?')}` | "
                    f"{s_icon} {status} | {t_icon} | {o_icon} |"
                )

            lines.append(f"\nUse `/prompt view {job_id} <node_key>` for full details.")
            return "\n".join(lines)
        except requests.exceptions.RequestException as e:
            return f"❌ Connection error: {e}"

    def _view(self, parts: list) -> str:
        if len(parts) < 4:
            return "❌ Usage: `/prompt view <job_id> <node_key>`"

        job_id, node_key = parts[2], parts[3]
        try:
            resp = _HTTP_SESSION.get(
                f"{self.valves.orchestrator_url}/prompts/{job_id}/{node_key}",
                headers={"X-API-Key": self.valves.api_key},
                timeout=self.valves.request_timeout
            )
            if resp.status_code != 200:
                hint = self._drift_hint() if resp.status_code == 401 else ""
                return f"❌ Error: {resp.json().get('detail', resp.text)}{hint}"

            d = resp.json()
            status = d.get("status", "?")
            s_icon = STATUS_ICONS.get(status, status)
            model = d.get("assigned_model") or "default"

            lines = [
                f"## 🔍 Node `{d.get('node_key', '?')}` — {d.get('title', '')}",
                f"**Status:** {s_icon} {status} · "
                f"**Order:** {d.get('execution_order', '?')} · **Model:** `{model}`\n",
            ]

            if d.get("prompt_template"):
                lines.append("### Original Template")
                lines.append(f"```\n{d['prompt_template']}\n```\n")

            if d.get("optimized_prompt"):
                lines.append("### Optimized Prompt")
                lines.append(f"```\n{d['optimized_prompt']}\n```\n")

            if d.get("has_output"):
                lines.append("### Output Preview")
                lines.append(f"```\n{d.get('output_preview', '')}\n```\n")

            if status in ("pending", "failed"):
                lines.append(
                    f"💡 Editable — use `/prompt edit {job_id} {node_key} <new prompt>`."
                )

            return "\n".join(lines)
        except requests.exceptions.RequestException as e:
            return f"❌ Connection error: {e}"

    # Client-side prompt length cap. MUST stay aligned with
    # app/config.py:Settings.prompt_max_chars (default 16_384) so the
    # OWUI pre-check fails at the same threshold as the orchestrator's
    # update_prompt() server-side check. If the cap moves at the
    # orchestrator, update this constant to match — the values do not
    # auto-sync because the pipelines run in a separate container with
    # no app.config import path.
    _MAX_PROMPT_CHARS = 16_384

    def _edit(self, raw_message: str) -> str:
        """Single-step edit: /prompt edit <job_id> <node_key> <new prompt>

        Everything after the node_key is treated verbatim as the new prompt,
        including newlines (#8.19 — no " ".join tokenization).
        """
        # Split only off the first 4 whitespace-delimited tokens to preserve
        # internal whitespace/newlines in the prompt body.
        header_parts = raw_message.split(None, 3)
        if len(header_parts) < 4:
            return (
                "❌ Usage: `/prompt edit <job_id> <node_key> <new prompt text>`\n\n"
                "Everything after `<node_key>` is used verbatim as the new prompt "
                "(newlines preserved)."
            )
        _, _, job_id, rest = header_parts
        # rest now contains node_key followed by the prompt body
        node_parts = rest.split(None, 1)
        if len(node_parts) < 2 or not node_parts[1].strip():
            return "❌ Missing prompt body. Usage: `/prompt edit <job_id> <node_key> <new prompt text>`"
        node_key, new_prompt = node_parts

        # #8.26 — client-side length validation
        if len(new_prompt) > self._MAX_PROMPT_CHARS:
            return (
                f"❌ Prompt too long: {len(new_prompt):,} chars (limit "
                f"{self._MAX_PROMPT_CHARS:,}). Shorten and retry."
            )

        return self._post_prompt(job_id, node_key, new_prompt)

    def _post_prompt(self, job_id: str, node_key: str, new_prompt: str) -> str:
        try:
            resp = _HTTP_SESSION.post(
                f"{self.valves.orchestrator_url}/prompts/{job_id}/{node_key}",
                json={"prompt": new_prompt},
                headers={"X-API-Key": self.valves.api_key},
                timeout=self.valves.request_timeout,
            )
            if resp.status_code != 200:
                try:
                    detail = resp.json().get("detail", resp.text)
                except ValueError:
                    detail = resp.text
                hint = self._drift_hint() if resp.status_code == 401 else ""
                return f"❌ Error: {detail}{hint}"

            d = resp.json()
            # #8.25 — defensive .get() with fallbacks
            old_len = d.get("old_length", "?")
            new_len = d.get("new_length", len(new_prompt))
            return (
                f"✅ **Prompt updated for `{node_key}`**\n\n"
                f"Old length: {old_len} chars → New length: {new_len} chars"
            )
        except requests.exceptions.RequestException as e:
            return f"❌ Connection error: {e}"

    # Back-compat: /prompt save <job> <node> <prompt> still works the same.
    def _save(self, raw_message: str) -> str:
        """Deprecated — use /prompt edit. Still accepted for compatibility."""
        header_parts = raw_message.split(None, 3)
        if len(header_parts) < 4:
            return "❌ Usage: `/prompt save <job_id> <node_key> <new prompt text>` (deprecated — prefer `/prompt edit`)"
        _, _, job_id, rest = header_parts
        node_parts = rest.split(None, 1)
        if len(node_parts) < 2 or not node_parts[1].strip():
            return "❌ Missing prompt body."
        node_key, new_prompt = node_parts
        if len(new_prompt) > self._MAX_PROMPT_CHARS:
            return (
                f"❌ Prompt too long: {len(new_prompt):,} chars (limit "
                f"{self._MAX_PROMPT_CHARS:,})."
            )
        return self._post_prompt(job_id, node_key, new_prompt)

    def pipe(self, user_message: str, model_id: str = "", messages: list = None, body: dict = None) -> Optional[str]:
        msg = user_message.strip()

        if not msg.startswith("/prompt"):
            return None

        # Only split off the leading /prompt <cmd> for routing; edit/save
        # need the full raw body below so newlines in the prompt body survive.
        parts = msg.split(None, 2)
        cmd = parts[1] if len(parts) > 1 else "help"

        if cmd == "help":
            return self._help()
        elif cmd == "list":
            return self._list(msg.split())
        elif cmd == "view":
            return self._view(msg.split())
        elif cmd == "edit":
            return self._edit(msg)
        elif cmd == "save":
            return self._save(msg)
        else:
            return f"❌ Unknown subcommand: `{cmd}`\n\n" + self._help()
