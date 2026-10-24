"""§17.1749 — code written FOR the engine is written against the engine.

Live (job 2772acae "Paper-trading research lab in scaffold engine", 2026-10-22): the brief said "add a paper-trading
research lab to the scaffold engine itself (this repository's code)", and every CodeGen step was given the brief, a
knowledge-base search and its siblings' output -- never one line of the engine it was extending. So it guessed:

    T7   from app.clients.searxng import search          # no such module
         from app.utils.searxng import searxng_search    # no such module
         getattr(settings, "SEARXNG_BASE_URL", ...)      # the field is `searxng_url`
    T9   from engine.secrets_store import get_secret     # no package `engine`
    T12  import psycopg2 ... CREATE TABLE ...             # the engine is async SQLAlchemy + Alembic

each wrapped in try/except, so a wrong guess fell through to a made-up default (`localhost:8080`) instead of failing.
The sandbox smoke cannot see it either: the engine's package is not in the sandbox, so a missing `app.*` import is a
skip (§17.434).

Both ends, from the running engine's own source (`app/` is the code that is executing this step):
  - `block_for` -- the step is SHOWN the engine: conventions, the package layout, the modules its subject touches with
    their real signatures, and the settings fields it may read;
  - `unresolved_refs` -- the output is REFUSED when it imports an engine module or name that does not exist, reads a
    settings field that does not exist, or imports a package the engine does not have -- unless one of this job's own
    steps writes it (a sibling module, a new function, a new settings field).
Only for a brief that targets the engine's own code (`targets_the_engine`); any other job's `app` is its own.
"""
from __future__ import annotations

import ast
import importlib.util
import re
import sys
from functools import lru_cache
from pathlib import Path

from app.modules.execution_codegen_gate import PYTHON_LANGS, extract_code_blocks

#: the package that is running -- read, never imported module by module
ENGINE_PKG = Path(__file__).resolve().parent.parent

#: "the scaffold engine itself", "this repository's code", "the engine's own codebase"
_TARGETS_RE = re.compile(
    r"\bscaffold[- ]engine(?:'s)?\s+(?:itself|own\s+(?:code|codebase|source|repo(?:sitory)?)|code(?:base)?|source|repo(?:sitory)?)\b"
    r"|\b(?:to|in|into|of)\s+this\s+(?:repository|repo|codebase)\b"
    r"|\bthis\s+(?:repository|repo|codebase)'s\s+code\b"
    r"|\bthe\s+engine's\s+own\s+(?:code|codebase|source|repo(?:sitory)?)\b",
    re.I)


def targets_the_engine(brief: dict | None) -> bool:
    """Is this job writing code that becomes part of the engine itself?"""
    if not isinstance(brief, dict):
        return False
    parts = [brief.get("title"), brief.get("description"), *(brief.get("goals") or []),
             *(brief.get("outputs_expected") or [])]
    return bool(_TARGETS_RE.search(" ".join(str(p) for p in parts if p)))


def _dotted(path: Path) -> str:
    rel = path.relative_to(ENGINE_PKG.parent).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _signature(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    a = fn.args
    names = [x.arg for x in a.posonlyargs + a.args]
    if a.vararg:
        names.append("*" + a.vararg.arg)
    elif a.kwonlyargs:
        names.append("*")
    names += [x.arg for x in a.kwonlyargs]
    if a.kwarg:
        names.append("**" + a.kwarg.arg)
    return f"{'async ' if isinstance(fn, ast.AsyncFunctionDef) else ''}def {fn.name}({', '.join(names)})"


def _call_shape(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> dict:
    """§17.1756 — what a call to this function must look like."""
    a = fn.args
    pos = [x.arg for x in a.posonlyargs + a.args]
    n_def = len(a.defaults)
    required_pos = pos[:len(pos) - n_def] if n_def else pos
    kwonly = [x.arg for x in a.kwonlyargs]
    kw_required = [x.arg for x, d in zip(a.kwonlyargs, a.kw_defaults, strict=True) if d is None]
    # an `async def` that yields is an async generator (iterated, or entered as an async context manager) -- never awaited
    agen = isinstance(fn, ast.AsyncFunctionDef) and any(
        isinstance(n, (ast.Yield, ast.YieldFrom)) for n in ast.walk(fn)
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)) or n is fn)
    kinds = {x.arg: k for x in a.posonlyargs + a.args + a.kwonlyargs if (k := _kinds(x.annotation))}
    return {"async": isinstance(fn, ast.AsyncFunctionDef) and not agen, "pos": pos, "required_pos": required_pos,
            "posonly": [x.arg for x in a.posonlyargs], "kwonly": kwonly, "kw_required": kw_required,
            "varargs": a.vararg is not None, "varkw": a.kwarg is not None, "kinds": kinds,
            "returns_tuple": _is_tuple_annotation(fn.returns)}


def _is_tuple_annotation(ann: ast.AST | None) -> bool:
    if isinstance(ann, ast.Constant) and isinstance(ann.value, str):
        try:
            ann = ast.parse(ann.value, mode="eval").body
        except SyntaxError:
            return False
    return (isinstance(ann, ast.Subscript) and isinstance(ann.value, ast.Name) and ann.value.id in ("tuple", "Tuple"))


#: §17.1763 — parameters whose values are a closed set the engine defines. Live (T13): `generate_json(…,
#: role="research")` (no such role -- get_model raises) with `model=` too (refused: one or the other); T9 asked
#: `values_for` for `"alpaca_paper_api_key"`, a name `set_secret` refuses, so the keys could never be stored.
def _role_ok(v: str) -> bool:
    from app.config import ROLE_FIELDS
    return v in ROLE_FIELDS


def _secret_name_ok(v: str) -> bool:
    return bool(re.match(r"^[A-Z][A-Z0-9_]{0,63}$", v))


_DOMAINS: dict[tuple[str, str], tuple] = {
    ("app.model_router", "role"): (_role_ok, "a model ROLE (`model_general`, `model_coder`, … from "
                                             "`app.config.ROLE_FIELDS`)"),
    ("app.modules.runner_secrets", "name"): (_secret_name_ok, "an UPPER_SNAKE secret name (`set_secret` refuses "
                                                              "any other)"),
    ("app.modules.runner_secrets", "wanted"): (_secret_name_ok, "UPPER_SNAKE secret names (`set_secret` refuses "
                                                                "any other)"),
}


def _domain_mismatches(call: ast.Call, mod: str, name: str, shape: dict, consts: dict | None) -> list[str]:
    out: list[str] = []
    given = {shape["pos"][i]: a for i, a in enumerate(call.args) if i < len(shape["pos"])}
    given.update({k.arg: k.value for k in call.keywords if k.arg})
    if mod == "app.model_router" and "role" in given and "model" in given:
        out.append(f"`{name}(…)`: pass `role=` or `model=`, never both (the router refuses the pair)")
    for param, val in given.items():
        rule = _DOMAINS.get((mod, param))
        if not rule:
            continue
        if isinstance(val, ast.Name) and consts and val.id in consts:
            val = consts[val.id]
        literals = ([e for e in val.elts] if isinstance(val, (ast.List, ast.Tuple, ast.Set)) else [val])
        literals = [consts[e.id] if isinstance(e, ast.Name) and consts and e.id in consts else e for e in literals]
        bad = [e.value for e in literals if isinstance(e, ast.Constant) and isinstance(e.value, str) and not rule[0](e.value)]
        if bad:
            out.append(f"`{name}(…)`: `{param}` takes {rule[1]}; {', '.join(repr(b) for b in bad)} is not one")
    return out


def _tuples_used_whole(tree: ast.AST, engine_funcs: dict, engine_mods: dict, index: dict) -> list[str]:
    """§17.1763 — a result the engine returns as a tuple, kept whole and never unpacked. Live (T13):
    `llm_output = await generate_json(…)` then `isinstance(llm_output, list)` -- always False (it is `(parsed,
    response)`), so every batch came back empty."""
    out: list[str] = []
    for n in ast.walk(tree):
        if not (isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name)):
            continue
        call = n.value.value if isinstance(n.value, ast.Await) else n.value
        if not isinstance(call, ast.Call):
            continue
        t = _engine_target(call, engine_funcs, engine_mods, index)
        if not t or not index[t[0]]["funcs"][t[1]].get("returns_tuple"):
            continue
        var = n.targets[0].id
        unpacked = any(
            (isinstance(m, ast.Assign) and isinstance(m.value, ast.Name) and m.value.id == var
             and isinstance(m.targets[0], (ast.Tuple, ast.List)))
            or (isinstance(m, ast.Subscript) and isinstance(m.value, ast.Name) and m.value.id == var
                and isinstance(m.slice, ast.Constant) and isinstance(m.slice.value, int))
            or (isinstance(m, ast.Return) and isinstance(m.value, ast.Name) and m.value.id == var)
            for m in ast.walk(tree))
        if not unpacked:
            sig = _signature_of(t[0], t[1]) or t[1]
            out.append(f"`{var} = {'await ' if isinstance(n.value, ast.Await) else ''}{t[1]}(…)`: `{t[1]}` returns a "
                       f"tuple ({sig.split(')')[0]}) -> tuple); unpack it (`parsed, resp = …`) before using it")
    return out


#: §17.1761 — the builtin kinds an annotation admits; only plain ones are read (anything else admits everything)
_KIND_NAMES = {"str": "str", "dict": "dict", "list": "list", "int": "int", "float": "float", "bool": "bool",
               "Dict": "dict", "List": "list", "None": "None"}


def _kinds(ann: ast.AST | None) -> frozenset[str] | None:
    if ann is None:
        return None
    if isinstance(ann, ast.Constant) and isinstance(ann.value, str):       # a string annotation (`from __future__`)
        try:
            ann = ast.parse(ann.value, mode="eval").body
        except SyntaxError:
            return None
    if isinstance(ann, ast.BinOp) and isinstance(ann.op, ast.BitOr):
        left, right = _kinds(ann.left), _kinds(ann.right)
        return left | right if left is not None and right is not None else None
    if isinstance(ann, ast.Subscript):
        base = ann.value.id if isinstance(ann.value, ast.Name) else getattr(ann.value, "attr", "")
        if base == "Optional":
            inner = _kinds(ann.slice)
            return inner | {"None"} if inner is not None else None
        return frozenset({_KIND_NAMES[base]}) if base in _KIND_NAMES else None
    if isinstance(ann, ast.Constant) and ann.value is None:
        return frozenset({"None"})
    if isinstance(ann, ast.Name) and ann.id in _KIND_NAMES:
        k = {_KIND_NAMES[ann.id]}
        if ann.id == "float":
            k.add("int")                                    # an int is a float to a type checker
        return frozenset(k)
    return None


def _literal_kind(v: ast.AST, consts: dict | None = None) -> str | None:
    """The builtin kind of a literal argument (or of a module constant bound to one)."""
    if isinstance(v, ast.Name) and consts and v.id in consts:
        v = consts[v.id]
    if isinstance(v, (ast.Dict, ast.DictComp)):
        return "dict"
    if isinstance(v, (ast.List, ast.ListComp)):
        return "list"
    if isinstance(v, ast.JoinedStr):
        return "str"
    if isinstance(v, ast.Constant):
        return {str: "str", bool: "bool", int: "int", float: "float", type(None): "None"}.get(type(v.value))
    return None


def _shape_signature(name: str, shape: dict) -> str:
    """`write_order(*, proposal_id, approval_id, broker_order_id=…)` from a call shape (§17.1785)."""
    req = set(shape.get("required_pos") or []) | set(shape.get("kw_required") or [])
    pos = [p if p in req else f"{p}=…" for p in shape.get("pos") or []]
    kw = [p if p in req else f"{p}=…" for p in shape.get("kwonly") or []]
    parts = pos + (["*args"] if shape.get("varargs") else (["*"] if kw else [])) + kw + (["**kwargs"] if shape.get("varkw") else [])
    return f"{'async ' if shape.get('async') else ''}{name}({', '.join(parts)})"


def _call_mismatches(call: ast.Call, name: str, shape: dict, coroutine_misused: bool,
                     consts: dict | None = None) -> list[str]:
    """§17.1756 — a call to an engine function that cannot work as written. §17.1785 — each finding ends with the
    whole signature: live (job f00ea97f T17, three attempts) "takes at most 0 positional argument(s)" / "missing
    `approval_id`" fixed one argument per attempt and broke another."""
    found = _call_mismatches_bare(call, name, shape, coroutine_misused, consts)
    return [f"{m} -- the signature is `{_shape_signature(name, shape)}`" for m in found]


def _call_mismatches_bare(call: ast.Call, name: str, shape: dict, coroutine_misused: bool,
                          consts: dict | None = None) -> list[str]:
    out: list[str] = []
    label = f"`{name}(…)`"
    # §17.1761 — live (T14): `frame_decision(node, brief=…, upstream={"proposal": …})` (upstream: str) and
    # `park_awaiting_decision(db, job_id, APPROVAL_NODE_KEY, frame)` (node: dict, given the string "paper_trade_approval")
    kinds = shape.get("kinds") or {}
    given = [(shape["pos"][i], a) for i, a in enumerate(call.args) if i < len(shape["pos"]) and not isinstance(a, ast.Starred)]
    given += [(k.arg, k.value) for k in call.keywords if k.arg]
    for param, val in given:
        want, got = kinds.get(param), _literal_kind(val, consts)
        if want and got and got not in want:
            out.append(f"{label}: `{param}` is `{' | '.join(sorted(want))}`; it was given a `{got}`"
                       + (f" (`{val.id}`)" if isinstance(val, ast.Name) else ""))
    if shape["async"] and coroutine_misused:
        out.append(f"{label} is `async def` in the engine: `await` it (called bare, it returns a coroutine, not a result)")
    if any(isinstance(x, ast.Starred) for x in call.args) or any(k.arg is None for k in call.keywords):
        return out                                       # *args / **kwargs at the call site: shapes unknowable
    n_pos = len(call.args)
    if n_pos > len(shape["pos"]) and not shape["varargs"]:
        out.append(f"{label} takes at most {len(shape['pos'])} positional argument(s) ({', '.join(shape['pos']) or 'none'}); "
                   f"it was given {n_pos}")
    given_kw = [k.arg for k in call.keywords if k.arg]
    allowed = set(shape["pos"]) - set(shape["posonly"]) | set(shape["kwonly"])
    unknown = [k for k in given_kw if k not in allowed]
    if unknown and not shape["varkw"]:
        out.append(f"{label} has no parameter {', '.join('`' + k + '`' for k in unknown)} "
                   f"(it takes {', '.join(shape['pos'] + ['*'] * bool(shape['kwonly']) + shape['kwonly'])})")
    filled = set(shape["pos"][:n_pos]) | set(given_kw)
    missing = [p for p in shape["required_pos"] + shape["kw_required"] if p not in filled]
    if missing:
        out.append(f"{label} is missing required argument(s) {', '.join('`' + m + '`' for m in missing)}")
    return out


def _engine_target(call: ast.Call, engine_funcs: dict, engine_mods: dict, index: dict) -> tuple[str, str] | None:
    if isinstance(call.func, ast.Name) and call.func.id in engine_funcs:
        return engine_funcs[call.func.id]
    if (isinstance(call.func, ast.Attribute) and isinstance(call.func.value, ast.Name)
            and call.func.value.id in engine_mods and call.func.attr in index[engine_mods[call.func.value.id]].get("funcs", {})):
        return engine_mods[call.func.value.id], call.func.attr
    return None


def _coroutines_used_as_values(tree: ast.AST, engine_funcs: dict, engine_mods: dict, index: dict) -> set[int]:
    """§17.1756 — the bare calls to an engine coroutine function whose coroutine is used as if it were the result.

    Live (T14): `frame = frame_decision(…)` then `park_awaiting_decision(db, job_id, KEY, frame)` -- never awaited.
    NOT flagged (measured on the engine's own source, 50 such calls, all correct): a coroutine handed to another
    function (`wait_for(f())`, `gather(f(), g())`, a heartbeat wrapper), returned, put in a collection, or assigned to a
    name that is later awaited or handed to a non-engine function. Flagged: a discarded call; an assignment whose
    name is never awaited and only reaches engine functions (which want values) or nothing; a direct argument to an
    engine function."""
    parent: dict[int, ast.AST] = {}
    for p in ast.walk(tree):
        for c in ast.iter_child_nodes(p):
            parent[id(c)] = p
    awaited_names = {n.value.id for n in ast.walk(tree) if isinstance(n, ast.Await) and isinstance(n.value, ast.Name)}

    def is_engine_coroutine(call: ast.AST) -> bool:
        if not isinstance(call, ast.Call):
            return False
        t = _engine_target(call, engine_funcs, engine_mods, index)
        return bool(t and index[t[0]]["funcs"][t[1]]["async"])

    def receiver(node: ast.AST) -> ast.Call | None:
        """The call this node is an argument (or keyword value) of."""
        p = parent.get(id(node))
        if isinstance(p, ast.keyword):
            p = parent.get(id(p))
        return p if isinstance(p, ast.Call) and node is not p.func else None

    out: set[int] = set()
    for call in ast.walk(tree):
        if not is_engine_coroutine(call):
            continue
        p = parent.get(id(call))
        if isinstance(p, ast.Await):
            continue
        if isinstance(p, ast.Expr):
            out.add(id(call))                                        # discarded
            continue
        # §17.1787 — indexed, an attribute read, compared, iterated or computed with: a coroutine has none of that
        if ((isinstance(p, (ast.Subscript, ast.Attribute)) and p.value is call)
                or isinstance(p, (ast.Compare, ast.BinOp, ast.BoolOp))
                or (isinstance(p, (ast.For, ast.comprehension)) and p.iter is call)):
            out.add(id(call))
            continue
        r = receiver(call)
        if r is not None:
            if _engine_target(r, engine_funcs, engine_mods, index):
                out.add(id(call))                                    # an engine function wants the value
            continue
        if isinstance(p, (ast.Assign, ast.AnnAssign)):
            targets = p.targets if isinstance(p, ast.Assign) else [p.target]
            if len(targets) != 1 or not isinstance(targets[0], ast.Name):
                continue
            name = targets[0].id
            if name in awaited_names:
                continue
            uses = [n for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id == name and isinstance(n.ctx, ast.Load)]
            handed_on = [u for u in uses if (rc := receiver(u)) is not None
                         and not _engine_target(rc, engine_funcs, engine_mods, index)]
            if not handed_on:
                out.add(id(call))                                    # never awaited, never handed to a consumer
    return out


@lru_cache(maxsize=1)
def module_index() -> dict[str, dict]:
    """``{"app.modules.x": {"doc": first docstring line, "names": every top-level name, "public": [signatures]}}``."""
    out: dict[str, dict] = {}
    for path in sorted(ENGINE_PKG.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        names: set[str] = set()
        public: list[str] = []
        values: list[str] = []
        funcs: dict[str, dict] = {}
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names.add(node.name)
                funcs[node.name] = _call_shape(node)
                if not node.name.startswith("_"):
                    public.append(_signature(node))
            elif isinstance(node, ast.ClassDef):
                names.add(node.name)
                if not node.name.startswith("_"):
                    public.append(f"class {node.name}")
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                for t in (node.targets if isinstance(node, ast.Assign) else [node.target]):
                    for n in ast.walk(t):
                        if isinstance(n, ast.Name):
                            names.add(n.id)
                            if not n.id.startswith("_") and n.id != "logger":
                                values.append(n.id)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                names.update((al.asname or al.name).split(".")[0] for al in node.names)
            elif isinstance(node, (ast.If, ast.Try)):
                # a name bound under `if TYPE_CHECKING:` / `try: import x` is still importable
                for n in ast.walk(node):
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        names.add(n.name)
                    elif isinstance(n, ast.alias):
                        names.add((n.asname or n.name).split(".")[0])
                    elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                        names.add(n.id)
        doc = (ast.get_docstring(tree) or "").strip().splitlines()
        out[_dotted(path)] = {"doc": doc[0][:160] if doc else "", "names": names, "public": public, "values": values,
                              "funcs": funcs}
    return out


@lru_cache(maxsize=1)
def settings_fields() -> dict[str, str]:
    """Every field of the engine's `Settings`, with its default -- blank for anything secret-shaped."""
    from app.config import settings
    out: dict[str, str] = {}
    for name, f in type(settings).model_fields.items():
        secretish = re.search(r"key|token|secret|password|passwd|credential", name, re.I)
        out[name] = "" if secretish else repr(f.default)[:80]
    for name in dir(type(settings)):
        if not name.startswith("_"):
            out.setdefault(name, "")
    return out


#: installed (other packages pull them in) but never used by the engine: both block the event loop it runs on
NOT_IN_THE_ENGINE = {
    "requests": "blocking HTTP on the engine's event loop; use `httpx.AsyncClient`",
    "psycopg2": "a blocking database driver; use `app.database.async_session`",
}


#: §17.1757 — the engine's test tooling (requirements-dev.txt), by import name. Not installed in the production image
#: this check runs in, and not readable there (no requirements files ship); live (job 2772acae T22 "Write integration
#: tests") was refused for `import pytest`. tests/test_codegen_for_the_engine_reads_the_engine.py keeps it equal to the
#: pinned test tools in requirements-dev.txt.
TEST_TOOLING = frozenset({"pytest", "pytest_asyncio", "pytest_timeout", "pytest_cov", "hypothesis", "mutmut"})


def _installed(top: str) -> bool:
    if top in sys.stdlib_module_names or top in sys.builtin_module_names or top in TEST_TOOLING:
        return True
    try:
        return importlib.util.find_spec(top) is not None
    except (ImportError, ValueError):
        return False


_SIBLING_FILE_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\.py\b")


def job_own_code(texts: list[str]) -> tuple[set[str], set[str]]:
    """What this job's own steps write: module names (``risk_controls.py``) and the top-level names their Python
    defines -- so a step may import a sibling, or a function or settings field a sibling adds."""
    modules: set[str] = set()
    names: set[str] = set()
    for t in texts:
        if not t:
            continue
        modules.update(_SIBLING_FILE_RE.findall(t))
        for lang, code in extract_code_blocks(t):
            if lang not in PYTHON_LANGS:
                continue
            try:
                tree = ast.parse(code)
            except SyntaxError:
                continue
            for n in ast.walk(tree):
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    names.add(n.name)
                elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
                    names.add(n.target.id)
                elif isinstance(n, ast.Assign):
                    names.update(x.id for x in n.targets if isinstance(x, ast.Name))
    return modules, names


_MODULE_OF_STEP_RE = re.compile(r"^\s*#\s*((?:[\w.-]+/)*)([A-Za-z_]\w*)\.py\b")


def step_module_path(row: dict) -> str:
    """The dotted module a step writes (`app.modules.x` unless its header names another folder)."""
    for lang, code in extract_code_blocks(str(row.get("output_text") or "")):
        m = _MODULE_OF_STEP_RE.match(code.lstrip().splitlines()[0]) if lang in PYTHON_LANGS and code.strip() else None
        if m:
            folder = m.group(1).strip("/").replace("/", ".")
            return f"{folder}.{m.group(2)}" if folder else f"app.modules.{m.group(2)}"
    leaf = step_module(row)
    return f"app.modules.{leaf}" if leaf else ""


def step_module(row: dict) -> str:
    """The module a step writes: its code's `# app/modules/x.py` header, else the first `x.py` its own text names."""
    for lang, code in extract_code_blocks(str(row.get("output_text") or "")):
        m = _MODULE_OF_STEP_RE.match(code.lstrip().splitlines()[0]) if lang in PYTHON_LANGS and code.strip() else None
        if m and not re.match(r"(?:test_\w+|\w+_test|conftest)$", m.group(2)):     # a step's tests are not its module
            return m.group(2)
    m = _SIBLING_FILE_RE.search(" ".join(str(row.get(k) or "") for k in ("prompt_template", "title", "description")))
    return m.group(1) if m else ""


def sibling_definitions(rows: list[dict]) -> dict[str, dict]:
    """§17.1762 — what each module this job's steps have already written defines: its top-level names and the call
    shape of each function, ``{"alpaca_stream": {"node": "T10", "names": {...}, "funcs": {...}}}``. A step not yet
    written is absent (nothing to check against)."""
    out: dict[str, dict] = {}
    for r in rows:
        mod = step_module(r)
        names: set[str] = set()
        defined: set[str] = set()
        funcs: dict[str, dict] = {}
        classes: dict[str, dict] = {}
        seen = False
        for lang, code in extract_code_blocks(str(r.get("output_text") or "")):
            if lang not in PYTHON_LANGS:
                continue
            head = _MODULE_OF_STEP_RE.match(code.lstrip().splitlines()[0]) if code.strip() else None
            if head and re.match(r"(?:test_\w+|\w+_test|conftest)$", head.group(2)):
                continue        # §17.1784 — a step's test file is not its module (live: T21 "had" FakeSecretStore)
            try:
                tree = ast.parse(code)
            except SyntaxError:
                continue
            seen = True
            for n in tree.body:
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    names.add(n.name)
                    defined.add(n.name)
                    funcs[n.name] = _call_shape(n)
                elif isinstance(n, ast.ClassDef):
                    names.add(n.name)
                    defined.add(n.name)
                    classes[n.name] = _class_shape(n)                     # §17.1798
                elif isinstance(n, (ast.Assign, ast.AnnAssign)):
                    for t in (n.targets if isinstance(n, ast.Assign) else [n.target]):
                        ids = {x.id for x in ast.walk(t) if isinstance(x, ast.Name)}
                        names.update(ids)
                        defined.update(ids)
                elif isinstance(n, (ast.Import, ast.ImportFrom)):
                    names.update((al.asname or al.name).split(".")[0] for al in n.names)
                elif isinstance(n, (ast.If, ast.Try)):
                    for x in ast.walk(n):
                        if isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                            names.add(x.name)
                        elif isinstance(x, ast.Name) and isinstance(x.ctx, ast.Store):
                            names.add(x.id)
        if mod and seen:
            out[mod] = {"node": r.get("node_key"), "names": names, "defined": defined, "funcs": funcs,
                        "classes": classes}
    return out


def _class_shape(c: ast.ClassDef) -> dict:
    """§17.1798 — a class's public surface: how it is built, its public methods (without `self`), and the fields of a
    data class. Live (job f00ea97f T25): the module index showed module-level functions only, so the whole
    `AlpacaPaperClient` API (get_account, get_position, list_orders, get_latest_trade) was invisible and the
    integration hard-coded a price of 100.00 and day-start equity equal to equity."""
    init: dict | None = None
    methods: dict[str, dict] = {}
    fields: list[str] = []
    for x in c.body:
        if isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef)):
            deco = {getattr(d, "id", None) or getattr(d, "attr", None) for d in x.decorator_list}
            sh = _call_shape(x)
            if "staticmethod" not in deco:
                for k in ("pos", "required_pos", "posonly"):
                    sh[k] = sh[k][1:] if sh[k] and sh[k][0] in ("self", "cls") else sh[k]
            if x.name == "__init__":
                init = sh
            elif not x.name.startswith("_") and "property" not in deco:
                methods[x.name] = sh
            elif "property" in deco and not x.name.startswith("_"):
                fields.append(x.name)
        elif isinstance(x, ast.AnnAssign) and isinstance(x.target, ast.Name) and not x.target.id.startswith("_"):
            fields.append(x.target.id)
    return {"init": init, "methods": methods, "fields": fields}


def sibling_refs(output: str, defs: dict[str, dict]) -> list[str]:
    """§17.1762 — an import of a name a sibling module does not define, or a call that does not fit its signature.

    Live (job 2772acae): T12 imported six names from T10's `alpaca_stream` that T10 never wrote and called T16's
    `append_audit_event(…, runner_id=…)` (no such parameter); T17 imported `ProposalGenerator` and
    `SimulatedOrderExecutor`, which neither T13 nor T15 defines. §17.1749 let any name through once some sibling
    defined it anywhere."""
    found: list[str] = []
    for lang, code in extract_code_blocks(output or ""):
        if lang not in PYTHON_LANGS:
            continue
        try:
            tree = ast.parse(code)
        except SyntaxError:
            continue
        imported: dict[str, tuple[str, str]] = {}
        mods: dict[str, str] = {}
        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
                leaf = n.module.split(".")[-1]
                if leaf not in defs or (n.module.startswith("app.") and n.module in module_index()):
                    continue
                d = defs[leaf]
                for al in n.names:
                    if al.name == "*":
                        continue
                    if al.name not in d["names"]:
                        # §17.1796 — functions first (what a caller needs), constants after
                        pub = [x for x in d["defined"] if not x.startswith("_") and x not in ("logger", "__all__")]
                        have = ", ".join(sorted(pub, key=lambda x: (x not in d["funcs"], x))[:24])
                        msg = (f"`from {n.module} import {al.name}`: `{leaf}` (step {d['node']}) has no `{al.name}`"
                               + (f" (it has {have})" if have else "")
                               # §17.1776 — live (f00ea97f T7/T8/T12): every ingestion step invented T4's
                               # `write_ingested_item`; what a sibling does not have, this step writes itself
                               + " -- use what it has, or write what this step needs in this step's own module "
                                 "(a table the job's migration creates is written with `async_session` + `text`)")
                        if msg not in found:
                            found.append(msg)
                    elif al.name in d["funcs"]:
                        imported[al.asname or al.name] = (leaf, al.name)
            elif isinstance(n, ast.Import):
                for al in n.names:
                    leaf = al.name.split(".")[-1]
                    if leaf in defs and not al.name in module_index():
                        mods[al.asname or leaf] = leaf
        # §17.1787 — a sibling's coroutine used as a value, the same rule §17.1756 applies to the engine's own. Live
        # (job f00ea97f T18): `… = client.get_order(…)["filled_qty"]` -- "'coroutine' object is not subscriptable"
        # in the sandbox, every attempt.
        pseudo = {leaf: {"funcs": d["funcs"]} for leaf, d in defs.items()}
        misused = _coroutines_used_as_values(tree, imported, mods, pseudo) if (imported or mods) else set()
        for n in ast.walk(tree):
            if not isinstance(n, ast.Call):
                continue
            target = None
            if isinstance(n.func, ast.Name) and n.func.id in imported:
                target = imported[n.func.id]
            elif (isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name) and n.func.value.id in mods):
                leaf = mods[n.func.value.id]
                if n.func.attr in defs[leaf]["funcs"]:
                    target = (leaf, n.func.attr)
                elif n.func.attr not in defs[leaf]["names"]:
                    found.append(f"`{n.func.value.id}.{n.func.attr}(…)`: `{leaf}` (step {defs[leaf]['node']}) has no "
                                 f"`{n.func.attr}`")
            if target:
                leaf, fname = target
                for m in _call_mismatches(n, fname, defs[leaf]["funcs"][fname], id(n) in misused):
                    m = m.replace("in the engine", f"in `{leaf}`")
                    if m not in found:
                        found.append(m + f" (`{leaf}`, step {defs[leaf]['node']})")
    return found


def imports_an_unwritten_sibling(output: str, others: list[dict], written: dict[str, dict]) -> list[str]:
    """§17.1787 — an import of a module another step has not written yet. Live (job f00ea97f T20, 16 attempts): the
    backtester imported T22's `paper_performance_metrics_calculator`, which a reset had emptied and T20 does not stand
    on -- §17.1749 allowed any module the plan names, so it passed the static check and every sandbox run failed with
    `No module named …`."""
    planned = {step_module(r): r.get("node_key") for r in others if step_module(r)}
    found: list[str] = []
    for lang, code in extract_code_blocks(output or ""):
        if lang not in PYTHON_LANGS:
            continue
        try:
            tree = ast.parse(code)
        except SyntaxError:
            continue
        for n in ast.walk(tree):
            mods = ([n.module] if isinstance(n, ast.ImportFrom) and n.level == 0 and n.module else
                    [a.name for a in n.names] if isinstance(n, ast.Import) else [])
            for m in mods:
                leaf = m.split(".")[-1]
                if leaf in planned and leaf not in written and m not in module_index():
                    msg = (f"`{m}`: that module is step {planned[leaf]}'s and is not written yet -- this step cannot "
                           "import it; use the modules that exist (listed above), or compute what it needs here")
                    if msg not in found:
                        found.append(msg)
    return found


def writes_a_siblings_module(output: str, row: dict, others: list[dict]) -> list[str]:
    """§17.1775 — an output whose code is headed with another step's module: it would overwrite that step's file."""
    theirs = {step_module(r): r.get("node_key") for r in others if step_module(r)}
    own = step_module({k: v for k, v in row.items() if k != "output_text"})
    found: list[str] = []
    for lang, code in extract_code_blocks(output or ""):
        m = _MODULE_OF_STEP_RE.match(code.lstrip().splitlines()[0]) if lang in PYTHON_LANGS and code.strip() else None
        if not m:
            continue
        leaf = m.group(2)
        if leaf in theirs and leaf != own and not re.match(r"(?:test_\w+|\w+_test|conftest)$", leaf):
            found.append(f"`# {m.group(1)}{leaf}.py`: that module is step {theirs[leaf]}'s; this step writes "
                         f"`{own}.py`" + (" -- import what it needs from it" if own else ""))
    return found


def replaces_an_engine_module(output: str, row: dict) -> list[str]:
    """§17.1762 — a step whose module has the name of an engine module it does not keep. Live: T12 wrote
    `kill_switch.py`; `app/modules/kill_switch.py` is the engine's VPN kill switch (§17.1670), imported by
    supervised_runs -- the lab's file would have replaced it."""
    path = step_module_path({**row, "output_text": output})
    index = module_index()
    if not path or path not in index:
        return []
    mod, hits = path.split(".")[-1], [path]
    existing = index[path]
    pub = {sig.split("(")[0].split()[-1] for sig in existing["public"]}
    if not pub:
        return []
    _, names = job_own_code([output])
    if len(pub & names) * 2 >= len(pub):
        return []                                       # the step extends the engine's module, keeping what it has
    return [f"`{mod}.py`: the engine already has `{hits[0]}` ({existing['doc'][:90] or 'in use'}); this step's file "
            f"would replace it. Name the new module for what it is (for example `paper_{mod}.py`), and import it "
            "under that name everywhere"]


async def job_code(db, job_id: str, *, except_node: str | None = None) -> tuple[set[str], set[str]]:
    """`job_own_code` over this job's steps, read from `dag_nodes`; a step's own last output is not its sibling."""
    from sqlalchemy import text
    rows = (await db.execute(text(
        "SELECT node_key, title, description, prompt_template, output_text FROM dag_nodes WHERE job_id = :j"),
        {"j": job_id})).mappings().all()
    texts = [" ".join(str(r[k] or "") for k in ("title", "description", "prompt_template")
                      + (() if r["node_key"] == except_node else ("output_text",)))
             for r in rows]
    return job_own_code(texts)


def _sibling(module: str, siblings: set[str]) -> bool:
    return module.split(".")[-1] in siblings


def _close_fields(attr: str, fields: dict[str, str]) -> list[str]:
    """The real fields a wrong name was reaching for: `SEARXNG_BASE_URL` → `searxng_url` (shared words, most first)."""
    # §17.1753 — five letters or more: on "edgar_user_agent" the word "user" offered `multi_user_enabled`
    want = {w for w in attr.lower().split("_") if len(w) >= 5}
    # §17.1754 — fields only: `settings_fields` also holds the class's own attributes, so `backtest_model_name` was
    # offered pydantic's `model_copy` and `model_dump` (job 2772acae T17)
    from app.config import settings
    real = type(settings).model_fields
    scored = sorted((-len(want & set(f.split("_"))), len(f), f) for f in fields
                    if f in real and want & set(f.split("_")))
    return [f for _, _, f in scored[:3]]


#: §17.1764 — what only the executor calls: code that parks or resolves a decision itself reads an answer nobody gave
_EXECUTOR_ONLY = frozenset({("app.modules.decision_pause", n)
                            for n in ("park_awaiting_decision", "resolve_decision", "pending_decision")})


def unresolved_refs(output: str, siblings: set[str] = frozenset(), sibling_names: set[str] = frozenset()) -> list[str]:
    """Every engine module, name, settings field or package the output reaches for that does not exist."""
    index = module_index()
    fields = settings_fields()
    found: list[str] = []

    def note(msg: str) -> None:
        if msg not in found:
            found.append(msg)

    for lang, code in extract_code_blocks(output or ""):
        if lang not in PYTHON_LANGS:
            continue
        try:
            tree = ast.parse(code)
        except SyntaxError:
            continue                                       # §17.428's gate owns a block that does not parse
        settings_aliases: set[str] = set()
        for n in ast.walk(tree):
            # §17.1754 — live (T14): approval drafted as `input("enter 'y' to approve")`; the engine is a server
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "input":
                note("`input(…)`: the engine is a server process with no terminal -- an operator answer comes through "
                     "the decision pause (`app.modules.decision_pause`), never a prompt that waits")
            if isinstance(n, ast.Import):
                for al in n.names:
                    mod = al.name
                    if mod.split(".")[0] == "app":
                        if mod not in index and not _sibling(mod, siblings):
                            note(f"`import {mod}`: the engine has no module `{mod}`")
                    elif mod.split(".")[0] in NOT_IN_THE_ENGINE:
                        note(f"`import {mod}`: {NOT_IN_THE_ENGINE[mod.split('.')[0]]}")
                    elif not _installed(mod.split(".")[0]) and not _sibling(mod, siblings):
                        note(f"`import {mod}`: `{mod.split('.')[0]}` is not installed in the engine")
            elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
                mod = n.module
                if mod.split(".")[0] == "app":
                    if mod not in index:
                        if not _sibling(mod, siblings):
                            note(f"`from {mod} import …`: the engine has no module `{mod}`")
                        continue
                    for al in n.names:
                        if al.name == "*":
                            continue
                        if (al.name not in index[mod]["names"] and f"{mod}.{al.name}" not in index
                                and al.name not in sibling_names and al.name not in siblings):
                            real = ", ".join(p.split("(")[0].split()[-1] for p in index[mod]["public"][:12])
                            note(f"`from {mod} import {al.name}`: `{mod}` has no `{al.name}`"
                                 + (f" (it has {real})" if real else ""))
                        if (mod, al.name) in _EXECUTOR_ONLY:
                            note(f"`from {mod} import {al.name}`: the executor's own half of the decision pause -- "
                                 "code asks the operator with `app.modules.operator_ask` (open_decision_job, "
                                 "answer_handler, answer_of)")
                        if mod == "app.config" and al.name == "settings":
                            settings_aliases.add(al.asname or "settings")
                elif mod.split(".")[0] in NOT_IN_THE_ENGINE:
                    note(f"`from {mod} import …`: {NOT_IN_THE_ENGINE[mod.split('.')[0]]}")
                elif not _installed(mod.split(".")[0]) and not _sibling(mod, siblings):
                    note(f"`from {mod} import …`: `{mod.split('.')[0]}` is not installed in the engine")
        # §17.1756 — calls to engine functions, by the name they were imported under
        engine_funcs: dict[str, tuple[str, str]] = {}
        engine_mods: dict[str, str] = {}
        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom) and n.level == 0 and n.module in index:
                for al in n.names:
                    if al.name in index[n.module].get("funcs", {}):
                        engine_funcs[al.asname or al.name] = (n.module, al.name)
                    elif f"{n.module}.{al.name}" in index:
                        engine_mods[al.asname or al.name] = f"{n.module}.{al.name}"
            elif isinstance(n, ast.Import):
                for al in n.names:
                    if al.asname and al.name in index:
                        engine_mods[al.asname] = al.name
        if engine_funcs or engine_mods:
            misused = _coroutines_used_as_values(tree, engine_funcs, engine_mods, index)
            consts = {n.targets[0].id: n.value for n in tree.body
                      if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name)
                      and isinstance(n.value, (ast.Constant, ast.Dict, ast.List, ast.JoinedStr))}
            for n in ast.walk(tree):
                if not isinstance(n, ast.Call):
                    continue
                target = None
                if isinstance(n.func, ast.Name) and n.func.id in engine_funcs:
                    target = engine_funcs[n.func.id]
                elif (isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name)
                      and n.func.value.id in engine_mods):
                    mod = engine_mods[n.func.value.id]
                    if n.func.attr in index[mod].get("funcs", {}):
                        target = (mod, n.func.attr)
                if target:
                    mod, fname = target
                    for m in _call_mismatches(n, fname, index[mod]["funcs"][fname], id(n) in misused, consts):
                        note(m)
                    for m in _domain_mismatches(n, mod, fname, index[mod]["funcs"][fname], consts):
                        note(m)
            for m in _tuples_used_whole(tree, engine_funcs, engine_mods, index):
                note(m)
        if not settings_aliases:
            continue
        for n in ast.walk(tree):
            attr = None
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id in settings_aliases:
                attr = n.attr
            elif (isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in ("getattr", "hasattr")
                  and len(n.args) >= 2 and isinstance(n.args[0], ast.Name) and n.args[0].id in settings_aliases
                  and isinstance(n.args[1], ast.Constant) and isinstance(n.args[1].value, str)):
                attr = n.args[1].value
            if attr and attr not in fields and attr not in sibling_names:
                close = _close_fields(attr, fields)
                note(f"`settings.{attr}`: the engine has no such setting"
                     + (f" (it has {', '.join('`' + c + '`' for c in close)})" if close else "")
                     + " -- a value the lab needs and the engine does not hold is a module constant (a fixed URL, a "
                       "limit); a value only the operator has (a contact address, an account) is read when the code "
                       "runs with `await values_for(db, [\"UPPER_SNAKE_NAME\"])` and the code refuses to run without it "
                       "(§17.1776); never `getattr(settings, …, default)`")
    return found


# ── §17.1760 a migration joins the engine's history; a value the step lacks is not a sample ─────────────────────
#
# Live (job 2772acae T2 "Define Postgres audit schema", 2026-10-22): the revision came back as `revision =
# "202410080001"`, `down_revision = None` and `APP_DB_ROLE = "scaffold_app"  # Replace with the actual role if it
# differs`, closing with "Set `down_revision` to your current Alembic head and `APP_DB_ROLE` to the engine's Postgres
# role before running". A second root makes `alembic upgrade head` fail on the engine's next start (two heads); the
# role does not exist; and both were handed to whoever reads the step. T3 shipped `research@example.com` as the SEC
# contact. Everything the step lacked the engine holds (its newest revision, its own database user), or only the
# operator has (a contact address) -- never a sample.

_REVISION_RE = re.compile(r"^\d{4}_[a-z0-9_]+$")
#: a step handing its own unfinished work to the reader: "replace with the actual role", "set X to your current head"
_HANDOFF_RE = re.compile(
    r"(?<!not )(?<!never )\b(?:replace|set|change|update|fill in)\b(?:\s+(?:this|it|`[^`\n]+`|[A-Za-z_][\w.]*)){0,2}"
    r"\s+(?:with|to)\s+(?:the\s+)?(?:actual|real|your)\b", re.I)


def revisions_in_code(texts: list[str]) -> set[str]:
    """The Alembic revision ids this job's own steps add."""
    out: set[str] = set()
    for t in texts:
        for lang, code in extract_code_blocks(t or ""):
            if lang not in PYTHON_LANGS:
                continue
            try:
                tree = ast.parse(code)
            except SyntaxError:
                continue
            for n in tree.body:
                if (isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name)
                        and n.targets[0].id == "revision" and isinstance(n.value, ast.Constant)):
                    out.add(str(n.value.value))
    return out


def _database_role() -> str:
    from app.config import settings
    m = re.match(r"^[a-z+]+://([^:@/]+)", str(getattr(settings, "database_url", "") or ""))
    return m.group(1) if m else ""


def migration_refs(output: str, sibling_revisions: set[str] = frozenset()) -> list[str]:
    """An Alembic revision that does not chain onto the engine's newest one (or one this job adds), or is not named
    the way the engine names its revisions."""
    head = _latest_migration()
    found: list[str] = []
    for lang, code in extract_code_blocks(output or ""):
        if lang not in PYTHON_LANGS:
            continue
        try:
            tree = ast.parse(code)
        except SyntaxError:
            continue
        top = {n.targets[0].id: n.value for n in tree.body
               if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name)}
        if "revision" not in top or "down_revision" not in top:
            continue
        rev = top["revision"].value if isinstance(top["revision"], ast.Constant) else None
        if rev and (ENGINE_PKG.parent / "alembic" / "versions" / f"{rev}.py").exists():
            continue                                       # a revision already in the engine's history, shown again
        down = top["down_revision"].value if isinstance(top["down_revision"], ast.Constant) else ...
        allowed = ({head} if head else set()) | (set(sibling_revisions) - {rev})
        if down is ... or down not in allowed:
            found.append(f"`down_revision = {down if down is ... else repr(down)}`: the engine's Alembic history ends at "
                         f"`{head}`; a new revision chains onto it (`down_revision = \"{head}\"`), or onto the revision "
                         "another step of this job adds -- a second root gives the engine two heads and "
                         "`alembic upgrade head` fails on its next start")
        if not (isinstance(rev, str) and _REVISION_RE.match(rev)):
            nxt = f"{int(head[:4]) + 1:04d}_<snake_name>" if head[:4].isdigit() else "NNNN_<snake_name>"
            found.append(f"`revision = {rev!r}`: the engine's revisions are named for their file, `{nxt}` "
                         f"(the file `alembic/versions/{nxt}.py`)")
        role = _database_role()
        for s_ in _string_literals(tree):
            m = re.search(r"\b(?:GRANT|REVOKE)\b[^;]*?\bON\b[^;]*?\b(?:TO|FROM)\s+\"?([A-Za-z_][A-Za-z0-9_]*)\"?(?![{\w])",
                          s_, re.I)
            if m and role and m.group(1).lower() not in (role.lower(), "public"):
                found.append(f"`{m.group(0)[:80]}`: the engine connects to its database as `{role}`; there is no role "
                             f"`{m.group(1)}`")
                break
    return found


def _string_literals(tree: ast.AST) -> list[str]:
    return [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def _prose_and_comments(output: str) -> str:
    """What the step says to its reader: the text around its code, and its code's comments."""
    parts: list[str] = [re.sub(r"```[\s\S]*?```", "\n", output or "")]
    for lang, code in extract_code_blocks(output or ""):
        if lang in PYTHON_LANGS:
            parts.extend(m.group(1) for m in re.finditer(r"#\s?(.*)$", code, re.M))
    return "\n".join(parts)


def placeholder_refs(output: str) -> list[str]:
    """A sample value, or a note handing the step's unfinished work to its reader, in code written for the engine.
    Test code may use sample data."""
    from app.modules.supervised_runs import _PLACEHOLDER_VALUE_RE
    found: list[str] = []
    for lang, code in extract_code_blocks(output or ""):
        if lang not in PYTHON_LANGS:
            continue
        first = code.lstrip().splitlines()[0] if code.strip() else ""
        if re.search(r"\btests?/|\btest_\w+\.py|conftest\.py", first) or re.search(r"^\s*def test_\w+", code, re.M):
            continue
        try:
            values = _string_literals(ast.parse(code))
        except SyntaxError:
            continue
        m = next((x for x in (_PLACEHOLDER_VALUE_RE.search(v) for v in values) if x), None)
        if m:
            line = m.string.strip().splitlines()[0] if m.string.strip() else m.group(0)
            found.append(f"`{line[:120]}`: `{m.group(0)}` is a sample, not a value. A value only the operator has "
                         "(a contact address, an account) is read when the code runs with "
                         "`await values_for(db, [\"UPPER_SNAKE_NAME\"])` (app.modules.runner_secrets -- the operator "
                         "stores it on the Machines page → Secrets), and the code refuses to run while it is missing "
                         "(§17.1776); a test mocks `values_for`")
    said = _prose_and_comments(output)
    for m in _HANDOFF_RE.finditer(said):
        line = said[said.rfind("\n", 0, m.start()) + 1:].split("\n", 1)[0].strip()
        found.append(f"`{line[:140]}`: the step hands its own unfinished work to the reader. What it lacked the engine "
                     "holds (its newest revision, its database user, its settings) and is shown above; what only the "
                     "operator has is read with `values_for(db, [\"UPPER_SNAKE_NAME\"])` and the code refuses to run "
                     "without it (§17.1776)")
        break
    return found


async def engine_findings(db, job_id: str, node_key: str, output: str) -> list[str]:
    """Every §17.1749–1760 refusal for one CodeGen output written for the engine."""
    from sqlalchemy import text

    from app.modules import engine_schema
    rows = (await db.execute(text(
        "SELECT node_key, title, description, prompt_template, output_text FROM dag_nodes WHERE job_id = :j"),
        {"j": job_id})).mappings().all()
    others = [str(r["output_text"] or "") for r in rows if r["node_key"] != node_key]
    texts = [" ".join(str(r[k] or "") for k in ("title", "description", "prompt_template")
                      + (() if r["node_key"] == node_key else ("output_text",)))
             for r in rows]
    mods, names = job_own_code(texts)
    found = unresolved_refs(output, mods, names)
    me = next((dict(r) for r in rows if r["node_key"] == node_key), {"node_key": node_key})
    _others = [dict(r) for r in rows if r["node_key"] != node_key]
    found += sibling_refs(output, sibling_definitions(_others))
    found += imports_an_unwritten_sibling(output, _others, sibling_definitions(_others))
    found += replaces_an_engine_module(output, me)
    found += writes_a_siblings_module(output, me, [dict(r) for r in rows if r["node_key"] != node_key])
    tables = dict(await engine_schema.live_tables(db))
    jobs_own = engine_schema.tables_in_code(others)
    for k, v in jobs_own.items():
        have = tables.get(k)
        tables[k] = v if have is None or v is None else have | v
    found += engine_schema.unknown_schema_refs(output, tables, set(jobs_own),
                                               engine_schema.foreign_keys_in_code(others))
    found += migration_refs(output, revisions_in_code(others))
    found += placeholder_refs(output)
    found += answers_read_where_asked(output)
    return found


def answers_read_where_asked(output: str) -> list[str]:
    """§17.1784 — an answer read in the same function that just opened the question. Live (job f00ea97f T16):
    `job_id = await open_decision_job(…)` then `answer = await answer_of(db, job_id, "approve_proposal")` on the next
    line -- None every time (nobody has answered), so every proposal came back "rejected"; the @answer_handler, where
    the answer actually arrives, only logged. The first lab's T14 did the same with the decision pause."""
    found: list[str] = []
    for lang, code in extract_code_blocks(output or ""):
        if lang not in PYTHON_LANGS:
            continue
        try:
            tree = ast.parse(code)
        except SyntaxError:
            continue
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            opened: set[str] = set()
            for n in ast.walk(fn):
                if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
                    call = n.value.value if isinstance(n.value, ast.Await) else n.value
                    if isinstance(call, ast.Call) and getattr(call.func, "id", getattr(call.func, "attr", "")) == "open_decision_job":
                        opened.add(n.targets[0].id)
            if not opened:
                continue
            for n in ast.walk(fn):
                if (isinstance(n, ast.Call) and getattr(n.func, "id", getattr(n.func, "attr", "")) == "answer_of"
                        and any(isinstance(a, ast.Name) and a.id in opened for a in n.args)):
                    found.append(f"`{fn.name}` reads `answer_of(…)` for the decision job it has just opened: nobody "
                                 "has answered yet, so it is always None. Return once the decisions are open; act on "
                                 "the answer in the `@answer_handler` (`on_answer=`), which the engine calls when the "
                                 "operator answers -- that is where an approved order is executed")
                    break
    return found


async def adopt_imported_siblings(db, job_id: str, node_key: str, output: str) -> list[str]:
    """§17.1788 — a step that imports another step's module stands on that step. Live (job f00ea97f T20 "walk-forward
    backtester", ~20 attempts): it used T21's `paper_spy_benchmark_tracker` (prices, the SPY benchmark) and T22's
    metrics, but stood on neither -- so neither the step nor its verifier was ever shown them (a verdict said the SPY
    module "is not among the provided upstream blocks"), and T22 was not even written. The dependency is the step's own
    code's; it is added (never one that would close a cycle), so the next attempt -- and its verifier -- see the module,
    and a step waiting on an unwritten one waits for it. Returns the node keys added."""
    from sqlalchemy import text as _t
    rows = [dict(r) for r in (await db.execute(_t(
        "SELECT node_key, title, description, prompt_template, output_text, depends_on FROM dag_nodes "
        "WHERE job_id = :j"), {"j": job_id})).mappings().all()]
    me = next((r for r in rows if r["node_key"] == node_key), None)
    if not me:
        return []
    owner = {step_module(r): r["node_key"] for r in rows if r["node_key"] != node_key and step_module(r)}
    deps = {r["node_key"]: [str(d) for d in (r["depends_on"] or [])] for r in rows}
    wanted: list[str] = []
    for lang, code in extract_code_blocks(output or ""):
        if lang not in PYTHON_LANGS:
            continue
        try:
            tree = ast.parse(code)
        except SyntaxError:
            continue
        for n in ast.walk(tree):
            mods = ([n.module] if isinstance(n, ast.ImportFrom) and n.level == 0 and n.module else
                    [a.name for a in n.names] if isinstance(n, ast.Import) else [])
            for m in mods:
                k = owner.get(m.split(".")[-1])
                if k and m not in module_index() and k not in wanted:
                    wanted.append(k)

    def upstream(key: str) -> set[str]:
        seen: set[str] = set()
        todo = list(deps.get(key, []))
        while todo:
            x = todo.pop()
            if x not in seen:
                seen.add(x)
                todo.extend(deps.get(x, []))
        return seen

    mine = upstream(node_key)
    add = [k for k in wanted if k not in mine and node_key not in upstream(k)]
    if not add:
        return []
    await db.execute(_t("UPDATE dag_nodes SET depends_on = depends_on || CAST(:add AS text[]), updated_at = NOW() "
                        "WHERE job_id = :j AND node_key = :k"), {"add": add, "j": job_id, "k": node_key})
    await db.commit()
    return add


def unresolved_reason(findings: list[str]) -> str:
    """The refusal the next attempt is drafted from (W.1 retry loop)."""
    return ("§17.1749 this step writes code for the engine itself, and it reaches for things the engine does not have:\n"
            + "\n".join(f"  - {f}" for f in findings)
            + "\nUse the modules, functions and settings listed in \"The engine's own code\" above, or a module this "
              "job's own steps write. Do not guess a name, and do not hide a guessed import behind try/except.")


# ── the block a CodeGen step is shown ────────────────────────────────────────

_STOP = frozenset({
    "implement", "python", "module", "with", "that", "this", "from", "into", "each", "every", "should", "must",
    "returns", "return", "function", "functions", "code", "step", "engine", "engine's", "scaffold", "using", "use",
    "the", "and", "for", "data", "when", "then", "only", "never", "make", "write", "build", "create", "add",
})

#: what every engine change touches, whatever its subject
_CORE = ("app.config", "app.database", "app.modules.runner_secrets", "app.utils.http_clients", "app.model_router")

CONVENTIONS = (
    "- Package `app`. Settings: `from app.config import settings`; field names are lower-case (listed below).\n"
    "- Database: async SQLAlchemy -- `from app.database import async_session` and `from sqlalchemy import text`; "
    "`async with async_session() as db: await db.execute(text(...), {...})`. A new table is an Alembic revision "
    "under `alembic/versions/`, never `CREATE TABLE` at runtime, and never psycopg2.\n"
    "- I/O is async: `httpx.AsyncClient` (not `requests`), `websockets` (not `websocket`); a blocking library runs "
    "in `run_in_executor`.\n"
    "- Logging: `logger = logging.getLogger(\"scaffold\")` (stdlib logging, not print, not structlog).\n"
    "- Secrets: held by the engine, read with `app.modules.runner_secrets.values_for(db, [NAMES])`; never logged, "
    "never put in a prompt, never read from the environment.\n"
    "- A value the code must not change (a fixed URL) is a module constant, not an environment lookup.\n"
    "- A value only the operator has (a contact e-mail an API requires, an account id) is read when the code runs "
    "with `await values_for(db, [\"UPPER_SNAKE_NAME\"])`; the operator stores it on the Machines page → Secrets; the "
    "code refuses to run while it is missing, and never ships a sample (`…@example.com`).\n"
    "- A new module never takes the name of an engine module listed in the layout below; name it for what it is.\n"
    "- The step is RUN before it passes (§17.1765): its tests, or an import of its module, against the engine with "
    "this job's finished modules, in a sandbox with no network and no secrets. `async_session` there is a fresh "
    "scratch database with the engine's schema and this job's migrations (§17.1783): test SQL against it (insert "
    "rows, call the code, read the result) rather than a fake session that ignores the query; mock HTTP clients and "
    "model calls. The tests must pass there.")


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z][a-z0-9]{3,}", (text or "").lower()) if w not in _STOP}


def _render_module(mod: str, max_defs: int = 14) -> str:
    info = module_index().get(mod)
    if not info:
        return ""
    defs = info["public"][:max_defs]
    more = len(info["public"]) - len(defs)
    vals = info.get("values") or []
    return (f"`{mod}` -- {info['doc'] or '(no docstring)'}\n"
            + (f"    names: {', '.join(vals[:12])}{' …' if len(vals) > 12 else ''}\n" if vals else "")
            + "".join(f"    {d}\n" for d in defs)
            + (f"    … {more} more\n" if more > 0 else ""))


# ── §17.1754 the engine's capabilities, named ────────────────────────────────
#
# Live (job 2772acae T14 "Implement approve-before-run step", brief: "reusing the engine's existing approve-before-run
# pattern"): refused (§17.1749) for guessing `app.modules.runner_approve`, the next draft fell back to
# `input("enter 'y' to approve")` -- a terminal prompt in a server process -- and passed. Nothing told it where approval
# lives, and ranking modules by shared words had shown the VPN `kill_switch` (dropped, §17.1749). So: a curated list.
# A step whose text names a capability is shown what implements it, with signatures read from the source; a name here
# that stops existing fails tests/test_codegen_for_the_engine_reads_the_engine.py, so the list cannot rot silently.

CAPABILITIES: tuple[dict, ...] = (
    {"name": "operator approval (approve-before-run)",
     "when": r"approv|approve-before-run|sign[- ]?off|operator (?:decides|decision|confirms)|human[- ]in[- ]the[- ]loop",
     # §17.1764 — live (T14): the code parked the decision itself and read the answer on the next line, before
     # anyone could give one; code asks through `operator_ask`, which opens a job the executor parks on
     "how": "Code asks the operator through `app.modules.operator_ask` -- never input(), a loop that waits, or "
            "decision_pause directly (that is the executor's). Build one `decision(key, question, detail, "
            "payload={...})` per item (options default to Approve / Reject), then `job_id = await "
            "open_decision_job(db, title=…, decisions=[…], on_answer=\"app.modules.<module>:<fn>\")`; it returns at "
            "once. The operator answers each in the UI. `fn` is `@answer_handler async def fn(job_id, node_key, "
            "answer, payload)`, called as each answer is recorded, with `answer = {\"choice\", \"note\", "
            "\"approved\"}`: THE HANDLER IS WHERE THE APPROVED WORK HAPPENS (an approved order is placed there, "
            "with what it needs carried in `payload`); act only when `answer[\"approved\"]` is True. The function "
            "that opens the decisions returns at once and never reads the answer itself (§17.1784: it does not exist "
            "yet). `await answer_of(db, job_id, key)` is for a later, separate read (None while open).",
     "names": {"app.modules.operator_ask": ["decision", "open_decision_job", "answer_handler", "answer_of"]}},
    {"name": "secrets the engine holds",
     "when": r"secret|api[ _-]?key|credential|broker key|password|token",
     "how": "Keys are held encrypted by the engine; `values_for(db, [NAMES])` is the only plaintext path. Never log a "
            "value, put it in a prompt or an output, or read it from the environment. Names are UPPER_SNAKE.",
     "names": {"app.modules.runner_secrets": ["values_for", "set_secret", "names"]}},
    {"name": "web search and news (SearXNG)",
     "when": r"searxng|\bnews\b|web search|search the web|headlines",
     "how": "`search_searxng(query)` runs one query through the engine's SearXNG with its relevance gate and returns "
            "result dicts; `get_searxng_client()` is the shared async client for anything else.",
     "names": {"app.modules.gt_extractor": ["search_searxng", "quick_research"],
               "app.utils.http_clients": ["get_searxng_client"]}},
    {"name": "model calls",
     "when": r"\bllm\b|language model|model_router|propos(?:e|al)|rationale|summari[sz]",
     "how": "Call by ROLE (`role=\"model_general\"`, `\"model_coder\"`, …), never a model name; `role` and `model` "
            "cannot both be passed. `generate_json` gives grammar-constrained JSON and returns a tuple: "
            "`parsed, resp = await generate_json(prompt, schema, role=…)` (`parsed` is None on failure); a JSON "
            "array needs `as_array=True`.",
     "names": {"app.model_router": ["generate", "generate_json", "tool_call"]}},
    {"name": "alerts to the operator",
     "when": r"alert|notif|kill[- ]?switch|\bhalt|warn the operator",
     "how": "`emit(kind=…, severity=…, message=…)` reaches every alert sink, honours a per-kind cooldown and never "
            "raises; omit `db` (a passed session is committed).",
     "names": {"app.observability.alerts": ["emit"]}},
    {"name": "recurring work (a schedule)",
     "when": r"schedul|\bcron\b|daily|every day|before (?:the )?market open|recurring|periodic",
     "how": "`get_scheduler()` is the engine's APScheduler (None when scheduling is off). Register with "
            "`add_job(fn, trigger=CronTrigger.from_crontab(...), id=\"<lab_…>\", jobstore=\"memory\", "
            "replace_existing=True, misfire_grace_time=settings.scheduler_misfire_grace_time)`; never an id starting "
            "`schedule_` (reconciled away). There is no general cron API; the hook is added where the engine starts.",
     "names": {"app.scheduler": ["get_scheduler"]}},
    {"name": "tables (a migration)",
     "when": r"schema|\btable|migration|alembic|audit (?:trail|log)",
     "how": "A new table is an Alembic revision in `alembic/versions/NNNN_snake_name.py` (revision = the file stem, "
            "down_revision = the newest revision below), using `op.execute(\"… IF NOT EXISTS …\")`; code reads and "
            "writes with `async_session` + `text`, naming only the columns the migration creates. The engine "
            "connects as the tables' owner, so GRANT/REVOKE cannot make a table append-only: a trigger that raises "
            "on UPDATE and DELETE does. An `sa.Column` object belongs to one table (build shared columns in a "
            "function, never a shared list).",
     "names": {"app.database": []}},
    {"name": "an API route or operator page",
     "when": r"dashboard|operator view|\bui\b|\bpage\b|endpoint|api route|\brouter\b",
     "how": "A route is `router = APIRouter()` in `app/routers/<name>.py`, imported and `include_router`-ed in "
            "app/main.py, guarded by `principal = Depends(get_principal)` + `assert_visible`. A page is a vanilla ES "
            "module `app/ui/static/views/<name>.js` exporting `default function name(container, params, opts)`, "
            "routed in app/ui/static/app.js and listed in nav.js. Run `make openapi-snapshot` after a route change.",
     "names": {"app.authz": ["get_principal", "assert_visible"]}},
)


def _latest_migration() -> str:
    versions = ENGINE_PKG.parent / "alembic" / "versions"
    try:
        files = sorted(p.stem for p in versions.glob("[0-9][0-9][0-9][0-9]_*.py"))
    except OSError:
        return ""
    return files[-1] if files else ""


def capabilities_for(node: dict) -> list[dict]:
    """The capabilities this step's own text names."""
    step = " ".join(str(node.get(k) or "") for k in ("title", "description", "prompt_template"))
    return [c for c in CAPABILITIES if re.search(c["when"], step, re.I)]


def _signature_of(mod: str, name: str) -> str:
    info = module_index().get(mod) or {}
    for sig in info.get("public", []):
        if sig.split("(")[0].split()[-1] == name:
            return sig
    return name if name in info.get("names", ()) else ""


def _render_capability(cap: dict) -> str:
    lines = [f"**{cap['name']}** -- {cap['how']}"]
    if cap["name"].startswith("tables"):
        last = _latest_migration()
        if last:
            lines.append(f"    newest revision: `{last}` (the next one's down_revision)")
    for mod, names in cap["names"].items():
        for n in names:
            sig = _signature_of(mod, n)
            if sig:
                lines.append(f"    `{mod}`: {sig}")
    return "\n".join(lines) + "\n"


def matching_settings(want: set[str], rare: int = 3) -> list[str]:
    """The settings fields a step's TITLE names: a word of six letters or more that at most `rare` fields have
    ("reddit" → `reddit_max_posts` …). Matched on the whole step text, or on common words ("order", "stats", "check"),
    every step of job 2772acae was offered unrelated knobs (`node_escalation_order` for "Implement order executor");
    a step that reaches for a setting it was not shown is told the real ones by `unresolved_refs`."""
    from app.config import settings
    fields = list(type(settings).model_fields)
    df: dict[str, int] = {}
    for f in fields:
        for w in set(f.split("_")):
            df[w] = df.get(w, 0) + 1
    return [f for f in fields if any(len(w) >= 6 and df[w] <= rare and w in want for w in f.split("_"))][:20]


def _render_job_tables(tables: dict, fks: dict | None = None, rules: dict | None = None) -> str:
    """§17.1786 — the job's own tables, exactly as its migration makes them. Live (job f00ea97f T13, T20): the plan's
    text said "filter on publication_time" while the migration made `published_ts`; each attempt followed the text and
    §17.1782 refused it."""
    fks = fks or {}
    rules = rules or {}

    def _rules(t: str) -> str:
        r = rules.get(t) or {}
        bits = ([f"required: {', '.join(r['required'])}"] if r.get("required") else []) + \
               ([f"UNIQUE {u}" for u in r.get("unique", [])]) + \
               ([f"JSON: {', '.join(r['json'])}"] if r.get("json") else []) + \
               ([f"CHECK {c}" for c in r.get("checks", [])])
        return ("\n        " + "; ".join(bits)) if bits else ""
    rows = [f"    {t}: " + ", ".join(f"{col}→{fks[t][col]}" if col in fks.get(t, {}) else col for col in sorted(c))
            + _rules(t) for t, c in sorted(tables.items()) if c]
    if not rows:
        return ""
    return ("### This job's tables (from its migration -- exact names; where this step's text names a table or column "
            "differently, these win)\n" + "\n".join(rows) + "\n"
            + ("    (`col→table.id` is a foreign key: insert the parent row first -- in tests too, the scratch database "
               "enforces it)\n" if any(fks.values()) else "")
            + ("    (a JSON/JSONB column takes `json.dumps(value)` with `CAST(:p AS json)` / `CAST(:p AS jsonb)` -- its own "
               "type -- through `text()`, never a dict; a CHECK lists the only values a row may hold -- tests use those "
               "values; a `required` column has no default, so every INSERT gives it; the tests of one run share one "
               "scratch database, so each test makes its own UNIQUE values, e.g. `uuid4().hex`)\n" if rules else ""))


def _render_job_modules(defs: dict[str, dict], own: str, budget: int = 6000) -> str:
    """§17.1796 — the public functions of every module this job has written, with exact signatures, one line each.
    Live (job f00ea97f T25, the integration step over ~20 modules): attempt after attempt guessed
    `ingest_congressional_trades`, `fetch_13f_holdings`, `generate_signals(as_of=…)`; the refusal's "it has" list was
    16 names sorted alphabetically -- in a module of constants, only constants."""
    lines: list[str] = []
    for mod in sorted(defs):
        if mod == own:
            continue
        sigs = [_shape_signature(f, sh) for f, sh in sorted(defs[mod]["funcs"].items()) if not f.startswith("_")]
        for cname, c in sorted((defs[mod].get("classes") or {}).items()):     # §17.1798
            if cname.startswith("_") or cname.endswith(("Error", "Exception")):
                continue
            if c["methods"]:
                ms = ", ".join(_shape_signature(m, sh) for m, sh in sorted(c["methods"].items()))
                sigs.append(f"class {_shape_signature(cname, c['init']) if c['init'] else cname + '()'} "
                            f"{{{ms}}}")
            elif c["fields"]:
                sigs.append(f"class {cname} {{fields: {', '.join(c['fields'])}}}")
        if sigs:
            lines.append(f"    {mod}: " + "; ".join(sigs))
    if not lines:
        return ""
    out = "### This job's modules (written -- their public functions, exact signatures)\n" + "\n".join(lines) + "\n"
    return out if len(out) <= budget else out[:budget].rsplit("\n", 1)[0] + "\n    …\n"


_REQUIRED_TESTS_RE = re.compile(r"\b(tests/test_\w+\.py)\b")


def missing_test_file(output: str, step_text: str) -> str | None:
    """§17.1801 — the test file this step's text requires and its output left out (no code block starts with its
    path), or None. Live (job f00ea97f, the entry-point steps of §17.1799): half the attempts dropped the required
    `tests/test_<module>.py` -- in a small step too -- and each drop cost a whole attempt and a verifier call."""
    m = _REQUIRED_TESTS_RE.search(step_text or "")
    if not m:
        return None
    path = m.group(1)
    for _lang, code in extract_code_blocks(output or ""):
        first = code.lstrip().splitlines()[0] if code.strip() else ""
        if path in first or (path.rsplit("/", 1)[-1] in first and first.lstrip().startswith("#")):
            return None
    return path


def block_for(node: dict, siblings: set[str] = frozenset(), budget: int = 15000, tables: dict | None = None,
              written: set[str] | None = None, fks: dict | None = None, rules: dict | None = None,
              modules: dict[str, dict] | None = None) -> str:
    """What a CodeGen step that extends the engine is shown of the engine (attached after the optimizer, §17.1042)."""
    index = module_index()
    layout: dict[str, list[str]] = {}
    for mod in index:
        head, _, leaf = mod.rpartition(".")
        if head:
            layout.setdefault(head, []).append(leaf)
    hit_fields = matching_settings(_words(str(node.get("title") or "")))
    # §17.1775 — which file is THIS step's. Live (job f00ea97f): T6's second attempt wrote T4's
    # `paper_audit_trail_writer.py`, and T9's third imported its own `_extract_tickers` from T5's module -- the step
    # was shown its siblings' code and never told which module was its own.
    own = step_module(node)
    others = sorted(set(siblings) - {own})
    # §17.1787 — only a module that is WRITTEN can be imported; when the caller knows which are, say so
    ready = sorted(set(others) & set(written)) if written is not None else others
    later = sorted(set(others) - set(written)) if written is not None else []
    mine = ""
    if own:
        mine = (f"\n**This step writes `app/modules/{own}.py`** (its first line `# app/modules/{own}.py`), and its "
                f"tests in `tests/test_{own}.py`. "
                + (f"The other modules -- {', '.join(f'`{m}.py`' for m in ready[:24])} -- belong to other steps: "
                   "import what they define, never write them or define their names here. " if ready else "")
                + (f"Not written yet, so NOT importable here: {', '.join(f'`{m}.py`' for m in later[:24])}. "
                   if later else "") + "\n")
    parts = [
        "\n\n## The engine's own code (read from the running engine, §17.1749)\n" + mine +
        "This step writes code that becomes part of the scaffold engine. Import only the engine modules and names "
        "listed here (they exist), packages the engine has installed, or the modules this job's own steps write"
        + (f" ({', '.join(sorted(siblings)[:20])})" if siblings else "") + ". A name not listed here does not "
        "exist: do not guess one, and do not hide a guess behind try/except.\n",
        "### Conventions\n" + CONVENTIONS + "\n",
        "### Modules every change uses\n" + "".join(_render_module(m) for m in _CORE),
    ]
    if modules:
        jm = _render_job_modules(modules, own)
        if jm:
            parts.insert(1, jm)
    if tables:
        jt = _render_job_tables(tables, fks, rules)
        if jt:
            parts.insert(1, jt)              # near the top: the block is cut at `budget`, the schema must survive
    caps = capabilities_for(node)
    if caps:
        parts.append("### The engine's own way to do what this step names\n" + "".join(_render_capability(c) for c in caps))
    if hit_fields:
        parts.append("### Settings fields this step may read (`settings.<name>`)\n"
                     + "".join(f"    {f}" + (f" = {settings_fields()[f]}" if settings_fields()[f] not in ("", "PydanticUndefined") else "") + "\n"
                               for f in hit_fields))
    parts.append("### Package layout\n" + "".join(
        f"    {head}: {', '.join(sorted(leaves)[:60])}{' …' if len(leaves) > 60 else ''}\n"
        for head, leaves in sorted(layout.items())))
    out = "\n".join(parts)
    return out if len(out) <= budget else out[:budget].rsplit("\n", 1)[0] + "\n    …\n"
