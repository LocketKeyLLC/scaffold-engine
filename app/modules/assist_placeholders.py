"""Placeholder resolution + substitution learning — extracted from assist_guide.py.

§17.856 (audit "assist decomposition") — the <SCREAMING_SNAKE> placeholder
lifecycle: detect placeholders in guidance (find_placeholders), resolve them from
the operator's environment/facts (resolve_placeholders, §17.851), and learn
concrete substitutions from a submit (extract_substitutions, §17.490). Self-
contained apart from model_router; every name re-exported from assist_guide.
"""

from __future__ import annotations

import json
import logging
import re

from app import model_router
from app.config import settings
from app.utils.tool_call_args import read_tool_args

logger = logging.getLogger("scaffold.assist_guide")


_PLACEHOLDER_TOKEN_RE = re.compile(r"<([A-Z][A-Z0-9_\-]{1,48})>")


# §17.854 (audit C3) — a resolver value is substituted verbatim into guidance
# that often becomes a shell command block, then auto-pinned and applied
# DETERMINISTICALLY forever. Reject any value carrying a shell metacharacter so a
# model (fed adversarial pasted output) can't inject e.g. `local-lvm; wipefs -a
# /dev/sda`. Also excludes the pre-existing `<` (nested token) and newline bars.
_UNSAFE_RESOLVER_VALUE_RE = re.compile(r"[;&|$`<>\n\r]")


_PLACEHOLDER_RESOLVER_TOOL = model_router.Tool(
    name="resolve_placeholders",
    description=(
        "Map each placeholder token to a concrete value from the operator's "
        "known facts, or a suggested fitting name for free-choice identifiers, "
        "or mark it unknown."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "resolutions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "token": {"type": "string", "description": "The placeholder name WITHOUT angle brackets."},
                        "value": {"type": "string", "description": "Concrete value, or empty when unknown."},
                        "kind": {"type": "string", "enum": ["known", "suggested", "unknown"],
                                 "description": "known = stated in the facts/profile; suggested = a free-choice name you propose; unknown = only the operator can supply it."},
                    },
                    "required": ["token", "kind"],
                },
            },
        },
        "required": ["resolutions"],
    },
)


_PLACEHOLDER_RESOLVER_SYSTEM = (
    "You resolve placeholder tokens in an operator walkthrough. For each token: "
    "if the operator's facts/profile state the actual value (an IP, URL, hostname, "
    "storage name, interface), return it EXACTLY as stated with kind=known. If the "
    "token names something NEW the operator is free to name (a new VM/container "
    "name, VMID, dataset, service user), propose ONE short fitting name for THIS "
    "project with kind=suggested. Only when neither applies (secrets, passwords, "
    "values truly not in the facts) use kind=unknown with an empty value. Never "
    "invent a kind=known value that is not literally supported by the facts. "
    "Each value must be ONLY that token's own part — when tokens compose (e.g. "
    "<POOL>/<DATASET>), never return a value that already includes a "
    "neighboring token's value."
)


async def resolve_placeholders(
    *, text: str, session_id: str, environment: Optional[dict],
    step_title: str = "", role: str = "model_general", db=None,
) -> tuple[str, dict]:
    """§17.851 — substitute <PLACEHOLDER> tokens in generated guidance.

    Returns ``(new_text, applied)`` where ``applied`` maps token →
    ``{"value": ..., "kind": known|suggested}``. Unknown tokens stay in the
    text. Fail-soft: any error returns the original text and ``{}``.
    """
    try:
        tokens = list(dict.fromkeys(_PLACEHOLDER_TOKEN_RE.findall(text or "")))
        if not tokens:
            return text, {}
        env = environment or {}
        subs = {str(k).upper(): str(v) for k, v in (env.get("substitutions") or {}).items() if str(v).strip()}
        applied: dict = {}
        out = text
        # Layer 1 — deterministic: pinned values win, no model involved.
        for tok in list(tokens):
            v = subs.get(tok.upper())
            if v:
                out = out.replace(f"<{tok}>", v)
                # source="operator" — an operator-set (or previously auto-pinned)
                # value, distinct from a fresh model suggestion (§17.854 C3).
                applied[tok] = {"value": v, "kind": "known", "source": "operator"}
                tokens.remove(tok)
        # Layer 2 — model-mapped against the facts ledger.
        facts = [str(f).strip() for f in (env.get("facts") or []) if str(f).strip()]
        if tokens and (facts or env.get("profile")):
            user = (
                (f"Step: {step_title}\n\n" if step_title else "")
                + "Operator facts:\n" + "\n".join(f"- {f}" for f in facts[:40])
                + (f"\n\nOperator profile: {env.get('profile')}" if env.get("profile") else "")
                + "\n\nPlaceholder tokens to resolve:\n"
                + "\n".join(f"- {t}" for t in tokens)
                + "\n\nCall resolve_placeholders with one entry per token."
            )
            resp = await model_router.tool_call(
                [
                    {"role": "system", "content": _PLACEHOLDER_RESOLVER_SYSTEM},
                    {"role": "user", "content": user},
                ],
                [_PLACEHOLDER_RESOLVER_TOOL],
                role=role,
                temperature=0.1,
                max_tokens=1024,
                tool_choice="auto",
            )
            if resp.success and resp.tool_calls:
                for r in (resp.tool_calls[0].arguments or {}).get("resolutions") or []:
                    tok = str(r.get("token") or "").strip().strip("<>")
                    val = str(r.get("value") or "").strip()
                    kind = r.get("kind")
                    if (tok in tokens and kind in ("known", "suggested") and val
                            and len(val) <= 200
                            and not _UNSAFE_RESOLVER_VALUE_RE.search(val)):
                        out = out.replace(f"<{tok}>", val)
                        # source="model" — a model-suggested value; tagged so the
                        # SPA pin editor / meta can flag it as not operator-set
                        # (§17.854 C3), even after auto-pin makes it deterministic.
                        applied[tok] = {"value": val, "kind": kind, "source": "model"}
        if applied:
            notes = []
            for tok, r in applied.items():
                src = "from your environment" if r["kind"] == "known" else "suggested — rename if you like"
                notes.append(f"- `{tok}` → `{r['value']}` ({src})")
            out = out.rstrip() + "\n\n---\n**Values filled in:**\n" + "\n".join(notes)
            # Auto-pin so the next generation is deterministic and the operator
            # can see/edit these in the Pinned values panel.
            if db is not None:
                try:
                    from app.modules import assist_agent
                    await assist_agent.set_environment(
                        session_id=session_id,
                        substitutions={t: r["value"] for t, r in applied.items()},
                        db=db,
                    )
                except Exception:
                    logger.warning("assist_placeholder_autopin_failed session=%s", session_id)
                    try:  # §17.888(#14)
                        await db.rollback()
                    except Exception:  # noqa: BLE001
                        pass
        return out, applied
    except Exception as exc:
        logger.warning("assist_placeholder_resolver_failed: %s", exc)
        return text, {}


# A walkthrough emits operator-supplied slots as <SCREAMING_SNAKE> (or
# <kebab>) placeholders (prompt_assembly §17.361). 2+ chars to avoid matching
# stray "<x>" in pasted output.
_PLACEHOLDER_RE = re.compile(r"<([A-Za-z][A-Za-z0-9_-]{1,})>")


_LEARN_SUBS_TOOL = model_router.Tool(
    name="report_values",
    description=(
        "Report the concrete value the operator actually used for each named "
        "placeholder, read from their pasted command output / evidence. Include "
        "a placeholder ONLY if its value is clearly present in the evidence; "
        "omit any you cannot determine with confidence. Do NOT guess."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "values": {
                "type": "object",
                "description": "Map of PLACEHOLDER name (no angle brackets) → concrete value.",
                "additionalProperties": {"type": "string"},
            }
        },
        "required": ["values"],
    },
)


def find_placeholders(text: str) -> list[str]:
    """Distinct placeholder names (no brackets) in a walkthrough, order-preserved."""
    seen: dict[str, None] = {}
    for m in _PLACEHOLDER_RE.findall(text or ""):
        seen.setdefault(m, None)
    return list(seen.keys())


async def extract_substitutions(
    *, guidance_text: str, evidence: str, role: str | None = None,
) -> dict:
    """Learn concrete values the operator used for the walkthrough's placeholders.

    Cheap gate: if the guidance emitted no placeholders, return {} WITHOUT an
    LLM call. Otherwise a single tool_call fills the placeholders it can read
    from the evidence. Fail-soft → {}.
    """
    placeholders = find_placeholders(guidance_text)
    if not placeholders:
        return {}
    role = role or settings.assist_guide_model_role
    try:
        resp = await model_router.tool_call(
            [
                {"role": "system", "content": (
                    "You extract the concrete values an operator used, from the "
                    "command output they pasted. Only report a value you can see "
                    "in the evidence; omit the rest. Never guess."
                )},
                {"role": "user", "content": (
                    f"Placeholders to fill (omit any you can't determine): "
                    f"{', '.join(placeholders)}\n\n"
                    f"Operator evidence:\n{evidence[:6000]}\n\n"
                    "Call report_values."
                )},
            ],
            [_LEARN_SUBS_TOOL],
            role=role,
            temperature=0.0,
            max_tokens=1024,
            tool_choice="auto",
        )
    except Exception as exc:
        logger.warning("assist_learn_extract_failed: %s", exc)
        return {}
    if not resp.success or not resp.tool_calls:
        return {}
    raw = (resp.tool_calls[0].arguments or {}).get("values") or {}
    if not isinstance(raw, dict):
        return {}
    # Keep only placeholders we actually asked about, with non-empty string
    # values; strip stray angle brackets the model may echo.
    allowed = set(placeholders)
    out: dict[str, str] = {}
    for k, v in raw.items():
        key = str(k).strip().strip("<>")
        val = str(v).strip()
        if key in allowed and val:
            out[key] = val
    return out
