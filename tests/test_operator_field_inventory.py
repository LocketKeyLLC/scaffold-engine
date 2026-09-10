"""§17.1007c — drift guard for operator-facing payload fields.

The gap this closes
-------------------
``test_sse_event_inventory.py`` guards event NAMES: it scans emitters for
``_sse("name", ...)`` and consumers for ``event_type == "name"`` and fails when
either side drifts from ``ALL_EVENT_NAMES``. A field inside a payload sails
straight past it — which is how ``failure_reason`` sat on the ``/exec/status``
wire for three months, rendered by the CLI and by nothing in the operator SPA,
while every static gate stayed green.

Same two-guard shape as the SSE inventory, one level down:

1. ``test_producer_keys_are_declared`` — every key the producer emits is in the
   inventory. Catches a new field nobody declared.
2. ``test_every_declared_field_surface_pair_holds`` — every (field, SURFACE)
   pair declared holds. Per-surface, not "somewhere": the first draft of this
   file asserted only that some file read the field, and it passed, because the
   CLI rendered ``failure_reason`` throughout the three months the console did
   not. A gate that stays green through the whole life of the bug it was
   written for is worse than no gate.

Static scan, no imports of the app, no services: this runs in ci-tier-0
alongside the other inventory tests.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from app.operator_fields import ALL_SURFACES, PAYLOADS

REPO_ROOT = Path(__file__).resolve().parent.parent

# Extensions worth scanning for a consumer. A field named in a .md file is
# documentation, not a surface that shows it to anyone.
CONSUMER_SUFFIXES = {".js", ".py", ".html"}


def _producer_keys(spec: dict) -> set[str]:
    """Every string key assigned in a dict literal inside the producer function.

    Parsed with ast rather than grepped: a regex over ``"key":`` also matches
    keys in log formats, SQL fragments and docstrings, and this gate is only
    useful if what it calls a payload key really is one.
    """
    source = (REPO_ROOT / spec["producer"]).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == spec["function"]:
            keys: set[str] = set()
            for sub in ast.walk(node):
                if isinstance(sub, ast.Dict):
                    for key in sub.keys:
                        if isinstance(key, ast.Constant) and isinstance(key.value, str):
                            keys.add(key.value)
            return keys
    pytest.fail(f"{spec['function']}() not found in {spec['producer']}")


def _consumer_files(root: str | None = None) -> list[Path]:
    files: list[Path] = []
    for candidate in ([root] if root else list(ALL_SURFACES)):
        base = REPO_ROOT / candidate
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if (
                path.is_file()
                and path.suffix in CONSUMER_SUFFIXES
                and "__pycache__" not in path.parts
                and "node_modules" not in path.parts
                and not path.name.startswith("test_")
            ):
                files.append(path)
    return files


_CONSUMER_TEXT: dict[str, dict[Path, str]] = {}


def _surface_text(surface: str) -> dict[Path, str]:
    if surface not in _CONSUMER_TEXT:
        texts: dict[Path, str] = {}
        for path in _consumer_files(surface):
            try:
                texts[path] = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
        _CONSUMER_TEXT[surface] = texts
    return _CONSUMER_TEXT[surface]


def _consumers_of(field: str, surface: str) -> list[str]:
    """Files under one surface that read this field by name. Word-bounded so
    ``status`` does not match ``job_status`` and call the gate satisfied."""
    pattern = re.compile(rf"\b{re.escape(field)}\b")
    return [
        str(p.relative_to(REPO_ROOT))
        for p, text in _surface_text(surface).items()
        if pattern.search(text)
    ]


# ── Guard 1: the producer cannot emit an undeclared field ────────────────

def _sql_projection_keys(spec: dict) -> set[str]:
    """Column names a SQL-projecting producer emits.

    §17.1009. This payload was originally `consumer_only` — I declined to parse
    SQL on the grounds that a brittle parser gives false confidence. That was
    half right: a general SQL parser would be brittle, but the shape here is
    narrow and checkable. The producer selects an explicit column list and
    returns `dict(row)`, so the emitted keys are exactly the SELECT's output
    names: the alias after `AS` where there is one, the bare column name
    otherwise. Anything the function then merges in (the computed phase keys)
    is picked up by the dict-literal scan below, so both halves are covered.

    A projection this scan cannot read (a `SELECT *`, a dynamic column list)
    fails loudly rather than silently passing — that is the difference between
    a limitation and a hole.
    """
    source = (REPO_ROOT / spec["producer"]).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == spec["function"]:
            selects = [
                m.group(1)
                for const in ast.walk(node)
                if isinstance(const, ast.Constant) and isinstance(const.value, str)
                for m in [re.search(r"SELECT\s+(.*?)\s+FROM", const.value, re.S | re.I)]
                if m
            ]
            if not selects:
                pytest.fail(f"{spec['function']}(): no readable SELECT found")
            keys: set[str] = set()
            for projection in selects:
                if "*" in projection:
                    pytest.fail(
                        f"{spec['function']}(): SELECT * cannot be checked — name the "
                        "columns, or move this payload to a response model"
                    )
                for part in projection.split(","):
                    part = part.strip().rstrip(")")
                    if not part:
                        continue
                    alias = re.search(r"\bAS\s+([A-Za-z_][A-Za-z0-9_]*)\s*$", part, re.I)
                    if alias:
                        keys.add(alias.group(1))
                        continue
                    bare = re.match(r"^[A-Za-z_][A-Za-z0-9_]*\.([A-Za-z_][A-Za-z0-9_]*)$", part)
                    if bare:
                        keys.add(bare.group(1))
                    elif re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", part):
                        keys.add(part)
            return keys
    pytest.fail(f"{spec['function']}() not found in {spec['producer']}")


def _pydantic_model_fields(spec: dict) -> set[str]:
    """Declared field names of a Pydantic response model.

    Read from app/schemas.py by ast rather than by importing it: this gate runs
    in ci-tier-0 with --noconftest and no services, and importing the schemas
    module drags in the app's dependency tree for no benefit.
    """
    source = (REPO_ROOT / "app" / "schemas.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == spec["model"]:
            return {
                stmt.target.id
                for stmt in node.body
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
            }
    pytest.fail(f"model {spec['model']} not found in app/schemas.py")


@pytest.mark.parametrize("name", sorted(PAYLOADS))
def test_producer_keys_are_declared(name):
    spec = PAYLOADS[name]
    declared = set(spec["operator_fields"]) | set(spec["internal_fields"])
    if spec.get("kind") == "sql_projection":
        emitted = _sql_projection_keys(spec)
        undeclared = sorted(emitted - declared)
        assert not undeclared, (
            f"{name}: {spec['function']}() selects these columns but "
            f"app/operator_fields.py does not declare them:\n"
            + "\n".join(f"  - {k}" for k in undeclared)
        )
        return
    if spec.get("kind") == "pydantic":
        undeclared = sorted(_pydantic_model_fields(spec) - declared)
        assert not undeclared, (
            f"{name}: {spec['model']} declares these fields, but "
            f"app/operator_fields.py does not:\n"
            + "\n".join(f"  - {k}" for k in undeclared)
        )
        return
    emitted = _producer_keys(spec)
    # The producer function builds more than one dict (the response envelope,
    # SQL params, counters). Only flag keys that look like payload fields the
    # inventory already knows the shape of — i.e. ignore keys never intended
    # for this payload by requiring an intersection first.
    if not emitted & declared:
        pytest.fail(f"{name}: no declared field found in {spec['function']}() — inventory is stale")
    suspicious = {
        k for k in emitted
        if k not in declared and k.islower() and not k.startswith("_") and "." not in k
    }
    # Keys from unrelated dicts in the same function would be noise, so this
    # asserts on the NODE dict specifically: every key sitting alongside the
    # ones we know about.
    node_dict_keys = _node_dict_keys(spec)
    undeclared = sorted(node_dict_keys - declared)
    assert not undeclared, (
        f"{name}: these keys are emitted by {spec['function']}() but are not in "
        f"app/operator_fields.py — declare them as operator-facing (and give them "
        f"a consumer) or add them to the internal list with a reason:\n"
        + "\n".join(f"  - {k}" for k in undeclared)
        + f"\n(ignored, non-payload-looking: {sorted(suspicious - node_dict_keys)})"
    )


def _node_dict_keys(spec: dict) -> set[str]:
    """Keys of the ONE dict literal in the producer that carries the payload —
    identified as the dict containing the most declared fields."""
    source = (REPO_ROOT / spec["producer"]).read_text(encoding="utf-8")
    tree = ast.parse(source)
    declared = set(spec["operator_fields"]) | set(spec["internal_fields"])
    best: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == spec["function"]:
            for sub in ast.walk(node):
                if isinstance(sub, ast.Dict):
                    keys = {
                        k.value for k in sub.keys
                        if isinstance(k, ast.Constant) and isinstance(k.value, str)
                    }
                    if len(keys & declared) > len(best & declared):
                        best = keys
    return best


# ── Guard 2: a declared operator field must be visible somewhere ─────────

def _field_surface_pairs():
    return sorted(
        (name, field, surface)
        for name, spec in PAYLOADS.items()
        for field, surfaces in spec["operator_fields"].items()
        for surface in surfaces
    )


@pytest.mark.parametrize(
    "payload,field,surface",
    _field_surface_pairs(),
    ids=lambda v: str(v),
)
def test_every_declared_field_surface_pair_holds(payload, field, surface):
    """THE failure_reason test, and note that it is per-SURFACE.

    An earlier draft of this gate asserted only "some file somewhere reads this
    field" — and it passed, because `cli/scaffold_cli/main.py` rendered
    failure_reason the whole time the console did not. A gate that goes green
    through the entire life of the bug it was written for is worse than no
    gate: it converts an open question into a false assurance.
    """
    consumers = _consumers_of(field, surface)
    assert consumers, (
        f"{payload}.{field} is declared as something '{surface}' must render, "
        f"but no file under {surface}/ reads it.\n"
        f"Either wire that surface to it, or change the requirement in "
        f"app/operator_fields.py to say which surfaces genuinely need it.\n"
        f"(This is exactly the shape of the failure_reason gap: produced since "
        f"§17.450, rendered by the CLI, dropped by the SPA for three months.)"
    )


def test_internal_fields_all_carry_a_reason():
    """An allow-list without justifications becomes a place to silence the gate
    rather than a record of a decision."""
    for name, spec in PAYLOADS.items():
        for field, reason in spec["internal_fields"].items():
            assert reason and len(reason) > 15, (
                f"{name}.{field} is allow-listed as internal with no real reason: {reason!r}"
            )


def test_a_field_cannot_be_both_operator_facing_and_internal():
    for name, spec in PAYLOADS.items():
        overlap = set(spec["operator_fields"]) & set(spec["internal_fields"])
        assert not overlap, f"{name}: {sorted(overlap)} declared both ways"


def test_the_scan_actually_finds_files():
    """A consumer scan over an empty file set passes everything. If the roots
    move, this gate must fail loudly rather than turn into a no-op."""
    for surface in ALL_SURFACES:
        files = _consumer_files(surface)
        assert len(files) > 5, (
            f"only {len(files)} files found under {surface}/ — the scan root is "
            "wrong and every assertion about that surface is inert"
        )
