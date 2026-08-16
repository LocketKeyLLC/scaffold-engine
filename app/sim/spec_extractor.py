"""
NL → spec extractor — front door of the engineering-design pipeline.

Takes free-form natural-language description of a design intent and
returns either a validated JSON spec (matching
``app/sim/spec_schema.json``) OR a structured rejection listing the
ambiguities the LLM refused to guess on. **Never** synthesizes
fake numbers to fill gaps — the §17.143 schema design specifically
requires units + at-least-one-of target/min/max, and the extractor
inherits that discipline.

Output contract (§17.144):
  Success → ``ExtractionResult(ok=True, spec={...}, spec_id=UUID)``
            and one row INSERTed into ``specs`` with
            ``confirmed_*=NULL``.
  Ambiguity → ``ExtractionResult(ok=False, ambiguities=[...])``
              and no DB row. Caller asks the human to resolve.
  LLM / parse / validation error →
              ``ExtractionResult(ok=False, errors=[...])`` and no
              DB row.

The two failure paths are kept distinct so a UI can render
ambiguities as "questions for the human" while errors render as
"the extractor itself broke."

The LLM is invoked at ``temperature=0`` so unambiguous briefs are
deterministic — the same brief through the same model always
produces the same spec (and therefore the same ``spec_sha256``).
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app import model_router
from app.config import settings
from app.utils.llm_retry import chat_until_nonempty
from app.sim.spec import (
    SCHEMA_VERSION,
    spec_sha256,
    validate_spec,
)
from app.utils.llm_parsing import parse_json_object

logger = logging.getLogger("scaffold")


# Spec schema embedded as a JSON string in the system prompt so the
# LLM sees the *exact* contract the validator will check. Loading the
# raw file rather than ``json.dumps(SCHEMA)`` preserves any author-
# intended formatting / comments-as-descriptions when the LLM reads it.
_SCHEMA_TEXT: str = Path(__file__).parent.joinpath("spec_schema.json").read_text(
    encoding="utf-8"
)

_SYSTEM_PROMPT = (
    "You are a strict engineering-spec extractor. Convert the user's "
    "design brief into JSON matching the schema below. You MUST emit "
    "ONLY a single JSON object — no prose, no markdown fences, no "
    "explanation.\n"
    "\n"
    "Output contract (the JSON object you return MUST match exactly "
    "one of these two shapes):\n"
    "\n"
    "  Success:   {\"spec\": <object matching the schema>}\n"
    "  Ambiguity: {\"ambiguities\": [{\"field\": \"<json-path>\", "
    "\"reason\": \"<what's unclear>\", \"question\": \"<short "
    "clarifying question for the human>\"}, ...]}\n"
    "\n"
    "Hard rules — if you cannot satisfy any of these, the brief is "
    "AMBIGUOUS and you must return the ambiguities form:\n"
    "  1. Every constraint must have a unit and at least one of "
    "target / min / max (numeric, never a string).\n"
    "  2. \"target\" alone is fine; pair with \"tolerance_pct\" for a "
    "symmetric band. Use min/max for asymmetric or one-sided bounds.\n"
    "  3. constraint.kind MUST be from the enumerated list in the "
    "schema. Reject the brief (ambiguity) before inventing a new kind.\n"
    "  4. Do NOT fabricate numbers. If the brief says \"fast\" or "
    "\"low power\" without a number, that is an ambiguity, not a "
    "license to pick a default.\n"
    "  5. constraint.id and interface.id must be lower_snake_case.\n"
    "\n"
    "Schema (authoritative; your output is validated against it):\n"
    "```json\n"
    f"{_SCHEMA_TEXT}"
    "```\n"
    "\n"
    "Example — success (unambiguous brief):\n"
    "Brief: \"RC low-pass filter with -3dB corner at 1 kHz ±5%, "
    "input swing 0-5V, output amplitude must not exceed 3.3V peak-"
    "to-peak. Operates at room temperature.\"\n"
    "Output: {\"spec\": {\"schema_version\": \"1.0.0\", \"design\": "
    "{\"name\": \"RC low-pass filter\", \"kind\": \"analog_circuit\", "
    "\"description\": \"First-order passive RC low-pass.\"}, "
    "\"constraints\": [{\"id\": \"fc_3db\", \"kind\": "
    "\"electrical.frequency\", \"description\": \"-3 dB corner "
    "frequency.\", \"target\": 1000.0, \"tolerance_pct\": 5.0, "
    "\"unit\": \"Hz\", \"criticality\": \"required\"}, {\"id\": "
    "\"vpp_max\", \"kind\": \"electrical.voltage\", \"description\": "
    "\"Maximum output peak-to-peak voltage.\", \"max\": 3.3, "
    "\"unit\": \"V\", \"criticality\": \"required\"}], "
    "\"interfaces\": [{\"id\": \"vin\", \"direction\": \"input\", "
    "\"kind\": \"analog_voltage\", \"voltage_range_v\": [0.0, 5.0]}, "
    "{\"id\": \"vout\", \"direction\": \"output\", \"kind\": "
    "\"analog_voltage\"}], \"environment\": {\"temperature_c\": "
    "[20.0, 25.0]}}}\n"
    "\n"
    "Example — ambiguity (vague brief):\n"
    "Brief: \"Make a fast filter.\"\n"
    "Output: {\"ambiguities\": [{\"field\": \"design.kind\", "
    "\"reason\": \"Analog, digital, or mixed-signal not specified.\", "
    "\"question\": \"Is this an analog circuit, a digital filter, or "
    "mixed-signal?\"}, {\"field\": \"constraints[0].target\", "
    "\"reason\": \"'Fast' is relative — no numeric corner or sample "
    "rate given.\", \"question\": \"What corner frequency (Hz) or "
    "sample rate do you want?\"}]}\n"
)


@dataclass(frozen=True)
class ExtractionAmbiguity:
    field: str
    reason: str
    question: str


@dataclass
class ExtractionResult:
    ok: bool
    spec: dict[str, Any] | None = None
    spec_id: uuid.UUID | None = None
    ambiguities: list[ExtractionAmbiguity] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    llm_raw_text: str = ""
    model_used: str = ""


def _parse_ambiguities(raw: list[Any]) -> list[ExtractionAmbiguity]:
    out: list[ExtractionAmbiguity] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        f = str(item.get("field", "")).strip()
        r = str(item.get("reason", "")).strip()
        q = str(item.get("question", "")).strip()
        if not (f or r or q):
            continue
        out.append(ExtractionAmbiguity(field=f, reason=r, question=q))
    return out


async def _insert_spec(
    db: AsyncSession,
    *,
    spec: dict[str, Any],
    job_id: uuid.UUID | None,
) -> uuid.UUID:
    row = await db.execute(
        text(
            """
            INSERT INTO specs (
                job_id, schema_version, spec_json, spec_sha256
            )
            VALUES (
                :job_id, :schema_version, CAST(:spec_json AS JSONB),
                :spec_sha256
            )
            RETURNING id
            """
        ),
        {
            "job_id": str(job_id) if job_id else None,
            "schema_version": spec.get("schema_version", SCHEMA_VERSION),
            "spec_json": json.dumps(spec),
            "spec_sha256": spec_sha256(spec),
        },
    )
    spec_id = row.scalar_one()
    await db.commit()
    return spec_id


async def extract_spec(
    nl_text: str,
    *,
    db: AsyncSession,
    job_id: uuid.UUID | None = None,
    model_role: str | None = None,
) -> ExtractionResult:
    """Extract a validated spec from a natural-language brief.

    On success persists one row into ``specs`` with ``confirmed_*=NULL``
    (the ``/confirm`` gate handler — landing in a follow-up commit —
    is responsible for writing those columns). Returns the row id so
    callers can thread it through to that handler.

    Never raises on LLM or parse failure; both surface as
    ``ExtractionResult(ok=False, errors=[...])``.
    """
    if not nl_text or not nl_text.strip():
        raise ValueError("nl_text must be non-empty")

    role = model_role or settings.spec_extractor_model_role

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": f"Brief: {nl_text.strip()}"},
    ]
    # §17.487 — retry-on-empty. The cloud thinking model spends num_predict on
    # reasoning first, so a tight cap (or an unlucky draw) returns success=True
    # with empty content. chat_until_nonempty re-draws before we treat it as a
    # hard failure; a true hard failure (success=False) returns immediately.
    resp = await chat_until_nonempty(
        model_router.chat,
        messages,
        {"role": role},
        temperature=0.0,
        max_tokens=settings.spec_extractor_max_tokens,
        draws=settings.spec_extractor_max_draws,
        label="spec_extractor",
    )

    if not resp.success or not (resp.text or "").strip():
        return ExtractionResult(
            ok=False,
            errors=[f"LLM call failed: {resp.error or 'empty response'}"],
            llm_raw_text=resp.text or "",
            model_used=resp.model or "",
        )

    parsed = parse_json_object(resp.text)
    if parsed is None:
        return ExtractionResult(
            ok=False,
            errors=["LLM output did not parse as a JSON object"],
            llm_raw_text=resp.text,
            model_used=resp.model or "",
        )

    if "ambiguities" in parsed and parsed.get("ambiguities"):
        ambig = _parse_ambiguities(parsed["ambiguities"])
        if ambig:
            return ExtractionResult(
                ok=False,
                ambiguities=ambig,
                llm_raw_text=resp.text,
                model_used=resp.model or "",
            )

    if "spec" not in parsed or not isinstance(parsed["spec"], dict):
        return ExtractionResult(
            ok=False,
            errors=[
                "LLM output is neither a {spec: {...}} nor a "
                "{ambiguities: [...]} envelope"
            ],
            llm_raw_text=resp.text,
            model_used=resp.model or "",
        )

    spec = parsed["spec"]
    validation = validate_spec(spec)
    if not validation.ok:
        return ExtractionResult(
            ok=False,
            errors=[
                f"{e.path}: {e.message}" for e in validation.errors
            ],
            llm_raw_text=resp.text,
            model_used=resp.model or "",
        )

    spec_id = await _insert_spec(db, spec=spec, job_id=job_id)
    return ExtractionResult(
        ok=True,
        spec=spec,
        spec_id=spec_id,
        llm_raw_text=resp.text,
        model_used=resp.model or "",
    )
