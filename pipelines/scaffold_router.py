"""
scaffold_router.py  --  Step 17 (v3 — triage + streaming auto-chain)
Open WebUI Pipeline: conversational triage before auto-chaining
/ideate → /dag → /execute/all for plain messages.

Slash commands still work:
  /idea <text>        -> POST /ideate  (manual, returns JSON)
  /dag <job_id>       -> POST /dag    (manual, returns JSON)
  /execute <job_id>   -> POST /execute
  /skip <job_id> <node_key> -> POST /skip
  /optimize <text>    -> POST /optimize
  /rag <query>        -> POST /rag
  /confirm <job_id>   -> POST /ideate/confirm (Phase 2)
  /go or /run         -> Synthesize conversation → auto-chain
  /status             -> GET  /status
  /help               -> show command list

Non-command messages trigger a conversational triage phase via a lightweight
model (qwen2.5:7b). The user discusses scope and goals, then types /go or
/run to launch the full Scaffold Engine pipeline.
"""

from typing import List, Optional, Generator, Iterator
import requests
import json
import time
import re
import threading
import queue
from pydantic import BaseModel, Field


class Pipeline:
    class Valves(BaseModel):
        api_key: str = ""
        orchestrator_url: str = "http://scaffold-orchestrator:8000"
        dag_timeout: int = 3600          # seconds to wait for DAG generation
        keepalive_interval: int = 10    # seconds between keepalive dots
        triage_model: str = "qwen3:4b"
        triage_timeout: int = 3600       # seconds to wait for triage model response
        ollama_url: str = "http://172.18.0.1:11434"

    def __init__(self):
        self.id = "scaffold_router"
        self.name = "Scaffold Router"
        self.valves = self.Valves()

    # ------------------------------------------------------------------
    # Triage: lightweight conversational phase before workflow launch
    # ------------------------------------------------------------------
    TRIAGE_SYSTEM_PROMPT = """You are a hands-on project planning assistant for Scaffold Engine. Respond ONLY in English.
The user has an idea they want to build. Your job is to actively help them
shape it into a clear, actionable scope — not just ask questions.

How to help:
- If the user provides a document, file, or specification, treat its content
  as primary project context. Do NOT ask the user to re-explain what is already
  in the document. Reference the document content directly. If the document
  already defines what is being built, constraints, and success criteria,
  summarize the scope from the document and suggest /go immediately.
- When the user describes something broad, break it into concrete options.
  Present 2-3 approaches with brief pros and cons for each.
- Make recommendations. Say which option you think best fits their stated goals
  and why.
- When the user picks a direction, help refine it further. Suggest specific
  components, technologies, or steps that would be involved.
- If something is ambiguous, propose a sensible default and ask if it works:
  "I'd suggest X because Y — does that work for you?"
- Keep responses focused and concise. No walls of text.
- One topic per response. Don't try to resolve everything at once.

Before suggesting /go, make sure these details are nailed down:
- WHAT specifically is being built (not just a category like "game server" —
  which game? which server software? what mods or plugins?)
- WHAT hardware or infrastructure it runs on (OS, CPU, RAM, storage, network)
- WHAT the success criteria are (what does "done" look like?)
- ANY key constraints (budget, timeline, existing equipment, skill level)

Do NOT suggest /go until the idea is specific enough that someone else could
start building it from the description alone.

Your goal is to collaboratively arrive at a specific, well-defined idea that
includes: what is being built, the key components, and the desired outcome.

When the scope is solid, write a clear 2-4 sentence summary of the final idea
and tell the user: "Type `/go` when you're ready to launch."

Do NOT execute anything. Do NOT invent requirements the user hasn't agreed to."""

    @staticmethod
    def _extract_text(content) -> str:
        """Extract plain text from message content (string or multimodal list)."""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for c in content:
                if not isinstance(c, dict):
                    continue
                if c.get("type") == "text" and c.get("text"):
                    parts.append(c["text"])
                elif c.get("type") in ("file", "document") and c.get("text"):
                    parts.append(c["text"])
                elif c.get("type") in ("file", "document") and c.get("content"):
                    parts.append(c["content"])
                elif c.get("text") and c.get("type") not in ("image",):
                    parts.append(c["text"])
            return " ".join(parts)
        return str(content) if content else ""

    def _clean_messages(self, messages: List[dict]) -> List[dict]:
        """Strip zero-width spaces and normalize content to plain text strings."""
        cleaned = []
        for m in messages:
            text = self._extract_text(m.get("content", ""))
            text = text.replace("\u200b", "").strip()
            if text:
                cleaned.append({"role": m["role"], "content": text})
        return cleaned

    def _call_triage(self, messages: List[dict]) -> str:
        """Call the lightweight triage model for conversational clarification."""
        clean = self._clean_messages(messages)
        payload = {
            "model": self.valves.triage_model,
            "messages": [
                {"role": "system", "content": self.TRIAGE_SYSTEM_PROMPT}
            ] + clean,
            "stream": False,
        }
        try:
            r = requests.post(
                f"{self.valves.ollama_url}/v1/chat/completions",
                json=payload,
                timeout=self.valves.triage_timeout,
            )
            if r.status_code == 200:
                return r.json()["choices"][0]["message"]["content"]
            else:
                return f"⚠️ Triage model error (HTTP {r.status_code}). You can skip triage by typing `/go` to launch directly."
        except requests.exceptions.ConnectionError:
            return "⚠️ Cannot reach Ollama for triage. You can skip triage by typing `/go` to launch directly."
        except Exception as e:
            return f"⚠️ Triage error: {e}. You can skip triage by typing `/go` to launch directly."

    def _synthesize_idea(self, messages: List[dict]) -> str:
        """Use the triage model to extract the final agreed-upon idea from the conversation."""
        clean_messages = self._clean_messages(messages)

        if not any(m["role"] == "user" for m in clean_messages):
            return ""

        # Build a plain-text transcript instead of replaying chat turns.
        # This avoids confusing the model with its own prior assistant outputs.
        transcript_lines = []
        for m in clean_messages:
            label = "User" if m["role"] == "user" else "Assistant"
            transcript_lines.append(f"{label}: {m['content']}")
        transcript = "\n\n".join(transcript_lines)

        synthesis_prompt = {
            "model": self.valves.triage_model,
            "messages": [
                {"role": "system", "content": (
                    "You extract a project description from a planning conversation. "
                    "Respond ONLY in English. "
                    "Write 3-6 plain sentences describing what will be built, using only "
                    "details the user confirmed. Be specific: include technologies, "
                    "components, architecture, and goals. Write as a direct project "
                    "description — not 'the user wants' but 'Build a...' or 'Set up a...'. "
                    "No preamble, no markdown, no labels, no meta-text like 'type /go'."
                )},
                {"role": "user", "content": (
                    "Here is the planning conversation. Extract the final agreed-upon plan:\n\n"
                    f"{transcript}"
                )}
            ],
            "stream": False,
        }
        try:
            r = requests.post(
                f"{self.valves.ollama_url}/v1/chat/completions",
                json=synthesis_prompt,
                timeout=self.valves.triage_timeout,
            )
            if r.status_code == 200:
                raw = r.json()["choices"][0]["message"]["content"].strip()
                print(f"[scaffold_router] Synthesis raw ({len(raw)} chars): {raw[:200]}")
                # Strip think/thinking tags the model may emit
                cleaned = re.sub(
                    r"<think(?:ing)?>.*?</think(?:ing)?>",
                    "", raw, flags=re.DOTALL
                ).strip()
                if cleaned:
                    return cleaned
                print(f"[scaffold_router] Synthesis cleaned to empty, using fallback")
            else:
                print(f"[scaffold_router] Synthesis HTTP {r.status_code}: {r.text[:300]}")
        except Exception as e:
            print(f"[scaffold_router] Synthesis error: {e}")

        # Fallback: concatenate user messages only
        user_texts = [m["content"] for m in clean_messages if m["role"] == "user"]
        fallback = " ".join(user_texts)
        print(f"[scaffold_router] Synthesis fallback ({len(fallback)} chars): {fallback[:200]}")
        return fallback

    # ------------------------------------------------------------------
    # Main entry point — sync GENERATOR (yields chunks)
    # ------------------------------------------------------------------
    def pipe(
        self,
        user_message: str,
        model_id: str,
        messages: List[dict],
        body: dict,
    ) -> Generator[str, None, None]:
        msg = user_message.strip()
        # Strip Open WebUI context injection to find the real user command
        import re as _re
        _ctx_match = _re.search(r'.*</context>\s*\n*(.*)', msg, _re.DOTALL)
        if _ctx_match:
            msg = _ctx_match.group(1).strip()

        # Force streaming — pipe() always yields chunks
        body["stream"] = True

        # --- /go or /run: synthesize conversation and launch pipeline ---
        if msg.lower() == "/go" or msg.lower() == "/run" or msg.lower().startswith("/go ") or msg.lower().startswith("/run "):
            # Build chat history (exclude the /go itself)
            chat_history = [m for m in messages
                            if not (m["role"] == "user"
                                    and isinstance(m.get("content"), str)
                                    and (m["content"].strip().lower().startswith("/go") or m["content"].strip().lower().startswith("/run")))]
            
            user_msgs_in_history = [m for m in chat_history if m["role"] == "user"]
            if not user_msgs_in_history:
                yield "Nothing to launch yet — describe your idea first, then type `/go`."
                return
            
            # Debug: show what we're working with
            yield f"📋 Synthesizing from {len(user_msgs_in_history)} user message(s)...\n\n"
            
            synthesized = self._synthesize_idea(chat_history)
            
            # Guard: don't launch with empty idea
            if not synthesized or len(synthesized.strip()) < 10:
                yield (
                    "⚠️ Synthesis produced an empty or too-short result. "
                    "Here's what I captured from your messages:\n\n"
                )
                for i, m in enumerate(user_msgs_in_history, 1):
                    content = m.get("content", "")
                    if isinstance(content, list):
                        content = " ".join(
                            c.get("text", "") for c in content if c.get("type") == "text"
                        )
                    yield f"{i}. {content[:200]}\n"
                yield "\nPlease try rephrasing your idea in a single message, then type `/go`."
                return
            
            yield f"> **Launching with:** {synthesized}\n\n---\n\n"
            yield from self._auto_chain(synthesized)
            return

        # --- /execute <job_id>: stream full execution ---
        if msg.lower().startswith("/execute"):
            parts = msg.split()
            if len(parts) < 2:
                yield "Usage: `/execute <job_id>`"
                return
            job_id = parts[1]
            headers = {"X-API-Key": self.valves.api_key}
            yield f"Executing all nodes for job `{job_id}`...\n\n"
            yield from self._execute_and_stream(job_id, 0, headers)
            return

        # --- /confirm <job_id> [feedback]: Phase 2 → auto-chain to /dag → /execute/all ---
        if msg.lower().startswith("/confirm"):
            parts = msg.split(None, 2)
            if len(parts) < 2:
                yield "Usage: `/confirm <job_id> [feedback]`"
                return
            job_id = parts[1]
            headers = {"X-API-Key": self.valves.api_key}
            payload = {"job_id": job_id}
            if len(parts) > 2:
                payload["feedback"] = parts[2]

            yield "🔬 Starting research and knowledge ingestion — this may take several minutes on CPU...\n\n"

            # Phase 2: /ideate/confirm (long-running — ~10 min on CPU)
            confirm_result = [None]
            confirm_error = [None]

            def _call_confirm():
                try:
                    confirm_result[0] = requests.post(
                        f"{self.valves.orchestrator_url}/ideate/confirm",
                        json=payload,
                        headers=headers,
                        timeout=3600,
                    )
                except Exception as e:
                    confirm_error[0] = e

            t = threading.Thread(target=_call_confirm, daemon=True)
            t.start()
            while t.is_alive():
                time.sleep(self.valves.keepalive_interval)
                if t.is_alive():
                    yield "\u200b"
            t.join()

            if confirm_error[0]:
                yield f"\n⚠️ Research phase error: {confirm_error[0]}"
                return

            r = confirm_result[0]
            if r is None:
                yield "\n⚠️ No response from research phase."
                return
            if r.status_code >= 400:
                try:
                    err = r.json().get("message") or r.json().get("detail") or r.text[:200]
                except Exception:
                    err = r.text[:200]
                yield f"\n⚠️ Research phase failed: {err}"
                return

            yield "\n✅ Research complete — generating execution plan...\n\n"

            # DAG generation
            dag_result = [None]
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
            while t.is_alive():
                time.sleep(self.valves.keepalive_interval)
                if t.is_alive():
                    yield "\u200b"
            t.join()

            if dag_error[0]:
                yield f"\n⚠️ DAG generation error: {dag_error[0]}"
                return

            r = dag_result[0]
            if r is None:
                yield "\n⚠️ No response from DAG generation."
                return
            if r.status_code >= 400:
                yield f"\n⚠️ DAG generation failed (HTTP {r.status_code})."
                return

            try:
                dag_data = r.json()
                num_nodes = dag_data.get("task_count", len(dag_data.get("tasks", [])))
            except (ValueError, KeyError):
                yield "\n⚠️ Unexpected response from DAG generation."
                return

            yield f"📋 Execution plan ready — running {num_nodes} steps...\n\n"

            # Execute all nodes
            yield from self._execute_and_stream(job_id, num_nodes, headers)
            return

        # --- /results <job_id>: fetch and display completed job output ---
        if msg.lower().startswith("/results"):
            parts = msg.split()
            if len(parts) < 2:
                yield "Usage: `/results <job_id>`"
                return
            job_id = parts[1]
            headers = {"X-API-Key": self.valves.api_key}

            try:
                r = requests.get(
                    f"{self.valves.orchestrator_url}/exec/status/{job_id}",
                    headers=headers,
                    timeout=30,
                )
            except requests.exceptions.ConnectionError:
                yield f"⚠️ Cannot reach orchestrator at {self.valves.orchestrator_url}."
                return
            except Exception as e:
                yield f"⚠️ Error: {e}"
                return

            if r.status_code == 404:
                yield f"⚠️ Job `{job_id}` not found."
                return
            if r.status_code >= 400:
                yield f"⚠️ Error fetching job (HTTP {r.status_code})."
                return

            try:
                data = r.json()
            except ValueError:
                yield "⚠️ Unexpected response from orchestrator."
                return

            status = data.get("status", data.get("job_status", "unknown"))
            yield f"**Job:** `{job_id}`\n"
            yield f"**Status:** {status}\n\n"

            # Show node results if available
            nodes = data.get("nodes", data.get("tasks", []))
            if nodes:
                yield "---\n\n"
                for node in nodes:
                    if isinstance(node, dict):
                        key = node.get("node_key", node.get("key", ""))
                        title = node.get("title", "")
                        node_status = node.get("status", "")
                        yield f"**{key}: {title}** ({node_status})\n\n"
                        output = node.get("output", "")
                        if output:
                            yield f"{output}\n\n"
                        yield "---\n\n"

            # Show compiled output
            compiled = data.get("compiled_output", "")
            if compiled:
                yield "## Final Output\n\n"
                yield compiled
            elif status in ("completed", "done"):
                yield "Job completed but no compiled output was stored."
            elif status == "executing":
                yield "Job is still running — check back shortly."
            elif status == "awaiting_confirmation":
                yield f"Job is waiting for confirmation. Type `/confirm {job_id}` to proceed."
            else:
                yield f"Job is in `{status}` state."
            return

        # --- Other slash commands: yield single response, return ---
        if msg.startswith("/"):
            yield self._handle_command(msg)
            return

        # --- Triage: conversational phase via lightweight model ---
        # Filter messages to only include user/assistant turns (no system)
        chat_messages = [m for m in messages if m["role"] in ("user", "assistant")]
        triage_response = self._call_triage(chat_messages)
        yield triage_response

    # ------------------------------------------------------------------
    # Auto-chain: full Scaffold Engine flow from a plain message
    # ------------------------------------------------------------------
    def _auto_chain(self, message: str) -> Generator[str, None, None]:
        headers = {"X-API-Key": self.valves.api_key}

        # ---- Phase 1: /ideate (can take ~500s, yield keepalive dots) ----
        yield "Let me think about this"

        ideas_result = [None]
        ideas_error = [None]

        def _call_ideas():
            try:
                ideas_result[0] = requests.post(
                    f"{self.valves.orchestrator_url}/ideate",
                    json={"idea": message},
                    headers=headers,
                    timeout=3600,
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

        # ---- Check for ideation workflow confirmation ----
        if ideas_data.get("status") == "awaiting_confirmation":
            feasibility = ideas_data.get("feasibility", {})
            is_feasible = feasibility.get("feasible", True)
            confidence = feasibility.get("confidence", 0)

            # Brief summary
            description = brief.get("description", "") if isinstance(brief, dict) else ""
            if description:
                yield f"{description}\n\n"

            yield f"**Feasibility:** {chr(9989) if is_feasible else chr(9888)} ({confidence:.0%} confidence)\n\n"

            risks = feasibility.get("risks", [])
            if risks:
                yield "**Risks to consider:**\n"
                for risk in risks:
                    yield f"- {risk}\n"
                yield "\n"

            clarifications = feasibility.get("clarifications_needed", [])
            if clarifications:
                yield "**A few things that could be more specific:**\n"
                for c in clarifications:
                    yield f"- **{c}**\n"
                yield "\n"

            yield "---\n\n"
            yield "**What would you like to do?**\n\n"
            yield f"- **Proceed as-is:** Type `/confirm {job_id}`\n"
            yield f"- **Proceed with changes:** Type `/confirm {job_id}` followed by your adjustments — for example:\n"
            yield f"  `/confirm {job_id} focus on Docker networking only, skip the storage setup`\n"
            yield f"- **Start over:** Describe a new idea and type `/go` again\n"
            return

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
                    if not compiled_output and payload.get("compiled_output_available"):
                        compiled_output = self._poll_compiled_output(job_id, headers)
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
            reason = payload.get("error") or payload.get("verification_reason") or "unknown"
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
    def _poll_compiled_output(self, job_id: str, headers: dict) -> str:
        """Fetch compiled_output via status endpoint when too large for SSE."""
        try:
            r = requests.get(
                f"{self.valves.orchestrator_url}/exec/status/{job_id}",
                headers=headers,
                timeout=15,
            )
            if r.status_code == 200:
                return r.json().get("compiled_output", "")
        except Exception:
            pass
        return ""

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
                    f"{self.valves.orchestrator_url}/ideate",
                    json={"idea": text},
                    headers={"X-API-Key": self.valves.api_key},
                    timeout=3600,
                )
                return self._fmt(r)

            elif cmd == "/dag":
                if len(parts) < 2:
                    return "Usage: /dag <job_id>"
                r = requests.post(
                    f"{self.valves.orchestrator_url}/dag",
                    json={"job_id": parts[1]},
                    headers={"X-API-Key": self.valves.api_key},
                    timeout=3600,
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
                    timeout=3600,
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
    # Helpers
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
| *(plain message)* | Discuss your idea with the triage assistant |
| `/go` or `/run` | Launch the pipeline with your discussed idea |
| `/idea <text>` | Submit idea directly (skip triage) |
| `/dag <job_id>` | Generate DAG from refined idea |
| `/execute <job_id>` | Execute next pending DAG node |
| `/confirm <job_id>` | Confirm ideation Phase 2 (research) |
| `/results <job_id>` | View a completed job's output |
| `/skip <job_id> <node_key>` | Skip a specific node |
| `/optimize <prompt>` | Optimize a prompt |
| `/rag <query>` | Query the knowledge base |
| `/status` | List active jobs |
| `/help` | Show this message |

**Workflow:** Describe your idea → discuss scope with the assistant → `/go` → review feasibility → `/confirm` → execution."""
