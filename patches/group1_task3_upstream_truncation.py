"""
Patch 3: Upstream output size management
Fetches upstream node outputs, truncates proportionally if over threshold,
and injects them into downstream prompts.
"""
import sys

FILE = "/home/aedefruscio/scaffold-engine/app/modules/execution_agent.py"

with open(FILE) as f:
    src = f.read()

original = src

# --- 3a. Add MAX_UPSTREAM_CHARS constant after NODE_TIMEOUT_SECONDS ---
src = src.replace(
    "NODE_TIMEOUT_SECONDS = 600",
    "NODE_TIMEOUT_SECONDS = 600\nMAX_UPSTREAM_CHARS = 6000",
)

# --- 3b. Add truncation helper before the _fetch_rag_context function ---
src = src.replace(
    '''async def _fetch_rag_context(query: str, top_k: int = 2, domain: str | None = None) -> str:''',
    '''def _truncate_output(text: str, max_chars: int) -> str:
    """Truncate text preserving first/last 20%, with a marker in the middle."""
    if len(text) <= max_chars:
        return text
    keep = max_chars
    head_len = int(keep * 0.2)
    tail_len = int(keep * 0.2)
    removed = len(text) - head_len - tail_len
    return (
        text[:head_len]
        + f"\\n[...truncated {removed} chars...]\\n"
        + text[-tail_len:]
    )


async def _fetch_upstream_outputs(
    db, job_id: str, depends_on: list[str]
) -> dict[str, str]:
    """Fetch output_text for upstream nodes by node_key."""
    if not depends_on:
        return {}
    rows = await db.execute(
        text(
            "SELECT node_key, output_text FROM dag_nodes "
            "WHERE job_id = :jid AND node_key = ANY(:keys) AND status = 'done'"
        ),
        {"jid": job_id, "keys": depends_on},
    )
    return {r.node_key: (r.output_text or "") for r in rows.fetchall()}


async def _fetch_rag_context(query: str, top_k: int = 2, domain: str | None = None) -> str:''',
)

# --- 3c. Inject upstream outputs into the prompt, AFTER Milvus injection and BEFORE "# 6. Execute" ---
src = src.replace(
    '''    elif tool == "Milvus":
        milvus_results = await _milvus_search(title)
        exec_prompt = f"{exec_prompt}\\n\\n## Knowledge Base Results\\n{milvus_results}"
        logger.info("milvus_context_injected: chars=%d node='%s'", len(milvus_results), title)

    # 6. Execute''',
    '''    elif tool == "Milvus":
        milvus_results = await _milvus_search(title)
        exec_prompt = f"{exec_prompt}\\n\\n## Knowledge Base Results\\n{milvus_results}"
        logger.info("milvus_context_injected: chars=%d node='%s'", len(milvus_results), title)

    # 5b. Inject upstream node outputs (with size management)
    depends_on = node.get("depends_on") or []
    if depends_on:
        upstream_outputs = await _fetch_upstream_outputs(db, job_id, depends_on)
        if upstream_outputs:
            total_chars = sum(len(v) for v in upstream_outputs.values())
            truncated_keys = []
            if total_chars > MAX_UPSTREAM_CHARS:
                # Truncate each proportionally
                for nk in upstream_outputs:
                    orig_len = len(upstream_outputs[nk])
                    share = max(200, int(MAX_UPSTREAM_CHARS * orig_len / total_chars))
                    if orig_len > share:
                        upstream_outputs[nk] = _truncate_output(upstream_outputs[nk], share)
                        truncated_keys.append(nk)
                logger.info(
                    "upstream_truncated",
                    extra=dict(
                        event="upstream_truncated",
                        node_key=node["node_key"],
                        original_chars=total_chars,
                        truncated_chars=sum(len(v) for v in upstream_outputs.values()),
                        upstream_nodes=truncated_keys,
                    ),
                )
            parts = [f"### {nk}\\n{text}" for nk, text in upstream_outputs.items()]
            exec_prompt = (
                exec_prompt
                + "\\n\\n## Upstream Node Outputs\\n"
                + "\\n\\n".join(parts)
            )

    # 6. Execute''',
)

if src == original:
    print("ERROR: No replacements applied — source text did not match.")
    sys.exit(1)

with open(FILE, "w") as f:
    f.write(src)

print("PATCH 3 APPLIED: Upstream output size management")
print("  - Added MAX_UPSTREAM_CHARS = 6000 constant")
print("  - Added _truncate_output() helper (first/last 20% preserved)")
print("  - Added _fetch_upstream_outputs() DB query")
print("  - Injected upstream outputs into downstream prompts with truncation")
