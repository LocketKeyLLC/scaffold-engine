"""
title: Execution Handler
author: scaffold-engine
version: 0.2.0
description: Interactive DAG execution control — status, approve, skip, retry.
"""

import json
import requests
from typing import Optional
from pydantic import BaseModel


# Module-level Session for connection reuse. ``_HTTP_SESSION.X(...)``
# replaces ``requests.X(...)`` so each chat-driven call reuses the
# keep-alive pool instead of opening a fresh TCP connection. Tests
# patch ``_HTTP_SESSION.get`` / ``.post`` directly.
_HTTP_SESSION = requests.Session()


# ─── SHARED: status icons — keep in sync across pipelines (#8.17) ───
# Pipelines load as isolated single-file modules; no shared imports possible.
# execution_handler has additional job-lifecycle states (executing, planning,
# blocked, completed, cancelled) that the other pipelines don't need.
# If you add/rename a status, update every pipeline file that has this block.
STATUS_ICONS = {
    "done":      "✅",
    "failed":    "❌",
    "running":   "🔄",
    "pending":   "⬜",
    "skipped":   "⏭️",
    # Extended job states — execution_handler only
    "executing": "🔄",
    "planning":  "📋",
    "blocked":   "🚫",
    "completed": "✅",
    "cancelled": "🚫",
}
# ─── END SHARED ───


def _safe_json(resp):
    """Parse resp.json() safely. Returns (data, error_message). Exactly one is None."""
    try:
        return resp.json(), None
    except (json.JSONDecodeError, requests.exceptions.JSONDecodeError, ValueError):
        body_preview = (resp.text or "")[:200] if hasattr(resp, "text") else ""
        msg = f"❌ Orchestrator returned non-JSON response (HTTP {resp.status_code})"
        if body_preview:
            msg += f"\n\n```\n{body_preview}\n```"
        return None, msg


def _format_output(output: str, max_chars: int = 600) -> str:
    """Render node output, truncating long strings and fencing code-like content."""
    if not output:
        return ""
    total = len(output)
    if total > max_chars:
        truncated = output[:max_chars]
        suffix = f"\n... [{total - max_chars} chars truncated]"
    else:
        truncated = output
        suffix = ""

    # Heuristic: code if multiline and no markdown header lines
    is_code = "\n" in truncated and not any(
        line.lstrip().startswith("#") for line in truncated.split("\n")
    )
    if is_code:
        return f"```\n{truncated}{suffix}\n```"
    return f"{truncated}{suffix}"


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

    Caller stores the bool on the Pipeline instance so user-facing error
    paths can append a drift hint to 401s — the drift print() warning
    here goes only to container logs (ops surface), so the OWUI UI is
    otherwise blind to the rotation hazard.
    """
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
        # 310s = orchestrator per-node execute timeout (300s) + 10s slack for
        # network and JSON serialization. See execution_agent.execute_next_node.
        request_timeout: int = 310
        # Snappy management endpoints (status, skip, retry) — 30s default.
        quick_timeout: int = 30

    def __init__(self):
        self.id = "execution_handler"
        self.name = "execution_handler"
        _bootstrap_valves("execution_handler")
        self.valves = self.Valves()
        self._api_key_drift_detected = _apply_env_fallbacks(
            "execution_handler", self.valves,
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
            "## ⚡ Execution Handler\n\n"
            "| Command | Description |\n"
            "|---|---|\n"
            "| `/exec status <job_id>` | Show execution state + next actionable node |\n"
            "| `/exec approve <job_id>` | Execute the next pending node |\n"
            "| `/exec skip <job_id> <node_key>` | Skip a node |\n"
            "| `/exec retry <job_id> <node_key>` | Reset a failed node to pending |\n"
            "| `/exec help` | Show this help |\n"
        )

    def _status(self, parts: list) -> str:
        if len(parts) < 3:
            return "❌ Usage: `/exec status <job_id>`"

        job_id = parts[2]
        try:
            resp = _HTTP_SESSION.get(
                f"{self.valves.orchestrator_url}/exec/status/{job_id}",
                headers={"X-API-Key": self.valves.api_key},
                timeout=self.valves.quick_timeout,
            )
        except requests.exceptions.RequestException as e:
            return f"❌ Connection error: {e}"

        d, err = _safe_json(resp)
        if err:
            return err
        if resp.status_code != 200:
            detail = d.get("detail", resp.text) if isinstance(d, dict) else resp.text
            hint = self._drift_hint() if resp.status_code == 401 else ""
            return f"❌ Error: {detail}{hint}"

        # execution_status() returns HTTP 200 with {"error": "..."} on job-not-found
        if isinstance(d, dict) and "error" in d and "job_status" not in d:
            return f"❌ {d['error']}"

        job_status = d.get("job_status", "unknown")
        job_title = d.get("job_title", "?")
        counts = d.get("counts", {}) or {}
        nodes = d.get("nodes", []) or []
        next_node = d.get("next_node")
        j_icon = STATUS_ICONS.get(job_status, job_status)

        lines = [
            f"## ⚡ Job `{job_id[:8]}...` — {job_title}",
            f"**Status:** {j_icon} {job_status}\n",
        ]

        count_parts = []
        for s in ["done", "pending", "running", "failed", "skipped"]:
            n = counts.get(s, 0)
            if n > 0:
                count_parts.append(f"{STATUS_ICONS.get(s, s)} {n} {s}")
        if count_parts:
            lines.append(" · ".join(count_parts) + "\n")

        if nodes:
            lines.append("| # | Node | Status | Deps Met | Action |")
            lines.append("|---|---|---|---|---|")
            for n in nodes:
                n_status = n.get("status", "unknown")
                s_icon = STATUS_ICONS.get(n_status, n_status)
                deps = "✅" if n.get("deps_met") else "⏳"
                action = ""
                if n.get("actionable"):
                    if n_status == "pending":
                        action = "→ `approve`"
                    elif n_status == "failed":
                        action = "→ `retry`"
                lines.append(
                    f"| {n.get('execution_order', '?')} "
                    f"| `{n.get('node_key', '?')}` "
                    f"| {s_icon} {n_status} | {deps} | {action} |"
                )

        if next_node:
            nn_key = next_node.get("node_key", "?")
            nn_title = next_node.get("title", "")
            nn_status = next_node.get("status", "")
            lines.append(f"\n🎯 **Next:** `{nn_key}` — {nn_title}")
            if nn_status == "pending":
                lines.append(f"Run `/exec approve {job_id}` to execute it.")
            elif nn_status == "failed":
                lines.append(f"Run `/exec retry {job_id} {nn_key}` then `/exec approve {job_id}`.")
        else:
            if counts.get("pending", 0) == 0 and counts.get("failed", 0) == 0:
                lines.append("\n🎉 **All nodes complete!**")
            else:
                lines.append("\n⏳ No actionable nodes — dependencies not yet met.")

        return "\n".join(lines)

    def _approve(self, parts: list) -> str:
        if len(parts) < 3:
            return "❌ Usage: `/exec approve <job_id>`"

        job_id = parts[2]
        try:
            resp = _HTTP_SESSION.post(
                f"{self.valves.orchestrator_url}/execute",
                json={"job_id": job_id},
                headers={"X-API-Key": self.valves.api_key},
                timeout=self.valves.request_timeout,
            )
        except requests.exceptions.RequestException as e:
            return f"❌ Connection error: {e}"

        d, err = _safe_json(resp)
        if err:
            return err
        if resp.status_code != 200:
            detail = d.get("detail", resp.text) if isinstance(d, dict) else resp.text
            hint = self._drift_hint() if resp.status_code == 401 else ""
            return f"❌ Error: {detail}{hint}"

        node_key = d.get("node_key", "?")
        title = d.get("title", "")
        model_used = d.get("model_used", "unknown")
        output = d.get("output", "") or ""
        verified = d.get("verified")
        verification_reason = d.get("verification_reason", "")
        confidence = d.get("confidence")
        error = d.get("error")
        status = d.get("status", "")

        # Node-level failure branch (tool error, LLM timeout, verifier hard-fail)
        if error or status == "failed":
            msg = [f"❌ **Node `{node_key}` failed**"]
            if title:
                msg.append(f"**Task:** {title}")
            msg.append(f"**Model:** `{model_used}`")
            if error:
                msg.append(f"**Error:** {error}")
            msg.append(
                f"\nRun `/exec status {job_id}` or "
                f"`/exec retry {job_id} {node_key}` to retry."
            )
            return "\n".join(msg)

        lines = [f"✅ **Node `{node_key}` executed**"]
        if title:
            lines.append(f"**Task:** {title}")
        lines.append(f"**Model:** `{model_used}`")

        # Verification verdict (new — surfaces fields orchestrator already returns)
        if verified is True:
            conf_str = (
                f" (confidence {confidence:.2f})"
                if isinstance(confidence, (int, float)) else ""
            )
            lines.append(f"**Verification:** ✅ Verified{conf_str}")
        elif verified is False:
            reason_str = f" — {verification_reason}" if verification_reason else ""
            lines.append(f"**Verification:** ⚠️ Failed{reason_str}")

        if output:
            lines.append(f"\n### Output\n{_format_output(output)}")

        lines.append(f"\nRun `/exec status {job_id}` to see what's next.")
        return "\n".join(lines)

    def _skip(self, parts: list) -> str:
        if len(parts) < 4:
            return "❌ Usage: `/exec skip <job_id> <node_key>`"

        job_id, node_key = parts[2], parts[3]
        try:
            resp = _HTTP_SESSION.post(
                f"{self.valves.orchestrator_url}/skip",
                json={"job_id": job_id, "node_key": node_key},
                headers={"X-API-Key": self.valves.api_key},
                timeout=self.valves.quick_timeout,
            )
        except requests.exceptions.RequestException as e:
            return f"❌ Connection error: {e}"

        d, err = _safe_json(resp)
        if err:
            return err
        if resp.status_code != 200:
            detail = d.get("detail", resp.text) if isinstance(d, dict) else resp.text
            hint = self._drift_hint() if resp.status_code == 401 else ""
            return f"❌ Error: {detail}{hint}"

        # skip_node returns {"status":"skipped",...} on success or
        # {"status":"error","message":...} on not-found. Don't claim success blindly.
        if d.get("status") == "skipped":
            return (
                f"⏭️ **Node `{node_key}` skipped.**\n\n"
                f"Run `/exec status {job_id}` to see what's next."
            )
        else:
            reason = d.get("message") or d.get("error") or "unknown error"
            return f"❌ Could not skip `{node_key}`: {reason}"

    def _retry(self, parts: list) -> str:
        if len(parts) < 4:
            return "❌ Usage: `/exec retry <job_id> <node_key>`"

        job_id, node_key = parts[2], parts[3]
        try:
            resp = _HTTP_SESSION.post(
                f"{self.valves.orchestrator_url}/exec/retry",
                json={"job_id": job_id, "node_key": node_key},
                headers={"X-API-Key": self.valves.api_key},
                timeout=self.valves.quick_timeout,
            )
        except requests.exceptions.RequestException as e:
            return f"❌ Connection error: {e}"

        d, err = _safe_json(resp)
        if err:
            return err
        if resp.status_code != 200:
            detail = d.get("detail", resp.text) if isinstance(d, dict) else resp.text
            hint = self._drift_hint() if resp.status_code == 401 else ""
            return f"❌ Error: {detail}{hint}"

        if d.get("status") == "reset":
            return (
                f"🔄 **Node `{node_key}` reset to pending.**\n\n"
                f"Run `/exec approve {job_id}` to re-execute it."
            )
        else:
            # Orchestrator uses 'message' per skip_node convention; 'error' legacy fallback.
            reason = d.get("message") or d.get("error") or "Unknown error"
            return f"❌ {reason}"

    def pipe(
        self,
        user_message: str,
        model_id: str = "",
        messages: list = None,
        body: dict = None,
    ) -> Optional[str]:
        msg = user_message.strip()

        if not msg.startswith("/exec"):
            return None

        parts = msg.split(None, 4)
        cmd = parts[1] if len(parts) > 1 else "help"

        if cmd == "help":
            return self._help()
        elif cmd == "status":
            return self._status(parts)
        elif cmd == "approve":
            return self._approve(parts)
        elif cmd == "skip":
            return self._skip(parts)
        elif cmd == "retry":
            return self._retry(parts)
        else:
            return f"❌ Unknown subcommand: `{cmd}`\n\n" + self._help()
