"""§17.798 — Citation-faithfulness (per-citation attribution) scoring.

Where B1 faithfulness (§17.448, ``faithfulness.py``) asks "is this claim
supported by SOME source in the collected context?" — treating the context as
one undifferentiated blob — citation faithfulness asks the STRICTER attribution
question: for an answer with inline ``[n]`` citation markers, does the SPECIFIC
source ``n`` that a sentence cites actually support that sentence? A claim can be
globally grounded (some source backs it) yet mis-attributed (the *cited* source
does not), and only per-citation scoring catches that. This is ALCE-style
citation precision (Gao et al., arXiv 2305.14627), the attribution complement to
RAGAS faithfulness.

It extends the same black-box, tool-call, default-OFF, fail-soft lineage as
``faithfulness.py`` (scores) and ``cove.py`` (corrects): the parse step is pure
and unit-tested, and one LLM judge decides support per citation instance with
retry/timeout/fail-soft so a coax miss never breaks the caller. Every failure
path returns ``None`` ("not scored").

Score = ``|supported citations| / |total citations|``. A citation whose source
number is out of range (a dangling ``[99]`` when only 5 sources exist) is counted
as unsupported deterministically, without an LLM call.
"""
from __future__ import annotations

import asyncio
import logging
import re

from app import model_router
from app.providers.base import Tool
from app.utils.tool_call_args import read_tool_args

logger = logging.getLogger("scaffold.citation_faithfulness")

# Inline citation marker: ``[2]`` or grouped ``[2, 5]`` / ``[2,5]``. ``[2][5]``
# is two adjacent single matches. Purely-numeric contents only — ``[i]``,
# ``[TODO]``, ``[...]`` and markdown links ``[text](url)`` are NOT citations.
_CITE_RE = re.compile(r"\[\s*(\d+(?:\s*,\s*\d+)*)\s*\]")
# Sentence segmentation on terminal punctuation + whitespace. Coarse but pure
# and deterministic — good enough to attach each marker to its sentence.
_SENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")

_MAX_INSTANCES = 40  # bound the judge prompt (and cost) on citation-dense answers
_MAX_SOURCE_CHARS = 2_000  # per cited source shown to the judge
_MAX_CLAIM_CHARS = 500
_TIMEOUT_S = 90
# §17.560 lesson (mirrored): the coaxed thinking-model judge intermittently
# emits prose with no parseable tool-call; give it room + retry the miss.
_JUDGE_MAX_TOKENS = 8192
_JUDGE_ATTEMPTS = 3


# Cite-aware answer generation — used by the eval gate so the generated answer
# actually carries ``[n]`` markers to score. Kept here (not duplicated in the
# script + test) so the two callers can't drift.
CITE_ANSWER_SYSTEM = (
    "You are a precise technical assistant. Answer the question using ONLY the "
    "numbered SOURCES provided. After each sentence, cite the source number(s) "
    "that support it in square brackets, e.g. 'Vectors are normalized [2].' Cite "
    "ONLY sources that actually support the sentence — never invent a citation. "
    "If the sources do not cover the question, say so rather than inventing an "
    "answer. Be concise (2-4 sentences)."
)

_JUDGE_SYSTEM = (
    "You are a strict citation checker. You are given numbered CITATIONS. Each "
    "has a CLAIM and the single SOURCE it cites. For each citation, decide "
    "whether that SOURCE — on its own — directly states or clearly entails the "
    "CLAIM. Judge ONLY the source shown for that citation; do not use outside "
    "knowledge or other citations' sources. Be conservative: if the source does "
    "not clearly support the claim, mark it unsupported. Report every citation "
    "via the tool, keyed by its index."
)

_JUDGE_TOOL = Tool(
    name="report_citation_support",
    description="Report, per citation index, whether the cited source supports the claim.",
    input_schema={
        "type": "object",
        "properties": {
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer",
                                  "description": "The 1-based CITATION index being judged."},
                        "supported": {"type": "boolean",
                                      "description": "True iff the cited source supports the claim."},
                    },
                    "required": ["index", "supported"],
                },
            },
        },
        "required": ["results"],
    },
)


def _source_text(source) -> str:
    """Normalize a source (str or ``{"text"/"content": ...}`` dict) to text."""
    if isinstance(source, str):
        return source
    if isinstance(source, dict):
        return str(source.get("text") or source.get("content") or "")
    return str(source or "")


def parse_citations(answer: str, n_sources: int) -> list[dict]:
    """Pure: extract per-citation instances from an answer's ``[n]`` markers.

    Returns one dict per (sentence, cited source) pair::

        {"claim": <sentence, markers stripped>, "source_id": <1-based int>,
         "in_range": <bool: 1 <= source_id <= n_sources>}

    A sentence citing ``[2, 5]`` yields two instances. Duplicate (claim,
    source_id) pairs within the answer are collapsed. Capped at ``_MAX_INSTANCES``.
    """
    instances: list[dict] = []
    seen: set[tuple[str, int]] = set()
    for sentence in _SENT_SPLIT_RE.split(answer or ""):
        ids: list[int] = []
        for m in _CITE_RE.finditer(sentence):
            ids.extend(int(x) for x in re.split(r"\s*,\s*", m.group(1)))
        if not ids:
            continue
        # Drop the markers, collapse the whitespace they leave behind, and trim
        # the trailing sentence punctuation now stranded by the removed marker
        # ("normalized [2]." → "normalized").
        claim = re.sub(r"\s+", " ", _CITE_RE.sub(" ", sentence)).strip()
        claim = claim.rstrip(" .!?,;:")[:_MAX_CLAIM_CHARS]
        if not claim:
            continue
        for sid in ids:
            key = (claim, sid)
            if key in seen:
                continue
            seen.add(key)
            instances.append({
                "claim": claim,
                "source_id": sid,
                "in_range": 1 <= sid <= n_sources,
            })
            if len(instances) >= _MAX_INSTANCES:
                return instances
    return instances


def _build_judge_prompt(instances: list[dict], sources: list) -> str:
    """Render the in-range instances as a numbered CLAIM/SOURCE block."""
    blocks = []
    for i, inst in enumerate(instances, start=1):
        src = _source_text(sources[inst["source_id"] - 1])[:_MAX_SOURCE_CHARS]
        blocks.append(
            f"CITATION {i}:\n  CLAIM: {inst['claim']}\n"
            f"  SOURCE [{inst['source_id']}]: {src}"
        )
    return "\n\n".join(blocks)


async def score_citation_faithfulness(
    answer: str,
    sources: list,
    *,
    role: str = "model_verifier",
    overrides: dict | None = None,
) -> dict | None:
    """Return ``{score, supported, total, cited, dangling, unsupported_citations}``
    or ``None`` (fail-soft).

    ``sources`` is 1-indexed by position (source ``[1]`` is ``sources[0]``);
    each element may be a string or a ``{"text"/"content": ...}`` dict.

    ``None`` when: input is empty, the answer has NO citations (attribution is
    undefined — "not scored"), or the LLM judge fails after retries. Dangling
    citations (source number out of range) are scored as unsupported without an
    LLM call.
    """
    if not (answer or "").strip() or not sources:
        return None
    instances = parse_citations(answer, len(sources))
    if not instances:
        return None

    in_range = [x for x in instances if x["in_range"]]
    dangling = [x for x in instances if not x["in_range"]]

    # Judge only the in-range instances; dangling ones are unsupported a priori.
    verdicts: dict[int, bool] = {}
    if in_range:
        prompt = _build_judge_prompt(in_range, sources)
        results = None
        for attempt in range(_JUDGE_ATTEMPTS):
            try:
                resp = await asyncio.wait_for(
                    model_router.tool_call(
                        messages=[
                            {"role": "system", "content": _JUDGE_SYSTEM},
                            {"role": "user", "content": prompt},
                        ],
                        tools=[_JUDGE_TOOL],
                        role=role,
                        overrides=overrides,
                        temperature=0.0,
                        max_tokens=_JUDGE_MAX_TOKENS,
                    ),
                    timeout=_TIMEOUT_S,
                )
            except asyncio.TimeoutError:
                logger.warning("citation_faithfulness_timeout: attempt=%d budget_s=%d",
                               attempt, _TIMEOUT_S)
                continue
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("citation_faithfulness_error: %s", exc)
                return None
            args = read_tool_args(resp)
            candidate = args.get("results") if args else None
            if isinstance(candidate, list) and candidate:
                results = candidate
                break
            logger.warning(
                "citation_faithfulness_no_results: attempt=%d (coax miss) — %s",
                attempt, "retrying" if attempt < _JUDGE_ATTEMPTS - 1 else "giving up",
            )
        if not results:
            return None
        for r in results:
            if isinstance(r, dict) and isinstance(r.get("index"), int):
                verdicts[r["index"]] = r.get("supported") is True

    total = len(instances)
    supported = 0
    unsupported: list[dict] = []
    for i, inst in enumerate(in_range, start=1):
        if verdicts.get(i) is True:
            supported += 1
        else:
            unsupported.append({"claim": inst["claim"][:200], "source_id": inst["source_id"]})
    for inst in dangling:
        unsupported.append({"claim": inst["claim"][:200], "source_id": inst["source_id"],
                            "dangling": True})

    return {
        "score": round(supported / total, 2),
        "supported": supported,
        "total": total,
        "cited": len(in_range),
        "dangling": len(dangling),
        "unsupported_citations": unsupported[:10],
    }
