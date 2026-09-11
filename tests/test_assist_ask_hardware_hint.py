"""§17.1019 — the RETRIEVAL side of the ask path needs the hardware too.

§17.1018 put the operator's named hardware into `render_research_grounding`,
which feeds the guide / fix / decision prepasses. The operator reported the
behaviour unchanged, and they were right: their question routes to `ask`, and
that path builds its retrieval hint with `_kb_hint_from(brief, environment)` —
brief goal plus substitution KEYS. Notes are not in either, so `SAX1V1K` never
reached the query on the path they were actually using.

The tell was in the engine's own answer: it listed "SAX1V1K" as one of the
models it would need in order to help, because §17.1018 had put the token in
the GENERATION prompt while retrieval still had no idea.

Controlled A/B on the deployed build, same question, same session, note toggled:

  note present -> "Spectrum app -> Services -> Router -> Advanced Settings ->
                   Port Forwarding", cited, with the exact fields
  note absent  -> "open 192.168.1.1 ... check the sticker on the router",
                   and asserts "Port forwarding is NOT in the app" (false)
"""
import pytest

from app.modules.assist_agent import _kb_hint_from

REAL_NOTE = {"text": ("Decision (established in conversation, 2026-08-29): abandon "
                      "the VLAN segmentation approach — the Spectrum SAX1V1K router "
                      "cannot trunk tagged VLANs and the ES2251 modem has no usable "
                      "LAN port.")}
BRIEF = {"title": "Secure Home Lab & Media/Game/AI Server"}
ENV = {"substitutions": {"JELLYFIN_IP": "192.168.1.20"}}


def test_the_model_number_reaches_the_retrieval_hint():
    hint = _kb_hint_from(BRIEF, ENV, [REAL_NOTE])
    assert "SAX1V1K" in hint, "the ask path still cannot retrieve on the hardware"
    assert "ES2251" in hint


def test_hardware_leads_the_hint():
    """§17.989 — the first terms decide what comes back, and the hint is capped
    at 300 chars, so a long brief must not push the model number out."""
    hint = _kb_hint_from({"description": "x " * 400}, ENV, [REAL_NOTE])
    assert hint.startswith("SAX1V1K"), hint[:80]
    assert "SAX1V1K" in hint


def test_the_existing_hint_content_is_preserved():
    hint = _kb_hint_from(BRIEF, ENV, [REAL_NOTE])
    assert "Secure Home Lab" in hint          # brief goal (§17.650)
    assert "JELLYFIN_IP" in hint              # substitution KEYS, not values
    assert "192.168.1.20" not in hint, "values must never leak into the query"


def test_notes_are_optional():
    """Every pre-§17.1019 caller passed two arguments."""
    hint = _kb_hint_from(BRIEF, ENV)
    assert "Secure Home Lab" in hint
    assert "SAX1V1K" not in hint


def test_no_hardware_note_changes_nothing():
    assert _kb_hint_from(BRIEF, ENV, [{"text": "Operator prefers purpose-based names."}]) \
        == _kb_hint_from(BRIEF, ENV)


def test_the_hint_stays_capped():
    many = [{"text": f"The router model RT{i:04d}X sits in the rack."} for i in range(40)]
    assert len(_kb_hint_from(BRIEF, ENV, many)) <= 300


def test_the_ask_path_passes_notes_through():
    """`_kb_hint_from` gaining a parameter is worthless if the caller omits it —
    which is exactly how §17.1018 missed this path."""
    import inspect
    from app.modules import assist_agent
    src = inspect.getsource(assist_agent.run_step_research)
    # §17.1023 — the call now also passes step_recap and wraps across lines.
    # Assert the PROPERTY (notes reach the hint), not the formatting.
    assert "_kb_hint_from(" in src and "mem.operator_notes" in src, (
        "run_step_research does not pass notes, so the ask path retrieves blind"
    )
