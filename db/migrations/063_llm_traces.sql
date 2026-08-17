-- 063_llm_traces.sql
-- §17.786 — full request/response trace capture for LLM calls.
--
-- Sprint J.3's `llm_call_logs` records the METRICS of every LLM call (tokens,
-- latency, USD cost, success) tagged by job/node — enough for cost rollups but
-- deliberately content-free. What it can't answer is "what did we actually send
-- the model, and what did it say back?" — the question that matters when a node
-- produced garbage, a prompt regressed, or a run needs replaying. This table is
-- the content sink: one row per LLM call carrying the request (prompt or
-- serialized messages + system + sampling params) and the response (text, tool
-- calls, error) alongside the same job/node/call_kind association keys, so a
-- trace JOINs 1:1 against its `llm_call_logs` metrics row on (job_id, node_id).
--
-- Content is captured only when the default-OFF master valve
-- `trace_capture_enabled` is on (storing full prompts/responses has cost + PII
-- implications), and each text field is truncated to
-- `trace_capture_max_chars` at write time. So this migration is a no-op on
-- behavior until an operator opts in.
--
-- ONE statement (single DO block) per the asyncpg "no multiple commands in a
-- prepared statement" rule (§17.140; cf. 057_assist_turns, 062_jobs_cost_budget).
-- All additive / IF NOT EXISTS — safe to re-run; no backfill.

DO $mig$
BEGIN
    CREATE TABLE IF NOT EXISTS llm_traces (
        id                BIGSERIAL PRIMARY KEY,
        job_id            UUID,
        node_id           UUID,
        call_kind         TEXT,           -- mirrors llm_call_logs.call_kind (e.g. 'synthesis')
        request_kind      TEXT NOT NULL,  -- generate | chat | tool_call | embed
        provider          TEXT NOT NULL,
        model             TEXT NOT NULL,
        system_prompt     TEXT,           -- truncated to trace_capture_max_chars
        request_content   TEXT,           -- prompt, or JSON-serialized messages (truncated)
        response_content  TEXT,           -- resp.text (truncated)
        tool_calls        JSONB,          -- serialized resp.tool_calls, NULL when none
        temperature       NUMERIC,
        max_tokens        INTEGER,
        prompt_tokens     INTEGER,
        completion_tokens INTEGER,
        latency_ms        INTEGER NOT NULL DEFAULT 0,
        success           BOOLEAN NOT NULL DEFAULT TRUE,
        error             TEXT,
        created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    -- Read paths: "this job's traces in order" (debug a run) and "recent
    -- traces" (tail the content stream). Mirrors llm_call_logs' index set.
    CREATE INDEX IF NOT EXISTS idx_llm_traces_job_id
        ON llm_traces (job_id) WHERE job_id IS NOT NULL;
    CREATE INDEX IF NOT EXISTS idx_llm_traces_created_at
        ON llm_traces (created_at DESC);

    COMMENT ON TABLE llm_traces IS
        'Full request/response content of each LLM call (§17.786). Populated only when settings.trace_capture_enabled is on; JOINs 1:1 to llm_call_logs metrics on (job_id, node_id).';
END
$mig$;
