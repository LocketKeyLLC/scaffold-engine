"""Scaffold Engine — DAG generator module.

Takes a refined brief (from Step 10) → LLM decomposition → validated DAG.
Reuses Workflow Architect validation logic:
  - Kahn-based cycle detection
  - Strategy inference (sequential/parallel/hybrid/conditional)
  - structural dependency checks (dangling depends_on refs, dead-end/orphan
    nodes) — §17.617 (audit #42): the earlier "I/O contract auditing" wording
    overstated this; validation is structural-graph, not per-node I/O typing.

Persists nodes to dag_nodes table. Job transitions: planning → executing.

Step 11 of 23-step build plan.
"""

import hashlib
import json
import logging
import re
from collections import deque
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app import model_router
from app.modules.dag_validator import (
    issue_set_signature,
    render_corrections_block,
    validate_tool_picks,
)
from app.utils.job_utils import fail_job as _fail_job
from app.utils.llm_parsing import diagnose_json_object_parse, parse_json_object

logger = logging.getLogger("scaffold.dag")

# ---------------------------------------------------------------------------
# Valid enums — imported from config (#101)
# ---------------------------------------------------------------------------

from app.config import (
    VALID_TASK_TYPES,
    VALID_STRATEGIES,
    VALID_TOOLS,
    VALID_DOMAINS,
    settings,
)

# ---------------------------------------------------------------------------
# DAG generation prompt
# ---------------------------------------------------------------------------

DAG_SYSTEM = """You are a workflow decomposition engine. Given a structured brief, produce a DAG of executable tasks.

OUTPUT FORMAT (strict JSON, no markdown fences):
{
  "strategy": "sequential | parallel | hybrid | conditional",
  "tasks": [
    {
      "id": "T1",
      "name": "max 5 words",
      "type": "research | decision | action | validation | output",
      "inputs": ["what this task consumes"],
      "outputs": ["what this task produces"],
      "depends_on": [],
      "tool": "LLM | SearXNG | Milvus | CodeGen | Shell",
      "domain": "prompt | rag | eng | llm | spec | null",
      "assigned_model": "model name or null",
      "notes": "optional execution hint",
      "is_deliverable": false
    }
  ]
}

Rules:
- Decompose the idea into SINGLE-OUTCOME execution steps (see "Single outcome per node" below): each step produces ONE discrete, independently verifiable result the operator can finish and check before moving on. Match the number of steps to the brief's actual distinct outcomes — never pad, and never inflate a step to swallow adjacent work. A simple brief is 3-6 steps; a multi-part host-action / infra / deployment brief is typically 8-20, because each install / configure / integrate / verify becomes its own step. Do not exceed 25. When unsure whether something is one step or several, SPLIT — smaller steps are easier to execute, verify, and recover than big ones.
- Every task must have a unique id (T1, T2, ...)
- depends_on references other task ids — only use ids you have defined
- No circular dependencies
- First task(s) must have empty depends_on — ONLY genuine starting work (nothing
  must happen before it) qualifies
- A node that documents, summarizes, verifies, validates, reviews, or reports on
  the OVERALL build is a terminal CONSUMER: it MUST depend on the nodes it covers
  (typically the build's leaf nodes) and MUST NOT have empty depends_on. Never
  leave a "Document the setup" / "Verify the build" node dependency-free — that
  makes it runnable before the work it describes exists.
- Last task(s) must be type "output" or "validation"
- Deliverable marker (§17.475): set "is_deliverable": true on EXACTLY the
  node(s) that produce the user-facing final artifact the brief asked for.
  Every other node sets "is_deliverable": false (or omits it). This is about
  the ARTIFACT, not topology:
  * For research / synthesis / docs briefs it is usually the terminal
    synthesize / document / final-output node.
  * For code briefs it is the node that EMITS THE CODE (the CodeGen node),
    even when validation or documentation nodes run AFTER it. The code is the
    deliverable; the docs/validation are supporting.
  * A mid-graph setup step (e.g. "Configure Tailscale", "Install Proxmox") is
    NEVER the deliverable, even if nothing depends on it — that is a dead-end
    branch, not the output.
  If two independent artifacts are genuinely the deliverable (e.g. a library
  AND its README), mark both. Never mark zero nodes.
- Keep task names to max 5 words
- Tool guide:
  * Milvus = ALWAYS use when the task involves the knowledge base, KB, internal docs, TOON files, or domain-specific lookup. Any mention of "knowledge base", "KB", "look up from", "retrieve from", or stored/internal knowledge MUST use Milvus, NEVER SearXNG.
    - When tool is Milvus, you MUST set "domain" to the most relevant knowledge domain: "prompt" (prompt engineering), "rag" (retrieval-augmented generation), "eng" (software engineering), "llm" (large language models), "spec" (specifications/architecture). If unsure, set "domain" to null.
    - When tool is NOT Milvus, set "domain" to null.
  * SearXNG = web search for EXTERNAL, current, or live information NOT in the knowledge base.
  * CodeGen = the deliverable IS executable code. The node produces a working
    script, function, module, or class as its primary output. The user runs the
    output. Examples: "Write the parser", "Implement the API endpoint",
    "Generate the Dockerfile". Do NOT use CodeGen for: listing file extensions,
    naming variables, designing schemas, choosing libraries, writing
    documentation, listing requirements, or describing what code should do.
    If the deliverable is a list, plan, decision, design doc, or explanation —
    even one ABOUT code — use LLM, not CodeGen.
  * Shell = the deliverable is an action performed on a host or external
    system: installing software, configuring services, modifying files on a
    target machine, enforcing firewall rules, starting/stopping containers,
    setting up networking. The output of a Shell node is a runbook the human
    executes (and, when a shell backend is wired, the engine executes
    directly). Examples: "Install Proxmox VE", "Configure GPU passthrough",
    "Deploy Jellyfin VM", "Enforce network isolation", "Set up Tailscale
    routes". A task whose verb is install / configure / deploy / set up /
    enforce / start / stop / restart against a host MUST be Shell, NEVER LLM.
  * LLM = general reasoning, summarization, analysis, planning, listing,
    decision-making, design, explanation, and documentation. LLM nodes
    produce text only — they cannot execute commands. Do NOT use LLM for any
    task whose deliverable is an action on a host or system: that is Shell
    (or CodeGen if the deliverable is a single self-contained script).
    This is the DEFAULT for purely informational deliverables.
Scope discipline (§17.363 — load-bearing, read every time):

A node's scope is EXACTLY what its `name` and `outputs` literally state, and
NOTHING ELSE. Inflating a node's scope to cover adjacent work is the most
common decomposition failure on multi-step host-action briefs (homelab,
infra rollout, deployment). The model is tempted to make each node
"self-contained" — install everything from scratch, configure all the
networking, set up every container — instead of starting from upstream
state and adding only the named delta. Resist this.

Hard rules (Shell verbs):
- A node named "Install X" produces ONLY a working X install. It does NOT
  also configure the network around X, deploy services that run on X, set
  up SSH/VPN access to X, or document the result. Each of those is a
  separate node downstream.
- A node named "Configure Y" assumes the upstream that creates Y has run.
  It does NOT reinstall the base system, recreate the host, or repeat any
  step the upstream already did. The runbook starts from "Y exists" and
  adds only the configuration delta.
- A node named "Deploy Z service" creates ONLY service Z. It does NOT
  create the other 3 services in the same pipeline, recreate the network,
  or reinstall the host. Sibling deploy nodes are sibling nodes — not
  contents of each other.

Hard rules (CodeGen verbs — §17.367 + §17.370):
- A node named "Write CLI interface", "Write entry-point", or "Write
  command-line interface" produces ONLY the THIN ENTRY-POINT: argparse
  setup, flag parsing, and dispatch into functions imported from the
  upstream parser / generator / etc. modules. §17.370 — the CLI node
  is the thin glue between argparse and the imported business logic.
  It does NOT re-define `extract_blocks`, `generate_filename`,
  `LANG_EXT`, or any other function that an upstream sibling already
  exported. The CLI imports; it does not re-implement.
- A node named "Implement <module>" or "Implement <feature>" produces
  ONLY that module/feature, as a Python module that the CLI imports.
  It does NOT also include `def main()`, an `argparse.ArgumentParser`,
  or `if __name__ == "__main__"` — those belong to the CLI node.
- A node named "Write unit tests for <X>" produces ONLY the test file
  for X (`test_<x>.py` with `def test_*` functions importing from X).
  It does NOT also re-implement X inline, define a second `main()`, or
  pull in CLI argument parsing. Tests import; they do not re-stub.
- Sibling CodeGen nodes must have COMPATIBLE APIs: if T2 ("Write CLI")
  defines `generate_filename(lang, index, pattern)` and T3 ("Implement
  parser") defines `generate_filename(language, index)`, the two
  artifacts can't compose. Each node's `notes` must reference the
  function signatures the sibling nodes export, and each node must use
  those exact signatures rather than re-inventing them.

Hard rules (universal):
- Each node's `outputs` field must be a tight description of the
  incremental artifact (e.g., "Jellyfin LXC created and running",
  "parser module exporting extract_blocks(text) → list[Block]") — NOT a
  catch-all like "fully deployed homelab" or "complete CLI tool" that
  overlaps every other node's outputs.

Single outcome per node (§17.642 — read every time):

Each node produces EXACTLY ONE outcome — one thing installed, one thing
configured, one file written, one decision made — with ONE way to verify it.
This is a DIFFERENT rule from scope discipline above: scope discipline says a
node must not repeat a sibling's work; single-outcome says a node must not
BUNDLE several results of its own. A node must satisfy both — one result, and
only that result. A node is TOO BIG (split it) if ANY of these hold:
- its `name` needs "and", a comma, or a slash to describe it ("Create LXC,
  install Jellyfin, map GPU");
- its `outputs` list holds more than one distinct artifact;
- finishing it has more than one natural "stop and check it worked" moment;
- it bundles create + configure + integrate + verify of the same thing;
- it ACQUIRES A PREREQUISITE and then uses it (download an ISO/template/image,
  add a package repo) — the acquisition is its own node;
- it CREATES a resource and then CONFIGURES that resource (set its network/IP,
  add users, install software, edit its config) — creation and configuration
  are separate outcomes.
A good rule of thumb: if the human-facing walkthrough for the node would need
more than one "Phase", it is more than one outcome — split it so each node's
walkthrough is a single short phase.

Splitting heuristic (each distinct verb is its own node):
acquire-prerequisite ≠ create ≠ network-config, and install ≠ configure ≠ integrate ≠ verify.
So "Deploy Jellyfin with transcoding" is NOT one node; it is SIX:
  T_a Create the Jellyfin LXC        → outputs: empty Jellyfin LXC running
  T_b Install Jellyfin               → starts from T_a; the server package only
  T_c Map the GPU into the LXC       → starts from T_b; device passthrough only
  T_d Configure VA-API transcoding   → starts from T_c; the transcode config only
  T_e Bind-mount the media library   → starts from T_d; the mount only
  T_f Verify a hardware transcode    → starts from T_e; the one confirming test
Each is one result with one checkpoint. Apply the same split to any
"set up / deploy / configure X with Y" phrasing and to any node that would
otherwise create several sibling resources at once (e.g. "Create the 4 LXCs"
→ one create node per LXC). Prefer more small steps over few big ones.

Concretely, "Create unprivileged LXC" is NOT one node either — a real DAG that
made it one node produced a 900+-word walkthrough covering three phases at once.
Split it:
  T_a Download the OS template (if not present)  → outputs: template on host
  T_b Create the unprivileged container          → starts from T_a; the CT only
  T_c Set the container's static IP              → starts from T_b; network only
Creating the container and giving it a network identity are separate outcomes,
each with its own checkpoint.

Anti-example 1 (Shell — drawn from a real DAG that violated this rule —
homelab brief, 6-node Shell decomposition). T1, T2, T3, T5 EACH produced
runbooks that:
  - download + burn the Proxmox ISO
  - install Proxmox VE on the host
  - configure all 4 VLAN bridges
  - create all 4 LXC containers (Jellyfin, Ollama, AdGuard, Monitoring)
  - install + authenticate Tailscale on all 4 LXCs
  - set DNS on all 4 LXCs
  - disable telemetry on all 4 LXCs
Each node was ~95% identical to the others. An operator running them
in execution order would `pct create` the same LXC IDs three times and
get "VMID already in use" errors on the 2nd and 3rd attempts.

Anti-example 2 (CodeGen — §17.367, drawn from a real DAG: Markdown
code-block extractor, 4-CodeGen-node decomposition). T2 ("Write CLI
interface") and T3 ("Implement code block parser") each produced FULL
PROGRAMS:
  - T2 defined: `LANG_EXT` mapping, `parse_args()`, `extract_code_blocks()`,
    `generate_filename(lang, index, pattern)`, `def main()`,
    `argparse.ArgumentParser`, `if __name__ == "__main__"`
  - T3 defined: `language_extension_map` mapping, `code_block_pattern`,
    `generate_filename(language, index)` (DIFFERENT SIGNATURE),
    `process_file()`, `def main()`, `argparse.ArgumentParser`,
    `if __name__ == "__main__"`
Two `def main()`s, two `ArgumentParser`s, two extension maps under
different names, two incompatible `generate_filename` signatures. An
operator can't compose them — they're independent re-implementations
of the same program, not separable modules. The same shape as
Anti-example 1, different tool tag.

Anti-example 3 (CodeGen — §17.370, drawn from the §17.367-retry of
the same brief — finer-grained residual). T2 ("Write code block
parser") was clean — `import re`, `LANG_EXT`, `def extract_blocks` —
no main, no argparse. But T4 ("Write CLI interface") contained:
  - `def main()` + `argparse.ArgumentParser` (its actual job: the
    thin entry-point) ✓
  - `def parse_markdown` — T2's job (the parser) — reimplemented inline
  - `def generate_filename` — T3's job (the filename generator) —
    reimplemented inline with a different signature than T3 exported
  - `LANG_EXT = {...}` — T1's decision output — re-derived with 10
    different entries (html, css, xml, ruby) instead of T1's 9 (rust,
    go, dockerfile)
The CLI node became the whole program: argparse + parser + generator
+ map decision. Even though T2's scope was clean and T3's was mostly
clean, T4 reinvented T2's and T3's contributions inline. The §17.370
rule — CLI is the thin entry-point that imports, never reimplements —
closes this finer-grained regression.

The Good shape for the same brief:
  - T1 "Install Proxmox VE host"      → outputs: working Proxmox host with management IP
  - T2 "Configure VLAN bridges"       → starts from T1; adds vmbr0.<VLAN_*> stanzas; nothing else
  - T3 "Create LXC containers"        → starts from T2; runs `pct create` for the 4 LXCs; nothing else
  - T4 "Deploy Jellyfin in its LXC"   → starts from T3; installs + configures Jellyfin in its already-existing LXC; touches no other container
  - T5 "Deploy Ollama with GPU"       → starts from T3; GPU passthrough + Ollama install on its already-existing LXC; touches no other container
  - T6 "Enable Tailscale + DNS policy"→ starts from T5; one Tailscale exit node + AdGuard as resolver; consolidated, not per-LXC repeat
  - T7 "Validate the build"           → checks; no installs, no config edits
  - T8 "Document"                     → LLM; reads upstream; produces README

Each node's runbook starts from the prior node's terminal state — assume
the upstream ran, do not repeat its work. The `notes` field on each task
should make this explicit ("starts from T2's bridge config; adds LXC
creation only").

Other DAG-shape rules:
- Each node must produce DISTINCT output that no other node produces. Do NOT create multiple nodes that generate the same artifact (e.g., do not have separate "design script" and "write script" nodes that both produce the full script).
- Later nodes must EXTEND or VALIDATE earlier work, never recreate it. For example: T1 writes the code → T2 writes tests for it → T3 validates both — NOT T1 designs code → T2 rewrites the same code → T3 rewrites it again.
- If a task can be accomplished in one node, use one node. Prefer fewer, focused nodes over many overlapping ones.

EXAMPLE (4-node DAG for "Research the history of solar panels and summarize findings"):
{
  "strategy": "sequential",
  "tasks": [
    {"id": "T1", "name": "Search solar panel history", "type": "research", "inputs": ["solar panel history query"], "outputs": ["raw search results"], "depends_on": [], "tool": "SearXNG", "domain": null, "assigned_model": null, "notes": "Broad web search for timeline and key milestones"},
    {"id": "T2", "name": "Retrieve internal KB context", "type": "research", "inputs": ["solar panel keywords"], "outputs": ["KB matches"], "depends_on": ["T1"], "tool": "Milvus", "domain": "eng", "assigned_model": null, "notes": "Check knowledge base for any stored solar energy references"},
    {"id": "T3", "name": "Synthesize and summarize", "type": "action", "inputs": ["raw search results", "KB matches"], "outputs": ["summary draft"], "depends_on": ["T1", "T2"], "tool": "LLM", "domain": null, "assigned_model": null, "notes": "Combine sources into a coherent summary"},
    {"id": "T4", "name": "Format final output", "type": "output", "inputs": ["summary draft"], "outputs": ["final summary document"], "depends_on": ["T3"], "tool": "LLM", "domain": null, "assigned_model": null, "notes": "Write final summary to file", "is_deliverable": true}
  ]
}

EXAMPLE (8-node DAG for "Install Proxmox VE, set up Jellyfin + Ollama in containers, isolate via VLANs, enable Tailscale remote access"):
{
  "strategy": "sequential",
  "tasks": [
    {"id": "T1", "name": "Install Proxmox VE host", "type": "action", "inputs": ["target host details"], "outputs": ["working Proxmox host"], "depends_on": [], "tool": "Shell", "domain": null, "assigned_model": null, "notes": "ISO burn + install + management IP. STOPS at booted Proxmox. Does NOT configure VLANs, create LXCs, or install services."},
    {"id": "T2", "name": "Configure VLAN bridges", "type": "action", "inputs": ["working Proxmox host"], "outputs": ["VLAN-aware bridges"], "depends_on": ["T1"], "tool": "Shell", "domain": null, "assigned_model": null, "notes": "Starts from T1. Edits /etc/network/interfaces to add vmbr0.<VLAN_*> stanzas. Nothing else."},
    {"id": "T3", "name": "Create LXC containers", "type": "action", "inputs": ["VLAN-aware bridges"], "outputs": ["four empty running LXCs"], "depends_on": ["T2"], "tool": "Shell", "domain": null, "assigned_model": null, "notes": "Starts from T2. Runs pct create for the 4 LXCs (Jellyfin, Ollama, AdGuard, Monitoring) on appropriate VLANs. No service install."},
    {"id": "T4", "name": "Deploy Jellyfin service", "type": "action", "inputs": ["four empty running LXCs"], "outputs": ["Jellyfin LXC serving media"], "depends_on": ["T3"], "tool": "Shell", "domain": null, "assigned_model": null, "notes": "Starts from T3 — the Jellyfin LXC already exists and runs. Installs + configures only Jellyfin inside it. Does NOT touch the other 3 LXCs."},
    {"id": "T5", "name": "Deploy Ollama with GPU", "type": "action", "inputs": ["four empty running LXCs"], "outputs": ["Ollama LXC serving llama3"], "depends_on": ["T3"], "tool": "Shell", "domain": null, "assigned_model": null, "notes": "Starts from T3 — the Ollama LXC already exists. Adds GPU passthrough lines + installs Ollama. Does NOT touch the other 3 LXCs."},
    {"id": "T6", "name": "Enable Tailscale + DNS policy", "type": "action", "inputs": ["all service LXCs"], "outputs": ["one exit node + AdGuard as DNS"], "depends_on": ["T4", "T5"], "tool": "Shell", "domain": null, "assigned_model": null, "notes": "ONE exit node, not four. AdGuard configured as resolver for the other LXCs (not Cloudflare directly)."},
    {"id": "T7", "name": "Validate the build", "type": "validation", "inputs": ["all upstream runbooks"], "outputs": ["validation report"], "depends_on": ["T6"], "tool": "LLM", "domain": null, "assigned_model": null, "notes": "Read upstream outputs and check coherence. No installs, no config edits."},
    {"id": "T8", "name": "Document the setup", "type": "output", "inputs": ["all upstream runbooks"], "outputs": ["README.md"], "depends_on": ["T7"], "tool": "LLM", "domain": null, "assigned_model": null, "notes": "Documentation about the setup is text — LLM, not Shell.", "is_deliverable": true}
  ]
}
NOTE on the example above: it is drawn COARSE for brevity, to show scope
boundaries, tool selection, and deliverable marking. Under the single-outcome
rule, each multi-part node splits further into single-outcome nodes — "T3 Create
LXC containers" → one create node per LXC, and "T4 Deploy Jellyfin service" →
the six-node create→install→map-GPU→configure→mount→verify split shown above.
Real output should use that finer grain, not this compressed illustration.

EXAMPLE (5-node DAG for "Build a CLI tool that converts screenshots to a searchable PDF"):
{
  "strategy": "sequential",
  "tasks": [
    {"id": "T1", "name": "Decide library stack", "type": "decision", "inputs": ["project goals"], "outputs": ["chosen libraries and why"], "depends_on": [], "tool": "LLM", "domain": null, "assigned_model": null, "notes": "Pick OCR + PDF libs (e.g., pytesseract, pypdf) — text decision, NOT code"},
    {"id": "T2", "name": "List supported file types", "type": "decision", "inputs": ["chosen libraries"], "outputs": ["list of extensions"], "depends_on": ["T1"], "tool": "LLM", "domain": null, "assigned_model": null, "notes": "Plain list of extensions like .png .jpg — LLM not CodeGen"},
    {"id": "T3", "name": "Write the CLI script", "type": "action", "inputs": ["library stack", "file types"], "outputs": ["working Python script"], "depends_on": ["T1", "T2"], "tool": "CodeGen", "domain": null, "assigned_model": null, "notes": "Real code is the deliverable — CodeGen", "is_deliverable": true},
    {"id": "T4", "name": "Document usage", "type": "action", "inputs": ["working Python script"], "outputs": ["README content"], "depends_on": ["T3"], "tool": "LLM", "domain": null, "assigned_model": null, "notes": "Documentation about code is LLM, not CodeGen"},
    {"id": "T5", "name": "Validate end-to-end", "type": "validation", "inputs": ["working Python script", "README content"], "outputs": ["validation report"], "depends_on": ["T3", "T4"], "tool": "LLM", "domain": null, "assigned_model": null, "notes": "Validation is reasoning, not code"}
  ]
}"""

DAG_PROMPT = """Decompose this refined brief into a DAG of executable tasks:

---
{brief}
---

§17.663 — If (and ONLY if) the brief contains an `operator_decision` object (a key
choice the research surfaced, with an `options` list), you MUST include an early
node of `type: "decision"` named after that decision, put its options (each label
+ trade-off) AND the `suggested` default into that node's `notes`, and make the
downstream implementation tasks `depends_on` it — so the operator chooses the path
before the plan commits to one. This is separate from any decisions the plan
itself needs; add it in addition to those.

Return ONLY the JSON object. No preamble, no markdown."""


# ---------------------------------------------------------------------------
# Sprint W.3 — Validator-driven retry loop
# ---------------------------------------------------------------------------

async def _generate_dag_json(
    prompt: str, route_kwargs: dict, *, draws: int = 3,
) -> tuple:
    """§17.463 — generate the DAG JSON, re-drawing on a success+empty/unparseable
    response.

    The default generator role (``qwen3.5:397b-cloud`` since §17.440) is a
    *thinking* model that can spend its whole budget on reasoning and return
    ``success=True`` with EMPTY content — ``parse_json_object`` then yields None.
    Pre-§17.463 that hard-failed the entire DAG on attempt 1 (surfaced to the
    user as "DAG must have at least 2 tasks"), even though a fresh draw almost
    always lands (the §17.453 / §17.462 thinking-model-empty-content lesson).
    Gives the model 8192-token headroom (was 4096) and up to ``draws``
    independent re-draws. A hard failure (``success=False``) is surfaced
    immediately — only an empty/unparseable *successful* response is retried.

    Returns ``(last_resp, parsed_or_None, summed_duration_ms)``.
    """
    total_ms = 0
    resp = None
    for d in range(draws):
        resp = await model_router.generate(
            prompt,
            system=DAG_SYSTEM,
            temperature=0.3,
            max_tokens=8192,
            **route_kwargs,
        )
        total_ms += getattr(resp, "total_duration_ms", 0) or 0
        if not resp.success:
            return resp, None, total_ms
        parsed = parse_json_object(resp.text)
        if parsed is not None:
            return resp, parsed, total_ms
        logger.warning(
            "dag_generate_redraw_on_empty: draw=%d/%d text_len=%d "
            "(thinking-model empty content, §17.463)",
            d + 1, draws, len(resp.text or ""),
        )
    return resp, None, total_ms


# ---------------------------------------------------------------------------
# §17.476 (Phase 2) — dependency-completeness / dead-end detection
# ---------------------------------------------------------------------------

def _reachable(start: str, adj: dict[str, list[str]]) -> set[str]:
    """Transitive reachable set from ``start`` over adjacency ``adj``
    (inclusive of ``start``). Iterative + visited-guarded so a malformed
    cyclic graph degrades instead of recursing forever."""
    seen: set[str] = set()
    stack = [start]
    while stack:
        k = stack.pop()
        if k in seen:
            continue
        seen.add(k)
        for nxt in adj.get(k, ()) or ():
            if nxt not in seen:
                stack.append(nxt)
    return seen


def _deliverable_ids(tasks: list[dict]) -> set[str]:
    """The deliverable set: explicitly-marked is_deliverable nodes, else the
    topological leaves (same fallback compile + persist use)."""
    ids = {t["id"] for t in tasks if t.get("id")}
    explicit = {t["id"] for t in tasks if t.get("is_deliverable") and t.get("id")}
    if explicit:
        return explicit
    referenced = {
        d for t in tasks for d in (t.get("depends_on") or []) if d in ids
    }
    return {i for i in ids if i not in referenced}


def detect_dead_ends(tasks: list[dict]) -> list[str]:
    """§17.476 — orphan / dead-end node ids (in task order).

    An orphan is a node that neither FEEDS a deliverable (in its transitive
    upstream closure) nor is FED BY one (a deliverable's descendant). This
    excludes legitimate downstream validation/docs nodes that consume the
    deliverable, and catches the Proxmox-style sibling branch ("configure
    Tailscale") that hangs off the trunk but flows into nothing.

    deliverable set = is_deliverable, else topological leaves (fallback).
    """
    ids = {t["id"] for t in tasks if t.get("id")}
    if not ids:
        return []
    fwd = {
        t["id"]: [d for d in (t.get("depends_on") or []) if d in ids]
        for t in tasks
    }
    rev: dict[str, list[str]] = {i: [] for i in ids}
    for t in tasks:
        for d in fwd[t["id"]]:
            rev[d].append(t["id"])

    deliverables = _deliverable_ids(tasks)
    covered: set[str] = set()
    for d in deliverables:
        covered |= _reachable(d, fwd)   # nodes that FEED the deliverable
        covered |= _reachable(d, rev)   # nodes FED BY the deliverable
    return [t["id"] for t in tasks if t.get("id") and t["id"] not in covered]


def auto_link_dead_ends(tasks: list[dict], orphans: list[str]) -> str | None:
    """Last resort — wire each orphan into the primary deliverable's
    depends_on so the persisted graph is connected. Cycle-safe by
    construction: an orphan is not a deliverable descendant, so it cannot
    transitively depend on the deliverable. Returns the chosen primary
    deliverable id, or None if there is nothing to link to.

    Primary = the deliverable with the largest upstream closure (the most
    synthesis-like), tie → earliest in task order.
    """
    if not orphans:
        return None
    ids = {t["id"] for t in tasks if t.get("id")}
    fwd = {
        t["id"]: [d for d in (t.get("depends_on") or []) if d in ids]
        for t in tasks
    }
    deliverables = _deliverable_ids(tasks)
    if not deliverables:
        return None
    order = {t["id"]: i for i, t in enumerate(tasks) if t.get("id")}
    primary = max(
        deliverables,
        key=lambda d: (len(_reachable(d, fwd)), -order.get(d, 0)),
    )
    by_id = {t["id"]: t for t in tasks if t.get("id")}
    ptask = by_id[primary]
    deps = list(ptask.get("depends_on") or [])
    orphan_set = set(orphans)
    for o in orphans:
        if o != primary and o not in deps and o in by_id:
            deps.append(o)
    ptask["depends_on"] = deps
    return primary


def connect_isolated_nodes(tasks: list[dict]) -> list[str]:
    """§17.668 — wire any ISOLATED node (empty depends_on AND nothing depends on
    it → zero edges) into the graph. ``detect_dead_ends`` MISSES these when the
    generator marks them ``is_deliverable`` (a deliverable trivially "reaches
    itself"), so `auto_link_dead_ends` never fires — the homelab test left
    config/verify steps (Configure Jellyfin, Verify SSH, …) floating with no
    deps, claimable from t=0 and flagged "disconnected from the graph". This is a
    deterministic connectivity guarantee independent of is_deliverable: each
    isolated node is chained onto the nearest preceding step (highest
    execution_order below its own), including previously-wired isolated nodes, so
    config/verify run AFTER the build and in a sensible order. Cycle-safe — an
    isolated node has no path to/from anything, and it only ever gains a
    dependency on a LOWER-order node. Returns the wired node ids."""
    real = [t for t in tasks if t.get("id")]
    ids = {t["id"] for t in real}
    depended_on: set[str] = set()
    has_dep: dict[str, bool] = {}
    for t in real:
        deps = [d for d in (t.get("depends_on") or []) if d in ids]
        has_dep[t["id"]] = bool(deps)
        depended_on.update(deps)
    iso_ids = {t["id"] for t in real
               if not has_dep[t["id"]] and t["id"] not in depended_on}
    if not iso_ids:
        return []
    order = {
        t["id"]: (t["execution_order"] if t.get("execution_order") is not None else i)
        for i, t in enumerate(real)
    }
    by_id = {t["id"]: t for t in real}
    # anchor pool = the connected nodes; grows as we wire isolated ones so they
    # chain instead of all hanging off a single node.
    anchor_pool = [t["id"] for t in real if t["id"] not in iso_ids]
    wired: list[str] = []
    for iid in sorted(iso_ids, key=lambda x: order.get(x, 0)):
        to = order.get(iid, 0)
        preds = [a for a in anchor_pool if a != iid and order.get(a, 0) < to]
        if not preds:  # nothing earlier — fall back to any connected node
            preds = [a for a in anchor_pool if a != iid]
        if not preds:  # degenerate: every node was isolated
            continue
        anchor = max(preds, key=lambda a: order.get(a, 0))
        by_id[iid]["depends_on"] = [anchor]
        anchor_pool.append(iid)
        wired.append(iid)
    return wired


def _enforce_deliverable_marking(tasks: list[dict]) -> list[str]:
    """§17.669 — §17.475 says a MID-GRAPH setup step is NEVER the deliverable, but
    the generator over-marks (homelab test: 8/10, incl. 'Download Proxmox ISO',
    'Configure GPU passthrough'). A DAG should mark the node(s) that produce the
    FINAL artifact, not every step. Enforce it deterministically: ``is_deliverable``
    survives only on a TERMINAL node (nothing depends on it) OR a CodeGen node
    (per §17.475 the code IS the deliverable even with downstream validation).
    Mid-graph non-CodeGen marks are cleared. Only removes over-marks — it does not
    add new ones, except the safety fallback to terminal leaves if every mark was
    mid-graph (so we never drop to zero deliverables). Returns the unmarked ids."""
    real = [t for t in tasks if t.get("id")]
    ids = {t["id"] for t in real}
    depended_on: set[str] = set()
    for t in real:
        depended_on |= {d for d in (t.get("depends_on") or []) if d in ids}
    marked = [t for t in real if t.get("is_deliverable")]
    if not marked:
        return []  # zero-marked is handled by the leaf fallback in generate_dag
    def keeps(t: dict) -> bool:
        terminal = t["id"] not in depended_on
        return terminal or str(t.get("tool", "")).lower() == "codegen"
    keep = {t["id"] for t in marked if keeps(t)}
    if not keep:  # every mark was mid-graph non-CodeGen → fall back to leaves
        keep = {t["id"] for t in real if t["id"] not in depended_on}
    unmarked: list[str] = []
    for t in marked:
        if t["id"] not in keep:
            t["is_deliverable"] = False
            unmarked.append(t["id"])
    for t in real:  # only relevant in the leaf-fallback branch
        if t["id"] in keep and not t.get("is_deliverable"):
            t["is_deliverable"] = True
    return unmarked


# §17.645 — terminal reporting verbs. A node whose name says it documents /
# verifies / summarizes / reviews the build is a downstream CONSUMER of the
# build, not a starting step. If the generator leaves it with empty depends_on
# it becomes dependency-ready from t=0, so the assist "next" claim can hand it
# to the operator out of order ("Document the setup" as step 2). detect_dead_ends
# misses it because a depless leaf counts as a deliverable (it is trivially
# "covered"), so this is a separate, narrow backstop.
_TERMINAL_REPORT_RE = re.compile(
    r"\b(document|documentation|summar|verif|validat|review|write[- ]?up|"
    r"final report)\b",
    re.IGNORECASE,
)


def wire_orphan_terminal_nodes(tasks: list[dict]) -> list[str]:
    """Wire a depless terminal-reporting node into the build leaves it should
    follow. Returns the list of node ids that were rewired (for logging).

    Only touches a node that (a) has empty depends_on, (b) has a
    terminal-reporting name, and (c) is not the graph's only real work — it is
    wired to depend on every current leaf (node nothing depends on) that is not
    itself a depless terminal-reporting node. Cycle-safe: the node was depless,
    and leaves do not reference it, so no back-edge can exist.
    """
    ids = [t["id"] for t in tasks if t.get("id")]
    if len(ids) < 2:
        return []
    id_set = set(ids)
    referenced = {
        d for t in tasks for d in (t.get("depends_on") or []) if d in id_set
    }
    leaves = [i for i in ids if i not in referenced]

    def _is_depless_terminal(t: dict) -> bool:
        return (not (t.get("depends_on") or [])
                and bool(_TERMINAL_REPORT_RE.search(t.get("name") or "")))

    depless_terminal = {t["id"] for t in tasks if t.get("id") and _is_depless_terminal(t)}
    if not depless_terminal:
        return []
    rewired: list[str] = []
    for t in tasks:
        if t.get("id") not in depless_terminal:
            continue
        # Predecessors = leaves other than this node and other depless terminal
        # nodes (so two orphan reporting nodes don't wire to each other only).
        preds = [l for l in leaves if l != t["id"] and l not in depless_terminal]
        if not preds:
            continue
        t["depends_on"] = sorted(preds)
        rewired.append(t["id"])
    return rewired


def render_dead_end_corrections(
    tasks: list[dict], orphans: list[str], next_attempt: int,
) -> str:
    """Correction block fed back to the generator so it re-decomposes a graph
    where every node flows into a deliverable."""
    by_id = {t["id"]: t for t in tasks if t.get("id")}
    lines = [
        f"## Dependency-completeness corrections needed (attempt {next_attempt})",
        "",
        "These nodes produce output that NO deliverable (is_deliverable) node "
        "consumes, even transitively — they are dead-end branches that hang off "
        "the graph and flow into nothing:",
        "",
    ]
    for o in orphans:
        name = (by_id.get(o) or {}).get("name", "?")
        lines.append(f"- {o} ({name})")
    lines.append("")
    lines.append(
        "Re-decompose so EVERY node feeds the final deliverable (directly or "
        "through the chain): either make a deliverable depend on the orphan's "
        "output, fold the orphan's work into a node the deliverable already "
        "uses, or remove it if it is genuinely unnecessary. Do not leave any "
        "node disconnected from the deliverable."
    )
    return "\n".join(lines)


async def _generate_dag_with_validator(
    brief_data: dict,
    route_kwargs: dict,
) -> dict:
    """Run LLM → validator → strict-retry loop, returning the chosen DAG.

    Returns a dict with keys:
        dag_data:        parsed DAG JSON (None on hard failure of attempt 1)
        raw_text:        last LLM raw text (for error surfacing)
        model:           model name returned by the last LLM call
        duration_ms:     summed total_duration_ms across all calls
        warnings:        list of validator-related diagnostic strings
        error:           populated only when attempt 1 fails outright
        attempts:        how many generator calls happened (1..max_attempts)
        validator_calls: how many validator calls happened (0..max_attempts)

    Validator behavior:
        - Runs after each parse, before the next retry.
        - "fail-open": if the validator LLM errors or returns malformed JSON,
          we ship the current DAG (rather than failing the job).
        - Circuit-breaker: if two consecutive validator passes return the
          identical issue set, the regenerator clearly isn't taking the hint —
          break out and ship the current DAG with a warning.
        - On the last allowed attempt with issues still present, ship the
          DAG and surface the remaining issues as a warning.
    """
    warnings: list[str] = []
    total_duration_ms = 0
    last_text = ""
    last_model: str | None = None
    corrections_block: str | None = None
    last_issue_signature: tuple | None = None

    if settings.dag_validator_enabled:
        max_attempts = 1 + settings.dag_validator_max_retries
    else:
        max_attempts = 1

    dag_data: dict | None = None

    # §17.576 — learning flywheel: inject similar high-grounding exemplars as
    # few-shot "proven prior solutions" (opt-in; "" when disabled or none found).
    # Computed once outside the retry loop; fail-soft inside retrieve_exemplars.
    _exemplar_block = ""
    try:
        from app.modules.flywheel import retrieve_exemplars
        _ex_query = ""
        if isinstance(brief_data, dict):
            _ex_query = str(brief_data.get("description") or brief_data.get("title") or "")
        _exemplar_block = await retrieve_exemplars(_ex_query or json.dumps(brief_data)[:500])
    except Exception:
        _exemplar_block = ""

    for attempt in range(1, max_attempts + 1):
        prompt_body = DAG_PROMPT.format(brief=json.dumps(brief_data, indent=2))
        prompt = (corrections_block + "\n\n" + prompt_body) if corrections_block else prompt_body
        if _exemplar_block:
            prompt = _exemplar_block + prompt

        # §17.463 — retry-on-empty around the generator call (thinking-model
        # empty-content guard). 8192-token headroom + up to 3 re-draws.
        resp, parsed, draw_ms = await _generate_dag_json(prompt, route_kwargs)
        last_text = resp.text or ""
        last_model = resp.model
        total_duration_ms += draw_ms

        if not resp.success:
            if attempt == 1:
                return {
                    "dag_data": None, "raw_text": last_text, "model": last_model,
                    "duration_ms": total_duration_ms, "warnings": warnings,
                    "error": resp.error, "attempts": attempt, "validator_calls": 0,
                }
            warnings.append(f"validator_retry_call_failed_attempt_{attempt}: {resp.error}")
            break

        if parsed is None:
            if attempt == 1:
                return {
                    "dag_data": None, "raw_text": last_text, "model": last_model,
                    "duration_ms": total_duration_ms, "warnings": warnings,
                    "error": "LLM output was not valid JSON",
                    "attempts": attempt, "validator_calls": 0,
                }
            warnings.append(f"validator_retry_parse_failed_attempt_{attempt}")
            break

        dag_data = parsed

        if not settings.dag_validator_enabled:
            return {
                "dag_data": dag_data, "raw_text": last_text, "model": last_model,
                "duration_ms": total_duration_ms, "warnings": warnings,
                "error": None, "attempts": attempt, "validator_calls": 0,
            }

        # Validator pass.
        outcome = await validate_tool_picks(
            dag_data.get("tasks", []),
            model_overrides=route_kwargs.get("overrides"),
            max_tokens=settings.dag_validator_max_tokens,
            empty_redraws=settings.dag_validator_empty_redraws,
        )

        if outcome.error:
            warnings.append(f"validator_failed_open_attempt_{attempt}: {outcome.error}")
            return {
                "dag_data": dag_data, "raw_text": last_text, "model": last_model,
                "duration_ms": total_duration_ms, "warnings": warnings,
                "error": None, "attempts": attempt, "validator_calls": attempt,
            }

        # §17.476 — dependency-completeness check alongside the tool-pick
        # validator. An orphan node (feeds / is fed by no deliverable) is the
        # §17.471-474 defect; flag-and-retry so the model re-decomposes. Any
        # survivor is auto-linked in generate_dag as a deterministic last
        # resort. Same retry budget as the tool-pick validator.
        dead_ends = (
            detect_dead_ends(dag_data.get("tasks", []))
            if settings.dag_dead_end_check_enabled else []
        )

        if not outcome.issues and not dead_ends:
            if attempt > 1:
                warnings.append(f"validator_clean_after_retry_attempt_{attempt}")
            return {
                "dag_data": dag_data, "raw_text": last_text, "model": last_model,
                "duration_ms": total_duration_ms, "warnings": warnings,
                "error": None, "attempts": attempt, "validator_calls": attempt,
            }

        # Something to fix (tool-pick issues and/or dead-ends).
        if attempt == max_attempts:
            if outcome.issues:
                warnings.append(
                    f"validator_retries_exhausted: {len(outcome.issues)} issue(s) "
                    f"remain after {attempt} attempts: " + "; ".join(
                        f"{i.node_id}:{i.current_tool}->{i.proposed_tool}"
                        for i in outcome.issues
                    )
                )
            if dead_ends:
                warnings.append(
                    f"dead_end_retries_exhausted: {len(dead_ends)} orphan node(s) "
                    f"remain after {attempt} attempts: " + ", ".join(dead_ends)
                    + " (auto-link fallback will connect them)"
                )
            return {
                "dag_data": dag_data, "raw_text": last_text, "model": last_model,
                "duration_ms": total_duration_ms, "warnings": warnings,
                "error": None, "attempts": attempt, "validator_calls": attempt,
            }

        sig = (issue_set_signature(outcome.issues), tuple(sorted(dead_ends)))
        if sig == last_issue_signature:
            warnings.append(
                f"validator_circuit_break_attempt_{attempt}: identical issue "
                f"set ({len(outcome.issues)} tool-pick, {len(dead_ends)} "
                f"dead-end) — regenerator not converging"
            )
            return {
                "dag_data": dag_data, "raw_text": last_text, "model": last_model,
                "duration_ms": total_duration_ms, "warnings": warnings,
                "error": None, "attempts": attempt, "validator_calls": attempt,
            }
        last_issue_signature = sig

        correction_parts: list[str] = []
        if outcome.issues:
            warnings.append(
                f"validator_found_{len(outcome.issues)}_issues_attempt_{attempt}: "
                + "; ".join(
                    f"{i.node_id}:{i.current_tool}->{i.proposed_tool}"
                    for i in outcome.issues
                )
            )
            correction_parts.append(
                render_corrections_block(outcome.issues, attempt + 1)
            )
        if dead_ends:
            warnings.append(
                f"dead_end_found_{len(dead_ends)}_attempt_{attempt}: "
                + ", ".join(dead_ends)
            )
            correction_parts.append(
                render_dead_end_corrections(
                    dag_data.get("tasks", []), dead_ends, attempt + 1,
                )
            )
        corrections_block = "\n\n".join(correction_parts)

    # Reached only when the loop broke mid-flight after attempt 1 (e.g., a
    # retry call/parse failed). dag_data here is the most recent successful parse.
    return {
        "dag_data": dag_data, "raw_text": last_text, "model": last_model,
        "duration_ms": total_duration_ms, "warnings": warnings,
        "error": None, "attempts": attempt, "validator_calls": min(attempt, max_attempts),
    }


# ---------------------------------------------------------------------------
# §17.181 — DAG input hash for re-entry idempotency
# ---------------------------------------------------------------------------

def _compute_dag_input_hash(
    brief: Any,
    model: str | None,
    model_overrides: dict | None,
) -> str:
    """SHA-256 over the inputs that fully determine the generated DAG.

    Used by ``generate_dag`` to tell "idempotent retry of the same /dag call"
    (return 409 — DAG already materialized) from "the brief changed since the
    last generation" (recompute when safe). Stable across processes: the JSON
    serialization uses ``sort_keys=True`` and a single separator pair so a key
    reordering in Postgres' JSONB return doesn't flip the hash.
    """
    payload = {
        "brief": brief if isinstance(brief, (dict, list)) else json.loads(brief)
            if isinstance(brief, str) else brief,
        "model": model,
        "model_overrides": model_overrides or {},
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Core DAG generation
# ---------------------------------------------------------------------------

async def generate_dag(
    job_id: str,
    db: AsyncSession,
    model: str | None = None,
    model_overrides: dict | None = None,
) -> dict:
    """Generate a DAG from a job's refined brief and persist nodes.

    Returns dict with job_id, strategy, task_count, tasks, edges, validation.
    """
    uid = UUID(job_id)

    # 1. Fetch job + refined brief + existing dag_nodes count under a row lock.
    # FOR UPDATE serializes concurrent /dag calls on the same job_id so the
    # status + node-count checks below are race-free with the INSERT block.
    # Lock is released on commit/rollback at the end of this transaction.
    # §17.181: also fetch dag_input_hash (NULL on pre-§17.181 jobs) so the
    # re-entry guard can tell idempotent retry from a brief-edit drift.
    result = await db.execute(
        text("""
            SELECT j.status, j.refined_brief, j.dag_input_hash, j.research_data,
                   (SELECT COUNT(*) FROM dag_nodes WHERE job_id = j.id) AS node_count
            FROM jobs j
            WHERE j.id = :id
            FOR UPDATE OF j
        """),
        {"id": uid},
    )
    row = result.first()
    if not row:
        await db.rollback()
        return {"error": f"Job {job_id} not found"}

    status, brief, stored_hash, research_data, node_count = row
    # H6: accept 'planning' OR 'running' — execute_all_nodes flips to 'running'
    # before calling generate_dag on auto-gen path.
    if status not in ("planning", "running"):
        await db.rollback()
        return {
            "error": "Job is not in an executable planning/running status",
            "job_id": job_id,
            "current_status": status,
            "http_status": 409,
        }
    if not brief:
        await db.rollback()
        return {"error": "Job has no refined_brief — run idea refinement first"}

    brief_data = brief if isinstance(brief, dict) else json.loads(brief)
    # §17.663 — thread the research-surfaced operator decision (§17.662) into the
    # brief so the DAG builds an explicit `decision` node for it. Merged BEFORE
    # the input hash so a different surfaced decision correctly re-plans.
    brief_data = _brief_with_operator_decision(brief_data, research_data)
    current_hash = _compute_dag_input_hash(brief_data, model, model_overrides)

    # §17.181: re-entry guard. Three cases when nodes already exist:
    #   (a) stored_hash matches current_hash → idempotent retry, return 409.
    #   (b) stored_hash differs (or is NULL on pre-§17.181 rows) AND some
    #       existing node has already left 'pending' → execution underway,
    #       refuse with an explicit drift message rather than blowing away
    #       in-flight work. NULL hash treated as "unknown" → conservative 409.
    #   (c) stored_hash differs AND all existing nodes are still 'pending' →
    #       log + DELETE the stale nodes and fall through to fresh generation.
    if (node_count or 0) > 0:
        if stored_hash is not None and stored_hash == current_hash:
            logger.warning(
                "idempotency_rejected: job=%s existing_nodes=%d hash_match=1",
                job_id, node_count,
            )
            await db.rollback()
            return {
                "error": "DAG already exists for this job",
                "job_id": job_id,
                "node_count": node_count,
                "http_status": 409,
            }

        non_pending = (await db.execute(
            text("""
                SELECT COUNT(*) FROM dag_nodes
                WHERE job_id = :j AND status <> 'pending'
            """),
            {"j": uid},
        )).scalar_one()
        if stored_hash is None or non_pending > 0:
            logger.warning(
                "idempotency_rejected: job=%s existing_nodes=%d "
                "non_pending=%d stored_hash=%s drift=1",
                job_id, node_count, non_pending,
                "null" if stored_hash is None else "set",
            )
            await db.rollback()
            return {
                "error": (
                    "DAG already exists for this job and cannot be recomputed "
                    "safely (execution has started or hash is unknown)"
                ),
                "job_id": job_id,
                "node_count": node_count,
                "http_status": 409,
            }

        logger.warning(
            "dag_input_drift: job=%s existing_nodes=%d — brief or overrides "
            "changed since last generation; recomputing", job_id, node_count,
        )
        await db.execute(
            text("DELETE FROM dag_nodes WHERE job_id = :j"),
            {"j": uid},
        )

    # 2-3. Call LLM (with W.3 validator-driven retry loop) and parse output.
    route_kwargs = (
        {"model": model} if model
        else {"role": "model_general", "overrides": model_overrides}
    )
    gen_result = await _generate_dag_with_validator(brief_data, route_kwargs)
    validator_warnings = gen_result["warnings"]

    if gen_result["dag_data"] is None:
        # Hard failure on the first attempt — propagate the original error.
        if gen_result["error"] == "LLM output was not valid JSON":
            # §17.293 — surface JSONDecodeError diagnostics (lineno /
            # colno / msg / pos) alongside the truncated raw output.
            # Pre-§17.293 the operator only saw `raw_output[:500]` and
            # had to eyeball the snippet for the syntax error. The
            # diagnose helper re-parses with `json.loads` to recover
            # the same exception parse_json_object swallows, so the
            # field is available without changing the parser API.
            parse_diag = diagnose_json_object_parse(gen_result["raw_text"])
            await _fail_job(db, uid, "Failed to parse DAG JSON from LLM output")
            return {
                "job_id": job_id,
                "status": "failed",
                "error": "LLM output was not valid JSON",
                "raw_output": gen_result["raw_text"][:500],
                "parse_error": parse_diag,  # None if the first parse would have succeeded
            }
        await _fail_job(db, uid, f"LLM DAG generation failed: {gen_result['error']}")
        return {"job_id": job_id, "status": "failed", "error": gen_result["error"]}

    dag_data = gen_result["dag_data"]
    tasks = dag_data.get("tasks", [])
    if len(tasks) < 2:
        await _fail_job(db, uid, "DAG must have at least 2 tasks")
        return {"job_id": job_id, "status": "failed", "error": "Less than 2 tasks generated"}

    # §17.645 — wire depless terminal-reporting nodes (document / verify /
    # summarize the build) into the build leaves they should follow, BEFORE the
    # dead-end pass. Without deps such a node is claimable from t=0, so the
    # assist "next" claim hands it to the operator out of order (live browser
    # test: "Document setup for beginner" surfaced as step 2, before anything
    # was built). detect_dead_ends can't catch it — a depless leaf counts as a
    # deliverable — so this narrow deterministic fix runs first.
    rewired_terminals = wire_orphan_terminal_nodes(tasks)
    if rewired_terminals:
        validator_warnings.append(
            f"terminal_nodes_wired: {', '.join(rewired_terminals)} had no "
            f"dependencies but document/verify the build — wired to the build "
            f"leaves so they run last, not first"
        )
        logger.warning(
            "dag_terminal_nodes_wired: job=%s nodes=%s", job_id, rewired_terminals,
        )

    # §17.476 (Phase 2) — dead-end auto-link, last resort. The validator loop
    # already flagged orphans and retried; any survivor is wired into the
    # primary deliverable's depends_on here so the persisted graph is connected
    # (cycle-safe — an orphan can't transitively depend on the deliverable).
    if settings.dag_dead_end_check_enabled:
        orphans = detect_dead_ends(tasks)
        if orphans:
            primary = auto_link_dead_ends(tasks, orphans)
            if primary:
                validator_warnings.append(
                    f"dead_end_auto_linked: {len(orphans)} orphan node(s) "
                    f"({', '.join(orphans)}) wired into deliverable {primary} "
                    f"after the generator did not connect them"
                )
                logger.warning(
                    "dag_dead_end_auto_linked: job=%s orphans=%s primary=%s",
                    job_id, orphans, primary,
                )

    # 3b-4b. Node-count enforcement + normalize + semantic validation
    # (single try/except so any ValueError from these steps fails the job cleanly)
    try:
        tasks = _enforce_node_count(tasks)
        normalized, errors, normalize_warnings = _normalize_tasks(tasks)
        if errors:
            await _fail_job(db, uid, f"Task validation errors: {'; '.join(errors)}")
            return {"job_id": job_id, "status": "failed", "errors": errors}
        # §17.668 — connectivity guarantee: wire any isolated node (zero edges)
        # into the graph. Catches the is_deliverable-marked orphans that
        # detect_dead_ends/auto_link_dead_ends miss (see connect_isolated_nodes).
        if settings.dag_dead_end_check_enabled:
            _iso_wired = connect_isolated_nodes(normalized)
            if _iso_wired:
                validator_warnings.append(
                    f"isolated_nodes_connected: {', '.join(_iso_wired)} had no "
                    f"dependencies and nothing depended on them (floating from "
                    f"t=0) — chained onto the preceding step"
                )
                logger.warning(
                    "dag_isolated_nodes_connected: job=%s nodes=%s",
                    job_id, _iso_wired,
                )
        normalized, dag_warnings = validate_dag(normalized)
    except ValueError as exc:
        await _fail_job(db, uid, str(exc))
        return {"job_id": job_id, "status": "failed", "error": str(exc)}

    # 5. Build edges and validate graph
    edges = _build_edges(normalized)
    graph_errors, warnings = _validate_graph(normalized, edges)
    warnings.extend(dag_warnings)
    warnings.extend(normalize_warnings)  # #26 #25
    warnings.extend(validator_warnings)  # W.3
    if graph_errors:
        await _fail_job(db, uid, f"Graph validation errors: {'; '.join(graph_errors)}")
        return {"job_id": job_id, "status": "failed", "errors": graph_errors}

    # 6. Infer strategy
    strategy = dag_data.get("strategy", "")
    if strategy not in VALID_STRATEGIES:
        strategy = _infer_strategy(normalized)

    # 7. Persist DAG nodes to database
    # 6b. Compute leaf set (#97): node is a leaf if nothing depends on it.
    referenced = set()
    for t in normalized:
        for dep in t.get("depends_on", []) or []:
            referenced.add(dep)
    leaf_keys = {t["id"] for t in normalized if t["id"] not in referenced}

    # §17.475: deliverable fallback. If the model marked no node as the
    # deliverable (old prompt, omission, or a non-conforming draw), fall back
    # to the topological leaves so compile's primary (explicit) path still
    # fires. When the model DID mark deliverable(s), honor exactly those.
    if not any(t.get("is_deliverable") for t in normalized):
        for t in normalized:
            t["is_deliverable"] = t["id"] in leaf_keys

    # §17.669 — clear MID-GRAPH deliverable over-marks (§17.475: a setup step is
    # never the deliverable). Runs after the graph is fully connected (§17.668),
    # so "terminal" is computed on the final edges.
    _demarked = _enforce_deliverable_marking(normalized)
    if _demarked:
        validator_warnings.append(
            f"deliverable_overmark_cleared: {', '.join(_demarked)} were marked "
            f"is_deliverable but are mid-graph setup steps — cleared (§17.475)"
        )
        logger.warning(
            "dag_deliverable_overmark_cleared: job=%s nodes=%s", job_id, _demarked,
        )

    try:
        for i, task in enumerate(normalized):
            await db.execute(
                text("""
                    INSERT INTO dag_nodes
                        (job_id, node_key, title, node_type, status,
                         depends_on, assigned_model, prompt_template,
                         execution_order, tool, domain, is_output_node,
                         is_deliverable)
                    VALUES
                        (:job_id, :node_key, :title, :node_type, 'pending',
                         :depends_on, :assigned_model, :prompt_template,
                         :execution_order, :tool, :domain, :is_output_node,
                         :is_deliverable)
                """),
                {
                    "job_id": uid,
                    "node_key": task["id"],
                    "title": task["name"],
                    "node_type": _map_node_type(task["type"]),
                    "depends_on": task.get("depends_on", []),
                    "assigned_model": task.get("assigned_model"),
                    "prompt_template": task.get("notes"),
                    "execution_order": i,
                    "tool": task.get("tool", "LLM"),
                    "domain": task.get("domain"),
                    "is_output_node": task["id"] in leaf_keys,
                    "is_deliverable": bool(task.get("is_deliverable", False)),
                },
            )

        # 8. Transition job to executing and persist the input hash so a
        # re-entry with the same inputs is recognized as idempotent
        # (§17.181). The hash is stored *after* successful node persist so a
        # mid-flight failure doesn't leave a hash pointing at no DAG.
        await db.execute(
            text("""
                UPDATE jobs
                SET status = 'executing',
                    dag_input_hash = :h
                WHERE id = :id
            """),
            {"id": uid, "h": current_hash},
        )
        await db.commit()
    except Exception as exc:
        await db.rollback()
        logger.exception("dag_insert_failed: job=%s", job_id)
        await _fail_job(db, uid, f"DAG persistence failed: {exc}")
        return {"job_id": job_id, "status": "failed", "error": f"DAG persistence failed: {exc}"}
    logger.info("dag_generated: job=%s node_count=%d", job_id, len(normalized))

    # 9. Generate Mermaid diagram
    mermaid = _render_mermaid(normalized, edges)

    return {
        "job_id": job_id,
        "status": "executing",
        "strategy": strategy,
        "task_count": len(normalized),
        "tasks": normalized,
        "edges": edges,
        "warnings": warnings,
        "mermaid_dag": mermaid,
        "model_used": gen_result["model"],
        "duration_ms": gen_result["duration_ms"],
        "validator_attempts": gen_result["attempts"],
        "validator_calls": gen_result["validator_calls"],
    }


# ---------------------------------------------------------------------------
# Node count enforcement
# ---------------------------------------------------------------------------

def _enforce_node_count(
    tasks: list[dict], min_count: int = 3, max_count: int = 10
) -> list[dict]:
    """Enforce node count bounds. Truncates excess nodes and cleans dangling refs."""
    if len(tasks) < min_count:
        # #23: undercount is a hard failure now. generate_dag catches ValueError
        # at the validate_dag boundary and rolls the job to failed via _fail_job.
        raise ValueError(
            f"dag_undercount: got {len(tasks)} tasks, required minimum {min_count}"
        )

    if len(tasks) > max_count:
        # §17.615 (audit #14) — topology-aware truncation. The terminal
        # synthesis/output node is a SINK (nothing depends on it) and is
        # typically the highest-numbered, so the old tail-drop silently deleted
        # the deliverable — and the persist-time fallback then re-marked a
        # mutilated leaf as deliverable, so the user's requested artifact was
        # never produced with NO error. Preserve sink (terminal/deliverable)
        # nodes and drop from the middle (higher-numbered non-sinks) instead.
        def _num(t: dict) -> int:
            return int(re.sub(r"\D", "", t.get("id", "0")) or "0")

        depended_upon = {d for t in tasks for d in t.get("depends_on", [])}
        sinks = [t for t in tasks if t["id"] not in depended_upon]
        non_sinks = sorted((t for t in tasks if t["id"] in depended_upon), key=_num)

        if len(sinks) >= max_count:
            # Pathological: more terminal nodes than the cap — can't keep them
            # all. Keep the lowest-numbered sinks; the deliverable is still among
            # the preserved terminals.
            logger.warning(
                "dag_truncate_sink_overflow: sinks=%d exceeds max_count=%d",
                len(sinks), max_count,
            )
            kept = sorted(sinks, key=_num)[:max_count]
        else:
            slots = max_count - len(sinks)
            kept = sorted(non_sinks[:slots] + sinks, key=_num)

        kept_keys = {t["id"] for t in kept}
        dropped_keys = {t["id"] for t in tasks} - kept_keys

        # Rewrite depends_on to remove references to dropped nodes
        for task in kept:
            task["depends_on"] = [
                d for d in task.get("depends_on", []) if d in kept_keys
            ]

        logger.warning(
            "dag_truncated: original_count=%d kept_count=%d dropped_keys=%s sinks_preserved=%d",
            len(tasks), len(kept), sorted(dropped_keys), len(sinks),
        )
        return kept

    return tasks


# ---------------------------------------------------------------------------
# Task normalization (from WA tool logic)
# ---------------------------------------------------------------------------

def _normalize_tasks(tasks: list[dict]) -> tuple[list[dict], list[str], list[str]]:
    """Normalize and validate task list. Returns (tasks, errors, warnings).

    #26: warnings list surfaces silent coercions (unknown type/tool defaulting)
    and #25 Milvus-without-domain to the caller instead of log-only.
    """
    errors: list[str] = []
    warnings: list[str] = []
    normalized: list[dict] = []
    seen_ids: set[str] = set()

    for i, raw in enumerate(tasks):
        if not isinstance(raw, dict):
            errors.append(f"Task {i}: must be an object")
            continue

        task_id = str(raw.get("id", "")).strip()
        name = str(raw.get("name", "")).strip()
        task_type = str(raw.get("type", "")).strip()

        if not task_id:
            errors.append(f"Task {i}: missing 'id'")
            continue
        if not name:
            errors.append(f"Task {i}: missing 'name'")
            continue  # #99
        # §17.507 — the 5-word cap (#104) is a stylistic guideline given to the
        # LLM, not a data constraint (the `title` column is unbounded). A
        # non-deterministic model occasionally returns a 6-7 word name; failing
        # the ENTIRE DAG build over one aesthetic miss (and marking the job
        # `failed`) is wrong. Coerce-with-warning instead — truncate to 5 words
        # — consistent with the unknown-type/tool coercions below. Full detail
        # still lives in the node's `notes`/description.
        name_words = name.split()
        if len(name_words) > 5:  # #104
            truncated = " ".join(name_words[:5])
            msg = (
                f"Task {task_id or i}: name >5 words, truncated "
                f"{name!r} -> {truncated!r}"
            )
            logger.warning(msg)
            warnings.append(msg)
            name = truncated
        if task_type not in VALID_TASK_TYPES:
            msg = f"Task {task_id}: unknown type '{task_type}', coercing to 'action'"  # #26
            logger.warning(msg)
            warnings.append(msg)
            task_type = "action"
        if task_id in seen_ids:
            errors.append(f"Task {i}: duplicate id '{task_id}'")
            continue

        seen_ids.add(task_id)

        depends_on = raw.get("depends_on", [])
        if not isinstance(depends_on, list):
            depends_on = []

        task = {
            "id": task_id,
            "name": name,
            "type": task_type,
            "inputs": raw.get("inputs", []) if isinstance(raw.get("inputs"), list) else [],
            "outputs": raw.get("outputs", []) if isinstance(raw.get("outputs"), list) else [],
            "depends_on": [str(d).strip() for d in depends_on if str(d).strip()],
            "tool": str(raw.get("tool", "LLM")).strip(),
        }
        # Preserve domain for Milvus nodes (validated against VALID_DOMAINS)
        # #108: compute once, reuse for gate + validity check
        raw_domain = raw.get("domain")
        domain_val = str(raw_domain).strip().lower() if raw_domain else ""
        if domain_val and domain_val not in ("none", "null"):
            if domain_val in VALID_DOMAINS:
                task["domain"] = domain_val
            else:
                logger.warning(
                    "invalid_domain_defaulted: node_key=%s original_domain=%s",
                    task_id, raw_domain,
                )
        if task["tool"] not in VALID_TOOLS:
            msg = f"Task {task_id}: unknown tool '{task['tool']}', coercing to 'LLM'"  # #26
            logger.warning(msg)
            warnings.append(msg)
            task["tool"] = "LLM"
        raw_model = str(raw.get("assigned_model", "")).strip()
        if raw_model and raw_model.lower() not in ("none", "null", ""):
            task["assigned_model"] = raw_model
        # §17.427 — do NOT auto-assign a model to CodeGen nodes. Leaving
        # assigned_model unset lets execution_agent route them through the
        # `model_coder` role (config.py:196 → execution_agent.py:799). The old
        # hardcode here (since the Apr-2 initial commit) set "qwen2.5-coder:7b",
        # which made `_assigned` truthy and silently bypassed the model_coder
        # role — pinning ALL generated code to the local specialized coder that
        # §17.346's A/B test explicitly rejected (21× slower on this CPU AND it
        # ignored the no-markdown-fences instruction the cloud model honored).
        if raw.get("notes"):
            task["notes"] = str(raw["notes"]).strip()

        # §17.475: model-asserted deliverable marker. Truthy-coerce; default
        # False. A persist-time leaf fallback (in generate_dag) fills this when
        # the model marks no node, so old prompts / omissions still compile.
        task["is_deliverable"] = bool(raw.get("is_deliverable", False))

        # #25: Milvus nodes require a domain; warn if missing or dropped as invalid
        if task.get("tool") == "Milvus" and not task.get("domain"):
            msg = f"Task {task_id}: Milvus tool requires 'domain' field; none set"
            logger.warning(msg)
            warnings.append(msg)

        normalized.append(task)

    return normalized, errors, warnings


# ---------------------------------------------------------------------------
# DAG semantic validation (standalone, unit-testable)
# ---------------------------------------------------------------------------

def validate_dag(nodes: list[dict]) -> tuple[list[dict], list[str]]:
    """Validate and clean a parsed DAG node list.

    Performs:
      - Dependency reference validation (strips invalid refs)
      - Self-reference removal
      - Tool validation (defaults invalid tools to 'LLM')
      - Cycle detection via topological sort

    Returns (cleaned_nodes, warnings). Raises ValueError on cycles.
    """
    warnings: list[str] = []
    valid_keys = {n["id"] for n in nodes}

    for node in nodes:
        nk = node["id"]

        # ── Tool validation ──
        if node.get("tool") not in VALID_TOOLS:
            original = node.get("tool")
            node["tool"] = "LLM"
            msg = f"invalid_tool_defaulted: node_key={nk} original_tool={original} defaulted_to=LLM"
            logger.warning(msg)
            warnings.append(msg)

        # ── Self-reference removal ──
        if nk in node.get("depends_on", []):
            node["depends_on"] = [d for d in node["depends_on"] if d != nk]
            msg = f"self_reference_removed: node_key={nk}"
            logger.warning(msg)
            warnings.append(msg)

        # ── Invalid dependency removal ──
        cleaned_deps: list[str] = []
        for dep in node.get("depends_on", []):
            if dep in valid_keys:
                cleaned_deps.append(dep)
            else:
                msg = (
                    f"invalid_dependency: node_key={nk} "
                    f"invalid_ref={dep} valid_keys={sorted(valid_keys)}"
                )
                logger.warning(msg)
                warnings.append(msg)
        node["depends_on"] = cleaned_deps

    # ── Cycle detection (Kahn's topological sort) ──
    in_degree: dict[str, int] = {n["id"]: 0 for n in nodes}
    adjacency: dict[str, list[str]] = {n["id"]: [] for n in nodes}
    for node in nodes:
        for dep in node["depends_on"]:
            adjacency[dep].append(node["id"])
            in_degree[node["id"]] += 1

    queue: deque[str] = deque(k for k, v in in_degree.items() if v == 0)
    sorted_count = 0
    while queue:
        cur = queue.popleft()
        sorted_count += 1
        for neighbor in adjacency[cur]:
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)

    if sorted_count != len(nodes):
        cycle_nodes = [k for k, v in in_degree.items() if v > 0]
        msg = f"dag_cycle_detected: involved_keys={cycle_nodes}"
        logger.error(msg)
        raise ValueError(msg)

    return nodes, warnings


# ---------------------------------------------------------------------------
# Graph validation (cycle detection via Kahn's algorithm)
# ---------------------------------------------------------------------------

def _build_edges(tasks: list[dict]) -> list[dict]:
    """Build edge list from task dependencies."""
    edges = []
    for task in tasks:
        for dep in task.get("depends_on", []):
            edges.append({"from": dep, "to": task["id"]})
    return edges


def _validate_graph(tasks: list[dict], edges: list[dict]) -> tuple[list[str], list[str]]:
    """Validate DAG structure (roots, leaves, connectivity). Returns (errors, warnings)."""
    errors: list[str] = []
    warnings: list[str] = []
    ids = [t["id"] for t in tasks]
    id_set = set(ids)


    # Check for roots and leaves
    sources = {e["from"] for e in edges}
    targets = {e["to"] for e in edges}
    roots = id_set - targets
    # #27: removed dead `leaves = id_set - sources`

    if not roots and len(ids) > 1:
        errors.append("No root node found (every task has a dependency)")

    # Check for disconnected nodes
    connected = sources | targets
    for tid in ids:
        if tid not in connected and len(ids) > 1:
            warnings.append(f"Task '{tid}' is disconnected from the graph")

    return errors, warnings


# ---------------------------------------------------------------------------
# Strategy inference (from WA tool logic)
# ---------------------------------------------------------------------------

def _infer_strategy(tasks: list[dict]) -> str:
    """Infer decomposition strategy from task structure."""
    task_map = {t["id"]: t for t in tasks}

    if any(t.get("type") == "decision" for t in tasks):
        return "conditional"

    parent_counts: dict[str, int] = {}
    has_join = False
    for task in tasks:
        deps = task.get("depends_on", [])
        if len(deps) > 1:
            has_join = True
        for dep in deps:
            parent_counts[dep] = parent_counts.get(dep, 0) + 1

    has_branch = any(c > 1 for c in parent_counts.values())

    if has_branch and has_join:
        return "hybrid"
    if has_branch:
        return "parallel"
    return "sequential"


# ---------------------------------------------------------------------------
# Mermaid rendering
# ---------------------------------------------------------------------------

def _render_mermaid(tasks: list[dict], edges: list[dict]) -> str:
    """Generate Mermaid flowchart from tasks and edges.

    #102: render even 1-2 task DAGs; Mermaid handles single nodes fine.
    Empty task list still returns empty string to avoid malformed output.
    """
    if not tasks:
        return ""

    lines = ["flowchart TD"]
    names = {t["id"]: t["name"] for t in tasks}
    for edge in edges:
        src, tgt = edge["from"], edge["to"]
        src_label = _safe_label(names.get(src, src))
        tgt_label = _safe_label(names.get(tgt, tgt))
        lines.append(f"  {src}[{src_label}] --> {tgt}[{tgt_label}]")
    return "\n".join(lines)


def _safe_label(value: str) -> str:
    # #28: escape all Mermaid-breaking chars, not just square brackets
    replacements = [
        ("[", "("), ("]", ")"),
        ("{", "("), ("}", ")"),
        ("|", "/"), ('"', "'"),
        ("#", "No."),
    ]
    for old, new in replacements:
        value = value.replace(old, new)
    return value


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _brief_with_operator_decision(brief_data: dict, research_data) -> dict:
    """§17.663 — merge the research-surfaced operator decision (§17.662's
    ``research_data['options']``) into the brief as ``operator_decision``, so
    generate_dag's prompt directs an explicit ``decision`` node.

    No-op (returns ``brief_data`` unchanged) when: it isn't a dict, it already
    carries an ``operator_decision``, ``research_data`` is missing/malformed, or
    the options payload has no non-empty ``options`` list. Never raises."""
    if not isinstance(brief_data, dict) or "operator_decision" in brief_data:
        return brief_data
    try:
        rd = research_data if isinstance(research_data, dict) else (
            json.loads(research_data) if research_data else None
        )
    except (ValueError, TypeError):
        return brief_data
    options = (rd or {}).get("options") if isinstance(rd, dict) else None
    if isinstance(options, dict) and options.get("options"):
        return {**brief_data, "operator_decision": options}
    return brief_data


def _map_node_type(task_type: str) -> str:
    """Map WA task types to dag_nodes.node_type enum."""
    mapping = {
        "research": "task",
        "decision": "decision",
        "action": "task",
        "validation": "checkpoint",
        "output": "task",
    }
    return mapping.get(task_type, "task")


# ---------------------------------------------------------------------------
# Sprint W.5 — Subgraph prompt-template regeneration
# ---------------------------------------------------------------------------

REGEN_SYSTEM = """You are updating short execution hints for downstream tasks after an upstream task changed.

You will be given:
  - the project goal
  - the changed root node + its NEW output (what the human just submitted)
  - a list of downstream nodes that depend on the root, each with current
    title, depends_on, and current execution hint (prompt_template)

For each downstream node, decide if its hint still aligns with the new
root output. Rewrite hints that no longer fit; leave aligned hints alone.

Rules:
  - Hints are short — one sentence, max 20 words.
  - Do NOT invent new tasks or change the title field; only rewrite the hint.
  - If a hint is fine as-is, return it unchanged in the output (or omit).
  - Only return updates for node_keys that appeared in the input.

OUTPUT FORMAT (strict JSON, no markdown fences):
{
  "updates": [
    {"node_key": "T2", "new_template": "Implement Rust parser using nom"},
    {"node_key": "T3", "new_template": "Write README documenting the Rust API"}
  ]
}

Return ONLY the JSON object."""


REGEN_PROMPT = """PROJECT GOAL:
{goal}

CHANGED ROOT NODE:
- node_key: {root_key}
- title: {root_title}
- new_output:
{root_output}

DOWNSTREAM NODES (depend transitively on the root):
{subgraph_yaml}

Rewrite hints that no longer align. Return ONLY the JSON."""


async def regenerate_subgraph(
    *,
    job_id: str,
    root_node_key: str,
    root_evidence: str,
    affected_keys: list[str],
    db: AsyncSession,
    model_overrides: dict | None = None,
) -> dict:
    """Rewrite ``prompt_template`` for nodes whose upstream just changed.

    Called by ``assist_replan.apply_selective_replan`` (or any future caller
    that wants a fresh hint for an affected subgraph). Fail-open: if the LLM
    call errors, returns malformed JSON, or the schema is wrong, returns
    {"regenerated": 0, "errors": [...]} and persists no template changes.

    Args:
        job_id: parent job UUID.
        root_node_key: the node whose output triggered the replan
            (its new ``output_text`` is in ``root_evidence``).
        root_evidence: the human-supplied output that diverged.
        affected_keys: the BFS-computed list of nodes that depend on
            ``root_node_key``. Empty list short-circuits.
        db: an open AsyncSession.
        model_overrides: optional per-call model-router overrides.

    Returns:
        dict with keys ``regenerated`` (int — count of UPDATE statements
        committed) and ``errors`` (list of strings — diagnostics).
    """
    if not affected_keys:
        return {"regenerated": 0, "errors": []}

    if not settings.assist_replan_regen_enabled:
        return {"regenerated": 0, "errors": ["regen_disabled"]}

    rows = (await db.execute(
        text("""
            SELECT j.refined_brief, n.node_key, n.title, n.prompt_template,
                   n.depends_on
              FROM dag_nodes n
              JOIN jobs j ON j.id = n.job_id
             WHERE n.job_id = :jid
               AND n.node_key = ANY(:keys)
        """),
        {"jid": job_id, "keys": affected_keys},
    )).mappings().all()

    if not rows:
        return {"regenerated": 0, "errors": ["subgraph_not_found"]}

    brief = rows[0]["refined_brief"] or {}
    if isinstance(brief, str):
        try:
            brief = json.loads(brief)
        except (ValueError, TypeError):
            brief = {}
    goal = (brief.get("description") or "").strip() if isinstance(brief, dict) else ""
    if not goal and isinstance(brief, dict):
        goals = brief.get("goals") or []
        if isinstance(goals, list) and goals:
            goal = str(goals[0])

    # Fetch root title for context.
    root_row = (await db.execute(
        text("SELECT title FROM dag_nodes WHERE job_id = :jid AND node_key = :nk"),
        {"jid": job_id, "nk": root_node_key},
    )).mappings().first()
    root_title = (root_row or {}).get("title") or "(unknown)"

    subgraph_lines = []
    for r in rows:
        deps = r.get("depends_on") or []
        deps_str = ", ".join(deps) if isinstance(deps, list) else str(deps)
        subgraph_lines.append(
            f"- node_key: {r['node_key']}\n"
            f"  title: {r['title']}\n"
            f"  depends_on: [{deps_str}]\n"
            f"  current_hint: {r.get('prompt_template') or '(none)'}"
        )
    subgraph_yaml = "\n".join(subgraph_lines)

    prompt = REGEN_PROMPT.format(
        goal=goal or "(unspecified)",
        root_key=root_node_key,
        root_title=root_title,
        root_output=(root_evidence or "")[:4000],
        subgraph_yaml=subgraph_yaml,
    )

    route_kwargs = {"role": "model_general"}
    if model_overrides:
        route_kwargs["overrides"] = model_overrides

    try:
        resp = await model_router.generate(
            prompt,
            system=REGEN_SYSTEM,
            temperature=0.2,
            max_tokens=settings.assist_replan_regen_max_tokens,
            **route_kwargs,
        )
    except Exception as exc:
        logger.warning("regen_subgraph_call_failed: job=%s error=%s", job_id, exc)
        return {"regenerated": 0, "errors": [f"call_failed: {exc}"]}

    if not resp.success:
        logger.warning(
            "regen_subgraph_response_unsuccessful: job=%s error=%s",
            job_id, resp.error,
        )
        return {"regenerated": 0, "errors": [f"response_unsuccessful: {resp.error}"]}

    parsed = parse_json_object(resp.text)
    if not isinstance(parsed, dict):
        logger.warning("regen_subgraph_parse_failed: raw=%r", (resp.text or "")[:200])
        return {"regenerated": 0, "errors": ["json_parse_failed"]}

    raw_updates = parsed.get("updates")
    if not isinstance(raw_updates, list):
        logger.warning("regen_subgraph_schema_mismatch: parsed=%r", parsed)
        return {"regenerated": 0, "errors": ["schema_mismatch"]}

    affected_set = set(affected_keys)
    regenerated = 0
    skipped: list[str] = []
    for raw in raw_updates:
        if not isinstance(raw, dict):
            continue
        nk = str(raw.get("node_key", "")).strip()
        new_template = str(raw.get("new_template", "")).strip()
        if not nk or not new_template:
            continue
        if nk not in affected_set:
            skipped.append(nk)
            continue
        await db.execute(
            text("""
                UPDATE dag_nodes
                   SET prompt_template = :tpl, updated_at = NOW()
                 WHERE job_id = :jid AND node_key = :nk
            """),
            {"tpl": new_template, "jid": job_id, "nk": nk},
        )
        regenerated += 1

    if regenerated:
        await db.commit()

    errors: list[str] = []
    if skipped:
        errors.append(f"ignored_unaffected_nodes: {','.join(sorted(set(skipped)))}")
    logger.info(
        "regen_subgraph_complete: job=%s root=%s affected=%d regenerated=%d skipped=%d",
        job_id, root_node_key, len(affected_keys), regenerated, len(skipped),
    )
    return {"regenerated": regenerated, "errors": errors}



