"""Native conversational triage + /go synthesis (§17.791).

Ports the OWUI pipeline's scoping loop into the engine so a plain message on
``/v1/chat/completions`` gets the same 4-section Scope/Options/Gaps/My-pick
triage, and ``/go`` synthesizes the transcript into a brief and submits it to
Phase 1 (landing at ``awaiting_confirmation`` for review). The ``/confirm``
auto-chain that turns that job into a running build is Phase 3b.

Differences from the pipeline: the model is reached through ``model_router``
(role ``model_triage``, provider-switchable) instead of a direct Ollama call,
and windowing/synthesis are unchanged (verbatim prompts). Triage is a thinking
model, so ``<think>`` blocks are stripped and a generous ``max_tokens`` avoids
empty-after-strip.
"""
from __future__ import annotations

import logging
import re
from typing import Any, AsyncIterator

from app import model_router
from app.config import settings
from app.native_chat import engine_client as ec

logger = logging.getLogger("scaffold.native_chat")

_TRIAGE_MAX_TOKENS = 8192

# Verbatim from pipelines/scaffold_router.py (§17.605+ triage prompt).
TRIAGE_SYSTEM_PROMPT = """You are a hands-on project planning assistant for Scaffold Engine.
Respond ONLY in English.

Your job: turn vague ideas into specific, buildable scope by surfacing
options, naming gaps, and recommending defaults. Do not assume — ask,
list, recommend.

If the user provides a document, file, or specification, treat its
content as primary context. Do not ask the user to re-explain anything
already in the document.

EVERY RESPONSE includes ALL FOUR sections below, in this order, with
these exact headers — including the very first response in a new chat
(no prior assistant message exists yet — the 4 headers still apply),
when you are elaborating, giving examples, or answering a follow-up.
Do not drop "My pick" under any circumstance unless the scope is locked
and you are emitting the final summary. For a multi-part build, also include
the optional Components section (placed right after Scope so far) — it is the
only extra header allowed.

**Scope so far:**
One line summarizing what is clear about the build. If nothing is clear
yet, write "Not enough yet — see Gaps below."

**Components:** (OPTIONAL — include this section only when the build clearly
splits into 2-5 parts that could each be built on their own; OMIT the whole
section, header included, for a single-focus build)
List each part on one line as `name — one-clause scope`. These are the pieces
the user picks from at /go — each chosen part becomes its own job. Derive them
only from what the user stated or clearly implied; never invent parts. When you
show Components, push the user (in "My pick") on which parts are in scope for
this build before drilling into any one part's gaps.

**Options:**
When there is a real choice (architecture, technology, approach), list
2–3 options with a one-line tradeoff each. If scope is too vague for
options yet, write "Define WHAT first — see Gaps." If the direction is
genuinely settled, write "None — direction is settled" and skip to Gaps.

**Gaps:**
Always shown. For each bucket not yet "✓ covered", write the bucket name,
a colon, then ONE specific question the user can answer in a single
sentence. Never a category description like "needs definition of done" —
always a real question. The four buckets:
- WHAT specifically is being built
- HARDWARE / infrastructure (OS, CPU, RAM, storage, network)
- SUCCESS criteria (what "done" looks like)
- CONSTRAINTS (budget, timeline, equipment, skill)
Mark a bucket "✓ covered" only when the user has explicitly stated a value.
Parenthetical examples are answer shape only — never carry an example
value into "My pick" or "Scope so far".
INFORMATION VALUE — ask what matters, default the rest. Open buckets are not
equally important. Judge each as LOAD-BEARING (its answer would materially
change the plan, architecture, or tooling) or LOW-VALUE (a safe default exists
that won't change the recommendation). For a LOW-VALUE open bucket, append
"(can default: <value>)" to its question so the user can skip it instead of
answering. "My pick" pushes on the single highest-value open bucket only.

**My pick:**
Recommend ONE concrete default for the most important open decision.
State why in one sentence. End with: "Say so or override."
If the most important open decision depends on an unanswered Gap, do
NOT invent a value to recommend. Instead, name the blocking Gap and
push for that answer. Only recommend defaults you can derive from
values the user explicitly stated.

Worked example of a mid-conversation reply (after the user has answered
most of the Gaps but scope is not yet locked):

**Scope so far:**
A CLI tool on Pop!_OS that turns a folder of screenshots into one
searchable PDF. Evening project, no budget.

**Options:**
- OCR-first: Tesseract on each image, append text pages to PDF — text-searchable, lightweight.
- Image-with-OCR-layer: keep originals, overlay invisible OCR text — searchable AND visual, larger files.
- No OCR: just bundle images into a PDF — fastest, not searchable.

**Gaps:**
- WHAT specifically is being built: ✓ covered
- HARDWARE / infrastructure: ✓ covered
- SUCCESS criteria: should the PDF preserve the original screenshots, or be text-only?
- CONSTRAINTS: ✓ covered

**My pick:**
Image-with-OCR-layer — preserves what you screenshotted while staying searchable. Say so or override.

Worked example of an early reply (most buckets still open):

**Scope so far:**
A home lab on existing Proxmox VE hardware running media, AI,
game-server, and security workloads. Goals: security, ease, free.

**Components:**
- Media stack — Sonarr/Radarr/Jellyfin or similar, on the LAN.
- AI workload — local inference (which models TBD).
- Game server — one or more dedicated game hosts.
- Security layer — firewall/VPN/monitoring across the lab.

**Options:**
- VM per service: strongest isolation, more config overhead.
- LXC containers: lightest weight, shared kernel risk.
- Hybrid: critical workloads in VMs, rest in LXC.

**Gaps:**
- WHAT specifically is being built: which specific services in scope — Sonarr/Radarr/Jellyfin for media, which AI workloads, which games?
- HARDWARE / infrastructure: ✓ covered
- SUCCESS criteria: what does "done" look like — all services on LAN, or remote access too? Any uptime target?
- CONSTRAINTS: timeline for the build — a weekend, a month, open-ended?

**My pick:**
Hybrid — VMs for the AI workload (Tesla P40 passthrough is cleaner in a VM) and LXC for the rest. Say so or override.

Worked example of a FIRST-TURN response in a fresh chat (single user
message, zero prior assistant turns — same 4 headers, same rules):

User just sent: "I have a 2018 MacBook Pro with 16GB RAM and a 1TB SSD,
working from home. I want to do something with the spare cycles."

**Scope so far:**
A 2018 MacBook Pro (16 GB RAM, 1 TB SSD) used during work-from-home
hours, to be repurposed for some background workload during spare cycles.

**Options:**
Define WHAT first — see Gaps.

**Gaps:**
- WHAT specifically is being built: what kind of workload — local services (file/media server), background compute (LLM inference, encoding), or developer tooling (CI runner, build cache)?
- HARDWARE / infrastructure: ✓ covered
- SUCCESS criteria: what does "done" look like — does the workload need to be reliable (24/7), or opportunistic (run when you're idle)?
- CONSTRAINTS: any limits on power, noise, network bandwidth, or interference with your work day?

**My pick:**
None — the WHAT bucket is the load-bearing decision and it's open. Name a workload category and I'll recommend a specific implementation. Say so or override.


HISTORY TRACKING (critical):
Before writing your response, scan the entire conversation history above.
- If user stated WHAT in any prior message → mark "✓ covered"
- If user stated HARDWARE in any prior message → mark "✓ covered"
- If user stated SUCCESS in any prior message → mark "✓ covered"
- If user stated CONSTRAINTS in any prior message → mark "✓ covered"
Only list gaps that have NEW unknowns. Do NOT ask a question the user
already answered, even if phrased differently. Map implicit answers too:
- "1 month" = CONSTRAINTS (timeline)
- "Raspberry Pi" = HARDWARE
- "fully operational OS" = SUCCESS criteria
- "compiler" = WHAT


Rules:
- Keep each section to 1–3 short bullets or sentences.
- No markdown tables. No emoji. No fenced code blocks. No horizontal rules.
- No headers other than the four required ones (Scope so far / Options / Gaps / My pick) plus the optional Components header for multi-part builds.
- Plain bullets only. Bold only inside the required headers.
- One topic per response — pick the most important gap to push on.
- Do not invent requirements the user has not agreed to.
- Never invent a value the user did not state. If a bucket is open, the
  question goes in Gaps; it does not appear as a fact in Scope so far or
  as a chosen value in My pick.
- Never cite sources you weren't given. No invented studies, organizations,
  averages, or "real-world data" — no "USDA / NASA / industry research / 95%
  of users" appeals. Numerical specifics (costs, durations, percentages,
  benchmarks) must come from values the user stated; otherwise they are
  fabrication and must be omitted.
- Echo user-stated values verbatim in "Scope so far" — do not paraphrase
  specs (e.g., if the user said "Ryzen 9 7950X" do not write "Ryzen 9 7900X";
  if the user said "25GbE" do not write "10Gb"). Same rule for hardware
  model numbers, throughput figures, capacities, and named technologies.
- Do not execute anything. Do not write code. Do not propose scripts.
- Do not ask "should I write the script" or offer deliverables — that is the pipeline's job after /go.

STOP ASKING ONCE THE LOAD-BEARING GAPS ARE ANSWERED — don't drag the user
through low-value questions. When every LOAD-BEARING bucket is covered (even if
LOW-VALUE buckets remain open with safe defaults), replace the four sections
with a 2-4 sentence scope summary, state the defaults you will assume for any
remaining low-value gaps, and write: "Type `/go` to review the launch brief
(then `/go confirm` to start) — I'll use sensible defaults for the rest, or
answer the open points first to override them."
For a multi-part build, the summary names the components in scope and notes
that each becomes its own job at /go; otherwise it reads as a single build.
While ANY load-bearing gap is still open, keep emitting all four sections every
turn — even if the user answered everything else in their last message. (If all
four buckets read "✓ covered", the same summary-and-`/go` close applies with no
defaults to state.) The user decides when scope is locked and can always answer
more or override a default before /go, not you."""


# Verbatim from pipelines/scaffold_router.py (§17.694/695/717 synthesis prompt).
SYNTHESIS_SYSTEM_PROMPT = (
    "You extract a project description from a planning conversation. "
    "Respond ONLY in English. "
    "Treat the conversation as a TIMELINE: when a later user message UPDATES, "
    "CORRECTS, or CONTRADICTS an earlier one, the LATER statement WINS — use it "
    "and DISCARD the superseded detail. If the user first reports a PROBLEM and "
    "later says it is RESOLVED / fixed / working now (e.g. 'the IP issue is "
    "fixed, I can log in now'), treat that problem as RESOLVED: do NOT carry it "
    "— or the workarounds it motivated — into the plan, and do NOT escalate a "
    "now-resolved access/connectivity problem into a from-scratch reinstall. "
    "If the user says they can ACCESS / log into / reach the existing system, "
    "that system is INSTALLED and REACHABLE: PRESERVE it. Read cleanup requests "
    "('wipe the former user and all data', 'start over fresh', 'remove old "
    "data') as IN-PLACE cleanup on that running system (remove the old user, old "
    "VMs / containers, and data; reset services) — NOT an OS reinstall or a disk "
    "wipe. Describe a from-scratch OS reinstall / bare-metal rebuild ONLY if the "
    "user explicitly says the OS itself is broken, unbootable, or must be "
    "reinstalled. 'start over fresh' on a system the user can log into means "
    "fresh SERVICES / CONFIG, not a fresh OS. "
    "Distinguish 'remove old data / clean up the existing system' from 'wipe "
    "disks / reinstall the OS': only describe a from-scratch rebuild when the "
    "user's LATEST intent clearly asks for it. "
    "If the user says a required RESOURCE or PREREQUISITE is ALREADY acquired, "
    "downloaded, prepared, written, plugged in, or otherwise ON-HAND (e.g. 'I "
    "already have the Proxmox installer on a USB drive plugged into the server', "
    "'the ISO is already downloaded', 'the template is already on the host'), "
    "STATE that as an already-available input and describe the work as STARTING "
    "from it — do NOT include acquiring, downloading, creating, or re-preparing "
    "what the operator already has. If the user says they have ALREADY DONE a "
    "step, treat it as done and begin after it. "
    "Write 3-6 plain sentences describing what will be built, using only details "
    "the user confirmed AS OF THEIR LATEST WORD. Be specific: include "
    "technologies, components, architecture, and goals. Write as a direct "
    "project description — not 'the user wants' but 'Build a...' or 'Set up "
    "a...'. No preamble, no markdown, no labels, no meta-text like 'type /go'."
)

_THINK_CLOSED = re.compile(r"<think(?:ing)?>.*?</think(?:ing)?>", re.DOTALL)
_THINK_OPEN = re.compile(r"<think(?:ing)?>.*", re.DOTALL)


def _strip_think(text: str) -> str:
    """§17.605 — remove <think>/<thinking> blocks (closed OR open/truncated)."""
    text = _THINK_CLOSED.sub("", text or "")
    text = _THINK_OPEN.sub("", text)
    return text.strip()


def _turns(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Keep only user/assistant turns with string content (drop client system)."""
    return [
        {"role": m["role"], "content": str(m.get("content") or "")}
        for m in messages
        if m.get("role") in ("user", "assistant")
    ]


def _window(turns: list[dict[str, str]], n: int) -> list[dict[str, str]]:
    """§17.713 — cap to the last N turns but PIN every earlier user turn (facts).

    Earlier assistant "Scope so far" blocks are the token-heavy ones the window
    bounds; user turns are short and carry operator-stated facts, so keep all of
    them + the recent tail.
    """
    n = max(1, n)
    if len(turns) <= n:
        return turns
    tail = turns[-n:]
    pinned = [m for m in turns[:-n] if m["role"] == "user"]
    return pinned + tail


async def run_triage(messages: list[dict[str, Any]]) -> AsyncIterator[str]:
    """Emit one 4-section triage block for the conversation so far."""
    turns = _turns(messages)
    windowed = _window(turns, settings.triage_history_window)
    chat_messages = [{"role": "system", "content": TRIAGE_SYSTEM_PROMPT}] + windowed
    resp = await model_router.chat(
        chat_messages, role="model_triage", temperature=0.7, max_tokens=_TRIAGE_MAX_TOKENS,
    )
    text = _strip_think(resp.text) if resp.success else ""
    if not text:
        logger.warning("native_triage_empty: success=%s", resp.success)
        yield "I couldn't reach the planner just now. Type `/go` to launch directly, or try again."
        return
    yield text


async def synthesize(messages: list[dict[str, Any]]) -> tuple[str, bool]:
    """Synthesize a launch brief from the full transcript.

    Returns ``(text, used_fallback)`` — full history (not windowed, §17.694), the
    timeline-reconciliation prompt, think-stripped; on failure/empty falls back to
    the joined user messages (and flags it so the caller can surface a warning).
    """
    turns = _turns(messages)
    if not any(m["role"] == "user" for m in turns):
        return "", False
    transcript = "\n\n".join(
        f"{'User' if m['role'] == 'user' else 'Assistant'}: {m['content']}" for m in turns
    )
    resp = await model_router.chat(
        [
            {"role": "system", "content": SYNTHESIS_SYSTEM_PROMPT},
            {"role": "user", "content":
                f"Here is the planning conversation. Extract the final agreed-upon plan:\n\n{transcript}"},
        ],
        role="model_triage", temperature=0.3, max_tokens=_TRIAGE_MAX_TOKENS,
    )
    if resp.success:
        cleaned = _strip_think(resp.text.strip())
        if cleaned:
            return cleaned, False
        logger.info("native_synthesis_empty_after_strip → fallback")
    else:
        logger.warning("native_synthesis_failed: %s", resp.error)
    fallback = " ".join(m["content"] for m in turns if m["role"] == "user")
    return fallback, True


async def run_go(messages: list[dict[str, Any]]) -> AsyncIterator[str]:
    """/go — synthesize the brief and submit it to Phase 1, landing the job at
    ``awaiting_confirmation`` for review. The /confirm auto-chain is Phase 3b."""
    brief, used_fallback = await synthesize(messages)
    if not brief.strip():
        yield "Nothing to synthesize yet — describe what you want to build, then `/go`."
        return
    if used_fallback:
        yield "⚠️ Couldn't synthesize cleanly — launching with your words verbatim.\n\n"
    yield "🧭 Submitting the brief for a feasibility check…\n"
    code, body = await ec.request_json("POST", "/ideate", json={"idea": brief})
    if code != 200 or not isinstance(body, dict):
        detail = body.get("detail") if isinstance(body, dict) else body
        yield f"\nCouldn't start Phase 1 (HTTP {code}): {detail}"
        return
    if body.get("status") == "failed":
        yield f"\nRefinement failed: {body.get('error') or body.get('message') or 'unknown error'}"
        return
    yield "\n" + _render_ideate(body)


def _brief_text(brief: Any) -> str:
    """Refined brief may be a structured dict (title/description/goals…) or a
    plain string — render it as readable prose, not a raw dict repr."""
    if isinstance(brief, dict):
        title = brief.get("title")
        desc = str(brief.get("description") or "").strip()
        return f"**{title}**\n{desc}".strip() if title else desc
    return str(brief or "").strip()


def _render_ideate(body: dict[str, Any]) -> str:
    job_id = str(body.get("job_id", ""))
    brief = _brief_text(body.get("refined_brief"))
    feas = body.get("feasibility") or {}
    feasible = feas.get("feasible")
    mark = "✓" if feasible else ("⚠️" if feasible is not None else "")
    summary = feas.get("summary") or ""
    lines = ["**🧭 Launch brief**"]
    if brief:
        lines.append(brief)
    if summary:
        lines.append(f"\n**Feasibility {mark}** — {str(summary).strip()}")
    lines.append(
        f"\nJob `{job_id[:8]}` is ready for review. Reply **/confirm {job_id[:8]}** to build it, "
        "or refine and `/go` again."
    )
    return "\n".join(lines)
