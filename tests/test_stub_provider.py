"""§17.1061 — the stub provider: canned, instant, schema-satisfying."""
import pytest

from app.providers import get_provider
from app.providers.base import Tool


@pytest.mark.asyncio
async def test_stub_chat_generate_and_stream():
    p = get_provider("stub")
    r = await p.chat_completion("stub", [{"role": "user", "content": "hello"}])
    assert r.success and "STUB" in r.text and r.provider == "stub"
    g = await p.generate("stub", "prompt")
    assert g.success and g.text
    chunks = [c async for c in p.stream_chat("stub", [{"role": "user", "content": "x"}])]
    assert "".join(chunks).strip().startswith("STUB")


@pytest.mark.asyncio
async def test_stub_tool_call_satisfies_required_schema():
    p = get_provider("stub")
    tool = Tool(name="record_decision", description="d", input_schema={
        "type": "object",
        "properties": {"action": {"type": "string", "enum": ["submit", "fix"]},
                       "confidence": {"type": "string", "enum": ["low", "high"]},
                       "steps": {"type": "array", "minItems": 1, "items": {"type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"]}}},
        "required": ["action", "confidence", "steps"]})
    r = await p.tool_call("stub", [{"role": "user", "content": "x"}], [tool])
    assert r.success and r.tool_calls and r.tool_calls[0].name == "record_decision"
    a = r.tool_calls[0].arguments
    assert a["action"] == "submit" and a["confidence"] == "low" and a["steps"] == [{"title": "stub"}]


def test_stub_is_registered_but_never_for_embeddings():
    p = get_provider("stub")
    assert p.supports_chat and p.supports_native_tools and not p.supports_embeddings
