#!/bin/bash
# Combined verification sequence for all 6 patches.
# Run from host machine: bash ~/scaffold-engine/patches/verify_all.sh
#
# Prerequisites: patches are at ~/scaffold-engine/patches/group1_task{1..6}_*.py
# Target: ~/scaffold-engine/app/modules/execution_agent.py

set -e

PATCHES_DIR="$HOME/scaffold-engine/patches"
TARGET="$HOME/scaffold-engine/app/modules/execution_agent.py"

echo "============================================"
echo "STEP 0: Backup original file"
echo "============================================"
cp "$TARGET" "${TARGET}.bak.$(date +%Y%m%d_%H%M%S)"
echo "  Backup created."

echo ""
echo "============================================"
echo "STEP 1: Apply Patch 1 — Node execution timeout"
echo "============================================"
python3 "$PATCHES_DIR/group1_task1_node_timeout.py"

echo ""
echo "============================================"
echo "STEP 2: Apply Patch 2 — Concurrent execution guard"
echo "============================================"
python3 "$PATCHES_DIR/group1_task2_concurrent_guard.py"

echo ""
echo "============================================"
echo "STEP 3: Apply Patch 3 — Upstream output size management"
echo "============================================"
python3 "$PATCHES_DIR/group1_task3_upstream_truncation.py"

echo ""
echo "============================================"
echo "STEP 4: Apply Patch 4 — Structured partial compile"
echo "============================================"
python3 "$PATCHES_DIR/group1_task4_structured_partial.py"

echo ""
echo "============================================"
echo "STEP 5: Apply Patch 5 — confidence NULL + TODO"
echo "============================================"
python3 "$PATCHES_DIR/group1_task5_confidence_null.py"

echo ""
echo "============================================"
echo "STEP 6: Apply Patch 6 — Milvus log enrichment"
echo "============================================"
python3 "$PATCHES_DIR/group1_task6_milvus_logs.py"

echo ""
echo "============================================"
echo "STEP 7: Verify all patches landed"
echo "============================================"
echo "Checking for key markers..."

grep -q "NODE_TIMEOUT_SECONDS" "$TARGET" && echo "  [OK] Patch 1: NODE_TIMEOUT_SECONDS constant" || echo "  [FAIL] Patch 1"
grep -q "asyncio.wait_for" "$TARGET" && echo "  [OK] Patch 1: asyncio.wait_for" || echo "  [FAIL] Patch 1 wait_for"
grep -q "status != 'running'" "$TARGET" && echo "  [OK] Patch 2: concurrent guard" || echo "  [FAIL] Patch 2"
grep -q "MAX_UPSTREAM_CHARS" "$TARGET" && echo "  [OK] Patch 3: MAX_UPSTREAM_CHARS constant" || echo "  [FAIL] Patch 3"
grep -q "_truncate_output" "$TARGET" && echo "  [OK] Patch 3: truncation helper" || echo "  [FAIL] Patch 3 helper"
grep -q "compile_status" "$TARGET" && echo "  [OK] Patch 4: compile_status field" || echo "  [FAIL] Patch 4"
grep -qv 'PARTIAL — some nodes' "$TARGET" && echo "  [OK] Patch 4: [PARTIAL] prefix removed" || echo "  [FAIL] Patch 4 prefix"
grep -q "TODO: Populate confidence via logprob" "$TARGET" && echo "  [OK] Patch 5: confidence TODO" || echo "  [FAIL] Patch 5"
grep -q "verification_complete" "$TARGET" && echo "  [OK] Patch 5: verification log" || echo "  [FAIL] Patch 5 log"
grep -q "milvus_retrieval" "$TARGET" && echo "  [OK] Patch 6: structured milvus log" || echo "  [FAIL] Patch 6"
grep -q "milvus_rerank" "$TARGET" && echo "  [OK] Patch 6: rerank log" || echo "  [FAIL] Patch 6 rerank"

echo ""
echo "============================================"
echo "STEP 8: Rebuild container"
echo "============================================"
echo "Run:"
echo "  cd ~/scaffold-engine && docker compose up -d --build scaffold-engine"
echo ""

echo "============================================"
echo "STEP 9: Run existing tests"
echo "============================================"
echo "Run:"
echo "  docker exec scaffold-orchestrator pytest tests/test_execution_agent.py -m smoke --timeout=30 -v"
echo ""

echo "============================================"
echo "STEP 10: Smoke test — concurrent guard"
echo "============================================"
echo "Run (replace JOB_ID with a real executing job):"
echo '  # First call should succeed:'
echo '  curl -N http://localhost:8000/execute/all/JOB_ID'
echo '  # Second concurrent call should return 409-equivalent error SSE:'
echo '  curl -N http://localhost:8000/execute/all/JOB_ID'
echo ""

echo "============================================"
echo "STEP 11: Check logs after a pipeline run"
echo "============================================"
echo "Run:"
echo '  docker logs scaffold-engine 2>&1 | grep -E "node_timeout|milvus_retrieval|milvus_rerank|verification_complete|upstream_truncated"'
echo ""

echo "============================================"
echo "All patches applied. Review steps 8-11 above."
echo "============================================"
