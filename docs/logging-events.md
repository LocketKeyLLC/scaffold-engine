# Scaffold Engine — Logging Event Catalog

> **Convention:** `snake_case`, past-tense verb-noun. All events use positional `%s` format (no kwargs) unless noted as `extra=dict(...)` style.
> **Output:** stdout (console) + `/var/log/scaffold/app.jsonl` (RotatingFileHandler 50MB×3).
> **Context:** `request_id` propagated via `structlog.contextvars`.
> **Event count:** 56 structured events across 13 source files.
> **Last audited:** 2026-04-04 (post-4.15 commit cleanup)

---

## Engine Lifecycle

### `engine_started`
- **Level:** INFO | **Source:** `app/main.py`

| Field | Type | Required | Description |
|---|---|---|---|
| `log_level` | string | yes | Configured log level |

### `engine_stopped`
- **Level:** INFO | **Source:** `app/main.py`
- **Fields:** none

### `ollama_connected`
- **Level:** INFO | **Source:** `app/main.py`

| Field | Type | Required | Description |
|---|---|---|---|
| `models_available` | int | yes | Number of models from Ollama API |

### `ollama_connection_failed`
- **Level:** WARNING | **Source:** `app/main.py`

| Field | Type | Required | Description |
|---|---|---|---|
| `url` | string | yes | Ollama base URL attempted |
| `error` | string | yes | Exception message |

### `milvus_connected`
- **Level:** INFO | **Source:** `app/main.py`

| Field | Type | Required | Description |
|---|---|---|---|
| `uri` | string | yes | Milvus connection URI |

### `milvus_connection_failed`
- **Level:** WARNING | **Source:** `app/main.py`

| Field | Type | Required | Description |
|---|---|---|---|
| `uri` | string | yes | Milvus URI attempted |
| `error` | string | yes | Exception message |

---

## Job Lifecycle

### `job_created`
- **Level:** INFO | **Source:** `app/modules/idea_refinement.py`

| Field | Type | Required | Description |
|---|---|---|---|
| `job` | uuid | yes | Job ID |

### `job_refined`
- **Level:** INFO | **Source:** `app/modules/idea_refinement.py`

| Field | Type | Required | Description |
|---|---|---|---|
| `job` | uuid | yes | Job ID |

### `job_failed`
- **Level:** ERROR | **Source:** `app/modules/idea_refinement.py`

| Field | Type | Required | Description |
|---|---|---|---|
| `job` | uuid | yes | Job ID |
| `error` | string | yes | Failure reason |

### `job_autocompleted`
- **Level:** INFO | **Source:** `app/modules/execution_agent.py`

| Field | Type | Required | Description |
|---|---|---|---|
| `job` | uuid | yes | Job ID |

### `stale_job_cleaned`
- **Level:** INFO | **Source:** `app/main.py` | **Added:** 4.14

| Field | Type | Required | Description |
|---|---|---|---|
| `job_id` | uuid | yes | Cleaned job ID |
| `old_status` | string | yes | Previous status (`running` or `planning`) |
| `new_status` | string | yes | New status (`failed` or `cancelled`) |
| `age_minutes` | float | yes | Minutes since last update |

---

## Startup

### `startup_cleanup_begin`
- **Level:** INFO | **Source:** `app/main.py` | **Added:** 4.14
- **Fields:** none

### `startup_cleanup_complete`
- **Level:** INFO | **Source:** `app/main.py` | **Added:** 4.14

| Field | Type | Required | Description |
|---|---|---|---|
| `running_to_failed` | int | yes | Stale running jobs transitioned to failed |
| `planning_to_cancelled` | int | yes | Stale planning jobs transitioned to cancelled |

### `startup_cleanup_failed`
- **Level:** ERROR | **Source:** `app/main.py` | **Added:** 4.14

| Field | Type | Required | Description |
|---|---|---|---|
| `error` | string | yes | Exception message |

---

## DAG Generation

### `dag_generated`
- **Level:** INFO | **Source:** `app/modules/dag_generator.py`

| Field | Type | Required | Description |
|---|---|---|---|
| `job` | uuid | yes | Job ID |
| `node_count` | int | yes | Nodes in generated DAG |

### `dag_generation_failed`
- **Level:** ERROR | **Source:** `app/modules/dag_generator.py`

| Field | Type | Required | Description |
|---|---|---|---|
| `job` | uuid | yes | Job ID |
| `error` | string | yes | Failure reason |

### `auto_dag_generation_failed`
- **Level:** ERROR | **Source:** `app/modules/execution_agent.py`

| Field | Type | Required | Description |
|---|---|---|---|
| `job` | uuid | yes | Job ID |
| `error` | string | yes | Exception message |

### `idempotency_rejected`
- **Level:** WARNING | **Source:** `app/modules/dag_generator.py` | **Added:** 4.13

| Field | Type | Required | Description |
|---|---|---|---|
| `job` | uuid | yes | Job ID |
| `existing_nodes` | int | yes | Number of existing DAG nodes |

### `dag_truncated`
- **Level:** WARNING | **Source:** `app/modules/dag_generator.py` | **Added:** 4.13

| Field | Type | Required | Description |
|---|---|---|---|
| `original_count` | int | yes | Node count before truncation |
| `kept_count` | int | yes | Node count after truncation |
| `dropped_keys` | list | yes | Sorted list of dropped node IDs |

### `dag_undercount`
- **Level:** WARNING | **Source:** `app/modules/dag_generator.py` | **Added:** 4.13

| Field | Type | Required | Description |
|---|---|---|---|
| `node_count` | int | yes | Number of nodes generated (below minimum) |

---

## DAG Validation

### `invalid_dependency`
- **Level:** WARNING | **Source:** `app/modules/dag_generator.py` (`validate_dag`) | **Added:** 4.13

| Field | Type | Required | Description |
|---|---|---|---|
| `node_key` | string | yes | Node with the invalid reference |
| `invalid_ref` | string | yes | The invalid dependency ID |
| `valid_keys` | list | yes | Sorted list of valid node IDs |

### `self_reference_removed`
- **Level:** WARNING | **Source:** `app/modules/dag_generator.py` (`validate_dag`) | **Added:** 4.13

| Field | Type | Required | Description |
|---|---|---|---|
| `node_key` | string | yes | Node that referenced itself |

### `invalid_tool_defaulted`
- **Level:** WARNING | **Source:** `app/modules/dag_generator.py` (`validate_dag`) | **Added:** 4.13

| Field | Type | Required | Description |
|---|---|---|---|
| `node_key` | string | yes | Node with the invalid tool |
| `original_tool` | string | yes | The invalid tool name |
| `defaulted_to` | string | yes | Always `LLM` |

### `invalid_domain_defaulted`
- **Level:** WARNING | **Source:** `app/modules/dag_generator.py` (`_normalize_tasks`) | **Added:** 4.16

| Field | Type | Required | Description |
|---|---|---|---|
| `node_key` | string | yes | Node with the invalid domain |
| `original_domain` | string | yes | The invalid domain value |

### `dag_cycle_detected`
- **Level:** ERROR | **Source:** `app/modules/dag_generator.py` (`validate_dag`) | **Added:** 4.13

| Field | Type | Required | Description |
|---|---|---|---|
| `involved_keys` | list | yes | Node IDs involved in the cycle |

---

## Node Execution

### `node_execution_started`
- **Level:** INFO | **Source:** `app/modules/execution_agent.py`

| Field | Type | Required | Description |
|---|---|---|---|
| `node` | string | yes | Node title |
| `job` | uuid | yes | Job ID |
| `model` | string | yes | Model tag used |

### `node_execution_failed`
- **Level:** ERROR | **Source:** `app/modules/execution_agent.py`

| Field | Type | Required | Description |
|---|---|---|---|
| `node` | string | yes | Node title |
| `error` | string | yes | Exception message |

### `node_timeout`
- **Level:** WARNING | **Source:** `app/modules/execution_agent.py` | **Added:** 4.12
- **Format:** `extra=dict(...)` style

| Field | Type | Required | Description |
|---|---|---|---|
| `node_key` | string | yes | Node key (e.g., "T1") |
| `tool` | string | yes | Tool assigned to the node |
| `elapsed_s` | float | yes | Actual elapsed time (seconds, 1 decimal) |
| `timeout_s` | int | yes | Configured timeout limit (600) |

### `node_verification_failed`
- **Level:** WARNING | **Source:** `app/modules/execution_agent.py`

| Field | Type | Required | Description |
|---|---|---|---|
| `node` | string | yes | Node title |
| `reason` | string | yes | Verification failure reason |

### `verification_complete`
- **Level:** INFO | **Source:** `app/modules/execution_agent.py` | **Added:** 4.12
- **Format:** `extra=dict(...)` style

| Field | Type | Required | Description |
|---|---|---|---|
| `node_key` | string | yes | Node key (e.g., "T1") |
| `verified` | bool | yes | Whether verification passed |
| `confidence` | float/null | yes | Always `None` until logprob extraction is implemented |

### `node_reset`
- **Level:** INFO | **Source:** `app/modules/execution_handler.py`

| Field | Type | Required | Description |
|---|---|---|---|
| `node` | string | yes | Node key (e.g., "T1") |
| `job` | uuid | yes | Job ID |

### `compiled_output_stored`
- **Level:** INFO | **Source:** `app/modules/execution_agent.py`

| Field | Type | Required | Description |
|---|---|---|---|
| `chars` | int | yes | Character count of compiled output |
| `job` | uuid | yes | Job ID |

### `partial_compiled`
- **Level:** INFO | **Source:** `app/modules/execution_agent.py`

| Field | Type | Required | Description |
|---|---|---|---|
| `job` | uuid | yes | Job ID |
| `chars` | int | yes | Character count (0 if null) |

### `partial_compile_failed`
- **Level:** WARNING | **Source:** `app/modules/execution_agent.py`

| Field | Type | Required | Description |
|---|---|---|---|
| `job` | uuid | yes | Job ID |
| `error` | string | yes | Exception message |

### `upstream_truncated`
- **Level:** INFO | **Source:** `app/modules/execution_agent.py` | **Added:** 4.12
- **Format:** `extra=dict(...)` style

| Field | Type | Required | Description |
|---|---|---|---|
| `node_key` | string | yes | Downstream node receiving truncated input |
| `original_chars` | int | yes | Total chars before truncation |
| `truncated_chars` | int | yes | Total chars after truncation |
| `upstream_nodes` | list | yes | Node keys whose output was truncated |

---

## Pipeline Completion

### `pipeline_completed`
- **Level:** INFO | **Source:** `app/modules/execution_agent.py`

| Field | Type | Required | Description |
|---|---|---|---|
| `job` | uuid | yes | Job ID |
| `total` | int | yes | Total node count |
| `passed` | int | yes | Nodes that passed verification |
| `failed` | int | yes | Nodes that failed |
| `duration_ms` | int | yes | Total pipeline execution time (ms) |

---

## Tool Dispatch

### `tool_dispatch`
- **Level:** INFO | **Source:** `app/modules/execution_agent.py`
- **Format:** `tool_dispatch: {tool} {action} node={node_key}`
- **Actions:** `auto_skip`, `blocked_manual`, `model_coder`, `web_search`, `rag_search`, `skip`

### `tool_dispatch_unknown`
- **Level:** WARNING | **Source:** `app/modules/execution_agent.py`

| Field | Type | Required | Description |
|---|---|---|---|
| `tool` | string | yes | Unrecognized tool name |
| `node` | string | yes | Node key |

---

## Retrieval & Context Injection

### `retrieval_completed`
- **Level:** INFO | **Source:** `app/modules/rag_pipeline.py`

| Field | Type | Required | Description |
|---|---|---|---|
| `query` | string | yes | Search query (truncated 200 chars) |
| `domain` | string | yes | Domain filter or "all" |
| `n_results` | int | yes | Results after filtering |
| `top_score` | float | yes | Highest score (4 decimal) |
| `latency_ms` | float | yes | Retrieval latency (ms, 1 decimal) |

### `search_executed`
- **Level:** INFO | **Source:** `app/modules/rag_pipeline.py`

| Field | Type | Required | Description |
|---|---|---|---|
| `vector_hits` | int | yes | Raw vector result count |
| `keyword_hits` | int | yes | Raw keyword result count |
| `query` | string | yes | Query (truncated 50 chars) |

### `rag_context_injected`
- **Level:** INFO | **Source:** `app/modules/execution_agent.py`

| Field | Type | Required | Description |
|---|---|---|---|
| `chars` | int | yes | Characters injected |
| `node` | string | yes | Node title |

### `searxng_context_injected`
- **Level:** INFO | **Source:** `app/modules/execution_agent.py`

| Field | Type | Required | Description |
|---|---|---|---|
| `chars` | int | yes | Characters injected |
| `node` | string | yes | Node title |

### `milvus_retrieval`
- **Level:** INFO | **Source:** `app/modules/execution_agent.py` | **Added:** 4.12
- **Format:** `extra=dict(...)` style
- **Replaces:** `milvus_context_injected` (removed in 4.12)

| Field | Type | Required | Description |
|---|---|---|---|
| `node_key` | string | yes | Node key (e.g., "T1") |
| `domain` | string | yes | Comma-separated domains found, or "all" |
| `top_k` | int | yes | Requested result count (5) |
| `results_returned` | int | yes | Actual results returned |
| `total_chars_injected` | int | yes | Characters of context injected |
| `reranker_used` | bool | yes | Whether reranking was applied |

### `milvus_rerank`
- **Level:** INFO | **Source:** `app/modules/execution_agent.py` | **Added:** 4.12
- **Format:** `extra=dict(...)` style

| Field | Type | Required | Description |
|---|---|---|---|
| `node_key` | string | yes | Node key |
| `candidates_in` | int | yes | Fused candidates before rerank |
| `candidates_out` | int | yes | Results after rerank |
| `top_score` | float | yes | Highest rerank score (4 decimal) |

### `searxng_search_failed`
- **Level:** WARNING | **Source:** `app/modules/execution_agent.py`

| Field | Type | Required | Description |
|---|---|---|---|
| `error` | string | yes | Exception message |

### `milvus_search_failed`
- **Level:** WARNING | **Source:** `app/modules/execution_agent.py` | **Updated:** 4.12
- **Format:** `extra=dict(...)` style

| Field | Type | Required | Description |
|---|---|---|---|
| `node_key` | string | yes | Node key where search failed |
| `error` | string | yes | Exception message |

---

## Reranker

### `crossencoder_loading`
- **Level:** INFO | **Source:** `app/rerankers.py`

| Field | Type | Required | Description |
|---|---|---|---|
| `model` | string | yes | CrossEncoder model name |

### `crossencoder_loaded`
- **Level:** INFO | **Source:** `app/rerankers.py`

| Field | Type | Required | Description |
|---|---|---|---|
| `elapsed_s` | float | yes | Load time (seconds, 1 decimal) |

### `crossencoder_load_failed`
- **Level:** ERROR | **Source:** `app/rerankers.py`

| Field | Type | Required | Description |
|---|---|---|---|
| `error` | string | yes | Exception message |

### `reranker_completed`
- **Level:** INFO | **Source:** `app/rerankers.py`

| Field | Type | Required | Description |
|---|---|---|---|
| `docs` | int | yes | Documents reranked |
| `elapsed_ms` | float | yes | Inference latency (ms) |
| `top_score` | float | yes | Highest rerank score (4 decimal) |

### `crossencoder_inference_failed`
- **Level:** WARNING | **Source:** `app/rerankers.py`

| Field | Type | Required | Description |
|---|---|---|---|
| `error` | string | yes | Exception message |

### `reranker_fallback_activated`
- **Level:** WARNING | **Source:** `app/rerankers.py`
- **Fields:** none

---

## HTTP Middleware

### `http_request_completed`
- **Level:** INFO | **Source:** `app/middleware/performance.py`

| Field | Type | Required | Description |
|---|---|---|---|
| `method` | string | yes | HTTP method |
| `path` | string | yes | Request URL path |
| `status` | int | yes | Response status code |
| `duration_ms` | int | yes | Request duration (ms) |

### `http_request_failed`
- **Level:** ERROR | **Source:** `app/middleware/error_logging.py`

| Field | Type | Required | Description |
|---|---|---|---|
| `exception` | string | yes | Exception class name |
| `method` | string | yes | HTTP method |
| `path` | string | yes | Request URL path |
| `error` | string | yes | Error message |

### `blocked_node_query_failed`
- **Level:** WARNING | **Source:** `app/modules/execution_agent.py`

| Field | Type | Required | Description |
|---|---|---|---|
| `job` | uuid | yes | Job ID |
| `error` | string | yes | Exception message |

### `prompt_updated`
- **Level:** INFO | **Source:** `app/modules/prompt_inspector.py`

| Field | Type | Required | Description |
|---|---|---|---|
| `node` | string | yes | Node key |
| `job` | uuid | yes | Job ID |

---

## Diagnostic Events (Tier 3 — not yet standardized)

Verifier parse traces (7 calls), prompt optimizer debug (4 calls), gt_extractor ops (4 calls), model_router retry/fallback (3 calls), error persistence failures (2 calls). These remain as descriptive strings — defer to a future pass.
