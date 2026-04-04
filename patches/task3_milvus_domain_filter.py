"""
Task 3: Milvus Domain Filtering Audit — Investigation + Patch

=== WHAT TO DO FIRST ===

Read these files before applying anything:
    1. app/modules/rag_pipeline.py  — find the Milvus search/query call
    2. app/modules/execution_agent.py — find _milvus_search() and _dispatch_tool()
    3. Check what `expr` parameter (if any) is passed to collection.search()

Run this audit command to see the current filtering behavior:
    grep -n "expr\|domain\|search(" ~/scaffold-engine/app/modules/rag_pipeline.py
    grep -n "expr\|domain\|_milvus_search" ~/scaffold-engine/app/modules/execution_agent.py

=== EXPECTED FINDINGS ===

Based on the carryover (§4.12, Task 6), `_milvus_search()` in execution_agent.py
already has a `domain` variable that gets logged:
    event="milvus_retrieval" ... domain=<value> ...

But the carryover does NOT mention an `expr` filter being applied to the actual
pymilvus `collection.search()` call.  Most likely the domain is logged but not
used as a query filter — all 83 entries are searched regardless.

=== PATCH ===

Apply the changes below to add optional domain filtering at query time.
This file is a standalone patch script (same pattern as the group1/group2 patches).

Usage:
    python3 ~/scaffold-engine/patches/task3_milvus_domain_filter.py

After applying, rebuild:
    docker compose up -d --build scaffold-orchestrator
"""

import sys
import os

# ──────────────────────────────────────────────────────────────────────
# PATCH A: Add domain filter to rag_pipeline.py search call
# ──────────────────────────────────────────────────────────────────────
#
# You need to manually inspect rag_pipeline.py and find the
# collection.search() call.  It will look something like:
#
#   results = collection.search(
#       data=[embedding],
#       anns_field="embedding",
#       param=search_params,
#       limit=top_k,
#       output_fields=[...],
#   )
#
# Add an `expr` parameter:
#
#   expr = f'domain == "{domain}"' if domain else None
#
#   results = collection.search(
#       data=[embedding],
#       anns_field="embedding",
#       param=search_params,
#       limit=top_k,
#       output_fields=[...],
#       expr=expr,                  # ← NEW: optional domain filter
#   )
#
# And update the function signature to accept `domain: str | None = None`.
#
# Below is the automated patch for execution_agent.py's _milvus_search,
# which is the call site from DAG execution.
# ──────────────────────────────────────────────────────────────────────

TARGET_FILE = os.path.expanduser("~/scaffold-engine/app/modules/execution_agent.py")


def apply_patch():
    if not os.path.exists(TARGET_FILE):
        print(f"ERROR: {TARGET_FILE} not found")
        sys.exit(1)

    with open(TARGET_FILE, "r") as f:
        content = f.read()

    # ── Patch 1: Pass domain from DAG node into _milvus_search ───────
    # In _dispatch_tool(), the Milvus branch calls _milvus_search().
    # We need to extract the domain field from the DAG node and pass it.
    #
    # Find the existing call pattern (from Task 6 patch):
    #   await self._milvus_search(query_text, node_key=node_key)
    #
    # Replace with:
    #   domain_filter = node.get("domain")  # from DAG node metadata
    #   await self._milvus_search(query_text, node_key=node_key, domain=domain_filter)

    OLD_DISPATCH = 'await self._milvus_search(query_text, node_key=node_key)'
    NEW_DISPATCH = (
        'domain_filter = node.get("domain")  # from DAG node metadata\n'
        '                result = await self._milvus_search(query_text, node_key=node_key, domain=domain_filter)'
    )

    if OLD_DISPATCH in content:
        # The old pattern has "result = " or just the call — check context
        # If it's assigned: "result = await self._milvus_search(...)"
        if f"result = {OLD_DISPATCH}" in content:
            content = content.replace(
                f"result = {OLD_DISPATCH}",
                NEW_DISPATCH,
            )
        else:
            content = content.replace(
                OLD_DISPATCH,
                NEW_DISPATCH.replace("result = ", ""),
            )
        print("✓ Patch 1: domain_filter extraction added to _dispatch_tool()")
    else:
        print("⚠ Patch 1: Could not find _milvus_search call pattern — apply manually")
        print(f"  Searched for: {OLD_DISPATCH}")

    # ── Patch 2: Add domain param to _milvus_search signature ────────
    # Current signature (post-Task 6):
    #   async def _milvus_search(self, query: str, node_key: str = "?"):
    # New:
    #   async def _milvus_search(self, query: str, node_key: str = "?", domain: str | None = None):

    OLD_SIG = 'async def _milvus_search(self, query: str, node_key: str = "?")'
    NEW_SIG = 'async def _milvus_search(self, query: str, node_key: str = "?", domain: str | None = None)'

    if OLD_SIG in content:
        content = content.replace(OLD_SIG, NEW_SIG)
        print("✓ Patch 2: domain parameter added to _milvus_search() signature")
    else:
        print("⚠ Patch 2: Could not find _milvus_search signature — apply manually")

    # ── Patch 3: Pass domain into the rag_pipeline search call ───────
    # Inside _milvus_search(), there's a call to the RAG pipeline.
    # This varies — it might be:
    #   results = await rag_pipeline.search(query, top_k=...)
    #   results = rag_pipe.retrieve(query, top_k=...)
    #   results = collection.search(data=[emb], ...)
    #
    # We add the domain parameter to whatever call exists.
    # Since the exact pattern varies, this patch adds the domain
    # to the structured log (which we know exists from Task 6)
    # and prints a manual instruction for the search call itself.

    # Update the structured log to include the actual domain_filter value
    OLD_LOG = 'event="milvus_retrieval"'
    if OLD_LOG in content:
        # The log line already has domain= — update it to use the parameter
        # instead of whatever hardcoded or extracted value is there now.
        #
        # We'll add a domain_filter variable assignment before the search call.
        # The log already captures domain — we just need to ensure the search
        # actually uses it.
        print("✓ Patch 3: milvus_retrieval log event found (domain field already present)")
    else:
        print("⚠ Patch 3: milvus_retrieval log event not found")

    # ── Patch 4: Add structured query log ────────────────────────────
    # Add a log event at the top of _milvus_search:
    #   logger.info('event="milvus_query" domain_filter=%s query_text=%.100s top_k=%s',
    #               domain or "all", query[:100], top_k)

    SEARCH_BODY_MARKER = NEW_SIG
    if SEARCH_BODY_MARKER in content:
        # Find the line after the docstring or first line of the method body
        # and insert the log.  We'll add it right after the signature.
        insert_log = (
            '\n        _domain_label = domain or "all"\n'
            '        logger.info(\n'
            '            \'event="milvus_query" domain_filter=%s query_text=%.100s\',\n'
            '            _domain_label, query[:100],\n'
            '        )\n'
        )

        # Find the first line after the def that starts with whitespace
        sig_idx = content.index(SEARCH_BODY_MARKER)
        # Find end of signature line
        sig_end = content.index("\n", sig_idx) + 1
        # Skip docstring if present
        rest = content[sig_end:]
        if rest.lstrip().startswith('"""') or rest.lstrip().startswith("'''"):
            # Find closing triple-quote
            quote = rest.lstrip()[:3]
            doc_start = content.index(quote, sig_end)
            doc_end = content.index(quote, doc_start + 3) + 3
            insert_pos = content.index("\n", doc_end) + 1
        else:
            insert_pos = sig_end

        content = content[:insert_pos] + insert_log + content[insert_pos:]
        print("✓ Patch 4: milvus_query structured log added")
    else:
        print("⚠ Patch 4: Could not locate insertion point for milvus_query log")

    # ── Write ────────────────────────────────────────────────────────
    with open(TARGET_FILE, "w") as f:
        f.write(content)

    print(f"\n✓ Patches written to {TARGET_FILE}")
    print()
    print("═" * 70)
    print("MANUAL STEP REQUIRED — rag_pipeline.py domain filter")
    print("═" * 70)
    print("""
Open app/modules/rag_pipeline.py and find the collection.search() call.
Add the `expr` parameter:

    # At the top of your search/retrieve function, add domain param:
    def search(self, query: str, top_k: int = 5, domain: str | None = None):

    # Before the search call:
    expr = f'domain == "{domain}"' if domain else None

    # In the search call:
    results = collection.search(
        data=[embedding],
        anns_field="embedding",
        param=search_params,
        limit=top_k,
        output_fields=[...],
        expr=expr,          # ← ADD THIS
    )

Then update _milvus_search() in execution_agent.py to pass `domain`
through to whatever rag_pipeline function it calls:

    # e.g., change:
    results = await self.rag.search(query, top_k=top_k)
    # to:
    results = await self.rag.search(query, top_k=top_k, domain=domain)

VALID DOMAINS: prompt, rag, eng, llm, spec
""")


if __name__ == "__main__":
    apply_patch()
