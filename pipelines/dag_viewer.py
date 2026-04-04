"""
dag_viewer.py  --  Step 18
Open WebUI Pipeline: fetch DAG for a job and render as Mermaid diagram.

Usage in Open WebUI:
  /dag <job_id>   -> fetches nodes from orchestrator, renders Mermaid
"""

from typing import List
import requests
from pydantic import BaseModel



class Pipeline:
    class Valves(BaseModel):
        api_key: str = ""
        orchestrator_url: str = "http://scaffold-orchestrator:8000"

    def __init__(self):
        self.id = "dag_viewer"
        self.name = "DAG Viewer"
        self.valves = self.Valves()

    def pipe(self, user_message: str, model_id: str, messages: List[dict], body: dict) -> str:
        msg = user_message.strip()

        if not msg.startswith("/dag"):
            return None

        parts = msg.split(None, 1)
        if len(parts) < 2:
            return "Usage: `/dag <job_id>`"

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
        status_icon = {
            "pending":  "⬜",
            "running":  "🔄",
            "done":     "✅",
            "failed":   "❌",
            "skipped":  "⏭️",
        }
        style_map = {
            "pending":  "fill:#444,stroke:#888,color:#fff",
            "running":  "fill:#1a6b9a,stroke:#4fc3f7,color:#fff",
            "done":     "fill:#1b5e20,stroke:#66bb6a,color:#fff",
            "failed":   "fill:#7f0000,stroke:#ef5350,color:#fff",
            "skipped":  "fill:#4a148c,stroke:#ce93d8,color:#fff",
        }

        lines = ["```mermaid", "graph TD"]

        for node in nodes:
            key = node["node_key"]
            title = node["title"].replace('"', "'")
            status = node.get("status", "pending")
            icon = status_icon.get(status, "⬜")
            lines.append(f'    {key}["{icon} {key}: {title}"]')

        for node in nodes:
            for dep in (node.get("depends_on") or []):
                lines.append(f"    {dep} --> {node['node_key']}")

        for node in nodes:
            key = node["node_key"]
            status = node.get("status", "pending")
            style = style_map.get(status, "")
            if style:
                lines.append(f"    style {key} {style}")

        lines.append("```")

        # Summary table
        job_status = data.get("job_status", "unknown")
        summary = [
            f"\n**Job:** `{job_id}`  |  **Status:** `{job_status}`\n",
            "| Node | Title | Status |",
            "|---|---|---|",
        ]
        for node in nodes:
            icon = status_icon.get(node.get("status", "pending"), "⬜")
            summary.append(
                f"| `{node['node_key']}` | {node['title']} | {icon} {node.get('status','pending')} |"
            )

        return "\n".join(lines) + "\n" + "\n".join(summary)
