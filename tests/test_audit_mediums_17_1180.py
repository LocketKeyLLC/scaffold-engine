"""§17.1180 — the MEDIUM findings the §17.1179 top-five pass did not cover.

Each test names the finding, the measured symptom, and the shape that made it
survive review.
"""
from __future__ import annotations

import inspect

import pytest


# ── M15: the memory guard unloaded a model the extract loop never loaded ─────

def test_the_research_unload_targets_the_role_the_extract_loop_used():
    """Both call sites resolved `model_verifier` while extracting with
    `model_research_extract`. Those were ONE role until §17.348 split them and
    the split never reached here — so on a local-model host the guard freed the
    wrong model and left the 5 GB extractor resident while the embedder
    cold-loaded, which is the precise squeeze it exists to prevent. Measured:
    `ollama_model_unload*` appears ZERO times in this host's logs, so neither
    its success nor its failure had ever been observed.
    """
    from app.modules import research_agent
    src = inspect.getsource(research_agent)
    assert '_unload_ollama_model(get_model("model_verifier"' not in src, (
        "the unload still names model_verifier"
    )
    n = src.count('_unload_ollama_model(get_model("model_research_extract", overrides))')
    assert n == 2, f"expected both call sites to unload the extract role, found {n}"


# ── M13: `issue` / `problem` as object nouns are not failure reports ─────────

CLAIMS = [
    "i've set up the issue tracker",
    "i installed the issue templates",
    "the problem-reporting page is configured",
    "i configured the problem dashboard",
    "i installed nginx",
]
NOT_CLAIMS = [
    "i ran into an issue",
    "there was a problem with the install",
    "the issue is the port",
    "i installed it but there are problems",
    "i had problems during the install",
    "i finished it, no problems.",
    "i haven't done it",
    "the install failed",
]


@pytest.mark.parametrize("msg", CLAIMS)
def test_an_object_noun_does_not_disqualify_a_completion_claim(msg):
    from app.modules.assist_policy import looks_like_completion_claim
    assert looks_like_completion_claim(msg), (
        f"{msg!r} is a completion claim; `issue`/`problem` here name a thing "
        "the operator built, not a failure"
    )


@pytest.mark.parametrize("msg", NOT_CLAIMS)
def test_the_trouble_sense_still_disqualifies(msg):
    """The widening must not cost the gate its job: standing alone, or followed
    by a preposition or copula, these still mean trouble."""
    from app.modules.assist_policy import looks_like_completion_claim
    assert not looks_like_completion_claim(msg), f"{msg!r} must not read as a claim"


# ── M7: a filename is not a domain ───────────────────────────────────────────

@pytest.mark.parametrize("token", ["server.js", "config.yaml", "App.jsx",
                                   "docker-compose.yml", "x.tar.gz", "main.go",
                                   "setup.py", "nginx.conf"])
def test_a_filename_is_not_a_lesson_token(token):
    """`_propose_plan_correction` SUBSTRING-matches these against every pending
    step's `prompt_template`. A Node or React plan names `server.js` in step
    after step, so one ruled-out lesson quoting it would stage a replan
    proposal against the operator's whole plan. The old inline comment called
    this harmless — that was the false premise."""
    from app.modules.assist_memory import _lesson_tokens
    assert token.lower() not in _lesson_tokens(f"ruled out: running {token} directly")


@pytest.mark.parametrize("domain", ["apt.servarr.com", "download.docker.com",
                                    "registry.example.org", "ppa.launchpad.net"])
def test_a_real_host_is_still_a_lesson_token(domain):
    from app.modules.assist_memory import _lesson_tokens
    assert domain.lower() in _lesson_tokens(f"ruled out: the {domain} repo is dead")


def test_a_backtick_literal_is_never_filtered_as_a_filename():
    """Backtick quoting is deliberate — the operator or the engine marked it.
    Filename filtering applies only to the bare dotted-token scan."""
    from app.modules.assist_memory import _lesson_tokens
    assert "node server.js" in _lesson_tokens("ruled out: `node server.js` as a service")


# ── M16: the shadow must not write into the operator's own records ───────────

def test_the_shadow_decision_does_not_write_a_friction_note():
    """`fire_shadow_decision`'s docstring gives as the REASON for its separate
    valve that the shadow "must not write diagnostic friction notes into real
    operator sessions" — and the body then called `record_friction`, which
    UPDATEs `assist_steps.friction_note` and is surfaced by `list_friction`,
    with a JSON dump of the operator's message in it."""
    import ast

    from app.modules import assist_decide
    src = inspect.getsource(assist_decide._shadow_decide_and_log)
    # Parse, do not grep: the explanatory comment in that function NAMES
    # `record_friction`, so a substring scan trips on its own rationale.
    tree = ast.parse(inspect.cleandoc(src))
    called = {
        (n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", ""))
        for n in ast.walk(tree) if isinstance(n, ast.Call)
    }
    assert "record_friction" not in called, (
        "the shadow still writes into the operator's step records"
    )
    assert "info" in called, "the diagnostic must still be recorded somewhere"


def test_the_shadow_docstring_still_states_the_contract():
    """The docstring is what made this a finding rather than a design choice.
    If it is ever removed, the guarantee goes with it."""
    from app.modules import assist_decide
    doc = " ".join((inspect.getdoc(assist_decide.fire_shadow_decision) or "").split())
    assert "must not write diagnostic friction notes into real operator sessions" in doc


def test_assist_decide_no_longer_re_exports_unused_regexes():
    """The two regexes were imported-but-unused (pyflakes flagged both) purely
    so `assist_agent` could reach `_compute_signals` through this module rather
    than through `assist_policy`, where it is defined."""
    import subprocess
    import sys
    r = subprocess.run([sys.executable, "-m", "pyflakes", "app/modules/assist_decide.py"],
                       capture_output=True, text=True)
    if r.returncode == 2 and "No module named" in (r.stderr or ""):
        pytest.skip("pyflakes not available in this image")
    assert r.stdout.strip() == "", f"assist_decide has dead imports again:\n{r.stdout}"


def test_compute_signals_is_imported_from_where_it_is_defined():
    from app.modules import assist_agent
    src = inspect.getsource(assist_agent)
    assert "from app.modules.assist_decide import _compute_signals" not in src
    assert "from app.modules.assist_policy import _compute_signals" in src
