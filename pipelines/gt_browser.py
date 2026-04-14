"""
Step 19: Ground Truth Browser — Open WebUI Pipeline
Commands: /gt list, /gt search <query>, /gt detail <entry_id>, /gt stats
Routes to scaffold-orchestrator GT endpoints.
"""

import json
import httpx
from typing import Optional, List
from pydantic import BaseModel, Field


class Pipeline:
    class Valves(BaseModel):
        api_key: str = Field(default="", description="Scaffold Engine API key (X-API-Key header)")
        orchestrator_url: str = Field(
            default="http://scaffold-orchestrator:8000",
            description="Scaffold orchestrator base URL",
        )
        timeout: int = Field(default=60, description="Request timeout seconds")

    def __init__(self):
        self.id = "gt_browser"
        self.name = "gt_browser"
        self.valves = self.Valves()

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
        """Synchronous HTTP call to orchestrator."""
        url = f"{self.valves.orchestrator_url}{path}"
        try:
            with httpx.Client(timeout=self.valves.timeout) as client:
                if method == "GET":
                    resp = client.get(url, params=params, headers={"X-API-Key": self.valves.api_key})
                else:
                    resp = client.post(url, json=json_body, headers={"X-API-Key": self.valves.api_key})
                resp.raise_for_status()
                return resp.json()
        except httpx.TimeoutException:
            return {"error": f"Timeout after {self.valves.timeout}s"}
        except httpx.HTTPStatusError as e:
            return {"error": f"HTTP {e.response.status_code}: {e.response.text[:200]}"}
        except Exception as e:
            return {"error": str(e)}

    def _handle_list(self, arg: str) -> str:
        """List TOON entries, paginated."""
        page = 1
        if arg.strip().isdigit():
            page = int(arg.strip())

        data = self._call("GET", "/gt/list", params={"page": page, "per_page": 20})
        if "error" in data:
            return f"❌ {data['error']}"

        total = data.get("total", 0)
        total_pages = data.get("total_pages", 1)
        entries = data.get("entries", [])

        if not entries:
            return "No entries found."

        lines = [f"📚 **TOON Entries** — Page {page}/{total_pages} ({total} total)\n"]
        lines.append("| # | Entry ID | Topic | Tags | Snippet |")
        lines.append("|---|---|---|---|---|")

        for i, e in enumerate(entries, start=(page - 1) * 20 + 1):
            eid = e.get("entry_id", "—")
            topic = e.get("title", "—")
            tags = e.get("tags", "—")
            snippet = e.get("snippet", "—")[:60]
            lines.append(f"| {i} | `{eid}` | {topic} | {tags} | {snippet} |")

        if total_pages > 1:
            lines.append(f"\n*Navigate:* `/gt list {page + 1}` for next page")

        return "\n".join(lines)

    def _handle_search(self, query: str) -> str:
        """Semantic search TOON entries."""
        data = self._call("POST", "/gt/search", json_body={"query": query, "top_k": 10})
        if "error" in data:
            return f"❌ {data['error']}"

        results = data.get("results", [])
        if not results:
            return f"No results for: *{query}*"

        lines = [f"🔍 **Search:** *{query}* — {len(results)} results\n"]
        lines.append("| # | Entry ID | Topic | Score | Snippet |")
        lines.append("|---|---|---|---|---|")

        for i, r in enumerate(results, 1):
            eid = r.get("entry_id", "—")
            topic = r.get("title", "—")
            score = r.get("score", 0)
            snippet = r.get("snippet", "—")[:60]
            lines.append(f"| {i} | `{eid}` | {topic} | {score:.4f} | {snippet} |")

        lines.append("\n*View full entry:* `/gt detail <entry_id>`")
        return "\n".join(lines)

    def _handle_detail(self, entry_id: str) -> str:
        """Show full TOON entry."""
        data = self._call("GET", f"/gt/detail/{entry_id.strip()}")
        if "error" in data:
            return f"❌ {data['error']}"

        if not data.get("found"):
            return f"Entry `{entry_id}` not found."

        lines = [
            f"📄 **Entry:** `{data.get('entry_id', '—')}`\n",
            f"**Topic:** {data.get('title', '—')}",
            f"**Tags:** {data.get('tags', '—')}",
            f"**Source:** {data.get('source_url', '—')}",
        ]

        url = data.get("source_url", "")
        if url:
            lines.append(f"**URL:** {url}")

        lines.append(f"\n---\n\n{data.get('content', 'No content')}")

        return "\n".join(lines)

    def _handle_stats(self) -> str:
        """Collection summary."""
        data = self._call("GET", "/gt/stats")
        if "error" in data:
            return f"❌ {data['error']}"

        total = data.get("total_entries", 0)
        topics = data.get("domains", {})
        tags = data.get("tags", {})
        sources = data.get("source_types", {})

        lines = [f"📊 **Knowledge Base Stats** — {total} entries\n"]

        # Topics
        lines.append("**Topics:**")
        for t, count in list(topics.items())[:15]:
            lines.append(f"- {t}: {count}")

        # Tags (top 15)
        if tags:
            lines.append("\n**Top Tags:**")
            for t, count in list(tags.items())[:15]:
                lines.append(f"- {t}: {count}")

        # Sources
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
