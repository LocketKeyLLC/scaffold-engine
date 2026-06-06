"""§17.435 — unit tests for the gen_ai.* LLM span emitter.

Uses the real OpenTelemetry SDK (present in the dev image; NOT in
requirements-ci.txt, so this module is collect_ignore'd for ci-smoke). The
tracer is patched to capture attributes; otel.is_initialized is forced.
"""
from types import SimpleNamespace

import pytest

from app.observability import llm_spans, otel
from app.utils.cost_tracking import current_job_id, current_node_id

pytestmark = pytest.mark.smoke


class _FakeSpan:
    def __init__(self):
        self.attrs: dict = {}
        self.ended = False

    def set_attribute(self, k, v):
        self.attrs[k] = v

    def end(self, end_time=None):
        self.ended = True


class _FakeTracer:
    def __init__(self):
        self.span = _FakeSpan()

    def start_span(self, name, start_time=None):
        self.span.name = name
        return self.span


def _resp(**kw):
    base = dict(
        model="qwen3-vl:235b-instruct-cloud", success=True, provider="ollama",
        total_duration_ms=1234, tokens_prompt=100, tokens_completion=50,
        tokens_per_sec=12.5, error=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture
def captured(monkeypatch):
    tracer = _FakeTracer()
    monkeypatch.setattr(otel, "is_initialized", lambda: True)
    monkeypatch.setattr("opentelemetry.trace.get_tracer", lambda *a, **k: tracer)
    return tracer


def test_noop_when_otel_not_initialized(monkeypatch):
    monkeypatch.setattr(otel, "is_initialized", lambda: False)
    called = {"n": 0}
    monkeypatch.setattr("opentelemetry.trace.get_tracer",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    llm_spans.record_llm_span(_resp())
    assert called["n"] == 0


def test_emits_gen_ai_attributes(captured):
    llm_spans.record_llm_span(_resp())
    a = captured.span.attrs
    assert a["gen_ai.system"] == "ollama"
    assert a["gen_ai.request.model"] == "qwen3-vl:235b-instruct-cloud"
    assert a["gen_ai.usage.input_tokens"] == 100
    assert a["gen_ai.usage.output_tokens"] == 50
    assert a["llm.latency_ms"] == 1234
    assert a["llm.success"] is True
    assert captured.span.ended is True
    assert captured.span.name == "llm.ollama.chat"


def test_tags_job_and_node(captured):
    tok_j = current_job_id.set("job-abc")
    tok_n = current_node_id.set("node-T1")
    try:
        llm_spans.record_llm_span(_resp())
    finally:
        current_job_id.reset(tok_j)
        current_node_id.reset(tok_n)
    a = captured.span.attrs
    assert a["scaffold.job_id"] == "job-abc"
    assert a["scaffold.node_id"] == "node-T1"


def test_error_response_marks_error(captured):
    llm_spans.record_llm_span(_resp(success=False, error="boom 500"))
    a = captured.span.attrs
    assert a["llm.success"] is False
    assert a["error"] is True
    assert "boom 500" in a["llm.error"]


def test_never_raises_on_bad_tracer(monkeypatch):
    monkeypatch.setattr(otel, "is_initialized", lambda: True)
    monkeypatch.setattr("opentelemetry.trace.get_tracer",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    # Must swallow — the LLM call path cannot be broken by telemetry.
    llm_spans.record_llm_span(_resp())
