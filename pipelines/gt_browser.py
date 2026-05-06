"""
Step 19: Ground Truth Browser — Open WebUI Pipeline
Commands: /gt list, /gt search <query>, /gt detail <entry_id>, /gt stats
Routes to scaffold-orchestrator GT endpoints.
"""

import requests
from typing import Optional, List
from pydantic import BaseModel, Field


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
        api_key: str = Field(default="", description="Scaffold Engine API key (X-API-Key header)")
        orchestrator_url: str = Field(
            default="http://scaffold-orchestrator:8000",
            description="Scaffold orchestrator base URL",
        )
        request_timeout: int = Field(default=60, description="Request timeout seconds")
        per_page: int = Field(default=20, description="Number of entries per /gt list page.")

    def __init__(self):
        self.id = "gt_browser"
        self.name = "gt_browser"
        _bootstrap_valves("gt_browser")
        self.valves = self.Valves()
        self._api_key_drift_detected = _apply_env_fallbacks(
            "gt_browser", self.valves,
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

    def pipe(
        self,
        user_message: str,
        model_id: str,
        messages: List[dict],
        body: dict,
    ) -> Optional[str]:
        """Route /gt commands. Return None for non-matching messages (pass-through)."""
        text = user_message.strip()

        if not text.startswith("/gt"):
            return None

        parts = text.split(maxsplit=2)
        cmd = parts[1] if len(parts) > 1 else "help"
        arg = parts[2] if len(parts) > 2 else ""

        if cmd == "list":
            return self._handle_list(arg)
        elif cmd == "search":
            if not arg:
                return "**Usage:** `/gt search <query>`\n\nExample: `/gt search embedding models`"
            return self._handle_search(arg)
        elif cmd == "detail":
            if not arg:
                return "**Usage:** `/gt detail <entry_id>`\n\nExample: `/gt detail TOON-001`"
            return self._handle_detail(arg)
        elif cmd == "stats":
            return self._handle_stats()
        else:
            return self._help()

    def _call(self, method: str, path: str, params: dict = None, json_body: dict = None) -> dict:
        """Synchronous HTTP call to orchestrator.

        Success: returns the parsed JSON dict from the orchestrator.
        Failure: returns ``{"_error": str, "_status_code": int | None}`` — the
        underscore prefix prevents collision with payload fields.
        """
        url = f"{self.valves.orchestrator_url}{path}"
        headers = {"X-API-Key": self.valves.api_key}
        try:
            if method == "GET":
                resp = _HTTP_SESSION.get(url, params=params, headers=headers, timeout=self.valves.request_timeout)
            else:
                resp = _HTTP_SESSION.post(url, json=json_body, headers=headers, timeout=self.valves.request_timeout)
        except requests.Timeout:
            return {"_error": f"Timeout after {self.valves.request_timeout}s", "_status_code": None}
        except requests.RequestException as e:
            return {"_error": f"Network error: {e}", "_status_code": None}

        status = resp.status_code

        try:
            data = resp.json()
        except ValueError:
            snippet = (resp.text or "")[:200]
            return {
                "_error": f"Orchestrator returned non-JSON response (HTTP {status}). Body: {snippet}",
                "_status_code": status,
            }

        if status >= 400:
            detail = ""
            if isinstance(data, dict):
                detail = data.get("detail", "") or data.get("error", "")
            msg = f"HTTP {status}: {detail}" if detail else f"HTTP {status}"
            return {"_error": msg, "_status_code": status}

        if isinstance(data, dict):
            return data
        return {"_raw": data}

    def _handle_list(self, arg: str) -> str:
        """List TOON entries, paginated."""
        page = 1
        if arg.strip().isdigit():
            page = max(1, int(arg.strip()))

        per_page = self.valves.per_page

        data = self._call("GET", "/gt/list", params={"page": page, "per_page": per_page})
        if "_error" in data:
            hint = self._drift_hint() if data.get("_status_code") == 401 else ""
            return f"❌ {data['_error']}{hint}"

        total = data.get("total", 0)
        total_pages = data.get("total_pages", 1)
        entries = data.get("entries", [])

        if not entries:
            return "No entries found."

        lines: List[str] = []

        if page > 1:
            lines.append(f"◀ *Previous:* `/gt list {page - 1}`\n")

        lines.append(f"📚 **TOON Entries** — Page {page}/{total_pages} ({total} total)\n")
        lines.append("| # | Entry ID | Topic | Tags | Snippet |")
        lines.append("|---|---|---|---|---|")

        offset = (page - 1) * per_page
        for i, e in enumerate(entries, start=offset + 1):
            eid = e.get("entry_id", "—")
            topic = e.get("title", "—")
            tags = e.get("tags", "—")
            snippet = (e.get("snippet") or "—")[:60]
            lines.append(f"| {i} | `{eid}` | {topic} | {tags} | {snippet} |")

        has_more = page < total_pages
        if has_more:
            lines.append(f"\n▶ *Next:* `/gt list {page + 1}`")
        elif page > 1:
            lines.append("\n*End of results.*")

        return "\n".join(lines)

    def _handle_search(self, query: str) -> str:
        """Semantic search TOON entries."""
        data = self._call("POST", "/gt/search", json_body={"query": query, "top_k": 10})
        if "_error" in data:
            hint = self._drift_hint() if data.get("_status_code") == 401 else ""
            return f"❌ {data['_error']}{hint}"

        results = data.get("results", [])
        if not results:
            return f"No results for: *{query}*"

        lines = [f"🔍 **Search:** *{query}* — {len(results)} results\n"]
        lines.append("| # | Entry ID | Topic | Score | Snippet |")
        lines.append("|---|---|---|---|---|")

        for i, r in enumerate(results, 1):
            eid = r.get("entry_id", "—")
            topic = r.get("title", "—")
            score = r.get("score", 0) or 0
            snippet = (r.get("snippet") or "—")[:60]
            try:
                score_str = f"{float(score):.4f}"
            except (TypeError, ValueError):
                score_str = str(score)
            lines.append(f"| {i} | `{eid}` | {topic} | {score_str} | {snippet} |")

        lines.append("\n*View full entry:* `/gt detail <entry_id>`")
        return "\n".join(lines)

    def _handle_detail(self, entry_id: str) -> str:
        """Show full TOON entry."""
        data = self._call("GET", f"/gt/detail/{entry_id.strip()}")

        if data.get("_status_code") == 404:
            return f"❌ Entry not found: `{entry_id}`"
        if "_error" in data:
            hint = self._drift_hint() if data.get("_status_code") == 401 else ""
            return f"❌ {data['_error']}{hint}"

        lines = [
            f"📄 **Entry:** `{data.get('entry_id', '—')}`\n",
            f"**Topic:** {data.get('title', '—')}",
            f"**Tags:** {data.get('tags', '—')}",
            f"**Source:** {data.get('source_url', '—')}",
            f"\n---\n\n{data.get('content', 'No content')}",
        ]
        return "\n".join(lines)

    def _handle_stats(self) -> str:
        """Collection summary."""
        data = self._call("GET", "/gt/stats")
        if "_error" in data:
            hint = self._drift_hint() if data.get("_status_code") == 401 else ""
            return f"❌ {data['_error']}{hint}"

        total = data.get("total_entries", 0)
        topics = data.get("domains", {}) or {}
        tags = data.get("tags", {}) or {}
        sources = data.get("source_types", {}) or {}

        lines = [f"📊 **Knowledge Base Stats** — {total} entries\n"]

        lines.append("**Topics:**")
        for t, count in list(topics.items())[:15]:
            lines.append(f"- {t}: {count}")

        if tags:
            lines.append("\n**Top Tags:**")
            for t, count in list(tags.items())[:15]:
                lines.append(f"- {t}: {count}")

        if sources:
            lines.append("\n**Source Files:**")
            for s, count in sources.items():
                lines.append(f"- `{s}`: {count}")

        return "\n".join(lines)

    def _help(self) -> str:
        return (
            "📚 **Ground Truth Browser**\n\n"
            "| Command | Description |\n"
            "|---|---|\n"
            "| `/gt list [page]` | List all TOON entries (paginated) |\n"
            "| `/gt search <query>` | Semantic search entries |\n"
            "| `/gt detail <entry_id>` | Show full entry content |\n"
            "| `/gt stats` | Collection summary (count, topics, tags) |\n"
        )
