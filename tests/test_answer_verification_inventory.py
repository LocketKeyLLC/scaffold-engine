"""§17.1027 — every operator-facing ANSWER must be verified against its grounding.

§17.1020's gate enumerates the retrieval paths so a query cannot ship without
grounding. This is its twin at the other end of the pipeline: the SET of places
that hand the operator a model-written answer, each of which must run
`verify_answer` (values traced to the grounding, citations judged against the
source they cite, one regeneration, then a visible warning) — or be exempt
here, with the reason written down.

Why a gate and not a fix: the day's log shows the same subsystem patched
thirteen times, each patch verified on the component just edited. A new answer
path added next month, or an existing one refactored, would silently lose the
check. This fails naming the function.

Host-static: AST only, no app import, so it runs in ci-tier-0 (--noconftest).
"""
import ast
import pathlib

import pytest

APP_MODULES = pathlib.Path(__file__).resolve().parents[1] / "app" / "modules"

# The marker of an operator-facing generation: a model call carrying a
# `label="assist_…"` string (the §17.465 retry helper's telemetry label).
LABEL_PREFIX = "assist_"
VERIFIER = "verify_answer"

# Module-level functions that carry an assist_ label and legitimately do NOT
# verify, each with the reason. Adding here is a deliberate act for review.
EXEMPT = {
    ("assist_guide.py", "summarize_step_progress"): (
        "writes the per-step recap (GOAL/DONE/OPEN), an internal ledger the "
        "operator sees only as a status line, not an answer with claims to check."
    ),
    ("assist_guide.py", "summarize_project_progress"): (
        "writes the project recap board; same internal-ledger status as the "
        "step recap, no external product facts are asserted to the operator."
    ),
    ("assist_research_lib.py", "_focus_web_query"): (
        "produces a search QUERY, not an answer; it has nothing to verify "
        "against and its output never reaches the operator."
    ),
}


# §17.1029 — sites that verify but may NOT regenerate (`regenerate=None`),
# with the reason. A streamed walkthrough has already reached the operator;
# replacing it after the fact would contradict what they read, so the stream
# site annotates only. Everything else must offer a regeneration.
ANNOTATE_ONLY = {
    ("assist_guide.py", "generate_guidance_stream"): (
        "the text has already streamed to the operator delta by delta; a "
        "regenerated replacement would contradict what they have read, so this "
        "site appends the Unverified-specifics footer and persists the annotated copy."
    ),
}


def _labels(fn: ast.AST) -> list[str]:
    out = []
    for sub in ast.walk(fn):
        if isinstance(sub, ast.Call):
            for kw in sub.keywords:
                if kw.arg == "label" and isinstance(kw.value, ast.Constant) \
                        and isinstance(kw.value.value, str):
                    out.append(kw.value.value)
    return out


def _calls(fn: ast.AST) -> set[str]:
    names = set()
    for sub in ast.walk(fn):
        if isinstance(sub, ast.Call):
            f = sub.func
            n = getattr(f, "id", None) or getattr(f, "attr", None)
            if n:
                names.add(n)
    return names


def _answer_sites():
    """(file, module-level function) for every function that makes an
    assist_-labelled model call. Nested helpers belong to their enclosing
    site and are not listed separately."""
    sites = []
    for path in sorted(APP_MODULES.glob("assist_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if any(lb.startswith(LABEL_PREFIX) for lb in _labels(node)):
                sites.append((path.name, node.name, node))
    return sites


def test_answer_sites_are_discoverable():
    """If this finds too few the gate is inert and everything below is vacuous."""
    names = {(f, n) for f, n, _ in _answer_sites()}
    assert ("assist_research_lib.py", "research_one") in names, names
    assert ("assist_guide.py", "generate_fix") in names, names
    assert len(names) >= 5, names


def test_every_answer_site_verifies_or_is_exempt_with_a_reason():
    missing = []
    for fname, name, node in _answer_sites():
        if (fname, name) in EXEMPT:
            continue
        if VERIFIER not in _calls(node):
            missing.append(f"{fname}:{node.lineno} {name}()")
    assert not missing, (
        "operator-facing answer path(s) with NO verification against their "
        "grounding — call verify_answer (app/modules/assist_evidence.py) on the "
        "final text, or register the site in EXEMPT with the reason:\n  "
        + "\n  ".join(missing)
    )


def test_the_two_live_paths_are_verified_not_exempt():
    """The ask and fix paths are where the operator's complaints came from;
    they may never be exempted."""
    for key in (("assist_research_lib.py", "research_one"),
                ("assist_guide.py", "generate_fix")):
        assert key not in EXEMPT, f"{key} must be verified, not exempt"
    verified = {(f, n) for f, n, node in _answer_sites() if VERIFIER in _calls(node)}
    assert ("assist_research_lib.py", "research_one") in verified
    assert ("assist_guide.py", "generate_fix") in verified


def test_every_exemption_names_a_real_site_and_argues_for_itself():
    names = {(f, n) for f, n, _ in _answer_sites()}
    for key, reason in list(EXEMPT.items()) + list(ANNOTATE_ONLY.items()):
        assert key in names, f"{key} is exempted but no such answer site exists"
        assert len(reason.split()) >= 12, f"{key} is exempted without an argument"


def test_the_guide_paths_are_verified_not_exempt():
    """§17.1029 lifted the walkthrough exemption; it may not quietly return."""
    for key in (("assist_guide.py", "generate_guidance"),
                ("assist_guide.py", "generate_guidance_stream")):
        assert key not in EXEMPT, f"{key} must be verified, not exempt"
    verified = {(f, n) for f, n, node in _answer_sites() if VERIFIER in _calls(node)}
    assert ("assist_guide.py", "generate_guidance") in verified
    assert ("assist_guide.py", "generate_guidance_stream") in verified


def test_the_verifier_exists_and_is_wired_to_the_evidence_module():
    src = (APP_MODULES / "assist_evidence.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fns = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert {VERIFIER, "derive_need", "rank_evidence", "unsupported_specifics"} <= fns


def _enclosing_function(tree: ast.AST, call: ast.Call) -> str:
    best = ""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.lineno <= call.lineno <= (node.end_lineno or node.lineno):
                best = node.name if not best else best  # outermost wins
    return best


@pytest.mark.parametrize("rel", ["assist_guide.py", "assist_research_lib.py"])
def test_the_verified_sites_regenerate_through_the_verifier(rel):
    """A site that calls verify_answer but hands it no `regenerate` callable
    has silently downgraded to annotate-only. Every path must offer one,
    except the registered ANNOTATE_ONLY stream site — and that one must say
    so with an explicit `regenerate=None`, never by omission."""
    tree = ast.parse((APP_MODULES / rel).read_text(encoding="utf-8"))
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            n = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if n == VERIFIER:
                found = True
                regen = next((kw for kw in node.keywords if kw.arg == "regenerate"), None)
                assert regen is not None, \
                    f"{rel}:{node.lineno} verify_answer(...) without regenerate="
                is_none = isinstance(regen.value, ast.Constant) and regen.value.value is None
                site = (rel, _enclosing_function(tree, node))
                if site in ANNOTATE_ONLY:
                    assert is_none, f"{site} is registered annotate-only but passes a regenerator"
                else:
                    assert not is_none, \
                        f"{rel}:{node.lineno} {site[1]}() passes regenerate=None without being registered in ANNOTATE_ONLY"
                # §17.1028 — and must say what may CREDIT a value (trusted=) and
                # what earlier replies already flagged (flagged=), or the
                # engine's own prior guess becomes provenance a turn later.
                # §17.1030 — and the session's source-confirmed ledger (sourced=).
                # §17.1032 — and the operator-owned hosts (owned_hosts=).
                # §17.1034 — and the confirmed tier (confirmed=).
                for req in ("trusted", "flagged", "sourced", "owned_hosts", "confirmed"):
                    assert any(kw.arg == req for kw in node.keywords), \
                        f"{rel}:{node.lineno} verify_answer(...) without {req}="
    assert found, f"{rel}: no verify_answer call"
