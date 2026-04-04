"""
Patch 1: Node execution timeout
Wraps each node's Ollama inference call in asyncio.wait_for with configurable timeout.
"""
import sys

FILE = "/home/aedefruscio/scaffold-engine/app/modules/execution_agent.py"

with open(FILE) as f:
    src = f.read()

original = src  # keep for verification

# --- 1a. Add asyncio import ---
src = src.replace(
    'import logging\nfrom typing import AsyncGenerator,  Optional\nfrom uuid import UUID\nimport httpx',
    'import asyncio\nimport logging\nimport time as _time_mod\nfrom typing import AsyncGenerator,  Optional\nfrom uuid import UUID\nimport httpx',
)

# --- 1b. Add constant after logger line ---
src = src.replace(
    "logger = logging.getLogger(__name__)\n\n# ---------------------------------------------------------------------------\n# Verify system prompt",
    "logger = logging.getLogger(__name__)\n\n# ---------------------------------------------------------------------------\n# Configurable constants\n# ---------------------------------------------------------------------------\nNODE_TIMEOUT_SECONDS = 600\n\n# ---------------------------------------------------------------------------\n# Verify system prompt",
)

# --- 1c. Wrap the model_router.chat execution block (lines 561-578) ---
src = src.replace(
    '''    # 6. Execute
    try:
        messages = [{"role": "user", "content": exec_prompt}]
        resp = await model_router.chat(messages=messages, model=exec_model)
        if not resp.success:
            raise RuntimeError(resp.error or "Model returned failure")
        output = resp.text.strip()
    except Exception as e:
        logger.error("node_execution_failed: node='%s' error=%s", title, e)
        await _set_node_status(db, node_id, "failed")
        await _log_execution(db, job_id, node_id, "error", str(e))
        return {
            "status": "failed",
            "node_key": node["node_key"],
            "title": title,
            "error": str(e),
            "message": "Node failed. Review error and retry or skip.",
        }''',
    '''    # 6. Execute (with timeout guard)
    _node_t0 = _time_mod.monotonic()
    try:
        async def _run_inference():
            messages = [{"role": "user", "content": exec_prompt}]
            resp = await model_router.chat(messages=messages, model=exec_model)
            if not resp.success:
                raise RuntimeError(resp.error or "Model returned failure")
            return resp.text.strip()

        output = await asyncio.wait_for(
            _run_inference(), timeout=NODE_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        elapsed = round(_time_mod.monotonic() - _node_t0, 1)
        timeout_msg = (
            f"Node '{node['node_key']}' timed out after {elapsed}s "
            f"(limit: {NODE_TIMEOUT_SECONDS}s)"
        )
        logger.warning(
            "node_timeout",
            extra=dict(
                event="node_timeout",
                node_key=node["node_key"],
                tool=tool,
                elapsed_s=elapsed,
                timeout_s=NODE_TIMEOUT_SECONDS,
            ),
        )
        await _set_node_status(db, node_id, "failed", output=timeout_msg)
        await _log_execution(db, job_id, node_id, "error", timeout_msg)
        return {
            "status": "failed",
            "node_key": node["node_key"],
            "title": title,
            "error": timeout_msg,
            "reason": "timeout",
            "message": "Node timed out. Review timeout settings or retry.",
        }
    except Exception as e:
        logger.error("node_execution_failed: node='%s' error=%s", title, e)
        await _set_node_status(db, node_id, "failed")
        await _log_execution(db, job_id, node_id, "error", str(e))
        return {
            "status": "failed",
            "node_key": node["node_key"],
            "title": title,
            "error": str(e),
            "message": "Node failed. Review error and retry or skip.",
        }''',
)

if src == original:
    print("ERROR: No replacements applied — source text did not match.")
    sys.exit(1)

with open(FILE, "w") as f:
    f.write(src)

print("PATCH 1 APPLIED: Node execution timeout")
print(f"  - Added asyncio import")
print(f"  - Added NODE_TIMEOUT_SECONDS = 600 constant")
print(f"  - Wrapped model_router.chat in asyncio.wait_for")
