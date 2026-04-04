"""
Patch 6: Milvus log enrichment
Replaces flat string Milvus logs with structured fields for retrieval and reranking.
"""
import sys

FILE = "/home/aedefruscio/scaffold-engine/app/modules/execution_agent.py"

with open(FILE) as f:
    src = f.read()

original = src

# --- 6a. Replace _milvus_search with enriched version ---
src = src.replace(
    '''async def _milvus_search(query: str) -> str:
    """Call query_rag(), return formatted context."""
    try:
        rag_result = await query_rag(query, domain=None, top_k=5)
        results = rag_result.get("results", [])
        if not results:
            return "No knowledge base results found."
        lines = []
        for i, doc in enumerate(results, 1):
            topic = doc.get("topic", "Unknown")
            content = doc.get("content", "")[:500]
            lines.append(f"[{i}] {topic}\\n    {content}")
        return "\\n\\n".join(lines)
    except Exception as e:
        logger.warning("milvus_search_failed: %s", e)
        return f"Knowledge base search failed: {e}"''',
    '''async def _milvus_search(query: str, node_key: str = "?") -> str:
    """Call query_rag(), return formatted context with structured logging."""
    try:
        rag_result = await query_rag(query, domain=None, top_k=5)
        results = rag_result.get("results", [])
        metadata = rag_result.get("metadata", {})

        # Structured retrieval log
        domains_found = set(r.get("domain", "unknown") for r in results)
        formatted_lines = []
        for i, doc in enumerate(results, 1):
            topic = doc.get("topic", "Unknown")
            content = doc.get("content", "")[:500]
            formatted_lines.append(f"[{i}] {topic}\\n    {content}")
        formatted = "\\n\\n".join(formatted_lines) if formatted_lines else ""
        total_chars = len(formatted)

        logger.info(
            "milvus_retrieval",
            extra=dict(
                event="milvus_retrieval",
                node_key=node_key,
                domain=",".join(sorted(domains_found)) if domains_found else "all",
                top_k=5,
                results_returned=len(results),
                total_chars_injected=total_chars,
                reranker_used=metadata.get("reranked", False),
            ),
        )

        # Structured rerank log (if reranking was used)
        if metadata.get("reranked", False):
            top_score = 0.0
            if results:
                scores = [r.get("scores", {}).get("rerank", 0.0) for r in results]
                top_score = max(scores) if scores else 0.0
            logger.info(
                "milvus_rerank",
                extra=dict(
                    event="milvus_rerank",
                    node_key=node_key,
                    candidates_in=metadata.get("fused_count", 0),
                    candidates_out=len(results),
                    top_score=round(top_score, 4),
                ),
            )

        if not results:
            return "No knowledge base results found."
        return formatted
    except Exception as e:
        logger.warning(
            "milvus_search_failed",
            extra=dict(event="milvus_search_failed", node_key=node_key, error=str(e)),
        )
        return f"Knowledge base search failed: {e}"''',
)

# --- 6b. Update the two call sites to pass node_key ---

# Call site 1: in _dispatch_tool (line ~397)
src = src.replace(
    '''    if tool == "Milvus":
        logger.info("tool_dispatch: %s rag_search node=%s", tool, nk)
        rag_results = await _milvus_search(task)''',
    '''    if tool == "Milvus":
        logger.info("tool_dispatch: %s rag_search node=%s", tool, nk)
        rag_results = await _milvus_search(task, node_key=nk)''',
)

# Call site 2: in execute_next_node (line ~557)
src = src.replace(
    '''    elif tool == "Milvus":
        milvus_results = await _milvus_search(title)
        exec_prompt = f"{exec_prompt}\\n\\n## Knowledge Base Results\\n{milvus_results}"
        logger.info("milvus_context_injected: chars=%d node='%s'", len(milvus_results), title)''',
    '''    elif tool == "Milvus":
        milvus_results = await _milvus_search(title, node_key=node["node_key"])
        exec_prompt = f"{exec_prompt}\\n\\n## Knowledge Base Results\\n{milvus_results}"''',
)

if src == original:
    print("ERROR: No replacements applied — source text did not match.")
    sys.exit(1)

with open(FILE, "w") as f:
    f.write(src)

print("PATCH 6 APPLIED: Milvus log enrichment")
print("  - _milvus_search now logs event='milvus_retrieval' with structured fields")
print("  - Reranking logged as event='milvus_rerank' with candidates_in/out, top_score")
print("  - Removed flat 'milvus_context_injected' string log")
print("  - Both call sites updated to pass node_key")
