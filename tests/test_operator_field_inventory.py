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


# §17.1135 (ledger D-7) — what counts as READING a payload field.
#
# The §17.1009 scan matched ``\bfield\b`` anywhere under the surface root, so
# ``created_at → CLI`` was satisfied by friction-note code and the gate could
# pass on coincidence. Two tightenings:
#   1. a read is an ACCESS — ``x.field`` / ``x?.field`` / ``x["field"]`` /
#      ``{ field }`` destructuring on the SPA; ``x["field"]`` / ``x.get("field"``
#      / ``x.field`` on the Python surfaces — with comments, docstrings and
#      CSS/prose out of the text first;
#   2. it must sit in code that TALKS TO THE PAYLOAD: a SPA file, or a Python
#      function, that contains one of the spec's ``paths`` fragments.

def _strip_js(src: str) -> str:
    src = re.sub(r"/\*[\s\S]*?\*/", "", src)
    return re.sub(r"(?m)^\s*//[^\n]*$|(?<=[;{}\s])//[^\n]*$", "", src)


def _strip_py(src: str) -> str:
    src = re.sub(r'"""[\s\S]*?"""|\'\'\'[\s\S]*?\'\'\'', "", src)
    return re.sub(r"(?m)#[^\n]*$", "", src)


def _js_reads(field: str, src: str) -> bool:
    f = re.escape(field)
    return bool(
        re.search(rf"[\w\)\]]\??\.{f}\b(?!\s*[:(=])", src)          # job.status / d?.status (not a key or call)
        or re.search(rf"""\[\s*["']{f}["']\s*\]""", src)              # row["status"]
        or re.search(rf"\{{[^{{}}]*\b{f}\b[^{{}}]*\}}\s*=\s*", src)   # const { status } = job
    )


def _py_reads(field: str, src: str) -> bool:
    f = re.escape(field)
    return bool(
        re.search(rf"""\[\s*["']{f}["']\s*\]""", src)                 # data["status"]
        or re.search(rf"""\.get\(\s*["']{f}["']""", src)               # data.get("status"
        or re.search(rf"""\b(?:row|r|j|job|node|n|step|s|d|data|item|it|payload|resp|body)\.{f}\b""", src)  # row.status
        or re.search(rf"""["']{f}["']\s+in\s+\w""", src)               # "status" in data
    )


def _py_functions_with_paths(src: str, paths: list[str]) -> list[str]:
    """Source of every function (any depth) whose body mentions a path fragment."""
    out: list[str] = []
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return [src] if any(p in src for p in paths) else []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            seg = ast.get_source_segment(src, node) or ""
            if any(p in seg for p in paths):
                out.append(seg)
    return out


def _js_scope(surface: str, paths: list[str]) -> set[Path]:
    """SPA files that talk to the payload: those naming a path fragment, plus
    one import hop each way — a fetcher module (store.js) feeds views, and a
    view hands the rows to a pure renderer module (dag_render.js) that never
    names the endpoint itself."""
    texts = {p: _strip_js(t) for p, t in _surface_text(surface).items() if p.suffix == ".js"}
    named = {p for p, src in texts.items() if any(fr in src for fr in paths)}
    imports: dict[Path, set[Path]] = {}
    for p, src in texts.items():
        deps = set()
        for m in re.finditer(r"""from\s+["'](\.{1,2}/[^"']+)["']""", src):
            target = (p.parent / m.group(1)).resolve()
            deps.add(target)
        imports[p] = deps
    scope = set(named)
    for p, deps in imports.items():
        if p in named:
            scope |= {d for d in deps if d in texts}          # renderer modules a consumer imports
        elif deps & named:
            scope.add(p)                                       # views fed by a fetcher module
    return scope


def _consumers_of(field: str, surface: str, paths: list[str] | None = None) -> list[str]:
    """Files under one surface that READ this field inside code that talks to
    the payload (see the note above). ``paths`` empty → whole-file scope, kept
    only for the self-tests."""
    hits: list[str] = []
    js_scope = _js_scope(surface, paths) if paths else None
    for p, text in _surface_text(surface).items():
        if p.suffix == ".js":
            src = _strip_js(text)
            if js_scope is not None and p.resolve() not in {q.resolve() for q in js_scope}:
                continue
            if _js_reads(field, src):
                hits.append(str(p.relative_to(REPO_ROOT)))
        elif p.suffix == ".py":
            src = _strip_py(text)
            scopes = _py_functions_with_paths(src, paths) if paths else [src]
            if any(_py_reads(field, seg) for seg in scopes):
                hits.append(str(p.relative_to(REPO_ROOT)))
        else:  # .html
            if (not paths or any(fr in text for fr in paths)) and _js_reads(field, _strip_js(text)):
                hits.append(str(p.relative_to(REPO_ROOT)))
    return hits


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
    consumers = _consumers_of(field, surface, PAYLOADS[payload].get("paths") or [])
    assert consumers, (
        f"{payload}.{field} is declared as something '{surface}' must render, "
        f"but no code under {surface}/ that talks to {PAYLOADS[payload].get('paths')} reads it "
        f"(an access like x.{field} / x[\"{field}\"], not a word match — §17.1135).\n"
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


# ── §17.1135 (ledger D-7) — the scan itself ───────────────────────────────

def test_every_payload_declares_the_paths_its_consumers_talk_to():
    for name, spec in PAYLOADS.items():
        assert spec.get("paths"), f"{name}: add `paths` (API path fragments the consuming code contains)"


def test_a_word_in_prose_or_a_comment_is_not_a_read():
    js = 'const x = job.title; // created_at is shown elsewhere\n/* status */ el("span", { text: "status" });'
    assert _js_reads("title", _strip_js(js))
    assert not _js_reads("created_at", _strip_js(js))
    assert not _js_reads("status", _strip_js(js)), "a key or a string is not a read"
    py = 'def f(data):\n    """created_at is documented here"""\n    # status: see below\n    return data.get("title")\n'
    assert _py_reads("title", _strip_py(py))
    assert not _py_reads("created_at", _strip_py(py))
    assert not _py_reads("status", _strip_py(py))


def test_a_read_outside_code_that_talks_to_the_payload_does_not_count(tmp_path):
    (tmp_path / "cli").mkdir()
    (tmp_path / "cli" / "main.py").write_text(
        "def friction():\n    note = api.get('/assist/x/friction')\n    return note['created_at']\n"
        "def show_job():\n    j = api.get('/jobs/' + jid)\n    return j['title']\n"
    )
    import tests.test_operator_field_inventory as m
    m._CONSUMER_TEXT.clear()
    old_root = m.REPO_ROOT
    try:
        m.REPO_ROOT = tmp_path
        assert m._consumers_of("title", "cli", ["/jobs/"]) == ["cli/main.py"]
        assert m._consumers_of("created_at", "cli", ["/jobs/"]) == [], "friction-note code must not satisfy a job field"
    finally:
        m.REPO_ROOT = old_root
        m._CONSUMER_TEXT.clear()
