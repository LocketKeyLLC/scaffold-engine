"""
Topology-selection stage — first reasoning step of the engineering-
design pipeline (§17.146).

Pipeline position:

    confirmed spec  ──►  select_topologies()  ──►  topology_selections row
                            │
                            ├── RAG retrieval (domain="eng", top_k=8)
                            │       └── retrieval_set = {entry_id, ...}
                            └── LLM proposes 2-4 candidates with citations
                                    └── enforce: every cite ∈ retrieval_set

Contract:

  * Reads through ``require_confirmed_spec`` — refuses to run on an
    unconfirmed spec. The §17.145 gate is what makes the stage's
    output an attestation against an operator-acknowledged input.
  * Hard-rejects any LLM-supplied citation that does not appear in
    the retrieval set. The whole step fails — no row is persisted —
    so a hallucinated citation can never sit in the audit table
    next to a real one. Matches the engineering-design checklist
    invariant "reject any reasoning step that cites a chunk not
    present in the retrieval set."
  * Persists a single ``topology_selections`` row on success; on any
    failure path, no row.
  * Never raises on LLM / RAG failure; failures surface as
    ``TopologySelectionResult(ok=False, errors=[...])`` — same posture
    as the simulator wrappers (§17.140 / 141 / 142) and the spec
    extractor (§17.144).
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app import model_router
from app.config import settings
from app.modules.rag_pipeline import query_rag
from app.sim.spec_store import (
    SpecNotConfirmedError,
    SpecNotFoundError,
    require_confirmed_spec,
)
from app.utils.llm_parsing import parse_json_object

logger = logging.getLogger("scaffold")

DEFAULT_TOP_K = 8
DEFAULT_DOMAIN = "eng_design"  # §17.329 — split from "eng" (software-eng) to give circuit/EDA content its own partition
MIN_CANDIDATES = 2
MAX_CANDIDATES = 4

# Cap on how much of each chunk we include in the LLM context. The
# embedded schema + few-shot already eats ~4 KB; 1500 chars × 8 chunks
# keeps the total prompt under ~16 KB which most providers handle fine.
_CHUNK_TRUNCATE = 1500


_SYSTEM_PROMPT = (
    "You are a topology-selection assistant for engineering design. "
    "Given (a) a confirmed engineering spec and (b) a set of retrieved "
    "reference chunks, propose 2–4 candidate topologies that satisfy "
    "the spec.\n"
    "\n"
    "You MUST emit ONLY a single JSON object — no prose, no markdown "
    "fences, no explanation outside the JSON.\n"
    "\n"
    "Output shape (mandatory):\n"
    "{\"candidates\": [\n"
    "  {\"name\": \"<short topology name>\",\n"
    "   \"description\": \"<one-sentence summary>\",\n"
    "   \"rationale\": \"<why this fits the spec's constraints>\",\n"
    "   \"citations\": [\"<entry_id from retrieved chunks>\", ...]\n"
    "  },\n"
    "  ...\n"
    "]}\n"
    "\n"
    "Hard rules:\n"
    "  1. Every candidate MUST have at least one entry from the "
    "retrieved chunks in its `citations` list. Do NOT invent "
    "entry_ids — copy them verbatim from the `entry_id=...` lines "
    "in the context.\n"
    "  2. If no retrieved chunk supports a candidate, omit that "
    "candidate. It is better to return fewer candidates than to "
    "fabricate support.\n"
    "  3. Citations must be the exact entry_id strings shown — do "
    "not abbreviate, hash, or reformat them.\n"
    "  4. 2–4 candidates total. If fewer than 2 are well-supported "
    "by the retrieval, return just the supported ones; the caller "
    "will surface the under-coverage to the operator.\n"
)


@dataclass
class TopologyCandidate:
    name: str
    description: str
    rationale: str
    citations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "rationale": self.rationale,
            "citations": list(self.citations),
        }


@dataclass
class TopologySelectionResult:
    ok: bool
    selection_id: uuid.UUID | None = None
    spec_id: uuid.UUID | None = None
    candidates: list[TopologyCandidate] = field(default_factory=list)
    rag_chunk_ids: list[str] = field(default_factory=list)
    rag_query: str = ""
    rag_domain: str | None = None
    model_used: str = ""
    errors: list[str] = field(default_factory=list)
    llm_raw_text: str = ""


def _build_rag_query(spec: dict[str, Any]) -> str:
    """Build a one-line retrieval query from the spec's design surface
    and its constraint kinds. We deliberately exclude numeric values
    — the retrieval should be about *topology families* relevant to
    the design's shape, not about which specific corner frequency was
    chosen."""
    design = spec.get("design") or {}
    name = str(design.get("name") or "").strip()
    kind = str(design.get("kind") or "").strip()
    description = str(design.get("description") or "").strip()
    constraint_kinds = sorted({
        c.get("kind") for c in spec.get("constraints", []) if c.get("kind")
    })
    parts: list[str] = []
    if kind:
        parts.append(f"design kind: {kind}")
    if name:
        parts.append(f"design: {name}")
    if description:
        parts.append(description[:300])
    if constraint_kinds:
        parts.append("constraints: " + ", ".join(constraint_kinds))
    return " | ".join(parts).strip() or "engineering design topology"


def _render_chunks(chunks: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for i, c in enumerate(chunks, start=1):
        eid = c.get("entry_id", "")
        title = (c.get("title") or "").strip()
        content = (c.get("content") or "").strip()[:_CHUNK_TRUNCATE]
        lines.append(
            f"[{i}] entry_id={eid} title={title!r}\n{content}"
        )
    return "\n\n".join(lines)


def _parse_candidates(body: dict[str, Any]) -> list[TopologyCandidate]:
    raw = body.get("candidates")
    if not isinstance(raw, list):
        return []
    out: list[TopologyCandidate] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        desc = str(item.get("description", "")).strip()
        rationale = str(item.get("rationale", "")).strip()
        cites = item.get("citations") or []
        if not (name and (desc or rationale)):
            continue
        if not isinstance(cites, list):
            continue
        out.append(
            TopologyCandidate(
                name=name,
                description=desc,
                rationale=rationale,
                citations=[str(c) for c in cites if isinstance(c, str)],
            )
        )
    return out


def _validate_citations(
    candidates: list[TopologyCandidate],
    retrieval_set: set[str],
) -> list[str]:
    """Return the list of hallucinated citation strings — empty list
    means every citation was retrieved. Caller fails the whole step
    on any non-empty list."""
    hallucinated: list[str] = []
    for c in candidates:
        if not c.citations:
            hallucinated.append(f"candidate {c.name!r} has no citations")
            continue
        for cite in c.citations:
            if cite not in retrieval_set:
                hallucinated.append(cite)
    return hallucinated


async def _insert_selection(
    db: AsyncSession,
    *,
    spec_id: uuid.UUID,
    candidates: list[TopologyCandidate],
    rag_chunk_ids: list[str],
    rag_query: str,
    rag_domain: str | None,
    model_used: str,
) -> uuid.UUID:
    payload = [c.to_dict() for c in candidates]
    row = await db.execute(
        text(
            """
            INSERT INTO topology_selections (
                spec_id, candidates, rag_chunk_ids,
                rag_query, rag_domain, model_used
            )
            VALUES (
                :spec_id, CAST(:candidates AS JSONB), :rag_chunk_ids,
                :rag_query, :rag_domain, :model_used
            )
            RETURNING id
            """
        ),
        {
            "spec_id": str(spec_id),
            "candidates": json.dumps(payload),
            "rag_chunk_ids": rag_chunk_ids,
            "rag_query": rag_query,
            "rag_domain": rag_domain,
            "model_used": model_used,
        },
    )
    sel_id = row.scalar_one()
    await db.commit()
    return sel_id


async def select_topologies(
    spec_id: uuid.UUID,
    *,
    db: AsyncSession,
    model_role: str | None = None,
    top_k: int = DEFAULT_TOP_K,
    domain: str | None = DEFAULT_DOMAIN,
) -> TopologySelectionResult:
    """Run the topology-selection stage against a confirmed spec.

    Returns ``TopologySelectionResult`` carrying the persisted row id
    on success. Raises ``SpecNotFoundError`` only on lookup failure
    (a missing spec is a programmer error, not a runtime data
    condition); ``SpecNotConfirmedError`` surfaces as an ``ok=False``
    result with an explicit error message so callers can show the
    operator-facing "confirm the spec first" hint.
    """
    try:
        spec_row = await require_confirmed_spec(db, spec_id)
    except SpecNotConfirmedError:
        return TopologySelectionResult(
            ok=False,
            spec_id=spec_id,
            errors=[
                f"spec {spec_id} is not confirmed; POST /specs/{spec_id}/confirm first"
            ],
        )
    # SpecNotFoundError bubbles up — the router maps it to 404.

    rag_query = _build_rag_query(spec_row.spec_json)
    rag_resp = await query_rag(rag_query, domain=domain, top_k=top_k)
    if rag_resp.get("status") != "ok":
        return TopologySelectionResult(
            ok=False,
            spec_id=spec_id,
            rag_query=rag_query,
            rag_domain=domain,
            errors=[f"RAG retrieval failed: {rag_resp.get('error', 'unknown')}"],
        )
    chunks: list[dict[str, Any]] = rag_resp.get("results") or []
    if not chunks:
        return TopologySelectionResult(
            ok=False,
            spec_id=spec_id,
            rag_query=rag_query,
            rag_domain=domain,
            errors=[
                "RAG retrieval returned 0 chunks for this spec — the "
                "engineering corpus may be empty or domain-misfiltered"
            ],
        )

    retrieval_set: set[str] = {
        str(c["entry_id"]) for c in chunks if c.get("entry_id")
    }
    rag_chunk_ids = sorted(retrieval_set)

    role = model_role or settings.spec_extractor_model_role
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                "Spec (validated JSON):\n"
                f"{json.dumps(spec_row.spec_json, separators=(',', ':'))}\n\n"
                "Retrieved chunks (cite by `entry_id` only — verbatim):\n"
                f"{_render_chunks(chunks)}"
            ),
        },
    ]
    resp = await model_router.chat(
        messages=messages,
        role=role,
        temperature=0.0,
        max_tokens=4096,
    )

    if not resp.success or not (resp.text or "").strip():
        return TopologySelectionResult(
            ok=False,
            spec_id=spec_id,
            rag_chunk_ids=rag_chunk_ids,
            rag_query=rag_query,
            rag_domain=domain,
            model_used=resp.model or role,
            errors=[f"LLM call failed: {resp.error or 'empty response'}"],
            llm_raw_text=resp.text or "",
        )

    parsed = parse_json_object(resp.text)
    if parsed is None:
        return TopologySelectionResult(
            ok=False,
            spec_id=spec_id,
            rag_chunk_ids=rag_chunk_ids,
            rag_query=rag_query,
            rag_domain=domain,
            model_used=resp.model or role,
            errors=["LLM output did not parse as a JSON object"],
            llm_raw_text=resp.text,
        )

    candidates = _parse_candidates(parsed)
    if not candidates:
        return TopologySelectionResult(
            ok=False,
            spec_id=spec_id,
            rag_chunk_ids=rag_chunk_ids,
            rag_query=rag_query,
            rag_domain=domain,
            model_used=resp.model or role,
            errors=["LLM produced no well-formed candidates"],
            llm_raw_text=resp.text,
        )

    hallucinated = _validate_citations(candidates, retrieval_set)
    if hallucinated:
        return TopologySelectionResult(
            ok=False,
            spec_id=spec_id,
            rag_chunk_ids=rag_chunk_ids,
            rag_query=rag_query,
            rag_domain=domain,
            model_used=resp.model or role,
            errors=[
                "hallucinated citation: " + h for h in hallucinated
            ],
            llm_raw_text=resp.text,
        )

    if not (MIN_CANDIDATES <= len(candidates) <= MAX_CANDIDATES):
        # Soft-fail with a clear under-coverage error rather than
        # silently persisting an unusable result.
        return TopologySelectionResult(
            ok=False,
            spec_id=spec_id,
            rag_chunk_ids=rag_chunk_ids,
            rag_query=rag_query,
            rag_domain=domain,
            model_used=resp.model or role,
            errors=[
                f"got {len(candidates)} candidates; need "
                f"{MIN_CANDIDATES}-{MAX_CANDIDATES}"
            ],
            candidates=candidates,
            llm_raw_text=resp.text,
        )

    selection_id = await _insert_selection(
        db,
        spec_id=spec_id,
        candidates=candidates,
        rag_chunk_ids=rag_chunk_ids,
        rag_query=rag_query,
        rag_domain=domain,
        model_used=resp.model or role,
    )

    return TopologySelectionResult(
        ok=True,
        selection_id=selection_id,
        spec_id=spec_id,
        candidates=candidates,
        rag_chunk_ids=rag_chunk_ids,
        rag_query=rag_query,
        rag_domain=domain,
        model_used=resp.model or role,
        llm_raw_text=resp.text,
    )
