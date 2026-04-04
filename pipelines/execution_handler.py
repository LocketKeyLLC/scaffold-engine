"""
title: Execution Handler
author: scaffold-engine
version: 0.1.0
description: Interactive DAG execution control — status, approve, skip, retry.
"""

import requests
from typing import Optional
from pydantic import BaseModel


class Pipeline:
    class Valves(BaseModel):
        api_key: str = ""
        orchestrator_url: str = "http://scaffold-orchestrator:8000"
        request_timeout: int = 310

    def __init__(self):
        self.id = "execution_handler"
        self.name = "execution_handler"
        self.valves = self.Valves()

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
            resp = requests.get(
                f"{self.valves.orchestrator_url}/exec/status/{job_id}",
                headers={"X-API-Key": self.valves.api_key},
                timeout=30
            )
            if resp.status_code != 200:
                return f"❌ Error: {resp.json().get('detail', resp.text)}"

            d = resp.json()
            status_icons = {"done": "✅", "failed": "❌", "running": "🔄", "pending": "⬜", "skipped": "⏭️", "executing": "🔄", "planning": "📋", "blocked": "🚫", "completed": "✅", "cancelled": "🚫"}
            j_icon = status_icons.get(d["job_status"], d["job_status"])

            lines = [
                f"## ⚡ Job `{job_id[:8]}...` — {d['job_title']}",
                f"**Status:** {j_icon} {d['job_status']}\n",
            ]

            # Counts summary
            count_parts = []
            for s in ["done", "pending", "running", "failed", "skipped"]:
                if d["counts"].get(s, 0) > 0:
                    count_parts.append(f"{status_icons.get(s, s)} {d['counts'][s]} {s}")
            lines.append(" · ".join(count_parts) + "\n")

            # Node table
            lines.append("| # | Node | Status | Deps Met | Action |")
            lines.append("|---|---|---|---|---|")
            for n in d["nodes"]:
                s_icon = status_icons.get(n["status"], n["status"])
                deps = "✅" if n["deps_met"] else "⏳"
                action = ""
                if n["actionable"]:
                    if n["status"] == "pending":
                        action = "→ `approve`"
                    elif n["status"] == "failed":
                        action = "→ `retry`"
                lines.append(f"| {n['execution_order']} | `{n['node_key']}` | {s_icon} {n['status']} | {deps} | {action} |")

            # Next node callout
            if d["next_node"]:
                n = d["next_node"]
                lines.append(f"\n🎯 **Next:** `{n['node_key']}` — {n['title']}")
                if n["status"] == "pending":
                    lines.append(f"Run `/exec approve {job_id}` to execute it.")
                elif n["status"] == "failed":
                    lines.append(f"Run `/exec retry {job_id} {n['node_key']}` then `/exec approve {job_id}`.")
            else:
                if d["counts"].get("pending", 0) == 0 and d["counts"].get("failed", 0) == 0:
                    lines.append("\n🎉 **All nodes complete!**")
                else:
                    lines.append("\n⏳ No actionable nodes — dependencies not yet met.")

            return "\n".join(lines)
        except requests.exceptions.RequestException as e:
            return f"❌ Connection error: {e}"

    def _approve(self, parts: list) -> str:
        if len(parts) < 3:
            return "❌ Usage: `/exec approve <job_id>`"

        job_id = parts[2]
        try:
            resp = requests.post(
                f"{self.valves.orchestrator_url}/execute",
                json={"job_id": job_id},
                headers={"X-API-Key": self.valves.api_key},
                timeout=self.valves.request_timeout
            )
            if resp.status_code != 200:
                return f"❌ Error: {resp.json().get('detail', resp.text)}"

            d = resp.json()

            if d.get("awaiting_approval"):
                lines = [
                    f"✅ **Node `{d.get('node_key', '?')}` executed**\n",
                    f"**Model:** `{d.get('model', 'default')}`",
                ]
                if d.get("output_preview"):
                    lines.append(f"\n### Output Preview\n```\n{d['output_preview'][:300]}\n```")
                lines.append(f"\nRun `/exec status {job_id}` to see what's next.")
                return "\n".join(lines)
            else:
                return f"✅ Execution result:\n```\n{str(d)[:500]}\n```"

        except requests.exceptions.RequestException as e:
            return f"❌ Connection error: {e}"

    def _skip(self, parts: list) -> str:
        if len(parts) < 4:
            return "❌ Usage: `/exec skip <job_id> <node_key>`"

        job_id, node_key = parts[2], parts[3]
        try:
            resp = requests.post(
                f"{self.valves.orchestrator_url}/skip",
                json={"job_id": job_id, "node_key": node_key},
                headers={"X-API-Key": self.valves.api_key},
                timeout=30
            )
            if resp.status_code != 200:
                return f"❌ Error: {resp.json().get('detail', resp.text)}"

            return f"⏭️ **Node `{node_key}` skipped.**\n\nRun `/exec status {job_id}` to see what's next."
        except requests.exceptions.RequestException as e:
            return f"❌ Connection error: {e}"

    def _retry(self, parts: list) -> str:
        if len(parts) < 4:
            return "❌ Usage: `/exec retry <job_id> <node_key>`"

        job_id, node_key = parts[2], parts[3]
        try:
            resp = requests.post(
                f"{self.valves.orchestrator_url}/retry",
                json={"job_id": job_id, "node_key": node_key},
                headers={"X-API-Key": self.valves.api_key},
                timeout=30
            )
            if resp.status_code != 200:
                return f"❌ Error: {resp.json().get('detail', resp.text)}"

            d = resp.json()
            if d.get("status") == "reset":
                return (
                    f"🔄 **Node `{node_key}` reset to pending.**\n\n"
                    f"Run `/exec approve {job_id}` to re-execute it."
                )
            else:
                return f"❌ {d.get('error', 'Unknown error')}"
        except requests.exceptions.RequestException as e:
            return f"❌ Connection error: {e}"

    def pipe(self, user_message: str, model_id: str = "", messages: list = None, body: dict = None) -> Optional[str]:
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
