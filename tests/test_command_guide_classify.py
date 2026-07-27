"""§17.628 — top-level natural-language command classification.

`command_guide.classify_command` is the engine-wide sibling of
`assist_guide.classify_turn`: it maps a plain top-level message (no active
assist session) to a READ-ONLY engine intent so the pipeline can drive the
right component by talking.

Pins:
  * a successful tool call returns the model's intent + confidence + slots;
  * the read-only intent surface is exactly the Phase-1 set (no mutating verbs
    leak in — a regression here would let a misfire write/delete);
  * fail-soft: any model error / no-tool-call / garbage intent / empty message
    degrades to intent='none' (confidence 'low') so the caller falls through to
    triage instead of misfiring a command.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules import command_guide


def _resp(args: dict | None, success: bool = True):
    r = MagicMock()
    r.success = success
    if success and args is not None:
        call = MagicMock()
        call.arguments = args
        r.tool_calls = [call]
    else:
        r.tool_calls = []
    return r


async def _classify(args, *, success=True, message="hello"):
    with patch.object(command_guide.model_router, "tool_call",
                      new=AsyncMock(return_value=_resp(args, success))):
        return await command_guide.classify_command(message=message)


# ── intent surface (Phase 1 reads + Phase 2 writes; no destructive verbs) ──


@pytest.mark.smoke
def test_intent_surface_is_complete():
    assert set(command_guide.COMMAND_INTENTS) == {
        # Phase 1 — reads
        "status", "results", "rag_query", "jobs_list", "jobs_find",
        "model_list", "model_available", "model_probe", "help",
        # Phase 4 (§17.655) — remaining safe reads
        "schedule_list", "research_list", "research_find", "logs", "cost",
        "health", "config", "work_here", "work_next",
        # Phase 7 (§17.658) — ground-truth KB reads + prompt view
        "gt_list", "gt_search", "gt_stats", "prompts_view",
        # Phase 2 — mutating/expensive
        "research_topic", "schedule_add", "model_set", "model_reset",
        "optimize", "jobs_rename",
        # Phase 5 (§17.656) — research ingest variants + session rename
        "research_url", "research_github", "research_openapi", "research_rename",
        # Phase 7 (§17.658) — ground-truth extraction (write)
        "gt_extract",
        # Phase 6 (§17.657) — workflow control (all confirmed in the pipeline)
        "confirm_job", "execute_job", "retry_node", "skip_node", "cancel_job",
        "cleanup",
        # Phase 3 — destructive (always confirmed in the pipeline)
        "jobs_delete", "schedule_delete", "research_delete",
        # safe default
        "none",
    }


@pytest.mark.smoke
def test_destructive_intents_are_the_only_delete_verbs():
    # Exactly three DELETE (permanent data-removal) intents exist; the pipeline
    # gates all behind a confirm. cancel_job is a distinct reversible workflow
    # verb (§17.657), NOT a delete. Truly destructive words still must not leak.
    deletes = [i for i in command_guide.COMMAND_INTENTS if "delete" in i]
    assert set(deletes) == {"jobs_delete", "schedule_delete", "research_delete"}
    for banned in ("purge", "wipe", "drop"):
        assert not any(banned in i for i in command_guide.COMMAND_INTENTS), banned


@pytest.mark.smoke
def test_route_tool_requires_intent_and_confidence():
    schema = command_guide._ROUTE_TOOL.input_schema
    assert schema["required"] == ["intent", "confidence"]
    assert set(schema["properties"]["intent"]["enum"]) == set(command_guide.COMMAND_INTENTS)


# ── happy paths — intent + slots ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_status_high_confidence():
    out = await _classify({"intent": "status", "confidence": "high"})
    assert out["intent"] == "status"
    assert out["confidence"] == "high"


@pytest.mark.asyncio
async def test_rag_query_carries_query_slot():
    out = await _classify({"intent": "rag_query", "confidence": "high",
                           "query": "ZFS on non-ECC RAM"})
    assert out["intent"] == "rag_query"
    assert out["query"] == "ZFS on non-ECC RAM"


@pytest.mark.asyncio
async def test_results_carries_job_ref_slot():
    out = await _classify({"intent": "results", "confidence": "high",
                           "job_ref": "proxmox"})
    assert out["intent"] == "results"
    assert out["job_ref"] == "proxmox"


@pytest.mark.asyncio
async def test_slots_are_stripped():
    out = await _classify({"intent": "jobs_find", "confidence": "high",
                           "query": "  kubernetes  "})
    assert out["query"] == "kubernetes"


# ── Phase 4 (§17.655) reads — new slot carries + no-slot intents ───────────


@pytest.mark.asyncio
async def test_research_find_carries_query():
    out = await _classify({"intent": "research_find", "confidence": "high",
                           "query": "zfs on non-ecc ram"})
    assert out["intent"] == "research_find"
    assert out["query"] == "zfs on non-ecc ram"


@pytest.mark.asyncio
@pytest.mark.parametrize("intent", ["logs", "cost"])
async def test_logs_cost_carry_job_ref(intent):
    out = await _classify({"intent": intent, "confidence": "high",
                           "job_ref": "proxmox"})
    assert out["intent"] == intent
    assert out["job_ref"] == "proxmox"


@pytest.mark.asyncio
@pytest.mark.parametrize("intent", [
    "schedule_list", "research_list", "health", "config",
    "work_here", "work_next",
])
async def test_no_slot_reads_classify(intent):
    # These carry no slot — classification alone is enough to route them.
    out = await _classify({"intent": intent, "confidence": "high"})
    assert out["intent"] == intent
    assert out["confidence"] == "high"


# ── write intents (Phase 2) carry their slots ─────────────────────────────


@pytest.mark.asyncio
async def test_research_topic_carries_topic_and_depth():
    out = await _classify({"intent": "research_topic", "confidence": "high",
                           "topic": "postgres tuning", "depth": "deep"})
    assert out["intent"] == "research_topic"
    assert out["topic"] == "postgres tuning"
    assert out["depth"] == "deep"


@pytest.mark.asyncio
async def test_invalid_depth_dropped():
    out = await _classify({"intent": "research_topic", "confidence": "high",
                           "topic": "x", "depth": "extreme"})
    assert out["depth"] == ""  # not one of shallow/medium/deep


@pytest.mark.asyncio
async def test_schedule_add_carries_cron_topic_tz():
    out = await _classify({"intent": "schedule_add", "confidence": "high",
                           "topic": "AI papers", "cron": "0 9 * * 1",
                           "tz": "America/New_York", "depth": "medium"})
    assert out["cron"] == "0 9 * * 1"
    assert out["topic"] == "AI papers"
    assert out["tz"] == "America/New_York"


@pytest.mark.asyncio
async def test_model_set_carries_role_and_name():
    out = await _classify({"intent": "model_set", "confidence": "high",
                           "model_role": "coder", "model_name": "kimi-k2.7-code:cloud"})
    assert out["model_role"] == "coder"
    assert out["model_name"] == "kimi-k2.7-code:cloud"


@pytest.mark.asyncio
async def test_optimize_carries_prompt():
    out = await _classify({"intent": "optimize", "confidence": "high",
                           "prompt": "Write a haiku about DAGs"})
    assert out["prompt"] == "Write a haiku about DAGs"


@pytest.mark.asyncio
async def test_jobs_rename_carries_ref_and_new_name():
    out = await _classify({"intent": "jobs_rename", "confidence": "high",
                           "job_ref": "homelab", "new_name": "Home Lab Setup"})
    assert out["job_ref"] == "homelab"
    assert out["new_name"] == "Home Lab Setup"


# ── Phase 5 (§17.656) research ingest variants + rename ────────────────────


@pytest.mark.asyncio
async def test_research_url_carries_url():
    out = await _classify({"intent": "research_url", "confidence": "high",
                           "url": "https://example.com/post"})
    assert out["intent"] == "research_url"
    assert out["url"] == "https://example.com/post"


@pytest.mark.asyncio
async def test_research_github_carries_repo():
    out = await _classify({"intent": "research_github", "confidence": "high",
                           "repo": "owner/repo"})
    assert out["intent"] == "research_github"
    assert out["repo"] == "owner/repo"


@pytest.mark.asyncio
async def test_research_openapi_carries_url():
    out = await _classify({"intent": "research_openapi", "confidence": "high",
                           "url": "https://api.x/openapi.json"})
    assert out["intent"] == "research_openapi"
    assert out["url"] == "https://api.x/openapi.json"


@pytest.mark.asyncio
async def test_research_rename_carries_ref_and_new_name():
    out = await _classify({"intent": "research_rename", "confidence": "high",
                           "job_ref": "zfs", "new_name": "ZFS Tuning Notes"})
    assert out["job_ref"] == "zfs"
    assert out["new_name"] == "ZFS Tuning Notes"


# ── Phase 6 (§17.657) workflow control ─────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("intent", ["confirm_job", "execute_job", "cancel_job"])
async def test_job_workflow_intents_carry_job_ref(intent):
    out = await _classify({"intent": intent, "confidence": "high",
                           "job_ref": "proxmox"})
    assert out["intent"] == intent
    assert out["job_ref"] == "proxmox"


@pytest.mark.asyncio
@pytest.mark.parametrize("intent", ["retry_node", "skip_node"])
async def test_node_workflow_intents_carry_job_ref_and_node_key(intent):
    out = await _classify({"intent": intent, "confidence": "high",
                           "job_ref": "kube", "node_key": "T3"})
    assert out["intent"] == intent
    assert out["job_ref"] == "kube"
    assert out["node_key"] == "T3"


@pytest.mark.asyncio
async def test_cleanup_intent_classifies_no_slot():
    out = await _classify({"intent": "cleanup", "confidence": "high"})
    assert out["intent"] == "cleanup"
    assert out["confidence"] == "high"


# ── Phase 7 (§17.658) ground-truth KB + prompt view ────────────────────────


@pytest.mark.asyncio
async def test_gt_search_carries_query():
    out = await _classify({"intent": "gt_search", "confidence": "high",
                           "query": "kubernetes networking"})
    assert out["intent"] == "gt_search"
    assert out["query"] == "kubernetes networking"


@pytest.mark.asyncio
async def test_gt_extract_carries_topic():
    out = await _classify({"intent": "gt_extract", "confidence": "high",
                           "topic": "zfs tuning"})
    assert out["intent"] == "gt_extract"
    assert out["topic"] == "zfs tuning"


@pytest.mark.asyncio
async def test_prompts_view_carries_job_ref():
    out = await _classify({"intent": "prompts_view", "confidence": "high",
                           "job_ref": "proxmox"})
    assert out["intent"] == "prompts_view"
    assert out["job_ref"] == "proxmox"


@pytest.mark.asyncio
@pytest.mark.parametrize("intent", ["gt_list", "gt_stats"])
async def test_gt_no_slot_reads_classify(intent):
    out = await _classify({"intent": intent, "confidence": "high"})
    assert out["intent"] == intent
    assert out["confidence"] == "high"


@pytest.mark.asyncio
async def test_fallback_carries_all_slots_empty():
    # Fail-soft dict must contain every slot key so callers can .get safely.
    with patch.object(command_guide.model_router, "tool_call",
                      new=AsyncMock(side_effect=RuntimeError("x"))):
        out = await command_guide.classify_command(message="research something")
    for k in ("topic", "depth", "cron", "tz", "model_role", "model_name",
              "prompt", "new_name", "query", "job_ref", "target_ref",
              "url", "repo", "node_key"):
        assert out[k] == ""


# ── destructive intents (Phase 3) carry target_ref ────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("intent", ["jobs_delete", "schedule_delete", "research_delete"])
async def test_delete_intents_carry_target_ref(intent):
    out = await _classify({"intent": intent, "confidence": "high",
                           "target_ref": "kubernetes"})
    assert out["intent"] == intent
    assert out["target_ref"] == "kubernetes"


# ── fail-soft → none ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_message_is_none_without_model_call():
    # Empty/whitespace short-circuits BEFORE any model call.
    with patch.object(command_guide.model_router, "tool_call",
                      new=AsyncMock()) as tc:
        out = await command_guide.classify_command(message="   ")
    assert out["intent"] == "none"
    tc.assert_not_called()


@pytest.mark.asyncio
async def test_model_exception_is_none():
    with patch.object(command_guide.model_router, "tool_call",
                      new=AsyncMock(side_effect=RuntimeError("ollama down"))):
        out = await command_guide.classify_command(message="what's running")
    assert out["intent"] == "none"
    assert out["confidence"] == "low"


@pytest.mark.asyncio
async def test_no_tool_call_is_none():
    out = await _classify(None, success=True)  # success but empty tool_calls
    assert out["intent"] == "none"


@pytest.mark.asyncio
async def test_unsuccessful_response_is_none():
    out = await _classify({"intent": "status", "confidence": "high"}, success=False)
    assert out["intent"] == "none"


@pytest.mark.asyncio
async def test_garbage_intent_is_none():
    out = await _classify({"intent": "launch_missiles", "confidence": "high"})
    assert out["intent"] == "none"


@pytest.mark.asyncio
async def test_invalid_confidence_coerced_to_low():
    out = await _classify({"intent": "status", "confidence": "banana"})
    assert out["intent"] == "status"
    assert out["confidence"] == "low"
