"""§17.435 — emit a gen_ai.* OpenTelemetry span for each completed LLM call.

Turns the existing generic httpx spans (which only show "POST :11434") into
LLM-semantic traces an LLM-observability backend (Arize Phoenix) renders with
model / token / latency views per job+node. Backend-agnostic — it just adds
attributes to whatever OTLP endpoint ``otel.init_tracing`` exports to.

Strictly opt-in + non-invasive:
  * No-op unless ``otel.is_initialized()`` (i.e. OTEL_ENABLED + endpoint set),
    so the default (off) path — and every test/CI run — never imports the OTel
    SDK here.
  * Never raises (it's called fire-and-forget from model_router._record_call,
    after the LLM call already returned). The call path is unaffected.
  * Point-in-time span: the call already completed, so we back-date the span
    start by the recorded duration so the trace window is accurate.

gen_ai.* attribute names follow the OpenTelemetry GenAI semantic conventions,
which Phoenix (and OpenInference) map natively.
"""
from __future__ import annotations

import logging
import time

logger = logging.getLogger("scaffold.otel")


def record_llm_span(resp) -> None:
    """Emit a gen_ai span for a completed ModelResponse. No-op unless OTel is
    initialized; never raises."""
    try:
        from app.observability import otel
        if not otel.is_initialized():
            return
        from opentelemetry import trace
        from app.utils.cost_tracking import (
            current_call_kind,
            current_job_id,
            current_node_id,
        )
    except Exception:
        return

    try:
        now_ns = time.time_ns()
        dur_ms = int(getattr(resp, "total_duration_ms", 0) or 0)
        start_ns = now_ns - dur_ms * 1_000_000
        provider = getattr(resp, "provider", "") or "unknown"

        tracer = trace.get_tracer("scaffold.llm")
        span = tracer.start_span(f"llm.{provider}.chat", start_time=start_ns)
        try:
            span.set_attribute("gen_ai.system", provider)
            model = getattr(resp, "model", None)
            if model:
                span.set_attribute("gen_ai.request.model", str(model))
            tp = getattr(resp, "tokens_prompt", None)
            if tp is not None:
                span.set_attribute("gen_ai.usage.input_tokens", int(tp))
            tc = getattr(resp, "tokens_completion", None)
            if tc is not None:
                span.set_attribute("gen_ai.usage.output_tokens", int(tc))
            tps = getattr(resp, "tokens_per_sec", None)
            if tps:
                span.set_attribute("gen_ai.response.tokens_per_second", float(tps))
            span.set_attribute("llm.latency_ms", dur_ms)
            success = bool(getattr(resp, "success", True))
            span.set_attribute("llm.success", success)
            # job/node/kind from the cost-tracking ContextVars (set by
            # execute_next_node / research lifecycle) so a trace ties to a job.
            for key, var in (
                ("scaffold.job_id", current_job_id),
                ("scaffold.node_id", current_node_id),
                ("scaffold.call_kind", current_call_kind),
            ):
                val = var.get()
                if val:
                    span.set_attribute(key, str(val))
            if not success:
                span.set_attribute("error", True)
                err = getattr(resp, "error", None)
                if err:
                    span.set_attribute("llm.error", str(err)[:500])
        finally:
            span.end(end_time=now_ns)
    except Exception:
        logger.debug("record_llm_span_failed", exc_info=True)
