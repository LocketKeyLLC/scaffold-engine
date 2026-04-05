"""
scaffold_router.py  --  Step 17 (v2 — streaming auto-chain)
Open WebUI Pipeline: auto-chains /ideas → /dag → /execute/all for plain messages.

Slash commands still work:
  /idea <text>        -> POST /ideas  (manual, returns JSON)
  /dag <job_id>       -> POST /dag    (manual, returns JSON)
  /execute <job_id>   -> POST /execute
  /skip <job_id> <node_key> -> POST /skip
  /optimize <text>    -> POST /optimize
  /rag <query>        -> POST /rag
  /status             -> GET  /status
  /help               -> show command list

Non-command messages trigger the full Scaffold Engine pipeline automatically.
"""

from typing import List, Optional, Generator, Iterator
import requests
import json
import time
import threading
import queue
from pydantic import BaseModel, Field


class Pipeline:
    class Valves(BaseModel):
        api_key: str = ""
        orchestrator_url: str = "http://scaffold-orchestrator:8000"
        dag_timeout: int = 600          # seconds to wait for DAG generation
        keepalive_interval: int = 10    # seconds between keepalive dots

    def __init__(self):
        self.id = "scaffold_router"
        self.name = "Scaffold Router"
        self.valves = self.Valves()

    # ------------------------------------------------------------------
    # Main entry point — now a sync GENERATOR (yields chunks)
    # ------------------------------------------------------------------
    def pipe(
        self,
        user_message: str,
        model_id: str,
        messages: List[dict],
        body: dict,
    ) -> Generator[str, None, None]:
        msg = user_message.strip()

        # --- Slash commands: yield single response, return ---
        if msg.startswith("/"):
            yield self._handle_command(msg)
            return

        # --- Auto-chain: /ideas → /dag → /execute/all ---
        yield from self._auto_chain(msg)

    # ------------------------------------------------------------------
    # Auto-chain: full Scaffold Engine flow from a plain message
    # ------------------------------------------------------------------
    def _auto_chain(self, message: str) -> Generator[str, None, None]:
        headers = {"X-API-Key": self.valves.api_key}

        # ---- Phase 1: /ideas (can take ~130s, yield keepalive dots) ----
        yield "Let me think about this"

        ideas_result = [None]
        ideas_error = [None]

        def _call_ideas():
            try:
                ideas_result[0] = requests.post(
                    f"{self.valves.orchestrator_url}/ideas",
                    json={"idea": message},
                    headers=headers,
                    timeout=300,
                )
            except Exception as e:
                ideas_error[0] = e

        t = threading.Thread(target=_call_ideas, daemon=True)
        t.start()

        while t.is_alive():
            time.sleep(self.valves.keepalive_interval)
            if t.is_alive():
                yield "\u200b"

        t.join()

        if ideas_error[0]:
            if isinstance(ideas_error[0], requests.exceptions.ConnectionError):
                yield "\nI couldn't reach the analysis engine. It may be restarting — please try again in a moment."
            elif isinstance(ideas_error[0], requests.exceptions.Timeout):
                yield "\n⚠️ The analysis engine is taking too long to respond. Please try again in a moment."
            else:
                yield f"\n⚠️ Error: {ideas_error[0]}"
            return

        r = ideas_result[0]
        if r is None:
            yield "\n⚠️ No response from the analysis engine."
            return

        if r.status_code >= 400:
            yield "\nI had trouble understanding that request. Could you rephrase it?"
            return

        try:
            ideas_data = r.json()
            job_id = ideas_data["job_id"]
        except (ValueError, KeyError) as e:
            yield f"\n⚠️ Unexpected response from the analysis engine: {e}"
            return

        brief = ideas_data.get("refined_brief", {})
        title = brief.get("title", "") if isinstance(brief, dict) else ""
        if title:
            yield f"\n**{title}**\n\n"
        else:
            yield "\n\n"

        # ---- Phase 2: /dag (long wait ~200s, yield keepalive dots) ----
        yield "Planning my approach"

        dag_result = [None]   # mutable container for thread result
        dag_error = [None]

        def _call_dag():
            try:
                dag_result[0] = requests.post(
                    f"{self.valves.orchestrator_url}/dag",
                    json={"job_id": job_id},
                    headers=headers,
                    timeout=self.valves.dag_timeout,
                )
            except Exception as e:
                dag_error[0] = e

        t = threading.Thread(target=_call_dag, daemon=True)
        t.start()

        # Yield keepalive dots while waiting
        elapsed = 0
        while t.is_alive():
            time.sleep(self.valves.keepalive_interval)
            elapsed += self.valves.keepalive_interval
            if t.is_alive():
                yield "\u200b"
            # Safety: don't wait longer than dag_timeout + buffer
            if elapsed > self.valves.dag_timeout + 30:
                yield "\n\nPlanning is taking longer than expected. Please try again with a simpler question."
                return

        t.join()

        # Check for errors
        if dag_error[0]:
            if isinstance(dag_error[0], requests.exceptions.Timeout):
                yield "\n\nPlanning is taking longer than expected. Please try again with a simpler question."
            elif isinstance(dag_error[0], requests.exceptions.ConnectionError):
                yield "\n\nI couldn't reach the analysis engine. It may be restarting — please try again in a moment."
            else:
                yield f"\n\n⚠️ Error during planning: {dag_error[0]}"
            return

        r = dag_result[0]
        if r is None:
            yield "\n\n⚠️ No response from DAG generation."
            return

        if r.status_code >= 400:
            yield "\n\nI wasn't able to plan an approach for that question. Please try rephrasing or simplifying."
            return

        try:
            dag_data = r.json()
            num_nodes = dag_data.get("task_count", len(dag_data.get("tasks", [])))
        except (ValueError, KeyError):
            yield "\n\n⚠️ Unexpected response from DAG generation."
            return

        yield f"\nReady — executing {num_nodes} steps...\n\n"

        # ---- Phase 3: /execute/all (SSE stream) ----
        yield from self._execute_and_stream(job_id, num_nodes, headers)

    # ------------------------------------------------------------------
    # SSE consumer for /execute/all
    # ------------------------------------------------------------------
    def _execute_and_stream(
        self, job_id: str, total_nodes: int, headers: dict
    ) -> Generator[str, None, None]:

        # --- Connect and validate ---
        try:
            r = requests.post(
                f"{self.valves.orchestrator_url}/execute/all",
                json={"job_id": job_id},
                headers=headers,
                stream=True,
                timeout=(10, None),
            )
        except requests.exceptions.ConnectionError:
            yield "I couldn't reach the analysis engine. It may be restarting — please try again in a moment."
            return
        except Exception as e:
            yield f"⚠️ Error starting execution: {e}"
            return

        if r.status_code == 409:
            yield "That question is already being processed. Please wait for it to complete."
            return
        if r.status_code >= 400:
            yield f"⚠️ Execution failed (HTTP {r.status_code}). Please try again."
            return

        # --- Read SSE in a background thread, yield from main thread ---
        q = queue.Queue()

        def _sse_reader():
            """Reads SSE lines and puts parsed events on the queue."""
            try:
                event_type = None
                data_buffer = ""
                for raw_line in r.iter_lines(decode_unicode=True):
                    if raw_line is None:
                        continue
                    line = raw_line.strip() if raw_line else ""

                    if line == "":
                        if event_type and data_buffer:
                            q.put(("event", event_type, data_buffer))
                        event_type = None
                        data_buffer = ""
                        continue

                    if line.startswith("event:"):
                        event_type = line[6:].strip()
                    elif line.startswith("data:"):
                        data_buffer += line[5:].strip()

                # Flush remaining buffer
                if event_type and data_buffer:
                    q.put(("event", event_type, data_buffer))

                q.put(("done", None, None))
            except Exception as e:
                q.put(("error", str(e), None))
            finally:
                r.close()

        reader_thread = threading.Thread(target=_sse_reader, daemon=True)
        reader_thread.start()

        # --- Main thread: consume queue, yield to Open WebUI ---
        failed_nodes = []
        compiled_output = None
        compile_status = None

        while True:
            try:
                msg_type, field1, field2 = q.get(timeout=10)
            except queue.Empty:
                # No SSE event in 10s — yield invisible keepalive
                yield "\u200b"
                continue

            if msg_type == "error":
                yield from self._recover_from_disconnect(job_id, headers)
                return

            if msg_type == "done":
                break

            # msg_type == "event"
            event_type = field1
            data = field2

            # Yield progress messages
            yield from self._handle_sse_event(event_type, data, failed_nodes)

            # Capture pipeline_complete data
            if event_type == "pipeline_complete":
                try:
                    payload = json.loads(data)
                    compiled_output = payload.get("compiled_output", "")
                    compile_status = payload.get("compile_status", "complete")
                    failed_nodes_list = payload.get("failed_nodes", [])
                    if failed_nodes_list:
                        failed_nodes.extend(failed_nodes_list)
                except json.JSONDecodeError:
                    pass

        reader_thread.join(timeout=5)

        # ---- Render final output ----
        if compiled_output is not None and compiled_output:
            if compile_status == "partial" and failed_nodes:
                yield f"\n⚠️ **Partial results** — {len(failed_nodes)} of {total_nodes} steps could not be completed:\n"
                for fn in failed_nodes:
                    if isinstance(fn, dict):
                        title = fn.get("title", fn.get("node_key", "?"))
                        reason = fn.get("reason", "unknown")
                        yield f"- **{title}**: {reason}\n"
                    else:
                        yield f"- {fn}\n"
                yield "\n---\n\n"
                yield compiled_output
            else:
                yield compiled_output
        else:
            # Fallback: no pipeline_complete event — poll for result
            yield "\n⏳ Fetching final output...\n"
            time.sleep(3)
            try:
                sr = requests.get(
                    f"{self.valves.orchestrator_url}/exec/status/{job_id}",
                    headers=headers,
                    timeout=15,
                )
                if sr.status_code == 200:
                    status_data = sr.json()
                    fallback_output = status_data.get("compiled_output", "")
                    if fallback_output:
                        yield fallback_output
                    else:
                        yield "✅ All steps completed. No compiled output was returned — check `/exec status " + job_id + "` for details."
                else:
                    yield "✅ All steps completed. Use `/exec status " + job_id + "` to view results."
            except Exception:
                yield "✅ All steps completed. Use `/exec status " + job_id + "` to view results."

    # ------------------------------------------------------------------
    # Handle individual SSE events → yield progress messages
    # ------------------------------------------------------------------
    def _handle_sse_event(
        self, event_type: str, data: str, failed_nodes: list
    ) -> Generator[str, None, None]:
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            return

        if event_type == "node_start":
            node_key = payload.get("node_key", "?")
            title = payload.get("title", "")
            tool = payload.get("tool", "")
            yield f"🔄 Step {node_key}: {title} ({tool})...\n"

        elif event_type == "node_done":
            node_key = payload.get("node_key", "?")
            yield f"✅ Step {node_key} complete.\n"

        elif event_type == "node_failed":
            node_key = payload.get("node_key", "?")
            reason = payload.get("reason", "unknown")
            yield f"❌ Step {node_key} failed: {reason}\n"
            failed_nodes.append(payload)

        elif event_type == "blocked":
            node_key = payload.get("node_key", "?")
            blocked_by = payload.get("blocked_by", [])
            yield f"⏸️ Step {node_key} blocked (waiting on: {', '.join(blocked_by)})\n"

        elif event_type == "pipeline_complete":
            # Final output handled in _execute_and_stream after the loop
            pass

    # ------------------------------------------------------------------
    # Task 3: Recovery after SSE disconnect
    # ------------------------------------------------------------------
    def _recover_from_disconnect(
        self, job_id: str, headers: dict
    ) -> Generator[str, None, None]:
        yield "\n⏳ Connection interrupted — checking job status...\n"

        for attempt in range(3):
            time.sleep(5 if attempt == 0 else 10)
            try:
                r = requests.get(
                    f"{self.valves.orchestrator_url}/exec/status/{job_id}",
                    headers=headers,
                    timeout=15,
                )
                if r.status_code == 200:
                    data = r.json()
                    status = data.get("status", data.get("job_status", ""))
                    if status in ("completed", "done"):
                        compiled = data.get("compiled_output", "")
                        if compiled:
                            yield f"✅ Job completed successfully.\n\n"
                            yield compiled
                            return
                        else:
                            yield "✅ Job completed but no output was generated."
                            return
            except Exception:
                continue

        yield (
            f"⚠️ Connection to Scaffold Engine was lost during execution. "
            f"Job {job_id} may still be running. "
            f"You can check status later or try again."
        )

    # ------------------------------------------------------------------
    # Slash command dispatcher (returns a single string)
    # ------------------------------------------------------------------
    def _handle_command(self, msg: str) -> str:
        parts = msg.split(None, 2)
        cmd = parts[0].lower()

        try:
            if cmd == "/help":
                return self._help()

            elif cmd == "/idea":
                if len(parts) < 2:
                    return "Usage: /idea <description>"
                text = " ".join(parts[1:])
                r = requests.post(
                    f"{self.valves.orchestrator_url}/ideas",
                    json={"idea": text},
                    headers={"X-API-Key": self.valves.api_key},
                    timeout=1800,
                )
                return self._fmt(r)

            elif cmd == "/dag":
                if len(parts) < 2:
                    return "Usage: /dag <job_id>"
                r = requests.post(
                    f"{self.valves.orchestrator_url}/dag",
                    json={"job_id": parts[1]},
                    headers={"X-API-Key": self.valves.api_key},
                    timeout=1800,
                )
                return self._fmt(r)

            elif cmd == "/execute":
                if len(parts) < 2:
                    return "Usage: /execute <job_id>"
                r = requests.post(
                    f"{self.valves.orchestrator_url}/execute",
                    json={"job_id": parts[1], "skip_verify": False},
                    headers={"X-API-Key": self.valves.api_key},
                    timeout=1800,
                )
                return self._fmt(r)

            elif cmd == "/skip":
                if len(parts) < 3:
                    return "Usage: /skip <job_id> <node_key>"
                r = requests.post(
                    f"{self.valves.orchestrator_url}/skip",
                    json={"job_id": parts[1], "node_key": parts[2]},
                    headers={"X-API-Key": self.valves.api_key},
                    timeout=30,
                )
                return self._fmt(r)

            elif cmd == "/optimize":
                if len(parts) < 2:
                    return "Usage: /optimize <prompt text>"
                text = " ".join(parts[1:])
                r = requests.post(
                    f"{self.valves.orchestrator_url}/optimize",
                    json={"prompt": text, "skip_verify": False},
                    headers={"X-API-Key": self.valves.api_key},
                    timeout=1800,
                )
                return self._fmt(r)

            elif cmd == "/rag":
                if len(parts) < 2:
                    return "Usage: /rag <query>"
                text = " ".join(parts[1:])
                r = requests.post(
                    f"{self.valves.orchestrator_url}/rag",
                    json={"query": text, "top_k": 5},
                    headers={"X-API-Key": self.valves.api_key},
                    timeout=60,
                )
                return self._fmt(r)

            elif cmd == "/status":
                r = requests.get(
                    f"{self.valves.orchestrator_url}/status",
                    headers={"X-API-Key": self.valves.api_key},
                    timeout=10,
                )
                return self._fmt(r)

            else:
                return f"Unknown command: `{cmd}`\nType `/help` for available commands."

        except requests.exceptions.Timeout:
            return "⚠️ Request timed out. The orchestrator is still processing — check back shortly."
        except requests.exceptions.ConnectionError:
            return f"⚠️ Cannot reach orchestrator at {self.valves.orchestrator_url}. Is it running?"
        except Exception as e:
            return f"⚠️ Error: {e}"

    # ------------------------------------------------------------------
    # Helpers (unchanged from v1)
    # ------------------------------------------------------------------
    def _fmt(self, r: requests.Response) -> str:
        try:
            data = r.json()
        except Exception:
            return f"HTTP {r.status_code}: {r.text[:500]}"

        if r.status_code >= 400:
            return f"⚠️ Error {r.status_code}: {data.get('message') or data.get('detail') or r.text[:200]}"

        return f"```json\n{json.dumps(data, indent=2)}\n```"

    def _help(self) -> str:
        return """**Scaffold Router Commands**

| Command | Description |
|---|---|
| `/idea <text>` | Submit a new idea → refine → create job |
| `/dag <job_id>` | Generate DAG from refined idea |
| `/execute <job_id>` | Execute next pending DAG node |
| `/skip <job_id> <node_key>` | Skip a specific node |
| `/optimize <prompt>` | Optimize a prompt |
| `/rag <query>` | Query the knowledge base |
| `/status` | List active jobs |
| `/help` | Show this message |

Plain messages (no `/` prefix) automatically run the full analysis pipeline."""
