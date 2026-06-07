"""Sprint X.26 — OpenTelemetry init (strictly opt-in).

The OTel SDK and its instrumentation packages are NOT imported at module
load time — they're only imported inside `init_tracing()`, which is a
no-op unless `settings.otel_enabled` is True and an OTLP HTTP endpoint
is configured. This keeps the orchestrator's import graph and memory
footprint unchanged for the default (off) case.

Endpoint env: `OTEL_OTLP_ENDPOINT` (e.g. `http://otel-collector:4318/v1/traces`).
Service name: `OTEL_SERVICE_NAME` (default `scaffold-engine`).

When enabled, three instrumentations are wired:

  * FastAPI    — request spans w/ method, route, status
  * httpx      — outbound HTTP (Ollama, SearXNG, GitHub) propagated
  * asyncpg    — DB query spans

These cover ~all hot paths without manual span code. Failures during
init log a warning and abort tracing — the orchestrator continues to
run uninstrumented rather than refusing to start.
"""
from __future__ import annotations

import logging

from app.config import settings

logger = logging.getLogger("scaffold.otel")

_initialized: bool = False
_fastapi_instrumented: bool = False


def is_initialized() -> bool:
    return _initialized


def instrument_fastapi(app) -> bool:
    """§17.438 — attach the OTel FastAPI request-span middleware.

    MUST be called at app-BUILD time (before startup): instrument_app adds a
    middleware, and Starlette raises "Cannot add middleware after an
    application has started" if called from lifespan (which is what aborted
    OTel init when Phoenix was first activated). The TracerProvider it uses is
    set later by init_tracing() in lifespan — fine, since the middleware reads
    the provider at request time. No-op unless otel_enabled; idempotent;
    never raises fatally.
    """
    global _fastapi_instrumented
    if _fastapi_instrumented:
        return True
    if not settings.otel_enabled:
        return False
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        FastAPIInstrumentor.instrument_app(app, excluded_urls="/health,/metrics")
        _fastapi_instrumented = True
        logger.info('event="otel_fastapi_instrumented"')
        return True
    except Exception as exc:
        logger.warning('event="otel_fastapi_instrument_failed" err=%s', exc)
        return False


def init_tracing(app) -> bool:
    """Initialize OTel tracing if env-configured. Returns True when wired,
    False when skipped or failed. ``app`` is the FastAPI app for the
    FastAPIInstrumentor hook.

    Idempotent: subsequent calls are no-ops.
    """
    global _initialized
    if _initialized:
        return True
    if not settings.otel_enabled:
        return False
    endpoint = (settings.otel_otlp_endpoint or "").strip()
    if not endpoint:
        logger.info(
            'event="otel_skipped" reason="no_endpoint" '
            "(set otel_otlp_endpoint to enable)",
        )
        return False

    try:
        # All imports inline so the SDK is never loaded when OTel is off.
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
    except ImportError as exc:
        # OTel deps not installed → operator opted in via env but didn't
        # install the packages. Warn loudly so the misconfig is visible.
        logger.warning(
            'event="otel_init_failed" reason="import_error" err=%s '
            "(pip install -r requirements.txt to pick up opentelemetry-* deps)",
            exc,
        )
        return False

    try:
        resource = Resource.create({"service.name": settings.otel_service_name})
        provider = TracerProvider(resource=resource)
        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint))
        )
        trace.set_tracer_provider(provider)

        # §17.438 — FastAPI request-span instrumentation is attached at
        # app-BUILD time by instrument_fastapi() (called from main.py), NOT
        # here: instrument_app adds a middleware and this runs in lifespan,
        # where Starlette rejects it. Doing it here aborted the whole init
        # (and thus the gen_ai LLM spans) when Phoenix was first activated.
        #
        # httpx + asyncpg are best-effort — we wire what's installed and
        # log per-instrumentation failures without aborting the whole init.
        # §17.277 — bumped DEBUG → WARNING. DEBUG is invisible by default;
        # a misconfigured OTEL endpoint (or a missing instrumentation
        # package) would silently lose distributed-trace coverage without
        # any operator-visible signal. WARNING surfaces it on the normal
        # log stream while still not aborting init (best-effort intact).
        try:
            from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
            HTTPXClientInstrumentor().instrument()
        except Exception as exc:
            logger.warning("otel_httpx_instrument_failed: err=%s", exc)
        try:
            from opentelemetry.instrumentation.asyncpg import AsyncPGInstrumentor
            AsyncPGInstrumentor().instrument()
        except Exception as exc:
            logger.warning("otel_asyncpg_instrument_failed: err=%s", exc)

        _initialized = True
        logger.info(
            'event="otel_initialized" service=%s endpoint=%s',
            settings.otel_service_name, endpoint,
        )
        return True
    except Exception as exc:
        logger.warning('event="otel_init_failed" err=%s', exc)
        return False
