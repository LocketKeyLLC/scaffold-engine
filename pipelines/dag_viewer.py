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


class Pipeline:
    class Valves(BaseModel):
        api_key: str = ""
        orchestrator_url: str = "http://scaffold-orchestrator:8000"

    def __init__(self):
        self.id = "dag_viewer"
        self.name = "DAG Viewer"
        self.valves = self.Valves()

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
            r = requests.get(
                f"{self.valves.orchestrator_url}/dag/{job_id}",
                headers={"X-API-Key": self.valves.api_key},
                timeout=15,
            )
            if r.status_code == 404:
                return f"⚠️ Job `{job_id}` not found."
            if r.status_code != 200:
                return f"⚠️ Error {r.status_code}: {r.text[:200]}"

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
            header.insert(
                0,
                f"⚠️  DAG has {total_nodes} nodes; rendering first "
                f"{_MAX_NODES_RENDERED} only.\n",
            )

        return "\n".join(mermaid_lines) + "\n" + "\n".join(header + summary_rows)
