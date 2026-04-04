"""
title: Prompt Inspector
author: scaffold-engine
version: 0.1.0
description: View and edit optimized prompts for DAG nodes.
"""

import requests
from typing import Optional
from pydantic import BaseModel


class Pipeline:
    class Valves(BaseModel):
        api_key: str = ""
        orchestrator_url: str = "http://scaffold-orchestrator:8000"
        request_timeout: int = 30

    def __init__(self):
        self.id = "prompt_inspector"
        self.name = "prompt_inspector"
        self.valves = self.Valves()

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
            "| `/prompt edit <job_id> <node_key>` | Edit prompt (paste new prompt as next message) |\n"
            "| `/prompt help` | Show this help |\n"
        )

    def _list(self, parts: list) -> str:
        if len(parts) < 3:
            return "❌ Usage: `/prompt list <job_id>`"

        job_id = parts[2]
        try:
            resp = requests.get(
                f"{self.valves.orchestrator_url}/prompts/{job_id}",
                headers={"X-API-Key": self.valves.api_key},
                timeout=self.valves.request_timeout
            )
            if resp.status_code != 200:
                return f"❌ Error: {resp.json().get('detail', resp.text)}"

            data = resp.json()
            lines = [
                f"## 📋 Prompts for Job `{job_id[:8]}...`\n",
                f"**{data['node_count']} nodes**\n",
                "| # | Node | Status | Template | Optimized |",
                "|---|---|---|---|---|",
            ]
            for n in data["nodes"]:
                t_icon = "✅" if n["has_template"] else "❌"
                o_icon = "✅" if n["has_optimized"] else "⬜"
                status_icons = {"done": "✅", "failed": "❌", "running": "🔄", "pending": "⬜", "skipped": "⏭️"}
                s_icon = status_icons.get(n["status"], n["status"])
                lines.append(f"| {n['execution_order']} | `{n['node_key']}` | {s_icon} {n['status']} | {t_icon} | {o_icon} |")

            lines.append(f"\nUse `/prompt view {job_id} <node_key>` for full details.")
            return "\n".join(lines)
        except requests.exceptions.RequestException as e:
            return f"❌ Connection error: {e}"

    def _view(self, parts: list) -> str:
        if len(parts) < 4:
            return "❌ Usage: `/prompt view <job_id> <node_key>`"

        job_id, node_key = parts[2], parts[3]
        try:
            resp = requests.get(
                f"{self.valves.orchestrator_url}/prompts/{job_id}/{node_key}",
                headers={"X-API-Key": self.valves.api_key},
                timeout=self.valves.request_timeout
            )
            if resp.status_code != 200:
                return f"❌ Error: {resp.json().get('detail', resp.text)}"

            d = resp.json()
            status_icons = {"done": "✅", "failed": "❌", "running": "🔄", "pending": "⬜", "skipped": "⏭️"}
            s_icon = status_icons.get(d["status"], d["status"])

            lines = [
                f"## 🔍 Node `{d['node_key']}` — {d['title']}",
                f"**Status:** {s_icon} {d['status']} · **Order:** {d['execution_order']} · **Model:** `{d['assigned_model'] or 'default'}`\n",
            ]

            if d["prompt_template"]:
                lines.append("### Original Template")
                lines.append(f"```\n{d['prompt_template']}\n```\n")

            if d["optimized_prompt"]:
                lines.append("### Optimized Prompt")
                lines.append(f"```\n{d['optimized_prompt']}\n```\n")

            if d["has_output"]:
                lines.append("### Output Preview")
                lines.append(f"```\n{d['output_preview']}\n```\n")

            if d["status"] in ("pending", "failed"):
                lines.append(f"💡 Editable — use `/prompt edit {job_id} {node_key}` then paste new prompt.")

            return "\n".join(lines)
        except requests.exceptions.RequestException as e:
            return f"❌ Connection error: {e}"

    def _edit(self, parts: list) -> str:
        if len(parts) < 4:
            return "❌ Usage: `/prompt edit <job_id> <node_key>`\n\nThen paste the new prompt as your next message."

        job_id, node_key = parts[2], parts[3]

        # Check if there's a prompt in the conversation history (last user message before this one)
        # For now, instruct the user to use a two-step flow
        # In a future iteration, we can detect the follow-up message

        return (
            f"✏️ **Edit mode for `{node_key}`**\n\n"
            f"Send your new prompt as the next message, prefixed with:\n"
            f"```\n/prompt save {job_id} {node_key} <your prompt here>\n```"
        )

    def _save(self, parts: list) -> str:
        if len(parts) < 5:
            return "❌ Usage: `/prompt save <job_id> <node_key> <new prompt text>`"

        job_id, node_key = parts[2], parts[3]
        new_prompt = " ".join(parts[4:])

        try:
            resp = requests.post(
                f"{self.valves.orchestrator_url}/prompts/{job_id}/{node_key}",
                json={"prompt": new_prompt},
                headers={"X-API-Key": self.valves.api_key},
                timeout=self.valves.request_timeout
            )
            if resp.status_code != 200:
                return f"❌ Error: {resp.json().get('detail', resp.text)}"

            d = resp.json()
            return (
                f"✅ **Prompt updated for `{node_key}`**\n\n"
                f"Old length: {d['old_length']} chars → New length: {d['new_length']} chars"
            )
        except requests.exceptions.RequestException as e:
            return f"❌ Connection error: {e}"

    # Override pipe to also handle /prompt save
    def pipe(self, user_message: str, model_id: str = "", messages: list = None, body: dict = None) -> Optional[str]:
        msg = user_message.strip()

        if not msg.startswith("/prompt"):
            return None

        parts = msg.split(None, 4)
        cmd = parts[1] if len(parts) > 1 else "help"

        if cmd == "help":
            return self._help()
        elif cmd == "list":
            return self._list(parts)
        elif cmd == "view":
            return self._view(parts)
        elif cmd == "edit":
            return self._edit(parts)
        elif cmd == "save":
            return self._save(parts)
        else:
            return f"❌ Unknown subcommand: `{cmd}`\n\n" + self._help()
