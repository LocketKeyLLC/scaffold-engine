"""§17.1131 (ledger L-6) — every UUID-backed id path parameter is declared as
``UuidPath`` (422 on a malformed id, documented in OpenAPI), and every
``limit`` / ``offset`` query parameter is a bounded ``Query(...)``.

Static, AST-only (the app mounts its routers at startup, so a runtime walk
sees 9 routes); runs in ``make ci-tier-0``. A new handler that declares
``job_id: str`` fails here before it can turn a mistyped link into a 500.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ROUTERS = sorted((ROOT / "app" / "routers").glob("*.py"))

#: Path params whose backing column is TEXT or an integer — not UUIDs.
NON_UUID_ID_PARAMS = {
    "chat_id",       # assist_sessions.chat_id — the OWUI chat id (text)
    "entry_id",      # KB entry slug (toon_v2 entry_id, rag_entry_provenance.entry_id — text)
    "recipe_id",     # setup recipe slug
    "proposal_id",   # model_role_proposals.id — bigint → Int64Path
    "schedule_id",   # scheduled_jobs.id — integer → Int32Path
}
UUID_OK = {"UuidPath", "uuid.UUID", "UUID"}
INT_OK = {"Int32Path", "Int64Path"}
ROUTE_METHODS = {"get", "post", "put", "patch", "delete"}


def _handlers():
    for f in ROUTERS:
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                continue
            for dec in node.decorator_list:
                if (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
                        and dec.func.attr in ROUTE_METHODS and dec.args
                        and isinstance(dec.args[0], ast.Constant)):
                    yield f.name, dec.args[0].value, node


def _params(node):
    args = node.args.posonlyargs + node.args.args + node.args.kwonlyargs
    defaults = {}
    if node.args.defaults:
        defaults.update(zip(node.args.args[-len(node.args.defaults):], node.args.defaults, strict=True))
    defaults.update({a: d for a, d in zip(node.args.kwonlyargs, node.args.kw_defaults, strict=True) if d is not None})
    return [(a, defaults.get(a)) for a in args]


def _id_path_params():
    for fname, path, node in _handlers():
        for a, _ in _params(node):
            if re.fullmatch(r"[a-z_]*id", a.arg) and f"{{{a.arg}}}" in path:
                yield fname, path, a.arg, ast.unparse(a.annotation) if a.annotation else "MISSING"


def _depends_targets():
    """Functions used as ``Depends(fn)`` in a router — a router- or route-level
    dependency that declares ``session_id: str`` itself runs (and can 404)
    before the endpoint's own parameter validation is reported, which is how
    the assist router kept answering 404 for a malformed id (§17.1131)."""
    for f in ROUTERS:
        tree = ast.parse(f.read_text(encoding="utf-8"))
        names = {n.args[0].id for n in ast.walk(tree)
                 if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "Depends"
                 and n.args and isinstance(n.args[0], ast.Name)}
        for node in tree.body:
            if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name in names:
                for a, _ in _params(node):
                    if re.fullmatch(r"[a-z_]*id", a.arg):
                        yield f.name, node.name, a.arg, ast.unparse(a.annotation) if a.annotation else "MISSING"


@pytest.mark.parametrize("fname,func,name,ann", list(_depends_targets()))
def test_dependency_targets_declare_uuid_ids_as_uuidpath(fname, func, name, ann):
    if name in NON_UUID_ID_PARAMS:
        return
    assert ann in UUID_OK or ann in INT_OK, (
        f"{fname}: dependency `{func}({name}: {ann})` — declare it `{name}: UuidPath`; "
        "a plain str here lets the dependency run on a malformed id before the 422 is raised"
    )


def test_raw_path_param_reads_of_uuid_ids_guard_with_is_uuid():
    """A function that reads a UUID id RAW from ``request.path_params`` (a
    router-level dependency has no declared param to validate) must check it
    with ``is_uuid`` before acting — otherwise it runs, and can 404, on
    garbage before the endpoint's 422 is raised."""
    uuid_names = {"job_id", "session_id", "run_id", "artifact_id", "error_id"}
    bad = []
    for f in ROUTERS:
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                continue
            src = ast.unparse(node)
            reads = [n for n in uuid_names if f'path_params.get("{n}")' in src or f"path_params[\"{n}\"]" in src]
            if reads and "is_uuid(" not in src:
                bad.append(f"{f.name}:{node.name} reads {reads} raw without is_uuid()")
    assert not bad, "\n".join(bad)


def test_the_scan_finds_the_routes():
    rows = list(_id_path_params())
    assert len(rows) >= 80, rows  # 91 at §17.1131 — a collapse means the scanner broke, not the routers


@pytest.mark.parametrize("fname,path,name,ann", list(_id_path_params()),
                         ids=lambda v: v if isinstance(v, str) and v.startswith("/") else None)
def test_uuid_id_path_params_are_typed_uuidpath(fname, path, name, ann):
    if name in NON_UUID_ID_PARAMS:
        # text keys stay str; integer ids must be bounded to their column
        # (a fuzzed 21-digit schedule_id was an asyncpg int32 overflow 500)
        assert ann == "str" or ann in INT_OK, f"{fname} {path}: {name} — text key → str, integer id → Int32Path/Int64Path; got {ann}"
        return
    assert ann in UUID_OK, (
        f"{fname} {path}: `{name}: {ann}` — declare it `{name}: UuidPath` "
        "(app.utils.ids) so a malformed id is a 422, not a DataError 500"
    )


def test_limit_and_offset_are_bounded_queries():
    bad = []
    for fname, path, node in _handlers():
        for a, d in _params(node):
            if a.arg not in ("limit", "offset"):
                continue
            ds = ast.unparse(d) if d is not None else ""
            ann = ast.unparse(a.annotation) if a.annotation else ""
            bounded_default = ds.startswith("Query(") and "ge=" in ds and "le=" in ds
            bounded_annotated = ann.startswith("Annotated[") and "Query(" in ann and "ge=" in ann and "le=" in ann
            if not (bounded_default or bounded_annotated):
                bad.append(f"{fname} {path} {a.arg}: {ann} = {ds or '-'}")
    assert not bad, "pagination params need BOTH ge= and le= (a negative limit, or an offset above int64, reaches SQL as a 500):\n" + "\n".join(bad)


def test_uuidpath_pattern_is_the_canonical_form():
    from app.utils.ids import UUID_PATTERN
    ok = ["613dd1df-4c92-43f7-a35f-c9519add5701", "00000000-0000-0000-0000-000000000000"]
    bad = ["0", "not-a-uuid", "s1", "613dd1df4c9243f7a35fc9519add5701", "613dd1df-4c92-43f7-a35f-c9519add570", ""]
    assert all(re.fullmatch(UUID_PATTERN, v) for v in ok)
    assert not any(re.fullmatch(UUID_PATTERN, v) for v in bad)
