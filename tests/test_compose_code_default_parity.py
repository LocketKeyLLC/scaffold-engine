"""§17.2058 — the code default IS the deployed default.

docker-compose.yml hands the orchestrator ``NAME: "${NAME:-x}"``: when the
operator's .env does not set NAME, the engine runs with ``x``. The test lane
(``make test``, CI, the agent lane) does not go through compose — it builds
``Settings`` from the code defaults in app/config.py plus .env. Any valve whose
compose fallback differs from its code default therefore runs one way in every
deployed engine and the other way in every test. About thirty such valves had
drifted (ON in compose, OFF in code); that hid §17.2054, an integration test
that failed only because ASSIST_BLOCK_ON_INCOMPLETE_VERIFY was off in the lane.

This gate keeps the two in step. For every ``${NAME:-x}`` in a
docker-compose*.yml whose NAME is a Settings field, ``x`` must equal the field's
code default (compared the way pydantic would parse the env string). It also
checks literal ``NAME: value`` lines in the orchestrator's environment block —
those are not even operator-overridable, so a literal that differs from the
code default is the same split. Topology values that are SUPPOSED to differ
between a container and a bare checkout (the container's hostnames and paths)
are listed in ``_TOPOLOGY`` with the reason.

Static: parses app/config.py with ``ast`` and the compose files as text — no
app import, no services — so it runs in ci-tier-0 under --noconftest.
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_CONFIG = _ROOT / "app" / "config.py"

# ``${NAME:-default}`` — the default runs to the matching ``}`` (no nesting in
# our compose files; a default never contains ``}`` except JSON, handled by
# _compose_fallbacks' brace counting).
_FALLBACK_START = re.compile(r"\$\{([A-Z][A-Z0-9_]*):-")
# A literal env line in a map-style environment block: ``    NAME: value``.
_LITERAL_LINE = re.compile(r"^(\s+)([A-Z][A-Z0-9_]*):\s*(.*?)\s*(?:#.*)?$")

# Settings whose compose literal is the CONTAINER's topology (a service
# hostname, an in-image path) — a bare checkout cannot share it, and the lane
# sets the ones it needs itself. Each entry says why it may differ.
_TOPOLOGY: dict[str, str] = {
    "LOG_FILE": "in-image path on the scaffold-logs volume; a checkout (and the "
    "lane, which passes LOG_FILE=) has no such volume, so the code default is None",
}

# Only the BASE compose file is the deployed configuration for literal lines.
# docker-compose.dev.yml's literals (PARALLEL_EXECUTION_ENABLED=false,
# NODE_TOKEN_STREAMING_ENABLED=true, LOG_FILE="") are deliberate dev-only
# deviations documented in that overlay; no engine deploys it (both engines run
# docker-compose.yml[+lan.yml]). ``${NAME:-x}`` fallbacks are checked in EVERY
# compose file.
_LITERAL_FILES = ("docker-compose.yml",)


# ── code defaults ────────────────────────────────────────────────────────────

_UNKNOWN = object()


def _literal(node: ast.AST):
    try:
        return ast.literal_eval(node)
    except (ValueError, SyntaxError, TypeError):
        pass
    # ``30 * 86400``-style arithmetic on constants
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Mult, ast.Add, ast.Sub)):
        a, b = _literal(node.left), _literal(node.right)
        if isinstance(a, (int, float)) and isinstance(b, (int, float)) \
                and not isinstance(a, bool) and not isinstance(b, bool):
            return {ast.Mult: a * b, ast.Add: a + b, ast.Sub: a - b}[type(node.op)]
    return _UNKNOWN


def _default_of(value: ast.AST):
    """The code default an annotated Settings field assigns, or _UNKNOWN."""
    if isinstance(value, ast.Call):
        fn = value.func
        name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")
        if name == "Field":
            for kw in value.keywords:
                if kw.arg == "default":
                    return _literal(kw.value)
                if kw.arg == "default_factory":
                    f = kw.value
                    if isinstance(f, ast.Name) and f.id in ("list", "dict"):
                        return [] if f.id == "list" else {}
                    if isinstance(f, ast.Lambda):
                        return _literal(f.body)
                    return _UNKNOWN
            if value.args:
                return _literal(value.args[0])
            return _UNKNOWN
        if name == "SecretStr" and value.args:
            return _literal(value.args[0])
        return _UNKNOWN
    return _literal(value)


def code_defaults(source: str) -> dict[str, tuple[str, object]]:
    """ENV_NAME -> (annotation source, default) for every class Settings field."""
    tree = ast.parse(source)
    out: dict[str, tuple[str, object]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "Settings":
            for stmt in node.body:
                if (
                    isinstance(stmt, ast.AnnAssign)
                    and isinstance(stmt.target, ast.Name)
                    and stmt.value is not None
                ):
                    out[stmt.target.id.upper()] = (
                        ast.unparse(stmt.annotation),
                        _default_of(stmt.value),
                    )
    return out


# ── compose values ───────────────────────────────────────────────────────────


def compose_fallbacks(text: str) -> list[tuple[str, str, int]]:
    """(NAME, fallback, line) for every ``${NAME:-fallback}`` in a compose text."""
    out: list[tuple[str, str, int]] = []
    for m in _FALLBACK_START.finditer(text):
        i, depth = m.end(), 1
        while i < len(text) and depth:
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
            i += 1
        out.append((m.group(1), text[m.end(): i - 1], text.count("\n", 0, m.start()) + 1))
    return out


def orchestrator_literals(text: str) -> list[tuple[str, str, int]]:
    """(NAME, value, line) for literal env lines in scaffold-orchestrator's
    map-style ``environment:`` block (values with no ``${``)."""
    out: list[tuple[str, str, int]] = []
    in_svc = in_env = False
    env_indent = -1
    for n, line in enumerate(text.splitlines(), 1):
        if re.match(r"^  [A-Za-z0-9_-]+:\s*$", line):
            in_svc = line.strip() == "scaffold-orchestrator:"
            in_env = False
            continue
        if not in_svc:
            continue
        if re.match(r"^    environment:\s*$", line):
            in_env, env_indent = True, -1
            continue
        if in_env:
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            indent = len(line) - len(line.lstrip())
            if indent <= 4:
                in_env = False
                continue
            m = _LITERAL_LINE.match(line)
            if m and "${" not in m.group(3):
                out.append((m.group(2), _unquote(m.group(3)), n))
    return out


def _unquote(v: str) -> str:
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


# ── comparison (pydantic's reading of the env string) ────────────────────────

_TRUE = {"1", "true", "yes", "on", "t", "y"}
_FALSE = {"0", "false", "no", "off", "f", "n"}


def env_equals_default(env: str, annotation: str, default) -> bool:
    """Would pydantic read ``env`` as the same value as ``default``?"""
    if default is _UNKNOWN:
        return True  # an expression default the scan cannot evaluate statically
    if isinstance(default, bool):
        low = env.strip().lower()
        return (low in _TRUE) if default else (low in _FALSE)
    if default is None:
        # ``${X:-}`` hands pydantic "" — for an Optional str that is "" not None,
        # but both mean "unset" to every reader in app/; anything else differs.
        return env == ""
    if isinstance(default, (int, float)):
        try:
            return float(env) == float(default)
        except ValueError:
            return False
    if isinstance(default, (list, dict)):
        try:
            return json.loads(env) == default
        except ValueError:
            return False
    return env == str(default)


def drift(root: Path = _ROOT) -> list[str]:
    defaults = code_defaults((root / "app" / "config.py").read_text(encoding="utf-8"))
    bad: list[str] = []
    for f in sorted(root.glob("docker-compose*.yml")):
        text = f.read_text(encoding="utf-8")
        pairs = [("fallback", *t) for t in compose_fallbacks(text)]
        if f.name in _LITERAL_FILES:
            pairs += [("literal", *t) for t in orchestrator_literals(text)]
        for kind, name, env, line in pairs:
            if name not in defaults or name in _TOPOLOGY:
                continue
            ann, default = defaults[name]
            if not env_equals_default(env, ann, default):
                bad.append(f"{f.name}:{line} {name} {kind}={env!r} code default={default!r}")
    return bad


# ── tests ────────────────────────────────────────────────────────────────────


def test_compose_defaults_match_code_defaults():
    bad = drift()
    assert not bad, (
        "§17.2058 — these compose values differ from the app/config.py default, "
        "so the test lane runs a configuration no engine deploys. Make the code "
        "default the deployed value (one source of truth):\n  " + "\n  ".join(bad)
    )


_MUST_FLAG = [
    # (env, annotation, default)
    ("true", "bool", False),
    ("false", "bool", True),
    ("12000", "int", 4000),
    ("0.5", "float", 0.25),
    ("other", "str", "base"),
    ('["a","b"]', "list[str]", ["a"]),
    ('{"x": 1}', "dict[str, int]", {}),
    ("x", "str | None", None),
]

_MUST_PASS = [
    ("true", "bool", True),
    ("1", "bool", True),
    ("False", "bool", False),
    ("12000", "int", 12000),
    ("0.50", "float", 0.5),
    ("base", "str", "base"),
    ('["a"]', "list[str]", ["a"]),
    ("{}", "dict[str, int]", {}),
    ("", "str | None", None),
]


@pytest.mark.parametrize("env,ann,default", _MUST_FLAG)
def test_comparison_flags_a_difference(env, ann, default):
    assert not env_equals_default(env, ann, default)


@pytest.mark.parametrize("env,ann,default", _MUST_PASS)
def test_comparison_passes_an_equal_value(env, ann, default):
    assert env_equals_default(env, ann, default)


def test_scan_reads_more_than_one_field_and_fallback():
    """The scan must actually see the production files (a gate that parses
    nothing passes vacuously)."""
    defaults = code_defaults(_CONFIG.read_text(encoding="utf-8"))
    assert len(defaults) > 300
    unknown = sorted(k for k, (_, v) in defaults.items() if v is _UNKNOWN)
    assert len(unknown) < 10, unknown  # the evaluator must read nearly every default
    assert defaults["ASSIST_UMEM_MAX_CHARS"][1] is not _UNKNOWN
    assert defaults["EMBEDDING_CACHE_TTL_S"][1] == 30 * 86400
    text = (_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    names = {n for n, _, _ in compose_fallbacks(text)}
    assert len(names & set(defaults)) > 20
    assert "ASSIST_BLOCK_ON_INCOMPLETE_VERIFY" in names
    lits = {n for n, _, _ in orchestrator_literals(text)}
    assert {"SCHEDULER_ENABLED", "MAX_RETRIES"} <= lits


def test_gate_bites_on_a_planted_drift(tmp_path):
    """Whole-pipeline must-flag / must-pass on a two-field config."""
    cfg = tmp_path / "app"
    cfg.mkdir()
    src = (
        "from pydantic import Field\n"
        "class Settings:\n"
        "    alpha_enabled: bool = False\n"
        "    beta_chars: int = Field(default=4000, ge=1)\n"
        "    gamma_list: list[str] = Field(default_factory=lambda: ['q'])\n"
    )
    defaults = code_defaults(src)
    assert defaults["ALPHA_ENABLED"][1] is False
    assert defaults["BETA_CHARS"][1] == 4000
    assert defaults["GAMMA_LIST"][1] == ["q"]
    compose = (
        "services:\n"
        "  scaffold-orchestrator:\n"
        "    environment:\n"
        '      ALPHA_ENABLED: "${ALPHA_ENABLED:-true}"\n'
        '      BETA_CHARS: "${BETA_CHARS:-4000}"\n'
        '      GAMMA_LIST: \'${GAMMA_LIST:-["q"]}\'\n'
        '      SCHEDULER_ENABLED: "true"\n'
    )
    fb = {n: v for n, v, _ in compose_fallbacks(compose)}
    assert fb == {"ALPHA_ENABLED": "true", "BETA_CHARS": "4000", "GAMMA_LIST": '["q"]'}
    flagged = [n for n, v in fb.items() if not env_equals_default(v, *defaults[n])]
    assert flagged == ["ALPHA_ENABLED"]
    assert [n for n, _, _ in orchestrator_literals(compose)] == ["SCHEDULER_ENABLED"]


if __name__ == "__main__":  # measurement aid: python3 tests/... [repo root]
    import sys

    print("\n".join(drift(Path(sys.argv[1]) if len(sys.argv) > 1 else _ROOT)) or "no drift")
