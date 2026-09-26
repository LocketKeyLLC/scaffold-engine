"""§17.1179 (audit S2) — a routing behaviour must not live on ONE surface.

S2 called `pipelines/` "a second, hand-vendored implementation of the routing
layer" (12,146 lines). Measured, that overstates it in one direction and
understates it in another:

  * There are ZERO shared function names between the engine's assist modules
    and the pipeline's. The pipeline is not a copy of the engine's functions.
  * The pipeline DELEGATES the decision: it POSTs `/assist/{sid}/decide` and
    uses the engine's unified Decision.
  * But it then applies its own DETERMINISTIC VETOES to that decision — and
    the engine's own post-filter docstring says "Precedence mirrors the
    pipeline cascade", which is the §17.854/§17.1174 sibling-drift shape
    stated out loud.

So the real defect is narrow and nameable: a deterministic gate that exists
only in the pipeline is a behaviour an OWUI operator gets and an SPA, CLI or
MCP operator does not. Two such gates exist today (below). This test does not
fix them — porting §17.689 in particular changes live routing and is the
owner's call — it BOUNDS them, so the count can only go down.
"""
from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
ENGINE = ROOT / "app" / "modules" / "assist_policy.py"
PIPE = ROOT / "pipelines" / "_vendor" / "_assist_handlers.py"

_DEF = re.compile(r"^def (_?(?:looks_like|is)_[a-z0-9_]+)\(")

#: Deterministic gates the pipeline applies that the engine does not.
#: Each entry is a BEHAVIOUR ONE SURFACE HAS AND THE OTHERS DO NOT — a debt,
#: not an allowance. Removing an entry (by porting the gate into
#: `assist_policy`) is the fix; adding one requires a § entry saying why the
#: behaviour is legitimately OWUI-only.
KNOWN_PIPELINE_ONLY = {
    # §17.707 — "what do you still need from me?" → the input checklist.
    # High-precision phrasing; the safest of the two to port.
    "looks_like_checklist_request",
    # §17.689 — a confirmation of a proposed decision ("looks good", "yes")
    # routes to submit. Loose ON PURPOSE and safe only because the pipeline
    # scopes it to a pending proposal; porting it needs that same scoping or
    # it will commit steps on a bare "ok".
    "looks_like_decision_confirm",
}


def _detectors(path: pathlib.Path) -> set[str]:
    """Detector predicates, private/public normalised (the engine exports
    `looks_like_x`, the pipeline keeps `_looks_like_x`)."""
    return {m.group(1).lstrip("_")
            for line in path.read_text(encoding="utf-8").splitlines()
            if (m := _DEF.match(line))}


def test_no_new_gate_may_appear_on_the_pipeline_surface_alone():
    engine, pipe = _detectors(ENGINE), _detectors(PIPE)
    # shell_error / shell_evidence read as pipeline-only by NAME but the engine
    # applies the same patterns inline in `_override` (and `_SHELL_ERROR_RE` is
    # one of the 8 regexes the §17.1174 parity test compares byte-for-byte).
    inline_in_engine = {"looks_like_shell_error", "looks_like_shell_evidence"}
    pipeline_only = pipe - engine - inline_in_engine
    new = sorted(pipeline_only - KNOWN_PIPELINE_ONLY)
    assert not new, (
        "these deterministic gates exist only on the OWUI surface, so an SPA/CLI/MCP "
        f"operator does not get them: {new}. Port them into assist_policy, or add "
        "them to KNOWN_PIPELINE_ONLY with a § entry justifying the asymmetry."
    )


def test_the_known_divergence_list_does_not_rot():
    """Every name baselined above must still BE a pipeline-only gate. When one
    is ported (or deleted), this fails so the list shrinks instead of turning
    into decoration."""
    engine, pipe = _detectors(ENGINE), _detectors(PIPE)
    stale = sorted(n for n in KNOWN_PIPELINE_ONLY if n not in pipe or n in engine)
    assert not stale, (
        f"{stale} are no longer pipeline-only — remove them from KNOWN_PIPELINE_ONLY."
    )


def test_the_engine_post_filter_still_declares_it_mirrors_the_pipeline():
    """The engine's `_override` docstring is the load-bearing admission that
    these two cascades are meant to agree. If that sentence is ever removed,
    someone has decided they need not — and this test should be revisited
    rather than silently outlived."""
    src = ENGINE.read_text(encoding="utf-8")
    assert "Precedence mirrors the pipeline" in src, (
        "the engine no longer claims to mirror the pipeline cascade — is the "
        "duplication resolved, or was the claim just dropped?"
    )


@pytest.mark.parametrize("name", sorted(KNOWN_PIPELINE_ONLY))
def test_each_known_divergence_is_traceable_to_a_numbered_incident(name):
    """A baselined asymmetry with no § reference is untracked debt."""
    lines = PIPE.read_text(encoding="utf-8").splitlines()
    idx = next(i for i, ln in enumerate(lines) if _DEF.match(ln) and _DEF.match(ln).group(1).lstrip("_") == name)
    window = "\n".join(lines[max(0, idx - 25):idx])
    assert re.search(r"§17\.\d+", window), f"{name} cites no § incident"
