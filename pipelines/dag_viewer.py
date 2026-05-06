"""
dag_viewer.py  --  Step 18
Open WebUI Pipeline: fetch DAG for a job and render as Mermaid diagram.

Usage in Open WebUI:
  /dagviz <job_id>  -> fetches nodes from orchestrator, renders Mermaid
  (Renamed from /dag in #8.20 to avoid overlap with scaffold_router.)
"""

from typing import List, Optional
import requests
from pydantic import BaseModel


# Module-level Session for connection reuse. Tests patch
# ``_HTTP_SESSION.get`` directly.
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

# #8.21 — Mermaid has several reserved characters that break node labels:
# `"` ends the label, `[` `]` `(` `)` `{` `}` are grouping/shape syntax,
# `|` is used for edge labels, `#` starts comments. Plus backtick / newline
# confuse the renderer. Replace them with safe alternatives.
_MERMAID_LABEL_UNSAFE = {
    '"': "'",
    "[": "(",
    "]": ")",
    "{": "(",
    "}": ")",
    "|": "/",
    "#": "♯",   # musical sharp, visually similar
    "`": "'",
    "\n": " ",
    "\r": " ",
    "<": "⟨",   # mathematical angle bracket
    ">": "⟩",
}


def _escape_mermaid_label(label: str) -> str:
    """Sanitize a string for safe inclusion inside a Mermaid node label."""
    if not label:
        return ""
    # Mermaid itself has no escape syntax — best we can do is substitute.
    for bad, good in _MERMAID_LABEL_UNSAFE.items():
        label = label.replace(bad, good)
    return label


# #8.29 — cap Mermaid output so oversized DAGs don't flood chat.
_MAX_NODES_RENDERED = 200


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
# Int-typed valves with env fallback (e.g. request_timeout). Type-coerced
# in _apply_env_fallbacks below.
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
    # Int valves: applied only when valves.json did not include the field
    # (i.e. user has not explicitly set it via the OWUI valve UI).
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
    # Drift warning: api_key in valves.json differs from env (silent rotation hazard).
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
        self.id = "dag_viewer"
        self.name = "DAG Viewer"
        _bootstrap_valves("dag_viewer")
        self.valves = self.Valves()
        self._api_key_drift_detected = _apply_env_fallbacks(
            "dag_viewer", self.valves,
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

    async def on_startup(self):  # #8.27 — parity with other pipelines
        pass

    async def on_shutdown(self):  # #8.27
        pass

    def pipe(self, user_message: str, model_id: str, messages: List[dict], body: dict) -> Optional[str]:
        msg = user_message.strip()

        if not msg.startswith("/dagviz"):
            return None

        parts = msg.split(None, 1)
        if len(parts) < 2:
            return "Usage: `/dagviz <job_id>`"

        job_id = parts[1].strip()

        try:
            r = _HTTP_SESSION.get(
                f"{self.valves.orchestrator_url}/dag/{job_id}",
                headers={"X-API-Key": self.valves.api_key},
                timeout=self.valves.request_timeout,
            )
            if r.status_code == 404:
                return f"⚠️ Job `{job_id}` not found."
            if r.status_code != 200:
                hint = self._drift_hint() if r.status_code == 401 else ""
                return f"⚠️ Error {r.status_code}: {r.text[:200]}{hint}"

            data = r.json()
            nodes = data.get("nodes", [])
            if not nodes:
                return f"No DAG nodes found for job `{job_id}`."

            return self._render(job_id, data, nodes)

        except requests.exceptions.ConnectionError:
            return f"⚠️ Cannot reach orchestrator at {self.valves.orchestrator_url}. Is it running?"
        except Exception as e:
            return f"⚠️ Error: {e}"

    def _render(self, job_id: str, data: dict, nodes: list) -> str:
        status_icon = STATUS_ICONS  # #8.17 alias so existing references below keep working
        style_map = {
            "pending":  "fill:#444,stroke:#888,color:#fff",
            "running":  "fill:#1a6b9a,stroke:#4fc3f7,color:#fff",
            "done":     "fill:#1b5e20,stroke:#66bb6a,color:#fff",
            "failed":   "fill:#7f0000,stroke:#ef5350,color:#fff",
            "skipped":  "fill:#4a148c,stroke:#ce93d8,color:#fff",
        }

        # #8.29 — truncate oversized DAGs. Keep the first N nodes and their edges;
        # drop the rest and signal truncation in the summary.
        total_nodes = len(nodes)
        truncated = total_nodes > _MAX_NODES_RENDERED
        rendered_nodes = nodes[:_MAX_NODES_RENDERED] if truncated else nodes
        rendered_keys = {n["node_key"] for n in rendered_nodes}

        # #8.28 — single pass collects node decls, edges, styles, summary rows
        mermaid_lines = ["```mermaid", "graph TD"]
        edge_lines: list[str] = []
        style_lines: list[str] = []
        summary_rows: list[str] = []

        for node in rendered_nodes:
            key = node["node_key"]
            title_raw = node.get("title", "")
            title = _escape_mermaid_label(title_raw)
            status = node.get("status", "pending")
            icon = status_icon.get(status, "⬜")

            mermaid_lines.append(f'    {key}["{icon} {key}: {title}"]')

            for dep in (node.get("depends_on") or []):
                # Only draw edges to nodes that survived truncation
                if dep in rendered_keys:
                    edge_lines.append(f"    {dep} --> {key}")

            style = style_map.get(status, "")
            if style:
                style_lines.append(f"    style {key} {style}")

            summary_rows.append(
                f"| `{key}` | {title_raw} | {icon} {status} |"
            )

        mermaid_lines.extend(edge_lines)
        mermaid_lines.extend(style_lines)
        mermaid_lines.append("```")

        job_status = data.get("job_status", "unknown")
        header = [
            f"\n**Job:** `{job_id}`  |  **Status:** `{job_status}`\n",
            "| Node | Title | Status |",
            "|---|---|---|",
        ]
        if truncated:
            dropped_keys = [n["node_key"] for n in nodes[_MAX_NODES_RENDERED:]]
            # Show up to 20 dropped keys inline so the user can spot
            # whether a node they care about was clipped; full list is
            # available via /jobs + /dag/{job_id}.
            shown = ", ".join(f"`{k}`" for k in dropped_keys[:20])
            more = f" (+{len(dropped_keys) - 20} more)" if len(dropped_keys) > 20 else ""
            header.insert(
                0,
                f"⚠️  DAG has {total_nodes} nodes; rendering first "
                f"{_MAX_NODES_RENDERED} only.\n\n"
                f"**Dropped from diagram:** {shown}{more}\n",
            )

        return "\n".join(mermaid_lines) + "\n" + "\n".join(header + summary_rows)
