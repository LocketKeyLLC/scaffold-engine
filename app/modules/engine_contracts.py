"""§17.1833 — the keys one finished module hands another.

A step that wires two finished modules together -- ``signals = await load_batch_signals(...)`` then
``propose_trades(signals, ...)`` -- can only work when the dicts the first returns carry what the second requires. Live
(follow-up 5b4880c0 T6, 2026-10-26): f00ea97f's ``generate_signals`` returns ``{source, ticker, signal_type, direction,
rationale, published_ts}`` -- never saved, no id -- and ``propose_trades`` skips every signal without ``signal_id`` (or
``id``). Nothing in the chain writes a signal row, so every batch proposes nothing, and T6 failed three rounds of
attempts at a gap no code of its own can close.

Read statically, and only claimed when certain: a key is REQUIRED when the consumer subscripts it, or reads it with
``.get`` (alternatives joined by ``or``) and gives up on that item -- ``continue`` / ``raise`` / ``return`` -- when it is
missing. A producer's keys are the union over every return path of the dict literals it builds (through local helpers,
other finished modules, list comprehensions and lists built by ``append``, and later ``d["k"] = ...``). Any path the
reader cannot follow makes the producer unknown, and an unknown producer is never reported.
"""
from __future__ import annotations

import ast
import re

_MAX_DEPTH = 6


def _defs(code: str) -> tuple[dict[str, ast.AST], dict[str, tuple[str, str]]] | None:
    """A module's top-level functions, and the names it imports from `app.modules.*` (name -> (module, name))."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None
    funcs: dict[str, ast.AST] = {}
    imports: dict[str, tuple[str, str]] = {}
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            funcs[n.name] = n
        elif isinstance(n, ast.ImportFrom) and (n.module or "").startswith("app.modules."):
            for a in n.names:
                imports[a.asname or a.name] = (n.module.rsplit(".", 1)[1], a.name)
    return funcs, imports


def _unwrap(e: ast.AST) -> ast.AST:
    return e.value if isinstance(e, ast.Await) else e


def _get_key(e: ast.AST, names: set[str]) -> str | None:
    """`x.get("k")` / `x.get("k", d)` on one of `names` -> "k"."""
    if (isinstance(e, ast.Call) and isinstance(e.func, ast.Attribute) and e.func.attr == "get"
            and isinstance(e.func.value, ast.Name) and e.func.value.id in names and e.args
            and isinstance(e.args[0], ast.Constant) and isinstance(e.args[0].value, str)):
        return e.args[0].value
    return None


def _gives_up(body: list[ast.stmt]) -> bool:
    return any(isinstance(s, (ast.Continue, ast.Raise, ast.Return)) for s in body)


def required_keys(fn: ast.AST, param: str) -> list[frozenset[str]]:
    """What a function requires of the dicts in (or of) its parameter `param`: each entry a set of alternatives,
    any one of which satisfies it."""
    items = {param}
    for n in ast.walk(fn):                                   # `for s in signals:` -- each s is one of its dicts
        if isinstance(n, (ast.For, ast.AsyncFor)) and isinstance(n.iter, ast.Name) and n.iter.id == param \
                and isinstance(n.target, ast.Name):
            items.add(n.target.id)
        elif isinstance(n, ast.comprehension) and isinstance(n.iter, ast.Name) and n.iter.id == param \
                and isinstance(n.target, ast.Name):
            items.add(n.target.id)
    out: list[frozenset[str]] = []
    for n in ast.walk(fn):
        if (isinstance(n, ast.Subscript) and isinstance(n.ctx, ast.Load) and isinstance(n.value, ast.Name)
                and n.value.id in items and isinstance(n.slice, ast.Constant) and isinstance(n.slice.value, str)):
            out.append(frozenset({n.slice.value}))
    # `v = s.get("a") or s.get("b")` then `if v is None: continue` (or `if not v:`)
    gets: dict[str, frozenset[str]] = {}
    for n in ast.walk(fn):
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
            parts = n.value.values if isinstance(n.value, ast.BoolOp) and isinstance(n.value.op, ast.Or) \
                else [n.value]
            keys = [_get_key(p, items) for p in parts]
            if keys and all(keys):
                gets[n.targets[0].id] = frozenset(k for k in keys if k)
    for n in ast.walk(fn):
        if not isinstance(n, ast.If) or not _gives_up(n.body):
            continue
        t = n.test
        name = None
        if isinstance(t, ast.Compare) and isinstance(t.left, ast.Name) and len(t.ops) == 1 \
                and isinstance(t.ops[0], ast.Is) and isinstance(t.comparators[0], ast.Constant) \
                and t.comparators[0].value is None:
            name = t.left.id
        elif isinstance(t, ast.UnaryOp) and isinstance(t.op, ast.Not) and isinstance(t.operand, ast.Name):
            name = t.operand.id
        if name in gets:
            out.append(gets[name])
    return list(dict.fromkeys(out))


class _Reader:
    """The keys a producer's returned dicts can carry, followed across the job's finished modules."""

    def __init__(self, modules: dict[str, str]):
        self.parsed = {m: d for m, c in modules.items() if (d := _defs(c))}
        self.origins: set[str] = set()                       # the modules whose dict literals were reached

    def returns(self, mod: str, fname: str, depth: int = 0) -> frozenset[str] | None:
        if depth > _MAX_DEPTH or mod not in self.parsed:
            return None
        funcs, imports = self.parsed[mod]
        if fname in imports and fname not in funcs:
            return self.returns(*imports[fname], depth=depth + 1)
        fn = funcs.get(fname)
        if fn is None:
            return None
        keys: set[str] = set()
        rets = [n for n in _own_nodes(fn) if isinstance(n, ast.Return) and n.value is not None]   # not a nested def's
        for r in rets:
            got = self._expr(mod, fn, r.value, depth)
            if got is None:
                return None
            keys |= got
        return frozenset(keys)

    def _assigned(self, fn: ast.AST, name: str) -> tuple[list[ast.AST], set[str], list[ast.AST]]:
        """What `name` is assigned in `fn`, the keys later stored into it, and what is appended to it."""
        vals: list[ast.AST] = []
        stored: set[str] = set()
        appended: list[ast.AST] = []
        for n in ast.walk(fn):
            if isinstance(n, ast.Assign):
                for t in n.targets:
                    if isinstance(t, ast.Name) and t.id == name:
                        vals.append(n.value)
                    elif (isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name) and t.value.id == name
                          and isinstance(t.slice, ast.Constant) and isinstance(t.slice.value, str)):
                        stored.add(t.slice.value)
            elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and n.target.id == name and n.value:
                vals.append(n.value)
            elif (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "append"
                  and isinstance(n.func.value, ast.Name) and n.func.value.id == name and len(n.args) == 1):
                appended.append(n.args[0])
        return vals, stored, appended

    def _expr(self, mod: str, fn: ast.AST, e: ast.AST, depth: int, index: int | None = None) -> frozenset[str] | None:
        e = _unwrap(e)
        if isinstance(e, ast.Constant) and e.value is None:
            return frozenset()
        if isinstance(e, ast.Dict):
            if any(k is None or not (isinstance(k, ast.Constant) and isinstance(k.value, str)) for k in e.keys):
                return None                                  # `**spread` or a computed key: not readable
            self.origins.add(mod)
            return frozenset(k.value for k in e.keys)        # type: ignore[union-attr]
        if isinstance(e, ast.Tuple) and index is not None:
            return self._expr(mod, fn, e.elts[index], depth) if index < len(e.elts) else None
        if isinstance(e, (ast.List, ast.Tuple)) and index is None:
            out: set[str] = set()
            for x in e.elts:
                got = self._expr(mod, fn, x, depth)
                if got is None:
                    return None
                out |= got
            return frozenset(out)
        if isinstance(e, ast.ListComp):
            elt, gen = e.elt, e.generators[0]
            if (isinstance(elt, ast.Subscript) and isinstance(elt.value, ast.Name) and isinstance(gen.target, ast.Name)
                    and elt.value.id == gen.target.id and isinstance(elt.slice, ast.Constant)
                    and isinstance(elt.slice.value, int) and isinstance(gen.iter, ast.Name)):
                return self._name(mod, fn, gen.iter.id, depth, index=elt.slice.value)   # [e[2] for e in weighted]
            if isinstance(elt, ast.Name) and isinstance(gen.target, ast.Name) and elt.id == gen.target.id \
                    and isinstance(gen.iter, ast.Name):
                return self._name(mod, fn, gen.iter.id, depth)                         # [s for s in out]
            return self._expr(mod, fn, elt, depth)
        if isinstance(e, ast.Name):
            return self._name(mod, fn, e.id, depth, index=index)
        if isinstance(e, ast.Call) and isinstance(e.func, ast.Name):
            return self.returns(mod, e.func.id, depth + 1)
        if isinstance(e, ast.IfExp):
            a, b = self._expr(mod, fn, e.body, depth), self._expr(mod, fn, e.orelse, depth)
            return None if a is None or b is None else a | b
        return None

    def _name(self, mod: str, fn: ast.AST, name: str, depth: int, index: int | None = None) -> frozenset[str] | None:
        vals, stored, appended = self._assigned(fn, name)
        if not vals and not appended:
            return None                                      # a parameter, a global: not readable
        out: set[str] = set(stored)
        for v in vals:
            if isinstance(v, ast.List) and not v.elts and appended:
                continue                                     # `out = []` then `out.append(...)`
            got = self._expr(mod, fn, v, depth, index=index)
            if got is None:
                return None
            out |= got
        for a in appended:
            got = self._expr(mod, fn, a, depth, index=index)
            if got is None:
                return None
            out |= got
        return frozenset(out)


def _calls_in(fn: ast.AST, imports: dict[str, tuple[str, str]]):
    """(producer, consumer, consumer-param position or keyword) for each `v = [await] p(...)` later passed to `c`."""
    produced: dict[str, tuple[str, str]] = {}
    for n in ast.walk(fn):
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
            call = _unwrap(n.value)
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id in imports:
                produced[n.targets[0].id] = imports[call.func.id]
    for n in ast.walk(fn):
        call = n if isinstance(n, ast.Call) else None
        if not call or not isinstance(call.func, ast.Name) or call.func.id not in imports:
            continue
        for i, a in enumerate(call.args):
            if isinstance(a, ast.Name) and a.id in produced:
                yield produced[a.id], imports[call.func.id], i
        for kw in call.keywords:
            if kw.arg and isinstance(kw.value, ast.Name) and kw.value.id in produced:
                yield produced[kw.value.id], imports[call.func.id], kw.arg


def _param(fn: ast.AST, where: int | str) -> str | None:
    a = fn.args                                              # type: ignore[attr-defined]
    if isinstance(where, int):
        pos = [*a.posonlyargs, *a.args]
        return pos[where].arg if where < len(pos) else None
    return where if any(x.arg == where for x in [*a.posonlyargs, *a.args, *a.kwonlyargs]) else None


def contract_gaps(code: str, modules: dict[str, str]) -> list[dict]:
    """Each place `code` hands one finished module's dicts to another that requires a key none of them can carry:
    ``{producer, consumer, param, missing, carries, origin}`` (`producer`/`consumer` as ``module.function``; `missing`
    the required alternatives; `origin` the module whose dict literals the producer returns)."""
    mine = _defs(code)
    if not mine:
        return []
    funcs, imports = mine
    out: list[dict] = []
    seen: set[tuple] = set()
    for fn in funcs.values():
        for (pmod, pfn), (cmod, cfn), where in _calls_in(fn, imports):
            cdefs = _defs(modules.get(cmod, ""))
            cfunc = cdefs[0].get(cfn) if cdefs else None
            param = _param(cfunc, where) if cfunc else None
            if not param:
                continue
            need = required_keys(cfunc, param)
            if not need:
                continue
            reader = _Reader(modules)
            carries = reader.returns(pmod, pfn)
            if carries is None or not carries:
                continue                                     # unknown -- or nothing literal at all: no claim
            missing = [alt for alt in need if not alt & carries]
            key = (pmod, pfn, cmod, cfn, tuple(sorted(map(tuple, map(sorted, missing)))))
            if missing and key not in seen:
                seen.add(key)
                out.append({"producer": f"{pmod}.{pfn}", "consumer": f"{cmod}.{cfn}", "param": param,
                            "missing": [sorted(m) for m in missing], "carries": sorted(carries),
                            "origin": sorted(reader.origins)})
    return out


def returned_keys(code: str, fname: str) -> list[str]:
    """§17.1834 — the keys the dicts a module's own function returns can carry, read from that module alone; empty
    when unreadable (the caller then shows nothing). Live (5b4880c0 T6): `load_batch_context(...) -> dict[str, Any]`
    reached the batch as its signature and the first docstring line, "Load the equity and order-count context" -- and
    every attempt read `context["equity"]`, a key it never returns (it returns `day_start_equity`, `orders_today`)."""
    got = _Reader({"_": code}).returns("_", fname)
    return sorted(got) if got else []



# ── §17.1836 a test does not replace a finished module that never leaves the engine ──────────────────────────

#: what a module imports that reaches outside the engine: the network, a broker, a model, a person, a secret
_OUTSIDE_LIBS = frozenset({"httpx", "requests", "aiohttp", "urllib", "urllib3", "socket", "ssl", "http", "smtplib",
                           "websockets", "websocket", "alpaca", "alpaca_trade_api", "openai", "anthropic", "ollama",
                           "praw", "feedparser", "subprocess", "boto3", "paramiko", "asyncssh"})
_INSIDE_APP = frozenset({"app.database", "app.config"})


def _imports(code: str) -> set[str]:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return set()
    out: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            out |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module and not n.level:
            out.add(n.module)
    return out


def reaches_outside(code: str, job_modules: set[str], modules: dict[str, str] | None = None,
                    _seen: frozenset[str] = frozenset()) -> bool:
    """A module that imports a network/broker/model library, or an engine service other than the database and the
    settings (the model router, the operator's questions, the secrets store) -- itself or through a job module it
    imports (`modules`): what a test may stand in for. The backtester reaches the broker through the SPY tracker."""
    for imp in _imports(code):
        if imp.split(".")[0] in _OUTSIDE_LIBS:
            return True
        leaf = imp.rsplit(".", 1)[1] if imp.startswith("app.modules.") else ""
        if imp.startswith("app.") and imp not in _INSIDE_APP and leaf not in job_modules:
            return True
        if leaf in (modules or {}) and leaf not in _seen and reaches_outside(
                modules[leaf], job_modules, modules, _seen | {leaf}):        # type: ignore[index]
            return True
    return False


def _origin(modules: dict[str, str], mod: str, name: str, depth: int = 0) -> str | None:
    """The job module that defines `name` as reached through `mod` (following `from app.modules.x import name`)."""
    d = _defs(modules.get(mod, "")) if mod in modules else None
    if not d or depth > _MAX_DEPTH:
        return None
    funcs, imports = d
    if name in funcs:
        return mod
    if name in imports:
        return _origin(modules, *imports[name], depth=depth + 1)
    try:
        tree = ast.parse(modules[mod])
    except SyntaxError:
        return None
    if any(isinstance(n, ast.ClassDef) and n.name == name for n in tree.body):
        return mod
    return None


def _patch_targets(code: str) -> list[tuple[str, str, int]]:
    """(module, name, line) of each `patch("app.modules.m.name")`, `monkeypatch.setattr("app.modules.m.name", …)`,
    `patch.object(m, "name")` / `monkeypatch.setattr(m, "name", …)` with `m` a module imported from `app.modules`."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []
    alias: dict[str, str] = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module == "app.modules":
            alias.update({a.asname or a.name: a.name for a in n.names})
        elif isinstance(n, ast.Import):
            alias.update({a.asname: a.name.rsplit(".", 1)[1] for a in n.names
                          if a.asname and a.name.startswith("app.modules.")})
    out: list[tuple[str, str, int]] = []
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        fname = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else "")
        if fname not in ("patch", "setattr", "object") or not n.args:
            continue
        a0 = n.args[0]
        if isinstance(a0, ast.Constant) and isinstance(a0.value, str) and a0.value.startswith("app.modules."):
            parts = a0.value.split(".")
            if len(parts) >= 4:                   # §17.1870 -- `app.modules.m.httpx.AsyncClient` needs `m.httpx` first
                out.append((parts[2], parts[3], n.lineno))
        elif (isinstance(a0, ast.Name) and a0.id in alias and len(n.args) >= 2 and isinstance(n.args[1], ast.Constant)
              and isinstance(n.args[1].value, str)):
            out.append((alias[a0.id], n.args[1].value, n.lineno))
    return sorted(out, key=lambda t: t[2])


def _own_nodes(fn: ast.AST):
    """The nodes of a function's own body -- not of the functions and classes defined inside it."""
    todo = list(ast.iter_child_nodes(fn))
    while todo:
        n = todo.pop()
        yield n
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            todo.extend(ast.iter_child_nodes(n))


#: what reading a query's result is called: `result.mappings().all()`, `row = result.first()`, `.scalar()`
_ROW_CALLS = frozenset({"execute", "all", "fetchall", "fetchone", "fetchmany", "first", "one", "one_or_none",
                        "scalar", "scalars", "scalar_one", "scalar_one_or_none", "mappings"})


def query_return(code: str, fname: str) -> str:
    """§17.1837 — a function whose dicts cannot be read but that runs a query: its return expression and that query,
    "`[row[0] for row in rows]` of `SELECT payload FROM ingested_item …`"; empty otherwise. Live (5b4880c0 T6U6): the
    point-in-time store returns each row's payload only, its line showed `get_data_as_of(timestamp, *, db)`, and the
    generator wrote "the point-in-time store always returns the row id" -- then skipped every item for lacking one."""
    d = _defs(code)
    fn = d[0].get(fname) if d else None
    if fn is None:
        return ""
    sql = next((" ".join(c.value.split()) for c in _own_nodes(fn) if isinstance(c, ast.Constant)
                and isinstance(c.value, str) and c.value.lstrip().upper().startswith("SELECT")), "")
    # only a return built from the query's rows -- not `return None`, not a counter the function keeps
    rowish: set[str] = set()
    nodes = list(_own_nodes(fn))
    for _ in range(3):                                   # result -> rows -> row, a few hops
        for n in nodes:
            if isinstance(n, (ast.Assign, ast.AnnAssign)):
                v = n.value
                srcs = {x.attr for x in ast.walk(v) if isinstance(x, ast.Attribute)} if v is not None else set()
                names = {x.id for x in ast.walk(v) if isinstance(x, ast.Name)} if v is not None else set()
                if srcs & _ROW_CALLS or names & rowish:
                    tgts = n.targets if isinstance(n, ast.Assign) else [n.target]
                    rowish |= {x.id for t in tgts for x in ast.walk(t) if isinstance(x, ast.Name)}
            elif isinstance(n, (ast.For, ast.AsyncFor, ast.comprehension)) and \
                    {x.id for x in ast.walk(n.iter) if isinstance(x, ast.Name)} & rowish:
                rowish |= {x.id for x in ast.walk(n.target) if isinstance(x, ast.Name)}
    rets = [ast.unparse(r.value) for r in nodes if isinstance(r, ast.Return) and r.value is not None
            and ({x.id for x in ast.walk(r.value) if isinstance(x, ast.Name)} & rowish
                 or {x.attr for x in ast.walk(r.value) if isinstance(x, ast.Attribute)} & _ROW_CALLS)]
    if not (rets and sql):
        return ""
    return f"`{rets[-1][:80]}` of `{sql[:110]}{' …' if len(sql) > 110 else ''}`"


def return_note(code: str, fname: str) -> str:
    """What a function's line says it returns: "{a, b}" (its dicts' keys), or its query's shape, or nothing."""
    keys = returned_keys(code, fname)
    return "{" + ", ".join(keys) + "}" if keys else query_return(code, fname)


def _returns_hint(code: str, fname: str) -> str:
    """What a function really returns, as far as it can be read: its dict keys, else its return expression and the
    query it runs."""
    keys = returned_keys(code, fname)
    if keys:
        return f"dicts with keys {', '.join(f'`{k}`' for k in keys)}"
    return query_return(code, fname)


def internal_fakes(files: dict[str, str], modules: dict[str, str], own: set[str],
                   fks: dict[str, dict[str, str]] | None = None) -> list[str]:
    """§17.1836 — each test that replaces a function of another finished module of the job that never leaves the
    engine. Live (5b4880c0 T6U6, 2026-10-26): the generator's tests replaced `get_data_as_of` with a fake "as
    get_data_as_of would" that returned `"id": row[0]` -- the real one returns only each row's payload -- so the fix
    read `item.get("id")`, passed the sandbox, and on real data saves nothing and gives every signal `id: None`."""
    job = set(modules) | own
    # the step's own modules too: a test patches `load_batch_context` through the module under test, which imports it
    reach = {**modules, **{p.rsplit("/", 1)[1][:-3]: c for p, c in files.items()
                          if p.startswith("app/") and p.endswith(".py")}}
    found: list[str] = []
    seen: set[tuple[str, str]] = set()
    for path, code in files.items():
        if not path.startswith("tests/"):
            continue
        for mod, name, line in _patch_targets(code):
            origin = _origin(reach, mod, name) if mod in reach else None
            if not origin or origin in own or origin not in modules or (origin, name) in seen:
                continue
            if reaches_outside(modules[origin], job, reach, frozenset({origin})):
                continue
            seen.add((origin, name))
            hint = _returns_hint(modules[origin], name)
            # §17.1852 — and WHICH rows: the tables the real one reads (followed through the job's modules) and the
            # finished writers that insert them, so "insert the rows it reads" is an instruction, not a hope;
            # §17.1853 — parents first, along the job's foreign keys
            found.append(
                f"`{path}:{line}` replaces `{name}` (`{origin}`, a finished module that never leaves the engine) with "
                "a stand-in: call the real one -- against the scratch database, with the rows it reads, when it uses "
                "one -- so the test sees what it really does"
                + (f" (it returns {hint})" if hint else "")
                + rows_clause(modules, reads_tables(modules, origin, name), fks)
                + ". Mock only what reaches outside the engine -- the broker, the network, model calls, the clock")
    return found


# ── §17.1856 a patch target names what the module has ───────────────────────────────────────────────────────

def _top_names(code: str) -> tuple[list[str], list[str]]:
    """A module's top-level names: (what it defines -- functions, classes, assigned names; what it imports)."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return [], []
    defined: list[str] = []
    imported: list[str] = []
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.append(n.name)
        elif isinstance(n, (ast.Assign, ast.AnnAssign)):
            for t in (n.targets if isinstance(n, ast.Assign) else [n.target]):
                defined += [x.id for x in ast.walk(t) if isinstance(x, ast.Name)]
        elif isinstance(n, ast.ImportFrom) and n.module != "__future__":
            imported += [a.asname or a.name for a in n.names]
        elif isinstance(n, ast.Import):
            imported += [a.asname or a.name.split(".")[0] for a in n.names]
    return defined, imported


def _inserts_of(code: str) -> list[str]:
    """The tables a module's own functions INSERT INTO."""
    d = _defs(code)
    if not d:
        return []
    out: set[str] = set()
    for fn in d[0].values():
        for call in (n for n in _own_nodes(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                     and n.func.attr == "execute"):
            parts = _insert_parts(_sql_of(call, fn) or "")
            if parts:
                out.add(parts[0].lower())
    return sorted(out)


def _settings_patches(path: str, code: str, modules: dict[str, str]) -> list[str]:
    """§17.1876 — each `patch("app.config.settings.X", …)` / `patch.object(settings, "X", …)` of a field the engine's
    settings do not have: `patch` raises AttributeError before the test body runs. Live (5b4880c0 T5 a4, 2026-10-30):
    `patch("app.config.settings.REDDIT_MAX_POSTS", 10)` three times -- the field is `reddit_max_posts`."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []
    bound = {a.asname or a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module == "app.config"
             for a in n.names if a.name == "settings"}
    targets: list[tuple[str, int]] = []
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call) or not n.args:
            continue
        f = n.func
        fname = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else "")
        if fname not in ("patch", "setattr", "object"):
            continue
        a0 = n.args[0]
        if isinstance(a0, ast.Constant) and isinstance(a0.value, str) and a0.value.startswith("app.config.settings."):
            parts = a0.value.split(".")
            if len(parts) == 4:
                targets.append((parts[3], n.lineno))
        elif (isinstance(a0, ast.Name) and a0.id in bound and len(n.args) >= 2 and isinstance(n.args[1], ast.Constant)
              and isinstance(n.args[1].value, str)):
            targets.append((n.args[1].value, n.lineno))
    if not targets:
        return []
    from app.config import settings
    fields = list(getattr(type(settings), "model_fields", {}) or dir(settings))
    out: list[str] = []
    for name, line in sorted(targets, key=lambda t: t[1]):
        if hasattr(settings, name):
            continue
        like = [f for f in fields if f.lower() == name.lower()]
        reads = sorted({a.attr for m in modules.values() for a in ast.walk(_tree_or_empty(m))
                        if isinstance(a, ast.Attribute) and isinstance(a.value, ast.Name) and a.value.id == "settings"})
        out.append(f"`{path}:{line}` patches `{name}` on `app.config.settings`, which has no such field -- `patch` raises "
                   "AttributeError before the test body runs"
                   + (f"; the field is `{like[0]}`" if like else
                      f"; the job's modules read {', '.join(f'`settings.{r}`' for r in reads[:8])}" if reads else ""))
    return out


def _tree_or_empty(code: str) -> ast.AST:
    try:
        return ast.parse(code)
    except SyntaxError:
        return ast.Module(body=[], type_ignores=[])


def _whole_library_patches(path: str, code: str, reach: dict[str, str]) -> list[str]:
    """§17.1876 — each `patch("app.modules.m.httpx")` of a whole library on a module that catches that library's
    exceptions: every class on the mock is a mock, and `except (httpx.HTTPError, …)` raises `TypeError: catching
    classes that do not inherit from BaseException is not allowed`. Live (5b4880c0 T5 a2, 2026-10-30): three reddit
    tests, the module's own network fault path. The name it leaves the engine through is the thing to patch."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []
    out: list[str] = []
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call) or not n.args:
            continue
        f = n.func
        fname = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else "")
        a0 = n.args[0]
        if fname not in ("patch", "setattr") or not (isinstance(a0, ast.Constant) and isinstance(a0.value, str)):
            continue
        parts = a0.value.split(".")
        if len(parts) not in (4, 5) or parts[:2] != ["app", "modules"] or parts[2] not in reach:
            continue
        mod, lib = parts[2], parts[3]
        mtree = _tree_or_empty(reach[mod])
        if lib not in {a.asname or a.name for i in mtree.body if isinstance(i, ast.Import) for a in i.names}:
            continue
        caught = sorted({ast.unparse(x) for h in ast.walk(mtree) if isinstance(h, ast.ExceptHandler) and h.type is not None
                         for x in (h.type.elts if isinstance(h.type, ast.Tuple) else [h.type])
                         if isinstance(x, ast.Attribute) and isinstance(x.value, ast.Name) and x.value.id == lib})
        if not caught:
            continue
        used = sorted({x.func.attr for x in ast.walk(mtree) if isinstance(x, ast.Call) and isinstance(x.func, ast.Attribute)
                       and isinstance(x.func.value, ast.Name) and x.func.value.id == lib
                       and f"{lib}.{x.func.attr}" not in caught})
        through = f" (`app.modules.{mod}.{lib}.{used[0]}`)" if used else ""
        if len(parts) == 5:                                   # §17.1878 one exception class of it, patched by name
            if f"{lib}.{parts[4]}" in caught:
                out.append(f"`{path}:{n.lineno}` patches `{lib}.{parts[4]}` on `{mod}`, an exception class the module "
                           f"catches (`except ({', '.join(caught)})`): a mock is not an exception class, and the "
                           "`except` fails with `TypeError: catching classes that do not inherit from BaseException is "
                           "not allowed` -- patch only the name it leaves the engine through" + through)
            continue
        out.append(f"`{path}:{n.lineno}` patches `{lib}` whole on `{mod}`: every class on it becomes a mock, and the "
                   f"module's `except ({', '.join(caught)})` then fails with `TypeError: catching classes that do not "
                   "inherit from BaseException is not allowed` -- patch only the name it leaves the engine through"
                   + through)
    return out


def unknown_patch_targets(files: dict[str, str], modules: dict[str, str], own: set[str]) -> list[str]:
    """§17.1856 — each test that patches a name on a job module that has no such name. Live (5b4880c0 T5, 2026-10-28):
    `patch("app.modules.paper_operator_approval_gate.write_approval")` -- the gate writes `approval` itself with its
    own SQL and never imports `write_approval` (`paper_audit_trail_writer` defines it); the sandbox raised
    "AttributeError: module … does not have the attribute 'write_approval'" and the attempt was spent."""
    reach = {**modules, **{p.rsplit("/", 1)[1][:-3]: c for p, c in files.items()
                          if p.startswith("app/") and p.endswith(".py")}}
    found: list[str] = []
    seen: set[tuple[str, str]] = set()
    for path, code in files.items():
        if not path.startswith("tests/"):
            continue
        for mod, name, line in _patch_targets(code):
            if mod not in reach or (mod, name) in seen:
                continue
            defined, imported = _top_names(reach[mod])
            if name in defined or name in imported:
                continue
            seen.add((mod, name))
            public = [n for n in defined if not n.startswith("_")]
            where = [m for m, c in sorted(modules.items()) if name in _top_names(c)[0]]
            inserts = _inserts_of(reach[mod])
            # §17.1875 — what the module reaches through imports inside its functions: the name lives on THAT module
            from app.modules.engine_source import _call_time_imports
            try:
                late = {f.name: _call_time_imports(f) for f in ast.parse(reach[mod]).body
                        if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)) and _call_time_imports(f)}
            except SyntaxError:
                late = {}
            same = [(f, i) for f, imps in late.items() for i in imps if i == name or i.rsplit(".", 1)[-1] == name]
            late_note = ""
            if same:
                f, i = same[0]
                late_note = (f". `{name}` is imported inside `{f}` at call time and is never an attribute of `{mod}`: "
                             f"patch the name on `{i.rsplit('.', 1)[0] if '.' in i else i}` itself")
            elif late:
                late_note = ". It reaches outside through " + "; ".join(
                    f"`{f}`, which imports {', '.join('`' + i + '`' for i in imps[:3])} at call time"
                    for f, imps in sorted(late.items())[:3]) + " -- patch THOSE names on their own modules"
            found.append(
                f"`{path}:{line}` patches `{name}` on `{mod}`, which has no such name (it has "
                f"{', '.join(public[:12]) or 'nothing public'}; it imports {', '.join(imported[:12]) or 'nothing'}) -- "
                "the sandbox raises AttributeError"
                + (f". `{name}` is defined in `{where[0]}`, which `{mod}` never imports: patching it anywhere changes "
                   f"nothing `{mod}` does" if where else "")
                + (f" -- `{mod}` writes {', '.join(f'`{t}`' for t in inserts)} itself: call it and read the rows it "
                   "wrote" if inserts else "")
                + late_note
                + ". Patch a name where the code looks it up, and only what reaches outside the engine")
        found += _settings_patches(path, code, modules)                   # §17.1876
        found += _whole_library_patches(path, code, reach)                 # §17.1876
    return found


# ── §17.1857 a test mocks the secret a module reads ──────────────────────────────────────────────────────────

def _test_calls(code: str) -> list[tuple[str, str, int]]:
    """(module, function, line) of each call in a test file to a name imported from a job module -- `from
    app.modules.m import f` then `f(…)`, or `from app.modules import m` / `import app.modules.m as m` then `m.f(…)`."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []
    names: dict[str, tuple[str, str]] = {}
    mods: dict[str, str] = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("app.modules."):
            names.update({a.asname or a.name: (n.module.rsplit(".", 1)[1], a.name) for a in n.names})
        elif isinstance(n, ast.ImportFrom) and n.module == "app.modules":
            mods.update({a.asname or a.name: a.name for a in n.names})
        elif isinstance(n, ast.Import):
            mods.update({a.asname or a.name: a.name.rsplit(".", 1)[1] for a in n.names
                         if a.name.startswith("app.modules.") and a.name.count(".") == 2})
    out: list[tuple[str, str, int]] = []
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        if isinstance(f, ast.Name) and f.id in names:
            out.append((*names[f.id], n.lineno))
        elif isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id in mods:
            out.append((mods[f.value.id], f.attr, n.lineno))
    return out


def _str_consts(tree: ast.Module) -> dict[str, str]:
    out: dict[str, str] = {}
    for n in tree.body:
        targets = n.targets if isinstance(n, ast.Assign) else [n.target] if isinstance(n, ast.AnnAssign) else []
        v = getattr(n, "value", None)
        if targets and isinstance(v, ast.Constant) and isinstance(v.value, str):
            out.update({t.id: v.value for t in targets if isinstance(t, ast.Name)})
    return out


_SECRET_HOPS = 12


def secrets_read(modules: dict[str, str], mod: str, name: str, depth: int = 0,
                 _seen: set[tuple[str, str]] | None = None, patched: frozenset[tuple[str, str]] = frozenset()) -> dict[str, str]:
    """§17.1857 — the operator secrets `mod.name` reads through `values_for`, itself or through the job functions it
    calls: secret name -> the module that reads it ("" for a name the code computes). `name` is a function, a class
    (its constructor) or `Class.method`; a (module, name) in `patched` is a stand-in the test put there, so the chain
    stops at it. Live (5b4880c0 T5, 2026-10-28): `ingest_form_4_filings` reads `SEC_CONTACT_EMAIL`;
    `AlpacaPaperClient.from_secrets` reads `ALPACA_API_KEY_ID`, `ALPACA_API_SECRET_KEY`."""
    seen = _seen if _seen is not None else set()
    # deeper than the table walker: live (T8), view -> tracker -> `_fetch_spy_prices` -> `client.get_bars` ->
    # `_request` -> `_ensure_credentials` -> `_get_credentials` -> `values_for` is eight hops; `seen` stops cycles
    if depth > _SECRET_HOPS or (mod, name) in seen or mod not in modules or (mod, name.split(".")[0]) in patched:
        return {}
    seen.add((mod, name))
    d = _defs(modules[mod])
    if not d:
        return {}
    funcs, imports = d
    try:
        tree = ast.parse(modules[mod])
    except SyntaxError:
        return {}
    cls_name, _dot, meth = name.partition(".")
    cls = next((c for c in tree.body if isinstance(c, ast.ClassDef) and c.name == cls_name), None)
    node = funcs.get(name) if not meth else None
    if node is None and cls is not None:
        node = next((m for m in cls.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
                     and m.name == (meth or "__init__")), None)
        if node is None:
            return {}
    if node is None:
        if cls_name not in imports:
            return {}
        m2, n2 = imports[cls_name]
        return secrets_read(modules, m2, n2 + (f".{meth}" if meth else ""), depth, seen, patched)   # an import: no hop
    consts = _str_consts(tree)
    classes = {c.name for c in tree.body if isinstance(c, ast.ClassDef)}
    out: dict[str, str] = {}
    nodes = list(_own_nodes(node))
    # the lists bound in the body first: `_own_nodes` walks a stack, so a later statement comes before an earlier one
    local_lists: dict[str, list] = {n.targets[0].id: n.value.elts for n in nodes
                                    if isinstance(n, ast.Assign) and len(n.targets) == 1
                                    and isinstance(n.targets[0], ast.Name) and isinstance(n.value, (ast.List, ast.Tuple))}

    def _class_of(v: ast.AST) -> str | None:
        """`Class(…)` / `await Class.m(…)` -> Class, when it is a job class (imported or of this module)."""
        v = _unwrap(v)
        f = v.func if isinstance(v, ast.Call) else None
        c = f.id if isinstance(f, ast.Name) else f.value.id if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) else None
        return c if c and (c in imports or c in classes) else None
    # the instances bound in the body: `client = AlpacaPaperClient(…)`, then `client.get_bars(…)` (live, T8's view
    # through the SPY tracker: `_request` -> `_ensure_credentials` -> `_get_credentials` -> `values_for`)
    bound: dict[str, str] = {n.targets[0].id: c for n in nodes
                             if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name)
                             and (c := _class_of(n.value))}
    for n in nodes:
        if not isinstance(n, ast.Call):
            continue
        callee = n.func.id if isinstance(n.func, ast.Name) else n.func.attr if isinstance(n.func, ast.Attribute) else ""
        if isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name) and n.func.value.id in bound:
            for k, v in secrets_read(modules, mod, f"{bound[n.func.value.id]}.{callee}", depth + 1, seen, patched).items():
                out.setdefault(k, v)                                                   # `instance.method(…)`
            continue
        if meth and isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name) and n.func.value.id == "self":
            for k, v in secrets_read(modules, mod, f"{cls_name}.{callee}", depth + 1, seen, patched).items():
                out.setdefault(k, v)                                                   # `self.method(…)`
            continue
        if callee == "values_for":
            wanted = n.args[1] if len(n.args) > 1 else next((k.value for k in n.keywords if k.arg == "wanted"), None)
            elts = wanted.elts if isinstance(wanted, (ast.List, ast.Tuple)) else \
                local_lists.get(wanted.id, []) if isinstance(wanted, ast.Name) else []
            for e in elts:
                if isinstance(e, ast.Constant) and isinstance(e.value, str):
                    out.setdefault(e.value, mod)
                elif isinstance(e, ast.Name) and e.id in consts:
                    out.setdefault(consts[e.id], mod)
                else:
                    out.setdefault("", mod)
            if not elts:
                out.setdefault("", mod)
        elif isinstance(n.func, ast.Name) and callee != name and (callee in funcs or callee in imports
                                                                   or any(isinstance(c, ast.ClassDef) and c.name == callee
                                                                          for c in tree.body)):
            for k, v in secrets_read(modules, mod, callee, depth + 1, seen, patched).items():
                out.setdefault(k, v)
        elif isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name) \
                and n.func.value.id != cls_name and (n.func.value.id in imports or any(
                    isinstance(c, ast.ClassDef) and c.name == n.func.value.id for c in tree.body)):
            for k, v in secrets_read(modules, mod, f"{n.func.value.id}.{callee}", depth + 1, seen, patched).items():
                out.setdefault(k, v)                                                   # `Class.method(…)`
    return out


def _scopes(tree: ast.Module) -> list[tuple[int, int, str]]:
    """(first line incl. decorators, last line, name) of every function in a test file."""
    out = []
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            first = min([n.lineno] + [d.lineno for d in n.decorator_list])
            out.append((first, n.end_lineno or n.lineno, n.name))
    return out


def _scope_of(scopes: list[tuple[int, int, str]], line: int) -> str:
    """The innermost function a line is in ("" at module level)."""
    inner = [s for s in scopes if s[0] <= line <= s[1]]
    return min(inner, key=lambda s: s[1] - s[0])[2] if inner else ""


def _expects_a_raise(tree: ast.Module) -> list[tuple[int, int]]:
    """The line ranges of `with pytest.raises(…)` blocks: a call there is meant to fail before it does its work."""
    out = []
    for n in ast.walk(tree):
        if isinstance(n, (ast.With, ast.AsyncWith)) and any(
                isinstance(i.context_expr, ast.Call) and (getattr(i.context_expr.func, "attr", None) or
                                                          getattr(i.context_expr.func, "id", None)) == "raises"
                for i in n.items):
            out.append((n.lineno, n.end_lineno or n.lineno))
    return out


def runs_secrets_for_real(files: dict[str, str], modules: dict[str, str], own: set[str]) -> list[str]:
    """§17.1857 — each test that runs a job function (or builds a job class) that reads an operator secret, in a
    test file that patches `values_for` nowhere. Live (5b4880c0 T5, 2026-10-28, two rounds): the EDGAR ingester ran
    for real and the sandbox refused -- "Missing operator secret SEC_CONTACT_EMAIL; refusing to run" -- and T8's
    operator view built the broker client for real -- "Missing Alpaca paper trading secrets"; the §17.1839 block
    names `values_for` as a mock point, and neither test used it."""
    reach = {**modules, **{p.rsplit("/", 1)[1][:-3]: c for p, c in files.items()
                          if p.startswith("app/") and p.endswith(".py")}}
    found: list[str] = []
    for path, code in files.items():
        if not path.startswith("tests/"):
            continue
        try:
            tree = ast.parse(code)
        except SyntaxError:
            continue
        scopes, raises = _scopes(tree), _expects_a_raise(tree)
        # a patch counts for the test function it is in (its decorators too); one outside any test -- a fixture, a
        # helper, module level -- for the whole file. Live (5b4880c0 T8): other tests patched the SPY tracker, the
        # one at line 457 ran it for real; a file-wide set would have hidden it
        by_scope: dict[str, set[tuple[str, str]]] = {}
        for m, name, ln in _patch_targets(code):
            sc = _scope_of(scopes, ln)
            by_scope.setdefault(sc if sc.startswith("test") else "", set()).add((m, name))
        seen: set[tuple[str, str]] = set()
        for mod, fname, line in _test_calls(code):
            if mod not in reach or (mod, fname) in seen or any(a <= line <= b for a, b in raises):
                continue
            patched = frozenset(by_scope.get("", set()) | by_scope.get(_scope_of(scopes, line), set()))
            # `values_for` patched on the module that reads it, or on `runner_secrets` itself (every reader) -- live
            # (5b4880c0 T5, attempt 4): patched on the 13F and Reddit ingesters, not on the EDGAR one it also ran
            covered = {m for m, name in patched if name == "values_for"}
            secrets = secrets_read(reach, mod, fname, patched=patched)
            readers = sorted(set(secrets.values()) - covered) if "runner_secrets" not in covered else []
            if not readers:
                continue
            seen.add((mod, fname))
            named = sorted(s for s, r in secrets.items() if s and r in readers)
            found.append(
                f"`{path}:{line}` runs `{fname}` (`{mod}`) for real, and it reads the operator secret"
                + (f"{'s' if len(named) > 1 else ''} {', '.join(f'`{s}`' for s in named)}" if named else "s it needs")
                + f" through `values_for` ({', '.join(f'`{r}`' for r in readers)}) -- the sandbox has no secrets, "
                "so the code refuses to run (§17.1776). Patch "
                + ", ".join(f"`app.modules.{r}.values_for`" for r in readers)
                + " (where the module looks it up) to return a test value for each name"
                + (f" ({', '.join(f'`{s}`' for s in named)})" if named else "")
                + "; mock the network it then reaches as well")
    return found


# ── §17.1864 a retry keeps every test ───────────────────────────────────────────────────────────

_TEST_PATH_RE = re.compile(r"^\s*#\s*(tests/\S+\.py)\s*$")


def written_tests(output: str) -> dict[str, list[str]]:
    """The test functions each test file of an output defines, by path: module-level and in classes, in source
    order. Recorded with a refused attempt (§17.1864), so the next attempt is held to them."""
    from app.modules.execution_codegen_gate import PYTHON_LANGS, extract_code_blocks
    out: dict[str, list[str]] = {}
    for lang, code in extract_code_blocks(output or ""):
        if lang not in PYTHON_LANGS or not code.strip():
            continue
        m = _TEST_PATH_RE.match(code.lstrip().splitlines()[0])
        if not m:
            continue
        try:
            tree = ast.parse(code)
        except SyntaxError:
            continue
        names = [n.name for n in sorted((n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                                         and n.name.startswith("test")), key=lambda n: n.lineno)]
        if names:
            out.setdefault(m.group(1), []).extend(n for n in names if n not in out.get(m.group(1), []))
    return out


def dropped_tests(output: str, prior: dict[str, list[str]] | None) -> list[str]:
    """§17.1864 — each test file of an output that lacks a test, by name, that this step's earlier attempts wrote.
    Live (5b4880c0 T5, 2026-10-29, the §17.1863 round): the redraws kept every test (23, 23, 23; 11, 11; 6, 6; 5, 5)
    and each RETRY dropped them -- 23 tests, then 11 (13 dropped), 6, 5 -- the ones the sandbox had passed among
    them. A smaller file passes more easily and tests less. A test-file finding: a redraw, not the attempt."""
    if not prior:
        return []
    have = written_tests(output)
    # §17.1869 — a test file that does not compile is reported for that (`test_syntax_findings`), not as empty
    from app.modules.execution_codegen_gate import PYTHON_LANGS, extract_code_blocks
    broken: set[str] = set()
    for lang, code in extract_code_blocks(output or ""):
        m = _TEST_PATH_RE.match(code.lstrip().splitlines()[0]) if lang in PYTHON_LANGS and code.strip() else None
        if m:
            try:
                compile(code, "<block>", "exec", dont_inherit=True)
            except SyntaxError:
                broken.add(m.group(1))
    found: list[str] = []
    for path, names in sorted(prior.items()):
        gone = [n for n in names if n not in set(have.get(path, []))]
        if not gone or path in broken:
            continue
        found.append(
            f"`{path}` dropped {len(gone)} of the {len(names)} tests your earlier attempts wrote: "
            + ", ".join(f"`{n}`" for n in gone)
            + " -- keep every test by name; fix the ones the sandbox named, never drop them")
    return found


# ── §17.1863 a secret named by a constant is mocked by its name ─────────────────────────────────

def _values_for_mocks(code: str) -> list[tuple[str, int, list[str] | None]]:
    """(module, line, keys) of each patch of `values_for` on a job module (or on `runner_secrets`) in a test file:
    `keys` are the string keys of the literal dict the stand-in returns -- `AsyncMock(return_value={…})`,
    `patch(…, return_value={…})`, `lambda …: {…}` -- or None when the test returns something the engine cannot read."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []
    alias: dict[str, str] = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module == "app.modules":
            alias.update({a.asname or a.name: a.name for a in n.names})
        elif isinstance(n, ast.Import):
            alias.update({a.asname: a.name.rsplit(".", 1)[1] for a in n.names
                          if a.asname and a.name.startswith("app.modules.")})

    def _dict_keys(v: ast.AST | None) -> list[str] | None:
        if isinstance(v, ast.Lambda):
            v = v.body
        if isinstance(v, ast.Dict):
            return [k.value for k in v.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)]
        return None

    def _keys_of(mock: ast.AST | None, kws: list[ast.keyword]) -> list[str] | None:
        rv = next((k.value for k in kws if k.arg == "return_value"), None)
        if rv is None and isinstance(mock, ast.Call):
            rv = next((k.value for k in mock.keywords if k.arg == "return_value"), None)
        if rv is None and isinstance(mock, ast.Lambda):
            rv = mock
        return _dict_keys(rv)

    out: list[tuple[str, int, list[str] | None]] = []
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call) or not n.args:
            continue
        f = n.func
        fname = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else "")
        if fname not in ("patch", "setattr", "object"):
            continue
        a0 = n.args[0]
        mod: str | None = None
        rest = n.args[1:]
        if isinstance(a0, ast.Constant) and isinstance(a0.value, str) and a0.value.startswith("app.modules."):
            parts = a0.value.split(".")
            if len(parts) == 4 and parts[3] == "values_for":
                mod = parts[2]
        elif (isinstance(a0, ast.Name) and a0.id in alias and len(n.args) >= 2 and isinstance(n.args[1], ast.Constant)
              and n.args[1].value == "values_for"):
            mod, rest = alias[a0.id], n.args[2:]
        if mod is None:
            continue
        mock = next((k.value for k in n.keywords if k.arg == "new"), rest[0] if rest else None)
        out.append((mod, n.lineno, _keys_of(mock, n.keywords)))
    return sorted(out, key=lambda t: t[1])


def wrong_secret_mocks(files: dict[str, str], modules: dict[str, str], own: set[str]) -> list[str]:
    """§17.1863 — each `values_for` stand-in whose literal return dict lacks a secret the functions the test runs
    read through that module. Live (5b4880c0 T5, 2026-10-29, attempt 2): the test patched `values_for` on the 13F
    ingester to return `{"SEC_USER_AGENT": …}` -- a guess from the constant's name `SEC_USER_AGENT_SECRET`, whose
    value is `SEC_CONTACT_EMAIL`; the module read `secrets.get(SEC_USER_AGENT_SECRET)` and refused to run. The
    §17.1857 gate saw the patch and said nothing; the index never named the secret."""
    reach = {**modules, **{p.rsplit("/", 1)[1][:-3]: c for p, c in files.items()
                          if p.startswith("app/") and p.endswith(".py")}}
    found: list[str] = []
    for path, code in files.items():
        if not path.startswith("tests/"):
            continue
        mocks = _values_for_mocks(code)
        if not mocks:
            continue
        try:
            tree = ast.parse(code)
        except SyntaxError:
            continue
        scopes = _scopes(tree)
        calls = _test_calls(code)
        # §17.1875 — a test that expects a raise may hand the module no secret on purpose. Live (5b4880c0 T5 a1,
        # 2026-10-30): `patch(…values_for, AsyncMock(return_value={}))` under `pytest.raises(AlpacaSecretsMissingError)`
        raises = _expects_a_raise(tree)
        fn_ranges = sorted((f.lineno, getattr(f, "end_lineno", None) or f.lineno) for f in ast.walk(tree)
                           if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)))
        for mod, line, keys in mocks:
            if keys is None or (mod != "runner_secrets" and mod not in reach):
                continue
            rng = next((r for r in reversed(fn_ranges) if r[0] <= line <= r[1]), None)
            if rng and any(rng[0] <= a and b <= rng[1] for a, b in raises):
                continue
            sc = _scope_of(scopes, line)
            in_scope = [(m, f) for m, f, ln in calls if m in reach and (not sc.startswith("test") or _scope_of(scopes, ln) == sc)]
            need: dict[str, str] = {}                     # secret -> the function that reads it
            for m, f in in_scope:
                for secret, reader in secrets_read(reach, m, f).items():
                    if secret and (reader == mod or mod == "runner_secrets"):
                        need.setdefault(secret, f)
            missing = sorted(s for s in need if s not in keys)
            if not missing:
                continue
            consts: dict[str, str] = {}
            if mod in reach:
                try:
                    consts = {v: k for k, v in _str_consts(ast.parse(reach[mod])).items()}
                except SyntaxError:
                    pass
            named = ", ".join(f"`{s}`" + (f" (the value of its constant `{consts[s]}`)" if s in consts else "")
                              for s in missing)
            found.append(
                f"`{path}:{line}` patches `values_for` on `{mod}` to return "
                + (", ".join(f"`{k}`" for k in keys) if keys else "no key")
                + f"; `{need[missing[0]]}` reads {named} and refuses to run without it -- return a test value under "
                + ("that name" if len(missing) == 1 else "those names") + ": `{"
                + ", ".join(f'"{s}": …' for s in missing) + "}`")
    return found


# ── §17.1858 a made-up parent id is refused before the sandbox ──────────────────────────────────────────────

def _writes_table(fn: ast.AST) -> str | None:
    """The table a function's first INSERT targets."""
    for call in (n for n in _own_nodes(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                 and n.func.attr == "execute"):
        parts = _insert_parts(_sql_of(call, fn) or "")
        if parts:
            return parts[0].lower()
    return None


def made_up_parent_ids(files: dict[str, str], modules: dict[str, str],
                       fks: dict[str, dict[str, str]] | None) -> list[str]:
    """§17.1858 — each test that writes a child row with a literal number for a foreign key: through a finished
    writer (`write_order(approval_id=1, …)`) or its own `INSERT INTO signal (ingested_item_id, …) VALUES (1, …)`.
    Live (5b4880c0 T6 and T5, 2026-10-28, three sandbox runs): the scratch database refused each -- "Key
    (approval_id)=(1) is not present in table approval" -- after the clause had said "never a made-up one"."""
    if not fks:
        return []
    reach = {**modules, **{p.rsplit("/", 1)[1][:-3]: c for p, c in files.items()
                          if p.startswith("app/") and p.endswith(".py")}}
    found: list[str] = []

    def say(path: str, line: int, table: str, col: str, val, how: str) -> None:
        parent = fks[table][col]
        order, _edges = seed_order(fks, {table})
        steps = _seed_steps(modules, [t for t in order if t != table])
        found.append(
            f"`{path}:{line}` writes `{table}` {how} with a made-up id: `{table}.{col}`={val!r} references `{parent}`, "
            "and the scratch database refuses it (no such row) -- insert the parent rows first, in this order: "
            + "; ".join(steps) + " -- and pass the id each writer returned")

    for path, code in files.items():
        if not path.startswith("tests/"):
            continue
        try:
            tree = ast.parse(code)
        except SyntaxError:
            continue
        calls = {(m, f, ln) for m, f, ln in _test_calls(code)}
        for n in ast.walk(tree):
            if not isinstance(n, ast.Call):
                continue
            hit = next(((m, f) for (m, f, ln) in calls if ln == n.lineno), None)
            if not hit or hit[0] not in reach:
                continue
            d = _defs(reach[hit[0]])
            fn = d[0].get(hit[1]) if d else None
            table = _writes_table(fn) if fn is not None else None
            if not table or table not in fks:
                continue
            pos = [x.arg for x in fn.args.posonlyargs + fn.args.args]          # type: ignore[attr-defined]
            given = [(pos[i], a) for i, a in enumerate(n.args) if i < len(pos)] + [(k.arg, k.value) for k in n.keywords if k.arg]
            for param, val in given:
                if param in fks[table] and isinstance(val, ast.Constant) and isinstance(val.value, int) \
                        and not isinstance(val.value, bool):
                    say(path, n.lineno, table, param, val.value, f"through `{hit[1]}(…)`")
                    break
        for n in ast.walk(tree):
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and _is_query(n.value):
                parts = _insert_parts(n.value)
                if not parts or parts[0].lower() not in fks:
                    continue
                table, cols, vals = parts[0].lower(), [c.lower() for c in parts[1]], parts[2]
                for c, v in zip(cols, vals, strict=False):
                    if c in fks[table] and re.fullmatch(r"\d+", v):
                        say(path, n.lineno, table, c, int(v), "with its own INSERT")
                        break
    return found


# ── §17.1866 a parent row inserted in the test's session is committed before a writer with its own session ────

def _commits(fn: ast.AST) -> bool:
    return any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "commit"
               for n in _own_nodes(fn))


def _insert_execute(node: ast.AST, fn: ast.AST) -> bool:
    """Whether `node` holds a `db.execute(text("INSERT …"))`."""
    return any(isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute) and c.func.attr == "execute"
               and _insert_parts(_sql_of(c, fn) or "") is not None for c in ast.walk(node))


def _call_name(call: ast.Call) -> str:
    f = call.func
    return f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""


def _root_name(e: ast.AST) -> str | None:
    """`x`, `x.attr`, `x[0]`, `int(x)` -> "x"."""
    while isinstance(e, (ast.Attribute, ast.Subscript, ast.Await)):
        e = e.value
    if isinstance(e, ast.Call) and len(e.args) == 1 and isinstance(e.func, ast.Name) and e.func.id in ("int", "str"):
        return _root_name(e.args[0])
    return e.id if isinstance(e, ast.Name) else None


def uncommitted_parents(files: dict[str, str], modules: dict[str, str],
                        fks: dict[str, dict[str, str]] | None = None) -> list[str]:
    """§17.1866 — each test that hands a writer opening its own `async_session()` the id of a row it inserted in its
    own session and never committed: the writer's transaction cannot see it, and the scratch database refuses the
    child as if the parent did not exist. Live (5b4880c0 T5, 2026-10-29, attempts 1 and 3): the test's own
    `insert_ingested_item(db, …)` (an INSERT, no commit) then `write_signal(ingested_item_id=ingested_id, …)` --
    "Key (ingested_item_id)=(1) is not present in table ingested_item" -- and `write_approval(proposal_id=…)` before
    the `await db.commit()` that followed it; the attempts whose helpers committed passed the same tests. The §17.1853
    cause read it as a made-up id."""
    from app.modules.engine_source import _opens_own_session, _own_statements
    reach = {**modules, **{p.rsplit("/", 1)[1][:-3]: c for p, c in files.items()
                          if p.startswith("app/") and p.endswith(".py")}}
    own_sess: dict[str, dict[str, ast.AST]] = {}
    for mod, code in reach.items():
        d = _defs(code)
        if d:
            own_sess[mod] = {name: fn for name, fn in d[0].items() if _opens_own_session(fn)}
    found: list[str] = []
    for path, code in files.items():
        if not path.startswith("tests/"):
            continue
        try:
            tree = ast.parse(code)
        except SyntaxError:
            continue
        calls = _test_calls(code)
        fns = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]

        def writer_of(call: ast.Call, within: ast.AST | None = None) -> tuple[str, str, ast.AST] | None:
            """(module, writer, its def) for a call to a job function that opens its own session, directly or
            through one local helper that forwards to one."""
            name = _call_name(call)
            for m, f, ln in calls:
                if ln == call.lineno and f == name and m in own_sess and f in own_sess[m]:
                    return m, f, own_sess[m][f]
            if within is None and isinstance(call.func, ast.Name):
                helper = next((h for h in fns if h.name == call.func.id), None)
                if helper is not None:
                    for c in _own_nodes(helper):
                        if isinstance(c, ast.Call) and (w := writer_of(c, helper)):
                            return w
            return None

        local_pending = {h.name for h in fns if _writes_table(h) and not _commits(h)}
        committing = {h.name for h in fns if _commits(h)}      # a helper that commits commits the whole session
        for fn in fns:
            pending: dict[str, str] = {}                     # a tainted name -> what put it there
            for st in (x for x in _own_statements(fn) if isinstance(x, ast.stmt)):
                if any(isinstance(c, ast.Call) and _call_name(c) == "commit" for c in ast.walk(st)):
                    pending.clear()
                    continue
                if any(isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id in committing
                       for c in ast.walk(st)) and not any(writer_of(c) for c in ast.walk(st) if isinstance(c, ast.Call)):
                    pending.clear()
                    continue
                for call in (c for c in ast.walk(st) if isinstance(c, ast.Call)):
                    w = writer_of(call)
                    if not w:
                        continue
                    mod, writer, wfn = w
                    via = f"`{_call_name(call)}(…)` -- which calls `{writer}` -- " if _call_name(call) != writer else f"`{writer}(…)` "
                    given = [(k.arg, k.value) for k in call.keywords if k.arg] + [(None, a) for a in call.args]
                    hit = next(((param, _root_name(v)) for param, v in given if _root_name(v) in pending), None)
                    if not hit:
                        continue
                    param, var = hit
                    table = _writes_table(wfn)
                    parent = (fks or {}).get(table or "", {}).get(param or "")
                    fk = (f"`{table}.{param}` references `{parent.split('.')[0]}`: no such row" if parent
                          else "no such parent row")
                    found.append(
                        f"`{path}:{call.lineno}` hands {via}`{var}`, {pending[var]} -- a row in the test's own session, "
                        f"not committed: `{writer}` opens its own `async_session()` and sees only committed rows, so "
                        f"the scratch database refuses the child ({fk}). `await db.commit()` after inserting the "
                        "parent rows, before the call -- or insert them with a writer that opens its own session")
                    break
                if isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name):
                    v = _unwrap(st.value)
                    target = st.targets[0].id
                    if isinstance(v, ast.Call) and writer_of(v):
                        pending.pop(target, None)            # a writer with its own session commits what it wrote
                    elif isinstance(v, ast.Call) and isinstance(v.func, ast.Name) and v.func.id in local_pending:
                        pending[target] = f"the id `{v.func.id}(db, …)` returned at line {st.lineno} (an INSERT with no commit)"
                    elif _insert_execute(v, fn):
                        pending[target] = f"the id its own INSERT returned at line {st.lineno}"
                    elif (src := next((n.id for n in ast.walk(v) if isinstance(n, ast.Name) and n.id in pending), None)):
                        pending[target] = pending[src]
    return found


# ── §17.1867 a fake does what the module does with it ─────────────────────────────────────────────────────────

_MOCK_NAMES = ("MagicMock", "AsyncMock", "Mock", "NonCallableMock", "NonCallableMagicMock")
_SESSION_PARAMS = frozenset({"db", "session", "conn", "connection"})
_LITERALS = (ast.Constant, ast.Dict, ast.List, ast.Tuple, ast.Set, ast.JoinedStr)


def _call_chain(e: ast.AST) -> tuple[str, ...] | None:
    """`a.b.c` -> ("a", "b", "c"); None when the chain does not start at a name."""
    chain: list[str] = []
    while isinstance(e, ast.Attribute):
        chain.insert(0, e.attr)
        e = e.value
    return (e.id, *chain) if isinstance(e, ast.Name) else None


class _Fakes:
    """The fakes one scope of a test file builds: each name's value, each `a.b.return_value = x` chain, each
    `with patch(…) as m` / `@patch(…)` mock parameter. `is_async(patch_call)`: whether a `patch(…)` without
    `new_callable` replaces an `async def` -- `patch` then installs an `AsyncMock` itself."""

    def __init__(self, nodes: list[ast.AST], fn: ast.AST | None = None, is_async=lambda call: False):
        self.is_async = is_async
        self.bound: dict[str, list[tuple[int, ast.AST]]] = {}
        self.chains: dict[tuple[str, ...], list[tuple[int, ast.AST]]] = {}
        for n in nodes:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):   # §17.1869/1870 a local fake
                self.bound.setdefault(n.name, []).append((n.lineno, n))
            elif isinstance(n, ast.Assign) and len(n.targets) == 1:
                t = n.targets[0]
                if isinstance(t, ast.Name):
                    self.bound.setdefault(t.id, []).append((n.lineno, n.value))
                elif (chain := _call_chain(t)) and len(chain) > 1:
                    self.chains.setdefault(chain, []).append((n.lineno, n.value))
            elif isinstance(n, (ast.With, ast.AsyncWith)):
                for item in n.items:
                    if isinstance(item.optional_vars, ast.Name) and isinstance(item.context_expr, ast.Call):
                        self.bound.setdefault(item.optional_vars.id, []).append((n.lineno, item.context_expr))
        # §17.1871 — a name bound to another mock's attribute chain is an alias: what the test sets on it is set on
        # that chain. Live (5b4880c0 T5, 2026-10-30): `mock_client = mock_client_cls.return_value` then
        # `mock_client.submit_order = AsyncMock(…)` -- read as an unconfigured MagicMock, six false refusals.
        for alias_name, hits in list(self.bound.items()):
            for _ln, v in hits:
                chain = _call_chain(v) if isinstance(v, ast.Attribute) else None
                if not chain or len(chain) < 2:
                    continue
                for key, vals in list(self.chains.items()):
                    if key[0] == alias_name and len(key) > 1:
                        self.chains.setdefault((*chain, *key[1:]), []).extend(vals)
        if fn is not None:                                   # `@patch(…)` decorators fill the trailing parameters
            a = fn.args                                      # type: ignore[attr-defined]
            params = [x.arg for x in a.posonlyargs + a.args if x.arg not in ("self", "cls")]
            decos = [d for d in fn.decorator_list if isinstance(d, ast.Call) and _call_name(d) in ("patch", "object")   # type: ignore[attr-defined]
                     and not any(k.arg == "new" for k in d.keywords)]
            for d, name in zip(reversed(decos), params[len(params) - len(decos):], strict=False):
                self.bound.setdefault(name, []).append((fn.lineno, d))        # type: ignore[attr-defined]

    def last(self, name: str, before: int) -> ast.AST | None:
        hits = [v for ln, v in self.bound.get(name, []) if ln <= before]
        return hits[-1] if hits else None

    def chain(self, key: tuple[str, ...], before: int) -> ast.AST | None:
        hits = [v for ln, v in self.chains.get(key, []) if ln <= before]
        return hits[-1] if hits else None


class _Val:
    """A fake as the module will see it: a literal of some kind, a mock of some kind, or unknown; with the test's
    name and attribute path it came through, so a use can look up what the test set there."""
    __slots__ = ("kind", "mock", "node", "root", "path", "line", "rv", "scope", "hops")

    def __init__(self, kind: str | None = None, mock: str | None = None, node: ast.AST | None = None,
                 root: str | None = None, path: tuple[str, ...] = (), line: int = 0, rv: ast.AST | None = None,
                 scope: "_Fakes | None" = None, hops: int = 0):
        self.kind, self.mock, self.node, self.root, self.path, self.line, self.rv = kind, mock, node, root, path, line, rv
        self.scope = scope                       # §17.1869 -- the scope a value came from (a def's own bindings)
        self.hops = hops                         # §17.1873 -- how many resolutions deep this value was reached


_RESOLVE_HOPS = 24


def _resolve(e: ast.AST, fakes: _Fakes, before: int, _hops: int = 0) -> _Val:
    """§17.1873 — every value carries its depth, and `_step` resolves a `return_value` from THAT depth: a mock whose
    `__aenter__` returns itself (`client.__aenter__ = AsyncMock(return_value=client)`, the usual `async with` fake)
    used to re-enter `_resolve` with a fresh budget on every hop and never end. Live (5b4880c0 T5 a1/a2, 2026-10-30):
    `engine_source_gate_error … RecursionError`, every gate silent, two attempts spent in the sandbox unchecked."""
    out = _resolve_at(e, fakes, before, _hops)
    out.hops = max(out.hops, _hops)
    return out


def _resolve_at(e: ast.AST, fakes: _Fakes, before: int, _hops: int) -> _Val:
    e = _unwrap(e)
    if _hops > _RESOLVE_HOPS:
        return _Val()
    if isinstance(e, ast.Name):
        v = fakes.last(e.id, before)
        if v is None:
            return _Val(root=e.id)
        out = _resolve(v, fakes, before, _hops + 1)
        if out.root is None:
            out.root = e.id
        return out
    if isinstance(e, _LITERALS):
        from app.modules.engine_source import _literal_kind
        kind = _literal_kind(e) or ("tuple" if isinstance(e, ast.Tuple) else "set" if isinstance(e, ast.Set) else None)
        return _Val(kind=kind, node=e, line=getattr(e, "lineno", 0))
    if isinstance(e, ast.ClassDef):                                            # §17.1870 a class the test wrote
        return _Val(mock="class", node=e, line=e.lineno)
    if isinstance(e, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):      # §17.1869 a function the test wrote
        rv = e.body if isinstance(e, ast.Lambda) else next(
            (r.value for r in ast.walk(e) if isinstance(r, ast.Return) and r.value is not None), None)
        return _Val(mock="async def" if isinstance(e, ast.AsyncFunctionDef) else "def", node=e, line=e.lineno, rv=rv)
    if isinstance(e, ast.Call):
        name = _call_name(e)
        kw = {k.arg: k.value for k in e.keywords if k.arg}
        if name in _MOCK_NAMES:
            return _Val(mock="AsyncMock" if name == "AsyncMock" else "MagicMock", node=e, line=e.lineno,
                        rv=kw.get("return_value"))
        if name in ("patch", "object") and isinstance(e.func, (ast.Name, ast.Attribute)):
            if (new := _patch_new(e)) is not None:
                return _resolve(new, fakes, before, _hops + 1)
            nc = kw.get("new_callable")
            mock = ("AsyncMock" if isinstance(nc, ast.Name) and nc.id == "AsyncMock" else "MagicMock") if nc is not None \
                else ("AsyncMock" if fakes.is_async(e) else "MagicMock")
            return _Val(mock=mock, node=e, line=e.lineno, rv=kw.get("return_value"))
        if isinstance(e.func, ast.Attribute):
            return _Val()                                    # `patch.dict(…)`, `json.dumps(…)`: not read
        if isinstance(e.func, ast.Name) and isinstance(fakes.last(e.func.id, before),
                                                       (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            made = _resolve(e.func, fakes, before, _hops + 1)                 # §17.1870 `FakeClient()` / `make()`
            return _step(made, ("call", False), fakes, before)
        from app.modules.engine_source import _literal_kind
        kind = _literal_kind(e)
        return _Val(kind=kind, node=e, line=e.lineno) if kind else _Val()
    if isinstance(e, ast.Attribute):
        chain = _call_chain(e)
        if chain and (v := fakes.chain(chain, before)) is not None:
            return _resolve(v, fakes, before, _hops + 1)
        if chain and len(chain) == 2:
            base = _resolve(ast.Name(id=chain[0], ctx=ast.Load()), fakes, before, _hops + 1)
            if base.mock:
                return _step(base, ("attr", chain[1]), fakes, before)
    return _Val()


def _fits(fn: ast.AST, sig: tuple[int, frozenset[str]] | None, method: bool) -> str:
    """§17.1870 — why a call of shape `sig` (positional count, keyword names) does not fit a function the test wrote;
    empty when it does. Live (5b4880c0 T5, 2026-10-30): `class FakeClient: async def get(self, url)` called by the
    module as `client.get(url, params=…, timeout=30.0)` -- TypeError in the sandbox, eight tests."""
    if sig is None or isinstance(fn, ast.Lambda):
        return ""
    a = fn.args                                                               # type: ignore[attr-defined]
    pos = [x.arg for x in a.posonlyargs + a.args][1 if method else 0:]
    npos, kws = sig
    if npos > len(pos) and a.vararg is None:
        return f"takes {len(pos)} positional argument(s) ({', '.join(pos) or 'none'}); the module passes {npos}"
    allowed = set(pos) | {x.arg for x in a.kwonlyargs}
    unknown = sorted(k for k in kws if k not in allowed)
    if unknown and a.kwarg is None:
        return f"has no parameter {', '.join(f'`{k}`' for k in unknown)} (it takes {', '.join(pos + [x.arg for x in a.kwonlyargs]) or 'nothing'}); the module passes {', '.join(f'`{k}=`' for k in unknown)}"
    return ""


def _class_attr(cls: ast.ClassDef, name: str) -> ast.AST | None:
    """A method, a class-level binding, or what `__init__` stores as `self.<name>`; None when the class lacks it."""
    for n in cls.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return n
        if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in n.targets):
            return n.value
        if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and n.target.id == name and n.value is not None:
            return n.value
    init = next((n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "__init__"), None)
    for n in ast.walk(init) if init else ():
        if isinstance(n, ast.Assign) and any(isinstance(t, ast.Attribute) and t.attr == name and isinstance(t.value, ast.Name)
                                             and t.value.id == "self" for t in n.targets):
            return n.value
    return None


def _step(val: _Val, step: tuple[str, str | bool], fakes: _Fakes, before: int,
          sig: tuple[int, frozenset[str]] | None = None) -> _Val:
    """What the module gets after one use of a fake: `("attr", "get")`, `("call", awaited)`. §17.1870 — the mock
    model is the measured one (`python -c` in the dev image): every attribute of an `AsyncMock` is an `AsyncMock`
    and of a `MagicMock` a `MagicMock`; awaiting an `AsyncMock` call gives its `return_value` (an `AsyncMock` by
    default); an `AsyncMock` called without `await` is a coroutine; entering either kind with `async with` gives an
    `AsyncMock`; a `MagicMock` call awaited fails. Live (5b4880c0 T5, 2026-10-30): the old model said MagicMock in
    the last three places -- right by accident twice (`json()` on an `AsyncMock` response is a coroutine the module
    reads as a dict), wrong in the text every time."""
    what, arg = step
    if val.mock == "class":                                                   # §17.1870 a class the test wrote
        if what != "call":
            return _Val()
        return _Val(mock="instance", node=val.node, line=val.line, scope=val.scope)
    if val.mock == "instance":
        cls = val.node
        if what == "call":                                                    # an instance with `__call__` is callable
            dunder = _class_attr(cls, "__call__")                            # type: ignore[arg-type]
            if isinstance(dunder, (ast.FunctionDef, ast.AsyncFunctionDef)):
                rv = next((r.value for r in ast.walk(dunder) if isinstance(r, ast.Return) and r.value is not None), None)
                return _step(_Val(mock="async method" if isinstance(dunder, ast.AsyncFunctionDef) else "method",
                                  node=dunder, line=dunder.lineno, rv=rv, scope=val.scope), step, fakes, before, sig)
            return _Val(kind="instance-called", node=cls, line=val.line)
        if what != "attr":
            return _Val()
        member = _class_attr(cls, str(arg))                                  # type: ignore[arg-type]
        if member is None:
            return _Val(kind=f"missing:{arg}", node=cls, line=val.line)
        if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
            rv = next((r.value for r in ast.walk(member) if isinstance(r, ast.Return) and r.value is not None), None)
            return _Val(mock="async method" if isinstance(member, ast.AsyncFunctionDef) else "method", node=member,
                        line=member.lineno, rv=rv, scope=val.scope)
        return _resolve(member, val.scope or fakes, 10 ** 9, val.hops + 1)
    if val.mock in ("def", "async def", "method", "async method"):           # §17.1869 a function the test wrote
        if what != "call":
            return _Val()
        is_async = val.mock.startswith("async")
        if (bad := _fits(val.node, sig, val.mock.endswith("method"))):       # type: ignore[arg-type]
            return _Val(kind="signature", node=val.node, line=val.line, rv=ast.Constant(value=bad))
        if is_async and not arg:
            return _Val(kind="coroutine", node=val.node, line=val.line)
        if not is_async and arg:
            return _Val()                                    # awaiting what a plain def returns: not followed
        if val.rv is None:
            return _Val(kind="None", node=val.node, line=val.line)
        inner = _Fakes([x for x in ast.walk(val.node) if isinstance(x, ast.stmt)], None, fakes.is_async) \
            if isinstance(val.node, (ast.FunctionDef, ast.AsyncFunctionDef)) else _Fakes([], None, fakes.is_async)
        # §17.1873 — a name the def binds itself is its own: the caller's binding of the same name is not merged in.
        # Live (5b4880c0 T5 a1/a2): `def _fake_async_client(): client = MagicMock(); …; return client` and the test's
        # `client = _fake_async_client(…)` -- the caller's later line won `last("client")`, resolved to the call, into
        # the def, to its `client` again, without end (RecursionError, every gate silent, the sandbox ran unchecked)
        own_names = set(inner.bound)
        for k, v in fakes.bound.items():
            if k not in own_names:
                inner.bound.setdefault(k, []).extend(v)
        for k, v in fakes.chains.items():
            if not (k and k[0] in own_names):
                inner.chains.setdefault(k, []).extend(v)
        out = _resolve(val.rv, inner, 10 ** 9, val.hops + 1)
        out.scope = out.scope or inner
        return out
    if not val.mock:
        return _Val()
    fk = val.scope or fakes
    path = val.path + ((arg,) if what == "attr" else ("return_value",))     # type: ignore[operator]
    entering = bool(val.path) and val.path[-1] in ("__aenter__", "__aexit__")
    if what == "call" and arg and val.mock == "MagicMock" and not entering:
        # §17.1873 — awaited, a MagicMock fails whatever its `return_value` (measured: `await MagicMock(return_value=x)()`
        # is a TypeError); the chain and `return_value` hops below used to come first and follow the value instead
        return _Val(kind="MagicMock-awaited", node=val.node, root=val.root, path=val.path, line=val.line, scope=val.scope)
    if val.root and (v := fk.chain((val.root, *path), before)) is not None:
        out = _resolve(v, fk, before, val.hops + 1)
        if out.root is None:
            out.root, out.path = val.root, path
        out.scope = out.scope or val.scope
        return out
    if what == "call" and val.rv is not None:                                # `MagicMock(return_value=x)` called
        out = _resolve(val.rv, fk, before, val.hops + 1)
        if out.root is None:
            out.root, out.path = val.root, path
        out.scope = out.scope or val.scope
        return out
    if what == "call" and entering:                                           # entering either kind: an AsyncMock
        # §17.1875 — measured: `async with MagicMock() as c` yields a NEW AsyncMock, not the mock, unless the test set
        # its `__aenter__`; what the test set on the mock itself (`get`, `aclose`) is then never reached. Live
        # (5b4880c0 T5, 2026-10-30, four attempts, eight redraws): the finding named the response's `json()` as a
        # coroutine and asked for a MagicMock response -- the response WAS one; the client was never entered.
        prefix = (val.root, *val.path[:-1]) if val.root else None
        set_on = sorted({k[len(prefix)] for k in fk.chains                          # type: ignore[arg-type]
                         if prefix and len(k) > len(prefix) and k[:len(prefix)] == prefix
                         and k[len(prefix)] not in ("__aenter__", "__aexit__")}) if prefix else []
        if set_on and val.path[-1] == "__aenter__":
            return _Val(kind="entered-anew:" + ", ".join(set_on), node=val.node, root=val.root, path=val.path,
                        line=val.line, scope=val.scope)
        return _Val(mock="AsyncMock", node=val.node, root=val.root, path=path, line=val.line, scope=val.scope)
    if what == "call" and val.mock == "AsyncMock" and not arg:                # a coroutine, never awaited
        return _Val(kind="coroutine-mock", node=val.node, root=val.root, path=val.path, line=val.line, scope=val.scope)
    return _Val(mock=val.mock, node=val.node, root=val.root, path=path, line=val.line, scope=val.scope)


def _uses_empty_(u: dict) -> bool:
    return not (u["attrs"] or u["calls"] or u["called"] or u["as"])


def uses_calls_of(uses: dict, attr: str) -> set[str]:
    return set(uses["calls"]) if attr in uses["calls"] else set()


_CANNOT = {"str": "a str", "int": "an int", "float": "a float", "bool": "a bool", "None": "None", "tuple": "a tuple",
           "list": "a list", "dict": "a dict", "set": "a set", "date": "a date", "datetime": "a datetime",
           "Decimal": "a Decimal"}
_DICT_OK = frozenset({"get", "keys", "items", "values", "pop", "setdefault", "update", "copy"})


def _dict_literal_misuse(lit: ast.Dict, keys: dict, where: str) -> tuple[str, str] | None:
    """§17.1876/1879 — a literal dict against the keys the module reads from it, level by level: none of them
    there; a list (a str, a number) under a key the module reads as a dict; the same one level down. Live (5b4880c0
    T5, 2026-10-31): `json()` -> `{"filings": [...]}` where `_find_13f_hr_rows` reads `.get("filings", {}).get("recent")`
    -- "'list' object has no attribute 'get'" in the sandbox, two tests an attempt, three attempts."""
    from app.modules.engine_source import _literal_kind
    given = {k.value: v for k, v in zip(lit.keys, lit.values, strict=False)
             if isinstance(k, ast.Constant) and isinstance(k.value, str)}
    if len(given) != len(lit.keys) or not given or not keys:
        return None
    reads = " / ".join(f"`{k}`" for k in sorted(keys))
    if not set(given) & set(keys):
        return (f"a dict{where} with keys {', '.join(f'`{k}`' for k in given)}",
                f", which the module reads for {reads} -- none of them is there")
    for k in sorted(set(given) & set(keys)):
        sub, v = keys[k], given[k]
        kind = _literal_kind(v)
        if (sub.get("as") == "dict" or sub.get("keys")) and kind in ("list", "str", "int", "float", "bool", "None", "tuple"):
            return (f"a dict{where} whose `{k}` is {_CANNOT.get(kind, kind)}",
                    f", which the module reads as a dict (`.get(…)` / `[\"key\"]`"
                    f"{(' for ' + ' / '.join(f'`{x}`' for x in sorted(sub['keys']))) if sub.get('keys') else ''})")
        if isinstance(v, ast.Dict) and sub.get("keys") and (deeper := _dict_literal_misuse(v, sub["keys"], f'{where}["{k}"]')):
            return deeper
    return None


def _check_uses(val: _Val, uses: dict, fakes: _Fakes, before: int, done: str = "") -> str | None:
    """The first thing the module does with the fake that the fake cannot do, as "a fake whose `get()` returns a
    fake whose `json()` returns a list (line 290), which the module reads as a dict"; None when every use fits (or
    the fake is unknown)."""
    at = f" (line {val.line})" if val.line else ""
    if val.kind and val.kind.startswith("entered-anew:"):                    # §17.1875
        names = val.kind.split(":", 1)[1]
        return (f"{done}a NEW AsyncMock{at}: a mock's `__aenter__()` returns a fresh mock, not the mock itself, so what "
                f"the test set on the object the module enters (`{names}`) is never reached -- on that fake set "
                "`fake.__aenter__ = AsyncMock(return_value=fake)` and `fake.__aexit__ = AsyncMock(return_value=False)`")
    if val.kind == "MagicMock-awaited":
        return f"{done}a `MagicMock`{at}, which the module awaits -- a MagicMock cannot be awaited: make it an `AsyncMock`"
    if val.kind in ("coroutine", "coroutine-mock"):                          # §17.1869/1870
        if _uses_empty_(uses):
            return None                                      # called and dropped: a warning at most, never a failure
        needs = [m for m in sorted(uses["calls"])] + sorted(uses["attrs"]) + (["()"] if uses["called"] else [])
        how = ("enters it with `async with`" if "__aenter__" in needs else f"calls `.{needs[0]}()` on it"
               if needs and needs[0] in uses["calls"] else f"reads `.{needs[0]}` from it" if needs
               else "reads it as a dict" if uses["as"] == "dict" else "iterates it")
        if val.kind == "coroutine":
            return (f"{done}a coroutine{at}: the fake is `async def`, and the module calls it as a plain function, "
                    f"then {how} -- make the fake a plain `def` (or a `MagicMock`) returning the object")
        return (f"{done}a coroutine{at}: an `AsyncMock` method called without `await` (every attribute of an "
                f"`AsyncMock` is one), and the module {how} -- make that object a `MagicMock` (its plain calls "
                "answer at once; give the methods the module awaits an `AsyncMock` each)")
    if val.kind == "signature":                                               # §17.1870
        fn = val.node
        return (f"{done}`{getattr(fn, 'name', 'a function')}(…)`{at}, which {val.rv.value}"       # type: ignore[union-attr]
                " -- accept what the module passes (`*args, **kwargs` is enough)")
    if val.kind and val.kind.startswith("missing:"):                          # §17.1870
        attr = val.kind.split(":", 1)[1]
        cls = getattr(val.node, "name", "the class")
        how = ("enters it with `async with` -- it needs `async def __aenter__(self): return self` and "
               "`async def __aexit__(self, *exc): pass`" if attr == "__aenter__" else
               "enters it with `with` -- it needs `__enter__` and `__exit__`" if attr == "__enter__" else
               f"calls `.{attr}()` on it" if attr in uses_calls_of(uses, attr) else f"reads `.{attr}` from it")
        return f"{done}a `{cls}` instance{at}, which has no `{attr}`, and the module {how}"
    if val.kind == "instance-called":
        return f"{done}a `{getattr(val.node, 'name', 'class')}` instance{at}, which the module calls"
    if val.kind:
        needs = [m + "()" for m in sorted(uses["calls"])] + sorted(uses["attrs"]) + (["()"] if uses["called"] else [])
        if val.kind == "dict":
            needs = [n for n in needs if n.rstrip("()") not in _DICT_OK]
        if needs:
            return (f"{done}{_CANNOT.get(val.kind, 'a ' + val.kind)}{at}, and the module "
                    + ("calls it" if needs[0] == "()" else "enters it with `async with`" if needs[0] == "__aenter__()"
                       else "enters it with `with`" if needs[0] == "__enter__()"
                       else f"calls `.{needs[0]}` on it" if needs[0].endswith("()") else f"reads `.{needs[0]}` from it"))
        if uses["as"] == "dict" and val.kind in ("list", "tuple", "str", "int", "float", "bool", "set", "None"):
            return f"{done}{_CANNOT[val.kind]}{at}, which the module reads as a dict (`.get(…)` / `[\"key\"]`)"
        if val.kind == "dict" and uses.get("keys") and isinstance(val.node, ast.Dict) and val.node.keys:  # §17.1876
            if (bad := _dict_literal_misuse(val.node, uses["keys"], "")) is not None:
                return f"{done}{bad[0]}{at}{bad[1]}"
        return None
    if not val.mock:
        return None
    if uses["called"]:
        nxt = _step(val, ("call", uses["called"]["await"]), fakes, before)
        return _check_uses(nxt, uses["called"]["then"], fakes, before, done + "a fake whose call returns ")
    for m, info in sorted(uses["calls"].items(), key=lambda mi: mi[1].get("line", 0)):
        got = _step(val, ("attr", m), fakes, before)
        if got.kind and (got.kind.startswith("missing:") or got.kind == "instance-called"):
            return _check_uses(got, uses, fakes, before, done)
        nxt = _step(got, ("call", info["await"]), fakes, before, sig=info.get("sig"))
        if nxt.kind == "signature":
            return _check_uses(nxt, info["then"], fakes, before, done + f"a fake whose `{m}` is ")
        bad = _check_uses(nxt, info["then"], fakes, before, done + f"a fake whose `{m}()` returns ")
        if bad:
            return bad
    for a in sorted(uses["attrs"]):                                           # §17.1870 an attribute a class lacks
        got = _step(val, ("attr", a), fakes, before)
        if got.kind and got.kind.startswith("missing:"):
            return _check_uses(got, uses, fakes, before, done)
        if a in (uses.get("bound") or ()) and got.mock and got.kind is None:   # §17.1878 bound to a column
            return (f"{done}a fake whose `.{a}` is a mock{at}: the module writes `.{a}` to a database column, and a "
                    f"mock is not a value -- set `fake.{a} = \"…\"` on the object the call returns "
                    f"(`AsyncMock(return_value=SimpleNamespace({a}=\"…\"))`)")
        if got.mock and got.kind is None and a not in uses["calls"] and (given := _set_on(val, fakes)) and a not in given:
            # §17.1879 — the test set fields on this fake, and not the one the module reads. Live (5b4880c0 T5,
            # 2026-10-31): `MagicMock(content=content)` where the FINRA downloader returns `response.text` --
            # `io.StringIO(<MagicMock>)`, "initial_value must be str or None", two tests an attempt, three attempts
            return (f"{done}a fake with {', '.join(f'`{x}`' for x in sorted(given))} set{at}, where the module reads "
                    f"`.{a}` -- set `fake.{a} = …` too (the index shows what it reads)")
    return None


def _set_on(val: _Val, fakes: _Fakes) -> set[str]:
    """§17.1879 — the attribute names a test set on this fake: `MagicMock(content=…)` keywords and `fake.x = …` chains
    under the name and path it came through; empty when nothing was set (a bare mock is read as before)."""
    out: set[str] = set()
    if isinstance(val.node, ast.Call):
        out |= {str(k.arg) for k in val.node.keywords if k.arg and k.arg not in ("return_value", "side_effect", "spec",
                                                                                   "spec_set", "name", "wraps")}
    if val.root:
        prefix = (val.root, *val.path)
        out |= {k[len(prefix)] for k in fakes.chains if len(k) > len(prefix) and k[:len(prefix)] == prefix
                and k[len(prefix)] not in ("return_value", "side_effect", "__aenter__", "__aexit__")}
    return out


def _result_uses(code: str, matches, local_of=None) -> dict | None:
    """What a module does with what a call returns, over every `x = [await] <call>` whose call `matches`: a `called`
    use, or None when the module never binds such a result."""
    from app.modules.engine_source import _local_functions, _merge_uses, _new_use, _uses
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None
    local = _local_functions(tree)
    root = _new_use()
    klass: dict[int, ast.ClassDef] = {}
    for c in tree.body:
        if isinstance(c, ast.ClassDef):
            for m in c.body:
                klass[id(m)] = c

    def take(fn: ast.AST, name: str, awaited: bool, result: dict | None = None) -> None:
        if name.startswith("self.") and id(fn) in klass:
            # §17.1869 — `self._client = httpx.AsyncClient(…)`: what every method does with `self._client`, and what
            # the callers of a method that returns it do with that (live: `_ensure_client` returns it to `_request`)
            c = klass[id(fn)]
            methods = {m.name: m for m in c.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))}
            sub = _new_use()
            for m in methods.values():
                returns_it = any(isinstance(r, ast.Return) and r.value is not None and ast.unparse(r.value) == name
                                 for r in _own_nodes(m))
                res = None
                if returns_it:
                    outer = _result_uses(code, lambda cl, mn=m.name: isinstance(cl.func, ast.Attribute)
                                         and cl.func.attr == mn and isinstance(cl.func.value, ast.Name)
                                         and cl.func.value.id == "self")
                    res = outer["called"]["then"] if outer else None
                _merge_uses(sub, _uses(m, name, methods, res))
        else:
            sub = _uses(fn, name, local_of(fn) if local_of else local, result)
        if not root["called"]:
            root["called"] = {"await": awaited, "then": _new_use()}
        root["called"]["await"] = root["called"]["await"] or awaited
        _merge_uses(root["called"]["then"], sub)

    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for n in _own_nodes(fn):
            if (isinstance(n, ast.Assign) and len(n.targets) == 1
                    and isinstance((call := _unwrap(n.value)), ast.Call) and matches(call)):
                t = n.targets[0]
                if isinstance(t, ast.Name):
                    take(fn, t.id, isinstance(n.value, ast.Await))
                elif isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) and t.value.id == "self":
                    take(fn, f"self.{t.attr}", isinstance(n.value, ast.Await))
            elif isinstance(n, (ast.With, ast.AsyncWith)):                   # §17.1869 `async with X() as c:`
                for item in n.items:
                    call = _unwrap(item.context_expr)
                    if isinstance(call, ast.Call) and matches(call):
                        entered = _new_use()
                        enter = "__aenter__" if isinstance(n, ast.AsyncWith) else "__enter__"
                        then = _new_use()
                        if isinstance(item.optional_vars, ast.Name):
                            then = _uses(fn, item.optional_vars.id, local_of(fn) if local_of else local)
                        entered["calls"][enter] = {"await": isinstance(n, ast.AsyncWith), "then": then, "line": n.lineno}
                        if not root["called"]:
                            root["called"] = {"await": isinstance(item.context_expr, ast.Await), "then": _new_use()}
                        _merge_uses(root["called"]["then"], entered)
    return root if root["called"] else None


def _calls_into(code: str, mod: str, fn: ast.AST) -> bool:
    """§17.1872 — whether test function `fn` calls a name of job module `mod` directly."""
    lo, hi = fn.lineno, getattr(fn, "end_lineno", None) or fn.lineno
    return any(m == mod and lo <= ln <= hi for m, _f, ln in _test_calls(code))


def _asserts_on(fn: ast.AST, root: str | None) -> bool:
    """§17.1872 — whether the test asserts a mock bound to `root` was called (`m.assert_called…`, `m.call_args`,
    `m.called`, `m.await_count`): it expects the path through the mock to run."""
    if not root:
        return False
    for n in _own_nodes(fn):
        if isinstance(n, ast.Attribute) and (n.attr.startswith("assert_") or n.attr in (
                "called", "call_args", "call_args_list", "call_count", "await_count", "await_args", "awaited")):
            base = n.value
            while isinstance(base, ast.Attribute):
                base = base.value
            if isinstance(base, ast.Name) and base.id == root:
                return True
    return False


def _calls_raise_for_status(code: str) -> str | None:
    """§17.1872 — the first function of a module that calls `.raise_for_status()`; None when none does."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
                isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute) and c.func.attr == "raise_for_status"
                for c in _own_nodes(fn)):
            return fn.name
    return None


def responses_without_a_request(files: dict[str, str], modules: dict[str, str]) -> list[str]:
    """§17.1872 — a test builds an `httpx.Response(…)` without `request=` for a module that calls
    `raise_for_status()` on the response it is given: httpx refuses that call on a response that never had a request
    (`RuntimeError: Cannot call raise_for_status as the request instance has not been set on this response`). Live
    (5b4880c0 T5U2 a1, 2026-10-30): `return httpx.Response(status_code, text=text_content)` handed as `http_get`, nine
    of fourteen tests failed in the sandbox on `_download_file`'s `response.raise_for_status()` -- one attempt spent.
    A response whose `request` the test sets afterwards (`r.request = …`) is left alone."""
    found: list[str] = []
    callers = {m: f for m, c in modules.items() if (f := _calls_raise_for_status(c))}
    if not callers:
        return found
    for path, code in files.items():
        if not path.startswith("tests/"):
            continue
        try:
            tree = ast.parse(code)
        except SyntaxError:
            continue
        under_test = [m for m in callers if re.search(rf"\b{re.escape(m)}\b", code)]
        if not under_test:
            continue
        if any(isinstance(n, ast.Assign) and any(isinstance(t, ast.Attribute) and t.attr == "request" for t in n.targets)
               for n in ast.walk(tree)):
            continue
        direct = {a.asname or a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module == "httpx"
                  for a in n.names if a.name == "Response"}
        for n in ast.walk(tree):
            if not isinstance(n, ast.Call):
                continue
            f = n.func
            is_resp = (isinstance(f, ast.Attribute) and f.attr == "Response" and isinstance(f.value, ast.Name)
                       and f.value.id == "httpx") or (isinstance(f, ast.Name) and f.id in direct)
            if not is_resp or any(k.arg == "request" for k in n.keywords):
                continue
            mod = under_test[0]
            found.append(f"`{path}:{n.lineno}` builds an `httpx.Response(…)` without `request=`: "
                         f"`{mod}.{callers[mod]}` calls `raise_for_status()` on the response it is given, and httpx "
                         "refuses that on a response that never had a request (`RuntimeError: Cannot call "
                         "raise_for_status as the request instance has not been set on this response`) -- pass "
                         '`request=httpx.Request("GET", url)` to `httpx.Response(…)`')
    return found


def _use_sites(code: str, owner: str, name: str) -> set[str]:
    """§17.1871 — the functions of a module in which a patched name's call result is bound."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return set()
    dotted = f"{owner}.{name}" if owner else name
    out: set[str] = set()
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for n in _own_nodes(fn):
            call = None
            if isinstance(n, ast.Assign):
                call = _unwrap(n.value)
            elif isinstance(n, (ast.With, ast.AsyncWith)):
                call = next((_unwrap(i.context_expr) for i in n.items), None)
            if isinstance(call, ast.Call) and ast.unparse(call.func) == dotted:
                out.add(fn.name)
    return out


def _default_for(code: str, fname: str) -> str | None:
    """§17.1871 — the parameter `fname` stands in for when it is not given: `getter = http_get or _default_http_get`
    -> "http_get". None when the function is reached any other way. Live (5b4880c0 T5, 2026-10-30): a test handed
    `http_get=` and also patched `httpx.AsyncClient`, which only `_default_http_get` calls -- refused for a fake the
    tested path never touches."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None
    param: str | None = None
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) or fn.name == fname:
            continue
        a = fn.args
        params = {x.arg for x in a.posonlyargs + a.args + a.kwonlyargs}
        for n in _own_nodes(fn):
            if not (isinstance(n, ast.Name) and n.id == fname and isinstance(n.ctx, ast.Load)):
                continue
            # `p or fname` / `fname if p is None else p` / `p if p is not None else fname`
            parent = next((x for x in ast.walk(fn) if any(c is n for c in ast.iter_child_nodes(x))), None)
            ok = False
            if isinstance(parent, ast.BoolOp) and isinstance(parent.op, ast.Or) and parent.values[-1] is n \
                    and isinstance(parent.values[0], ast.Name) and parent.values[0].id in params:
                ok, cand = True, parent.values[0].id
            elif isinstance(parent, ast.IfExp):
                names = [x.id for x in ast.walk(parent.test) if isinstance(x, ast.Name) and x.id in params]
                ok, cand = bool(names), (names[0] if names else None)
            elif isinstance(parent, ast.Call) and parent.func is n:
                continue                                     # a direct call: not a default
            if not ok:
                return None
            if param is not None and param != cand:
                return None
            param = cand
    return param


def _target_uses(code: str, owner: str, name: str) -> dict | None:
    """What a module does with what a patched name returns: `values_for(…)`, `httpx.AsyncClient(…)`, or
    `self.meth(…)` inside class `owner`."""
    if owner and "." not in owner and owner[:1].isupper():
        return _result_uses(code, lambda c: isinstance(c.func, ast.Attribute) and c.func.attr == name
                            and isinstance(c.func.value, ast.Name) and c.func.value.id == "self")
    dotted = f"{owner}.{name}" if owner else name
    return _result_uses(code, lambda c: ast.unparse(c.func) == dotted)


def _patch_sites(code: str) -> list[tuple[ast.Call, str, str, str, ast.AST | None]]:
    """Each `patch(…)` / `patch.object(…)` in a test file: (the call, module, owner ("" or a class / an attribute
    chain the name hangs on), name, the enclosing function)."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []
    alias = _module_aliases(tree)
    owner: dict[int, ast.AST] = {}
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for x in _own_nodes(fn):
                owner[id(x)] = fn
            for d in fn.decorator_list:
                for x in ast.walk(d):
                    owner[id(x)] = fn
    out = []
    for n in ast.walk(tree):
        if not (isinstance(n, ast.Call) and _call_name(n) in ("patch", "object", "setattr") and n.args):
            continue
        site = _patch_target(n, alias)
        if site:
            out.append((n, *site, owner.get(id(n))))
    return out


def _patch_new(call: ast.Call) -> ast.AST | None:
    """§17.1869 — the value a patch installs when it is given one: `patch(t, new)`, `patch(t, new=x)`,
    `patch.object(o, "n", new)`, `monkeypatch.setattr("t", x)` / `monkeypatch.setattr(o, "n", x)`."""
    kw = {k.arg: k.value for k in call.keywords if k.arg}
    if "new" in kw:
        return kw["new"]
    a0 = call.args[0] if call.args else None
    string_target = isinstance(a0, ast.Constant) and isinstance(a0.value, str)
    want = 2 if string_target else 3
    return call.args[want - 1] if len(call.args) >= want else None


def _module_aliases(tree: ast.AST) -> dict[str, str]:
    alias: dict[str, str] = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module == "app.modules":
            alias.update({a.asname or a.name: a.name for a in n.names})
        elif isinstance(n, ast.Import):
            alias.update({a.asname: a.name.rsplit(".", 1)[1] for a in n.names
                          if a.asname and a.name.startswith("app.modules.")})
    return alias


def _patch_target(call: ast.Call, alias: dict[str, str]) -> tuple[str, str, str] | None:
    """(module, owner, name) a `patch(…)` / `patch.object(…)` call replaces, or None."""
    a0 = call.args[0] if call.args else None
    if isinstance(a0, ast.Constant) and isinstance(a0.value, str) and a0.value.startswith("app.modules."):
        parts = a0.value.split(".")
        if len(parts) >= 4:
            return parts[2], ".".join(parts[3:-1]), parts[-1]
    elif a0 is not None and len(call.args) >= 2 and isinstance(call.args[1], ast.Constant) \
            and isinstance(call.args[1].value, str):
        chain = _call_chain(a0)
        if chain and chain[0] in alias:
            return alias[chain[0]], ".".join(chain[1:]), call.args[1].value
    return None


def _async_in(reach: dict[str, str], mod: str, owner: str, name: str) -> bool:
    """§17.1867 — whether `patch("app.modules.<mod>.<owner>.<name>")` replaces an `async def` (`patch` then installs
    an `AsyncMock` itself): defined in the module, a method of one of its classes, or imported from the engine."""
    code = reach.get(mod)
    if not code:
        return False
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return False
    if owner and "." not in owner:
        cls = next((n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == owner), None)
        return any(isinstance(m, ast.AsyncFunctionDef) and m.name == name for m in (cls.body if cls else []))
    if owner:
        return False
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return isinstance(n, ast.AsyncFunctionDef)
        if isinstance(n, ast.ImportFrom) and n.module and any((a.asname or a.name) == name for a in n.names):
            src = next(a.name for a in n.names if (a.asname or a.name) == name)
            if n.module.startswith("app.modules.") and n.module.rsplit(".", 1)[1] in reach:
                return _async_in(reach, n.module.rsplit(".", 1)[1], "", src)
            from app.modules.engine_source import module_index
            return bool((module_index().get(n.module) or {}).get("funcs", {}).get(src, {}).get("async"))
    return False


def wrong_fakes(files: dict[str, str], modules: dict[str, str], own: set[str]) -> list[str]:
    """§17.1867 — each fake a test hands a job function (or patches into a job module) that cannot do what the module
    does with it: a literal where attributes are read or methods called, a list where a dict is read, a `MagicMock`
    where the module awaits. Read against the module's own uses (`_uses`), the fake resolved through the test's
    names, `return_value` chains and `patch(…)` arguments. Live (5b4880c0 T5, 2026-10-29): every attempt -- an
    `http_get` returning `"text"` or `(200, "text")` where `_download_file` calls `.raise_for_status()`; a client
    whose `get().json()` returned a list where `_fetch_house_disclosures` reads `data.get("results")`; a patched
    `_ensure_client` returning a `MagicMock` whose `.request(…)` the module awaits; the entry point's patched
    `AlpacaPaperClient` whose `.close()` it awaits (named by the sandbox, three attempts running)."""
    from app.modules.engine_source import _call_shape, _local_functions, _own_statements, _uses_text
    reach = {**modules, **{p.rsplit("/", 1)[1][:-3]: c for p, c in files.items()
                          if p.startswith("app/") and p.endswith(".py")}}
    found: list[str] = []
    for path, code in files.items():
        if not path.startswith("tests/"):
            continue
        try:
            tree = ast.parse(code)
        except SyntaxError:
            continue
        alias = _module_aliases(tree)

        def is_async(call: ast.Call) -> bool:
            t = _patch_target(call, alias)
            return bool(t) and _async_in(reach, *t)

        mod_fakes = _Fakes(tree.body, None, is_async)
        fns = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        by_line = {(ln, f): m for m, f, ln in _test_calls(code)}
        seen: set[tuple[int, str]] = set()
        shapes: dict[tuple[str, str], dict] = {}

        def fakes_for(fn: ast.AST | None) -> _Fakes:
            if fn is None:
                return mod_fakes
            fk = _Fakes([x for x in _own_statements(fn) if isinstance(x, ast.stmt)], fn, is_async)
            for k, v in mod_fakes.bound.items():
                fk.bound.setdefault(k, []).extend(v)
            return fk

        def say(line: int, what: str, bad: str, uses_text: str) -> None:
            if (line, what) in seen:
                return
            seen.add((line, what))
            found.append(f"`{path}:{line}` {what} {bad} -- give it what the module uses: {uses_text}")

        raises = _expects_a_raise(tree)                      # §17.1870 -- a call there is meant to fail early
        for fn in fns:
            fk = fakes_for(fn)
            for call in (c for c in _own_nodes(fn) if isinstance(c, ast.Call)):
                mod = by_line.get((call.lineno, _call_name(call)))
                if not mod or mod not in reach:
                    continue
                if any(a <= call.lineno <= b for a, b in raises):
                    continue
                key = (mod, _call_name(call))
                if key not in shapes:
                    d = _defs(reach[mod])
                    target = d[0].get(key[1]) if d else None
                    shapes[key] = _call_shape(target, None, _local_functions(ast.parse(reach[mod]))) if target else {}
                if not shapes[key]:
                    continue
                pos = shapes[key]["pos"]
                given = [(pos[i], a) for i, a in enumerate(call.args) if i < len(pos)] + [(k.arg, k.value) for k in call.keywords if k.arg]
                # §17.1869 — a fake session where the sandbox provides a real one. Live (5b4880c0 T5, 2026-10-30, every
                # attempt that reached the sandbox): `fake_db = MagicMock(); fake_db.execute = AsyncMock()` handed as
                # `db=`, then "'MagicMock' object can't be awaited" at the commit, or rows that were never written.
                for param, arg in given:
                    if param in _SESSION_PARAMS and (v := _resolve(arg, fk, call.lineno)).mock in ("MagicMock", "AsyncMock"):
                        say(call.lineno, f"hands `{key[1]}(…)` `{param}` =", f"a `{v.mock}`{f' (line {v.line})' if v.line else ''}: the sandbox's "
                            "`async_session` is a real scratch database with this job's tables, and a fake session answers "
                            "nothing the code reads", "`async with async_session() as db:` and pass that `db`; assert on the rows it wrote")
                uses = shapes[key].get("uses") or {}
                if not uses:
                    continue
                for param, arg in given:
                    if param in uses and (bad := _check_uses(_resolve(arg, fk, call.lineno), uses[param], fk, call.lineno)):
                        say(call.lineno, f"hands `{key[1]}(…)` `{param}` =", bad, f"`{param}{_uses_text(uses[param])}`")
        for pc, mod, owner, name, fn in _patch_sites(code):
            if mod not in reach:
                continue
            if fn is not None and any(fn.lineno <= a and b <= (fn.end_lineno or b) and any(
                    isinstance(c, ast.Call) and a <= c.lineno <= b for c in _own_nodes(fn)) for a, b in raises):
                continue                                     # §17.1870 -- a call of this test is meant to raise first
            # §17.1871 — a name used only as the default for a parameter this test passes is never reached
            if fn is not None and (sites := _use_sites(reach[mod], owner, name)):
                params = {_default_for(reach[mod], site) for site in sites}
                if None not in params and params:
                    given = {k.arg for m_, f_, ln in _test_calls(code) if m_ == mod and fn.lineno <= ln <= (fn.end_lineno or ln)
                             for c in _own_nodes(fn) if isinstance(c, ast.Call) and c.lineno == ln for k in c.keywords}
                    if params <= given:
                        continue
            if name == "async_session" and not owner:
                # §17.1869 — the session itself is never patched: the sandbox's is real (live: 19 patches of it on
                # eight modules in one file, each an `async def` the module cannot `async with`)
                say(pc.lineno, f"patches `async_session` on `{mod}` with", "a fake session: the sandbox's "
                    "`async_session` is a real scratch database with this job's tables, and nothing a fake answers is "
                    "what the code reads", "leave `async_session` alone; insert the rows the function reads and "
                    "assert on the rows it wrote")
                continue
            uses = _target_uses(reach[mod], owner, name)
            if not uses:
                continue
            fk = fakes_for(fn)
            new = _patch_new(pc)
            # the mock the patch installs: the value given, else the `as m` / decorator name (its chains apply), else
            # the call itself (a `setattr` without a value is not a patch)
            alias_name = next((n for n, hits in fk.bound.items() for _ln, v in hits if v is pc), None)
            if new is None and _call_name(pc) == "setattr":
                continue
            val = (_resolve(new, fk, pc.lineno) if new is not None else
                   _resolve(ast.Name(id=alias_name, ctx=ast.Load()), fk, 10 ** 9) if alias_name else _resolve(pc, fk, pc.lineno))
            if (bad := _check_uses(val, uses, fk, 10 ** 9)):
                # §17.1872 — a bare mock the module awaits on a path this test never calls into directly: the test
                # calls another module, which reaches the patched one only on a value it did not pass (live: the
                # approval gate's rejected approval never reaches `execute_order`'s `await client.submit_order(…)`).
                # Configured (`m.return_value = …`, `new=MagicMock(return_value=…)`) or asserted on
                # (`m.assert_called…`), the test expects that path and is held to it; the sandbox names a real one.
                if ("cannot be awaited" in bad and fn is not None and not _calls_into(code, mod, fn)
                        and val.rv is None and not any(k and k[0] == val.root for k in fk.chains)
                        and not _asserts_on(fn, val.root)):
                    continue
                say(pc.lineno, f"patches `{name}` on `{owner or mod}` with", bad, f"`{name}{_uses_text(uses)}`")
    return found


# ── §17.1870 a test reads what a returned class has ───────────────────────────────────────────────────────────

def _class_members(code: str, cname: str) -> set[str] | None:
    """The attributes an instance of `cname` (defined in `code`) has: annotated fields, class-level names, methods,
    properties and what any method stores as `self.x`; None when the class is not there (or inherits)."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None
    cls = next((n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cname), None)
    if cls is None or cls.bases and not all(getattr(b, "id", "") in ("object",) for b in cls.bases):
        return None
    out: set[str] = set()
    for n in cls.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.add(n.name)
            for x in ast.walk(n):
                if isinstance(x, ast.Assign):
                    out |= {t.attr for t in x.targets if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name)
                            and t.value.id == "self"}
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            out.add(n.target.id)
        elif isinstance(n, ast.Assign):
            out |= {t.id for t in n.targets if isinstance(t, ast.Name)}
    return out


def _returned_class(fn: ast.AST) -> str | None:
    """`-> RiskDecision`, `-> Optional[RiskDecision]`, `-> RiskDecision | None` -> "RiskDecision"."""
    ann = getattr(fn, "returns", None)
    if isinstance(ann, ast.Constant) and isinstance(ann.value, str):
        try:
            ann = ast.parse(ann.value, mode="eval").body
        except SyntaxError:
            return None
    if isinstance(ann, ast.BinOp) and isinstance(ann.op, ast.BitOr):
        names = [x for x in (ann.left, ann.right) if not (isinstance(x, ast.Constant) and x.value is None)]
        ann = names[0] if len(names) == 1 else None
    if isinstance(ann, ast.Subscript) and getattr(ann.value, "id", "") == "Optional":
        ann = ann.slice
    return ann.id if isinstance(ann, ast.Name) and ann.id[:1].isupper() else None


def unknown_result_attributes(files: dict[str, str], modules: dict[str, str]) -> list[str]:
    """§17.1870 — each attribute a test reads from what a job function returns -- a class of the job's modules --
    that the class lacks. Live (5b4880c0 T5, 2026-10-30): six tests read `result.passed` from `check_daily_loss(…)`,
    which returns a `RiskDecision` with `allowed` and `reason`; the index had shown the class's fields."""
    reach = {**modules, **{p.rsplit("/", 1)[1][:-3]: c for p, c in files.items()
                          if p.startswith("app/") and p.endswith(".py")}}
    found: list[str] = []
    members_of: dict[tuple[str, str], set[str] | None] = {}
    for path, code in files.items():
        if not path.startswith("tests/"):
            continue
        try:
            tree = ast.parse(code)
        except SyntaxError:
            continue
        by_line = {(ln, f): m for m, f, ln in _test_calls(code)}
        for fn in (n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))):
            bound: dict[str, tuple[str, str, str]] = {}      # name -> (module, function, class)
            for n in _own_nodes(fn):
                if not (isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name)):
                    continue
                call = _unwrap(n.value)
                if not isinstance(call, ast.Call):
                    continue
                mod = by_line.get((call.lineno, _call_name(call)))
                if not mod or mod not in reach:
                    continue
                d = _defs(reach[mod])
                target = d[0].get(_call_name(call)) if d else None
                cname = _returned_class(target) if target is not None else None
                if cname:
                    bound[n.targets[0].id] = (mod, _call_name(call), cname)
            if not bound:
                continue
            for n in sorted((x for x in _own_nodes(fn) if isinstance(x, ast.Attribute) and isinstance(x.value, ast.Name)
                             and x.value.id in bound and isinstance(x.ctx, ast.Load)), key=lambda x: x.lineno):
                mod, f, cname = bound[n.value.id]
                key = (mod, cname)
                if key not in members_of:
                    members_of[key] = _class_members(reach[mod], cname)
                have = members_of[key]
                if have is None or n.attr in have or n.attr.startswith("__"):
                    continue
                msg = (f"`{path}:{n.lineno}` reads `.{n.attr}` from what `{f}` (`{mod}`) returns, a `{cname}`, which has "
                       f"no `{n.attr}` -- it has {', '.join(f'`{a}`' for a in sorted(a for a in have if not a.startswith('_'))) or 'no public attribute'}")
                if msg not in found:
                    found.append(msg)
    return found


# ── §17.1876 a result is what its annotation says; every name a test uses is bound ─────────────────────────────

_COLLECTIONS = frozenset({"list", "dict", "set", "tuple"})
_SCALARS = frozenset({"int", "float", "bool", "Decimal"})
_BUILTIN_KINDS = {"dict": "dict", "list": "list", "set": "set", "tuple": "tuple", "int": "int", "float": "float",
                  "str": "str", "bool": "bool"}


def _result_misuse(n: ast.Name, par: ast.AST | None, kinds: frozenset[str]) -> tuple[str, str] | None:
    """(what the test does with the result, what the function returns instead) for a use its annotation refutes."""
    from app.modules.engine_source import _kinds_text as _kt
    real = kinds - {"None"}

    def _kinds_text(k: frozenset[str]) -> str:                   # with its article: "an `int`", "a `list`"
        t = _kt(k)
        return ("an " if t[:1] in "aeiou" else "a ") + f"`{t}`"
    text = " ".join(ast.unparse(par).split()) if par is not None else n.id
    text = text if len(text) <= 70 else text[:67] + "…"
    if kinds == {"None"}:
        if isinstance(par, ast.Compare) and len(par.ops) == 1 and isinstance(par.ops[0], (ast.Is, ast.IsNot, ast.Eq, ast.NotEq)) \
                and any(isinstance(c, ast.Constant) and c.value is None for c in (par.left, *par.comparators)):
            return None
        return (f"uses `{n.id}` (`{text}`)", "nothing (`-> None`) -- assert on what it did (the rows it wrote, the mock "
                "it called), never on its result")
    if not real:
        return None
    if isinstance(par, ast.Compare) and len(par.ops) == 1:
        other = par.comparators[0] if par.left is n else par.left
        numeric = isinstance(other, ast.Constant) and type(other.value) in (int, float)
        if numeric and real <= _COLLECTIONS:
            return (f"compares `{n.id}` (`{text}`)", f"{_kinds_text(kinds)} -- compare `len({n.id})`, or read its items")
        if numeric and real <= {"str"}:
            return (f"compares `{n.id}` (`{text}`)", f"{_kinds_text(kinds)} -- compare `len({n.id})`, or its text")
    if isinstance(par, ast.Call) and isinstance(par.func, ast.Name) and par.func.id == "isinstance" and len(par.args) == 2 \
            and par.args[0] is n and isinstance(par.args[1], ast.Name) and par.args[1].id in _BUILTIN_KINDS \
            and _BUILTIN_KINDS[par.args[1].id] not in kinds and not (par.args[1].id == "float" and "int" in kinds):
        return (f"asserts `{text}` on `{n.id}`", f"{_kinds_text(kinds)}, never a `{par.args[1].id}`")
    if isinstance(par, ast.Subscript) and par.value is n and real <= _SCALARS:
        return (f"subscripts `{n.id}` (`{text}`)", f"{_kinds_text(kinds)}, which has no items")
    if isinstance(par, ast.Call) and isinstance(par.func, ast.Name) and par.func.id == "len" and par.args \
            and par.args[0] is n and real <= _SCALARS:
        return (f"measures `{n.id}` (`{text}`)", f"{_kinds_text(kinds)}, which has no length")
    return None


def wrong_result_uses(files: dict[str, str], modules: dict[str, str]) -> list[str]:
    """§17.1876 — each test that uses what a job function returns as something its annotation says it is not: the
    result of a `-> None` function asserted on at all; a list or dict compared with a number; `isinstance(r, dict)`
    on a list; an int subscripted. Live (5b4880c0 T5, 2026-10-30, a3/a4): eleven tests asserted
    `isinstance(result, dict)` on `handle_approval_answer(…)`, which returns nothing; `ingest_news_for_ticker(…) >= 0`
    on a list; `ingest_form_4_for_tickers(…) >= 0` on a dict -- sixteen sandbox failures, every one readable here."""
    from app.modules.engine_source import _kinds
    reach = {**modules, **{p.rsplit("/", 1)[1][:-3]: c for p, c in files.items()
                          if p.startswith("app/") and p.endswith(".py")}}
    found: list[str] = []
    for path, code in files.items():
        if not path.startswith("tests/"):
            continue
        try:
            tree = ast.parse(code)
        except SyntaxError:
            continue
        by_line = {(ln, f): m for m, f, ln in _test_calls(code)}
        for fn in (n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))):
            # each name's bindings in source order: a use is governed by the latest one above it (None: not a result)
            binds: dict[str, list[tuple[int, tuple[str, str, frozenset[str]] | None]]] = {}
            for n in sorted((x for x in _own_nodes(fn) if isinstance(x, ast.Assign) and len(x.targets) == 1
                             and isinstance(x.targets[0], ast.Name)), key=lambda x: x.lineno):
                info = None
                call = _unwrap(n.value)
                if isinstance(call, ast.Call) and (mod := by_line.get((call.lineno, _call_name(call)))) and mod in reach:
                    d = _defs(reach[mod])
                    target = d[0].get(_call_name(call)) if d else None
                    kinds = _kinds(getattr(target, "returns", None)) if target is not None else None
                    if kinds:
                        info = (mod, _call_name(call), kinds)
                binds.setdefault(n.targets[0].id, []).append((n.lineno, info))
            if not any(i for b in binds.values() for _, i in b):
                continue
            parents = {id(c): p for p in ast.walk(fn) for c in ast.iter_child_nodes(p)}
            for n in sorted((x for x in _own_nodes(fn) if isinstance(x, ast.Name) and x.id in binds
                             and isinstance(x.ctx, ast.Load)), key=lambda x: x.lineno):
                prior = [i for ln, i in binds[n.id] if ln < n.lineno]
                if not prior or prior[-1] is None:
                    continue
                mod, f, kinds = prior[-1]
                bad = _result_misuse(n, parents.get(id(n)), kinds)
                if not bad:
                    continue
                msg = f"`{path}:{n.lineno}` {bad[0]}, what `{f}(…)` (`{mod}`) returns -- and `{f}` returns {bad[1]}"
                if msg not in found:
                    found.append(msg)
    return found


def undefined_test_names(files: dict[str, str], modules: dict[str, str]) -> list[str]:
    """§17.1876 — each name a test file uses and never binds (imports, defines, assigns, takes as a parameter): a
    NameError the instant the test runs, read with `symtable`, the compiler's own scoping (a file with a star import
    is left alone). Live (5b4880c0 T5 a4, 2026-10-30): `ingest_13f_holdings(…)` twice, its import dropped in a
    redraw; the sandbox blamed "a finished module reaches for a name that does not exist"."""
    import builtins
    import symtable
    known_base = set(dir(builtins)) | {"__file__", "__name__", "__doc__", "__spec__", "__loader__", "__package__",
                                       "__builtins__", "__path__", "__annotations__", "__debug__"}
    found: list[str] = []
    for path, code in files.items():
        if not path.startswith("tests/"):
            continue
        try:
            tree = ast.parse(code)
            top = symtable.symtable(code, path, "exec")
        except SyntaxError:
            continue
        if any(isinstance(n, ast.ImportFrom) and any(a.name == "*" for a in n.names) for n in ast.walk(tree)):
            continue
        known = known_base | {s.get_name() for s in top.get_symbols() if s.is_assigned() or s.is_imported() or s.is_parameter()}
        missing: set[str] = set()

        def walk(t: symtable.SymbolTable) -> None:
            for s in t.get_symbols():
                name = s.get_name()
                if not s.is_referenced() or name in known or s.is_assigned() or s.is_imported() or s.is_parameter() \
                        or s.is_free() or (t.get_type() == "class" and s.is_local()):
                    continue
                missing.add(name)
            for c in t.get_children():
                walk(c)
        walk(top)
        if not missing:
            continue
        where = {name: [m for m, c in sorted(modules.items()) if name in _top_names(c)[0]] for name in missing}
        for n in sorted((x for x in ast.walk(tree) if isinstance(x, ast.Name) and x.id in missing
                         and isinstance(x.ctx, ast.Load)), key=lambda x: x.lineno):
            msg = (f"`{path}:{n.lineno}` uses `{n.id}`, which the file never imports or defines -- `NameError` the "
                   "instant the test runs"
                   + (f"; `from app.modules.{where[n.id][0]} import {n.id}`" if where[n.id] else ""))
            if msg not in found:
                found.append(msg)
    return found


# ── §17.1839 what a test may mock: the exact names through which the job's modules leave the engine ────────────

_KIND = (("app.model_router", "a model call"), ("openai", "a model call"), ("anthropic", "a model call"),
         ("ollama", "a model call"), ("alpaca", "the broker"), ("app.modules.operator_ask", "the operator"),
         ("app.modules.runner_secrets", "a secret"), ("websockets", "the network"), ("httpx", "the network"),
         ("requests", "the network"), ("aiohttp", "the network"), ("urllib", "the network"), ("socket", "the network"))


def _first_use(tree: ast.AST, name: str) -> str:
    """§17.1846 — the statement where a module first calls `name`, so a stand-in returns what it is used as. Live
    (5b4880c0 T6, 2026-10-27): told to mock `generate_json`, the test's stand-in returned one value where the proposer
    unpacks two (`ValueError: not enough values to unpack (expected 2, got 1)`)."""
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for st in ast.walk(fn):
            if isinstance(st, (ast.Assign, ast.AnnAssign, ast.Expr, ast.Return)) and any(
                    isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id == name
                    for c in ast.walk(st)):
                text = " ".join(ast.unparse(st).split())
                return text if len(text) <= 120 else text[:117] + "..."
    return ""


def outside_points(modules: dict[str, str]) -> list[str]:
    """Each job module that leaves the engine itself, with the names it does it through, as patch targets:
    "`paper_trade_proposer`: `app.modules.paper_trade_proposer.generate_json` (a model call)". Live (5b4880c0 T6,
    2026-10-26): told to mock only model calls, the batch could not see that `propose_trades` calls the model through
    `generate_json`, and three of four attempts stood in for the data module, the risk check and the equity record."""
    out: list[str] = []
    for mod in sorted(modules):
        try:
            tree = ast.parse(modules[mod])
        except SyntaxError:
            continue
        pts: list[str] = []
        for n in tree.body:
            if isinstance(n, ast.ImportFrom) and n.module and not n.level:
                src = n.module
                kind = next((k for p, k in _KIND if src == p or src.startswith(p + ".")), None)
                if kind:
                    pts += [f"`app.modules.{mod}.{a.asname or a.name}` ({kind}"
                            + (f"; used as `{use}`" if (use := _first_use(tree, a.asname or a.name)) else "") + ")"
                            for a in n.names]
            elif isinstance(n, ast.Import):
                for a in n.names:
                    kind = next((k for p, k in _KIND if a.name == p or a.name.startswith(p + ".")), None)
                    if kind:
                        pts.append(f"`app.modules.{mod}.{(a.asname or a.name).split('.')[0]}` ({kind})")
        if pts:
            out.append(f"- `{mod}`: " + ", ".join(dict.fromkeys(pts)))
    return out


def outside_block(modules: dict[str, str]) -> str:
    pts = outside_points(modules)
    if not pts:
        return ""
    return ("\n\n## Where this job's modules leave the engine (§17.1839)\nA test mocks these names -- patched where the "
            "module uses them -- and nothing else; every other finished module runs for real against the scratch "
            "database.\n" + "\n".join(pts) + "\n")


# ── §17.1840 what one module writes into a JSON column and another reads back out ──────────────────────────────

_PARAM_RE = re.compile(r"^(?:CAST\s*\(\s*)?:(\w+)(?:\s+AS\s+\w+\s*\))?$", re.I)
_MAX_SITES = 8


def _split_values(vals: str) -> list[str]:
    out, depth, cur = [], 0, ""
    for ch in vals:
        if ch == "," and depth == 0:
            out.append(cur.strip())
            cur = ""
            continue
        depth += ch == "("
        depth -= ch == ")"
        cur += ch
    return [*out, cur.strip()] if cur.strip() else out


def _sql_of(call: ast.Call, fn: ast.AST) -> str | None:
    """The SQL text of `db.execute(text("..."), ...)`, or of `db.execute(sql, ...)` with `sql = text("...")`."""
    if not call.args:
        return None
    a = call.args[0]
    if isinstance(a, ast.Name):
        bound = [n.value for n in _own_nodes(fn) if isinstance(n, ast.Assign) and len(n.targets) == 1
                 and isinstance(n.targets[0], ast.Name) and n.targets[0].id == a.id]
        a = bound[0] if len(bound) == 1 else a
    if isinstance(a, ast.Call) and a.args and isinstance(a.args[0], ast.Constant) and isinstance(a.args[0].value, str):
        return a.args[0].value
    return None


def _insert_parts(sql: str) -> tuple[str, list[str], list[str]] | None:
    """`INSERT INTO t (a, b) VALUES (x, y) ...` -> (t, [a, b], [x, y]); the VALUES list read to its own closing
    parenthesis (an `ON CONFLICT (a, b)` after it is not part of it)."""
    m = re.search(r"INSERT\s+INTO\s+(\w+)\s*\(([^)]*)\)\s*VALUES\s*\(", sql, re.I)
    if not m:
        return None
    depth, i = 1, m.end()
    while i < len(sql) and depth:
        depth += {"(": 1, ")": -1}.get(sql[i], 0)
        i += 1
    if depth:
        return None
    return m[1], [c.strip() for c in m[2].split(",")], _split_values(sql[m.end():i - 1])


class _Eval:
    """Values of expressions in one module, followed through local assignments, local functions and the call sites of
    a function's parameters: each a string constant (`("s", "reddit")`) or a dict's keys (`("k", frozenset)`)."""

    def __init__(self, code: str, dumps_is_text: bool = False):
        self.tree = ast.parse(code)
        self.dumps_is_text = dumps_is_text                     # §17.1843 — `json.dumps(x)` read as the text it is
        self.funcs = {n.name: n for n in self.tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}

    def _assigned(self, fn: ast.AST, name: str) -> list[ast.AST]:
        return [n.value for n in _own_nodes(fn) if isinstance(n, (ast.Assign, ast.AnnAssign)) and n.value is not None
                and any(isinstance(t, ast.Name) and t.id == name
                        for t in (n.targets if isinstance(n, ast.Assign) else [n.target]))]

    def _sites(self, fname: str) -> list[tuple[ast.AST, ast.Call]]:
        out = []
        for f in self.funcs.values():
            for n in _own_nodes(f):
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == fname:
                    out.append((f, n))
        return out[:_MAX_SITES]

    @staticmethod
    def _arg(fn: ast.AST, call: ast.Call, param: str) -> ast.AST | None:
        for kw in call.keywords:
            if kw.arg == param:
                return kw.value
        names = [a.arg for a in [*fn.args.posonlyargs, *fn.args.args]]          # type: ignore[attr-defined]
        if param in names and names.index(param) < len(call.args):
            return call.args[names.index(param)]
        return None

    def value(self, fn: ast.AST, e: ast.AST, env: dict, depth: int = 0) -> list[tuple] | None:
        """Every value `e` can take in `fn` (None when one cannot be read)."""
        if depth > _MAX_DEPTH:
            return None
        e = _unwrap(e)
        if isinstance(e, ast.Constant) and isinstance(e.value, str):
            return [("s", e.value)]
        if isinstance(e, ast.Dict):
            if any(k is None or not (isinstance(k, ast.Constant) and isinstance(k.value, str)) for k in e.keys):
                # §17.1843 — still a dict, whatever its keys: for "is a dict bound", that is all that matters
                return [("k", frozenset(), None, fn, env)] if self.dumps_is_text else None
            return [("k", frozenset(k.value for k in e.keys), e, fn, env)]           # type: ignore[union-attr]
        if self.dumps_is_text and isinstance(e, ast.Call) and isinstance(e.func, ast.Name) and e.func.id == "dict":
            return [("k", frozenset(), None, fn, env)]                                # `dict(raw)`
        if isinstance(e, ast.Call) and isinstance(e.func, ast.Attribute) and e.func.attr == "dumps" and e.args:
            if self.dumps_is_text:
                return [("s", "")]
            return self.value(fn, e.args[0], env, depth + 1)                         # json.dumps(x)
        if isinstance(e, ast.Call) and isinstance(e.func, ast.Name) and e.func.id in self.funcs:
            callee = self.funcs[e.func.id]
            sub = {a.arg: (self._arg(callee, e, a.arg), fn, env) for a in callee.args.args + callee.args.kwonlyargs}
            out: list[tuple] = []
            for r in _own_nodes(callee):
                if isinstance(r, ast.Return) and r.value is not None and not (
                        isinstance(r.value, ast.Constant) and r.value.value is None):     # `return None`: nothing
                    got = self.value(callee, r.value, sub, depth + 1)
                    if got is None:
                        return None
                    out += got
            return out or None
        if isinstance(e, ast.Subscript) and isinstance(e.slice, ast.Constant) and isinstance(e.slice.value, str):
            base = self.value(fn, e.value, env, depth + 1)                       # record["payload"]
            if not base:
                return None
            out = []
            for b in base:
                if b[0] != "k" or b[2] is None or e.slice.value not in b[1]:
                    return None
                d, dfn, denv = b[2], b[3], b[4]
                v = d.values[[k.value for k in d.keys].index(e.slice.value)]
                got = self.value(dfn, v, denv, depth + 1)
                if got is None:
                    return None
                out += got
            return out
        if isinstance(e, ast.Name):
            if e.id in env:                                                      # a parameter: what the caller gave
                arg, cfn, cenv = env[e.id]
                return None if arg is None else self.value(cfn, arg, cenv, depth + 1)
            vals = self._assigned(fn, e.id)
            if not vals:
                return None
            out = []
            for v in vals:
                got = self.value(fn, v, env, depth + 1)
                if got is None:
                    return None
                out += got
            return out
        return None

    def envs(self, fn: ast.AST) -> list[dict]:
        """The bindings `fn` runs with: {} when it has no parameters the INSERT needs, else one per call site."""
        params = [a.arg for a in fn.args.args + fn.args.kwonlyargs]                  # type: ignore[attr-defined]
        if not params:
            return [{}]
        sites = self._sites(fn.name)                                                  # type: ignore[attr-defined]
        return [{p: (self._arg(fn, c, p), cf, {}) for p in params} for cf, c in sites] or [{}]


def json_writes(modules: dict[str, str]) -> list[dict]:
    """Each insert into a table that writes a JSON column: ``{table, column, module, by: {col: value}, keys}`` --
    `by` the columns given a string constant (the item's kind: `source = 'sec_edgar'`), `keys` the JSON dict's keys;
    an insert whose kind or keys cannot be read is left out."""
    out: list[dict] = []
    for mod, code in sorted(modules.items()):
        try:
            ev = _Eval(code)
        except SyntaxError:
            continue
        for fn in ev.funcs.values():
            for call in (n for n in _own_nodes(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                         and n.func.attr == "execute"):
                parts = _insert_parts(_sql_of(call, fn) or "")
                if not parts or len(call.args) < 2:
                    continue
                table, cols, vals = parts
                if len(cols) != len(vals):
                    continue
                params = _unwrap(call.args[1])
                for env in ev.envs(fn):
                    pmap = ev.value(fn, params, env)
                    if not pmap or len(pmap) != 1 or pmap[0][0] != "k":
                        continue
                    d, dfn, denv = pmap[0][2], pmap[0][3], pmap[0][4]
                    by: dict[str, str] = {}
                    keys: dict[str, frozenset] = {}
                    ok = True
                    for col, v in zip(cols, vals, strict=True):
                        lit = re.fullmatch(r"'([^']*)'", v)
                        if lit:
                            by[col] = lit[1]
                            continue
                        pm = _PARAM_RE.match(v)
                        if not pm or pm[1] not in [k.value for k in d.keys]:              # type: ignore[union-attr]
                            continue
                        got = ev.value(dfn, d.values[[k.value for k in d.keys].index(pm[1])], denv)  # type: ignore
                        if got and all(g[0] == "s" for g in got) and len({g[1] for g in got}) == 1:
                            by[col] = got[0][1]
                        elif got and all(g[0] == "k" for g in got):
                            keys[col] = frozenset().union(*(g[1] for g in got))
                        elif "json" in v.lower() or col == "payload":
                            ok = False
                    if ok and by and keys:
                        for col, ks in keys.items():
                            out.append({"table": table, "column": col, "module": mod, "by": by, "keys": ks})
    return out


def _cmp_const(test: ast.AST, disc: set[str]) -> tuple[str, set[str]] | None:
    """`source == "x"` / `"x" == source` / `source in ("x", "y")` on a name in `disc` -> (name, {"x", ...})."""
    if not (isinstance(test, ast.Compare) and len(test.ops) == 1):
        return None
    left, right, op = test.left, test.comparators[0], test.ops[0]
    if isinstance(right, ast.Name) and isinstance(left, ast.Constant):
        left, right = right, left
    if not (isinstance(left, ast.Name) and left.id in disc):
        return None
    if isinstance(op, ast.Eq) and isinstance(right, ast.Constant) and isinstance(right.value, str):
        return left.id, {right.value}
    if isinstance(op, ast.In) and isinstance(right, (ast.Tuple, ast.List, ast.Set)) and all(
            isinstance(x, ast.Constant) and isinstance(x.value, str) for x in right.elts):
        return left.id, {x.value for x in right.elts}                               # type: ignore[attr-defined]
    return None


class _Field:
    """Which keys of a JSON payload a function's result takes one field from, for one kind of item."""

    def __init__(self, ev: _Eval, kind: str, kinds: set[str]):
        self.ev, self.kind, self.kinds = ev, kind, kinds

    def _returns(self, stmts: list[ast.stmt], disc: set[str]) -> tuple[list[ast.AST], bool]:
        """The return expressions reached for this kind, and whether every path returned."""
        out: list[ast.AST] = []
        for st in stmts:
            if isinstance(st, ast.Return):
                if st.value is not None:
                    out.append(st.value)
                return out, True
            if isinstance(st, ast.If):
                c = _cmp_const(st.test, disc)
                if c:                                                    # the kind decides: one branch is taken
                    if self.kind in c[1]:
                        r, done = self._returns(st.body, disc)
                        out += r
                        if done:
                            return out, True
                    else:
                        r, done = self._returns(st.orelse, disc)
                        out += r
                        if done:
                            return out, True
                    continue
                r1, d1 = self._returns(st.body, disc)
                r2, d2 = self._returns(st.orelse, disc)
                out += r1 + r2
                if d1 and d2:
                    return out, True
            elif isinstance(st, (ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith)):
                out += self._returns(st.body, disc)[0]
            elif isinstance(st, ast.Try):
                out += self._returns(st.body, disc)[0]
                for h in st.handlers:
                    out += self._returns(h.body, disc)[0]
        return out, False

    def keys_read(self, fn: ast.AST, e: ast.AST, payload: set[str], disc: set[str], depth: int = 0) -> set[str] | None:
        """The payload keys `e` reads (through local names and local functions handed the payload)."""
        if depth > _MAX_DEPTH:
            return None
        out: set[str] = set()
        for n in ast.walk(e):
            k = _get_key(n, payload)
            if k:
                out.add(k)
            elif (isinstance(n, ast.Subscript) and isinstance(n.value, ast.Name) and n.value.id in payload
                  and isinstance(n.slice, ast.Constant) and isinstance(n.slice.value, str)):
                out.add(n.slice.value)
            elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id not in payload | disc:
                for v in self.ev._assigned(fn, n.id):
                    got = self.keys_read(fn, v, payload, disc, depth + 1)
                    if got is None:
                        return None
                    out |= got
            elif isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in self.ev.funcs:
                got = self.call_field(fn, n, None, payload, disc, depth + 1)
                if got is None:
                    return None
                out |= got
        return out

    def call_field(self, fn: ast.AST, call: ast.Call, field: str | None, payload: set[str], disc: set[str],
                   depth: int) -> set[str] | None:
        """The payload keys a local function's result (or its `field`) takes, called with the payload / the kind."""
        callee = self.ev.funcs[call.func.id]                                          # type: ignore[attr-defined]
        p2: set[str] = set()
        d2: set[str] = set()
        for a in callee.args.args + callee.args.kwonlyargs:                           # type: ignore[attr-defined]
            arg = self.ev._arg(callee, call, a.arg)
            if isinstance(arg, ast.Name) and arg.id in payload:
                p2.add(a.arg)
            elif isinstance(arg, ast.Name) and arg.id in disc:
                d2.add(a.arg)
        if not p2:
            return set()                                                             # never sees the payload
        return self.field_of(callee, field, p2, d2, depth + 1)

    def field_of(self, fn: ast.AST, field: str | None, payload: set[str], disc: set[str],
                 depth: int = 0) -> set[str] | None:
        """The payload keys the dicts `fn` returns take `field` from (all their keys when `field` is None)."""
        if depth > _MAX_DEPTH:
            return None
        rets, _ = self._returns(fn.body, disc)                                       # type: ignore[attr-defined]
        out: set[str] = set()
        for r in rets:
            r = _unwrap(r)
            if field is None:
                got = self.keys_read(fn, r, payload, disc, depth)
            elif isinstance(r, ast.Dict):
                ks = [k.value if isinstance(k, ast.Constant) else None for k in r.keys]
                if field not in ks:
                    return None
                got = self.keys_read(fn, r.values[ks.index(field)], payload, disc, depth)
            elif isinstance(r, ast.Call) and isinstance(r.func, ast.Name) and r.func.id in self.ev.funcs:
                got = self.call_field(fn, r, field, payload, disc, depth)
            elif isinstance(r, ast.Name):
                vals = self.ev._assigned(fn, r.id)
                got = set()
                for v in vals:
                    v = _unwrap(v)
                    if isinstance(v, ast.Call) and isinstance(v.func, ast.Name) and v.func.id in self.ev.funcs:
                        g = self.call_field(fn, v, field, payload, disc, depth)
                    elif isinstance(v, ast.Dict):
                        ks = [getattr(k, "value", None) for k in v.keys]
                        g = self.keys_read(fn, v.values[ks.index(field)], payload, disc, depth) if field in ks \
                            else None
                    else:
                        g = None
                    if g is None:
                        return None
                    got |= g
                for n in _own_nodes(fn):                                             # `v["field"] = ...` later
                    if (isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Subscript)
                            and isinstance(n.targets[0].value, ast.Name) and n.targets[0].value.id == r.id
                            and isinstance(n.targets[0].slice, ast.Constant) and n.targets[0].slice.value == field):
                        g = self.keys_read(fn, n.value, payload, disc, depth)
                        if g is None:
                            return None
                        got |= g
                if not vals:
                    return None
            else:
                return None
            if got is None:
                return None
            out |= got
        return out if rets else None


def _required_fields(fn: ast.AST, local: set[str]) -> list[tuple[str, str, ast.Call]]:
    """`v = R(x)` (R a local function) then `if not v["k"]: continue` (or `.get("k")`, `is None`; giving up with
    continue / raise / return): (v, "k", the call)."""
    calls: dict[str, ast.Call] = {}
    for n in _own_nodes(fn):
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
            c = _unwrap(n.value)
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id in local:
                calls[n.targets[0].id] = c
    out = []
    for n in _own_nodes(fn):
        if not isinstance(n, ast.If) or not _gives_up(n.body):
            continue
        t = n.test.operand if isinstance(n.test, ast.UnaryOp) and isinstance(n.test.op, ast.Not) else (
            n.test.left if isinstance(n.test, ast.Compare) and isinstance(n.test.ops[0], ast.Is) else None)
        if t is None:
            continue
        key = _get_key(t, set(calls)) if isinstance(t, ast.Call) else (
            t.slice.value if isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name) and t.value.id in calls
            and isinstance(t.slice, ast.Constant) and isinstance(t.slice.value, str) else None)
        holder = t.func.value if isinstance(t, ast.Call) and isinstance(t.func, ast.Attribute) else (
            t.value if isinstance(t, ast.Subscript) else None)
        base = holder.id if isinstance(holder, ast.Name) else ""
        if key and base in calls:
            out.append((base, key, calls[base]))
    return out


def _strings_in(ev: _Eval, fn: ast.AST, depth: int = 0) -> set[str]:
    """The string constants a function and the local functions it calls hold."""
    out = {c.value for c in ast.walk(fn) if isinstance(c, ast.Constant) and isinstance(c.value, str)}
    if depth < 3:
        for c in ast.walk(fn):
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id in ev.funcs \
                    and ev.funcs[c.func.id] is not fn:
                out |= _strings_in(ev, ev.funcs[c.func.id], depth + 1)
    return out


def payload_gaps(modules: dict[str, str]) -> list[dict]:
    """§17.1840 — each kind of item one module writes into a JSON column whose keys can never give the field another
    module requires of what it reads back: ``{table, column, kind, field, reader, reads, writers: {module: keys}}``.
    Live (5b4880c0, 2026-10-26): the EDGAR Form 4 and 13F ingesters write `sec_edgar` payloads with no ticker, and the
    generator takes a `sec_edgar` signal's ticker from nothing (`""`); Reddit payloads hold title/author/... and the
    generator reads `ticker`/`symbol` -- every SEC and Reddit signal dropped (`if not normalized["ticker"]: continue`).
    Read statically and claimed only when certain: an unreadable writer or reader path is never reported."""
    writes = [w for w in json_writes(modules) if w["keys"]]
    groups: dict[tuple[str, str, str], dict[str, list[dict]]] = {}
    for w in writes:
        for dcol, kind in w["by"].items():
            groups.setdefault((w["table"], w["column"], dcol), {}).setdefault(kind, []).append(w)
    out: list[dict] = []
    seen: set[tuple] = set()
    for mod, code in sorted(modules.items()):
        try:
            ev = _Eval(code)
        except SyntaxError:
            continue
        for fn in ev.funcs.values():
            sqls = " ".join(c.value for c in _own_nodes(fn) if isinstance(c, ast.Constant) and isinstance(c.value, str))
            for (table, col, dcol), kinds in groups.items():
                if not re.search(rf"\bFROM\s+{table}\b", sqls, re.I) or not re.search(rf"\b{col}\b|\*", sqls):
                    continue
                for _v, field, call in _required_fields(fn, set(ev.funcs)):
                    reader = ev.funcs[call.func.id]                                    # type: ignore[attr-defined]
                    if dcol not in _strings_in(ev, reader):
                        continue                                  # the reader never looks at this column: not its kind
                    payload = {n.targets[0].id for n in _own_nodes(reader) if isinstance(n, ast.Assign)
                               and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name)
                               and any(_get_key(x, {a.arg for a in reader.args.args}) == col  # type: ignore
                                       for x in ast.walk(n.value))}
                    disc = {c[0] for x in ast.walk(reader) if isinstance(x, ast.If)
                            and (c := _cmp_const(x.test, {y.id for y in ast.walk(x.test) if isinstance(y, ast.Name)}))
                            and c[1] & set(kinds)}
                    if not payload:
                        continue
                    for kind, ws in sorted(kinds.items()):
                        reads = _Field(ev, kind, set(kinds)).field_of(reader, field, payload, disc)
                        have = frozenset().union(*(w["keys"] for w in ws))
                        key = (table, col, kind, field)
                        if reads is None or reads & have or key in seen:
                            continue
                        seen.add(key)
                        out.append({"table": table, "column": col, "by": dcol, "kind": kind, "field": field,
                                    "reader": f"{mod}.{reader.name}", "reads": sorted(reads),  # type: ignore[attr-defined]
                                    "writers": {w["module"]: sorted(w["keys"]) for w in ws}})
    return out


# ── §17.1841 a test never deletes from the job's tables ──────────────────────────────────────────────────────────

_DELETE_RE = re.compile(r"\bDELETE\s+FROM\s+(\w+)|\bTRUNCATE\s+(?:TABLE\s+)?(\w+)", re.I)


_APPEND_ONLY_RE = re.compile(r"BEFORE\s+(?:UPDATE\s+OR\s+)?DELETE(?:\s+OR\s+UPDATE)?\s+ON\s+(\{?\w+\}?)", re.I)


def append_only_tables(codes: list[str]) -> set[str]:
    """The tables a job's migrations put a row trigger on that refuses DELETE: `BEFORE UPDATE OR DELETE ON t`, or the
    tables a loop applies `... ON {table}` to (the loop's string tuple)."""
    out: set[str] = set()
    for code in codes:
        for m in _APPEND_ONLY_RE.finditer(code):
            name = m[1]
            if not name.startswith("{"):
                out.add(name)
                continue
            try:
                tree = ast.parse(code)
            except SyntaxError:
                for blk in re.findall(r"```python\n(.*?)```", code, re.S):
                    try:
                        tree = ast.parse(blk)
                        break
                    except SyntaxError:
                        continue
                else:
                    continue
            var = name.strip("{}")
            for f in ast.walk(tree):
                if isinstance(f, ast.For) and isinstance(f.target, ast.Name) and f.target.id == var and isinstance(
                        f.iter, (ast.Tuple, ast.List)):
                    out |= {e.value for e in f.iter.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)}
    return out


def deletes_in_tests(files: dict[str, str], tables: set[str]) -> list[str]:
    """§17.1841 — each test file statement that deletes from one of the job's own tables. Live (5b4880c0 T6U7 x3,
    T6U9, T6U6, 2026-10-26/27): told "never DELETE from them yourself -- an append-only table refuses it", attempt
    after attempt still emptied `ingested_item` in an autouse fixture and every test errored at setup -- the sandbox
    empties the job's tables before each test already."""
    found: list[str] = []
    for path, code in sorted(files.items()):
        if not path.startswith("tests/"):
            continue
        try:
            tree = ast.parse(code)
        except SyntaxError:
            continue
        expected: set[int] = set()                                  # inside `with pytest.raises(...)`: the test
        for w in ast.walk(tree):                                    # that the table refuses it -- not a cleanup
            if isinstance(w, (ast.With, ast.AsyncWith)) and any(
                    isinstance(i.context_expr, ast.Call) and isinstance(i.context_expr.func, ast.Attribute)
                    and i.context_expr.func.attr == "raises" for i in w.items):
                expected |= {id(x) for x in ast.walk(w)}
        for n in ast.walk(tree):
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in expected:
                for m in _DELETE_RE.finditer(n.value):
                    t = m[1] or m[2]
                    if t in tables:
                        found.append(f"`{path}:{n.lineno}` deletes from `{t}`, an append-only table: with rows in it "
                                     "the DELETE fails, and without them it does nothing -- the sandbox empties this "
                                     "job's tables before every test. Drop it; insert only the rows the test needs")
                        break
    return found


# ── §17.1843 a dict is never bound to a column through text() ────────────────────────────────────────────────

def raw_dict_binds(files: dict[str, str]) -> list[str]:
    """Each statement in a step's modules that binds a Python dict as a parameter of `text()` SQL. Live (5b4880c0
    T6U8 x2, 2026-10-27; and f00ea97f:T4's writer, §17.1831): the 13F ingester bound its payload dict to `:payload`
    -- `invalid input for query argument $4: {'holdings': ...}` -- in every real run; the sandbox's hint said to bind
    `json.dumps(value)` with `CAST(:payload AS json)`, and the attempts kept the bind. Read statically through the
    module's helpers and call sites; a value it cannot read is no claim."""
    found: list[str] = []
    for path, code in sorted(files.items()):
        if not path.startswith("app/"):
            continue
        try:
            ev = _Eval(code, dumps_is_text=True)
        except SyntaxError:
            continue
        for fn in ev.funcs.values():
            for call in (n for n in _own_nodes(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                         and n.func.attr == "execute" and len(n.args) >= 2):
                sql = _sql_of(call, fn) or ""
                if not re.match(r"\s*(INSERT|UPDATE)\b", sql, re.I):
                    continue
                bad: set[str] = set()
                for env in ev.envs(fn):
                    pmap = ev.value(fn, _unwrap(call.args[1]), env)
                    if not pmap or len(pmap) != 1 or pmap[0][0] != "k" or pmap[0][2] is None:
                        continue
                    d, dfn, denv = pmap[0][2], pmap[0][3], pmap[0][4]
                    for k, v in zip(d.keys, d.values, strict=True):
                        if k is None:
                            continue
                        got = ev.value(dfn, v, denv)
                        if got and any(g[0] == "k" for g in got) and f":{k.value}" in sql:   # type: ignore[union-attr]
                            bad.add(k.value)                                                 # type: ignore[union-attr]
                for k in sorted(bad):
                    found.append(f"`{path}:{call.lineno}` binds a dict to `:{k}` through `text()`: asyncpg takes JSON "
                                 f"only as text -- bind `json.dumps(...)` and write `CAST(:{k} AS json)` (or `jsonb`, "
                                 "the column's own type) in the SQL")
    return found


# ── §17.1849 a code step is shown a passing test that runs the modules it must run through for real ─────────────

def _job_imports(code: str, job: set[str]) -> set[str]:
    return {i.rsplit(".", 1)[1] for i in _imports(code) if i.startswith("app.modules.") and i.rsplit(".", 1)[1] in job}


def real_data_examples(step_text: str, own: set[str], modules: dict[str, str], tests: dict[str, str],
                       budget: int = 16000) -> str:
    """The finished test files that run the modules this step's code must run through -- the ones its text names
    and the job modules those import -- against the scratch database: each inserts real rows (`INSERT INTO`) and
    stands in for no finished module that never leaves the engine. Live (5b4880c0 T6, 2026-10-27): two models in five
    rounds stood in for `load_batch_signals` / `load_batch_context` rather than insert the `ingested_item` rows the
    real chain turns into signals -- a test neither had seen done; T6U10's generator tests do exactly that."""
    job = set(modules)
    targets = step_targets(step_text, own, modules)
    if not targets:
        return ""
    scored: list[tuple[int, int, str]] = []
    for path, code in tests.items():
        if not path.startswith("tests/") or "INSERT INTO" not in code.upper():
            continue
        hit = _job_imports(code, job) & targets
        if not hit or internal_fakes({path: code}, modules, set()):
            continue
        scored.append((-len(hit), len(code), path))
    out: list[str] = []
    left = budget
    for _neg, size, path in sorted(scored):
        if size > left:
            continue
        out.append(f"### `{path}` (passing; runs {', '.join(f'`{m}`' for m in sorted(_job_imports(tests[path], job) & targets))} "
                   f"for real)\n```python\n{tests[path]}\n```\n")
        left -= size
        if len(out) == 2:
            break
    if not out:
        return ""
    return ("\n\n## How this job's tests run its finished modules for real (§17.1849)\nThese tests pass in the sandbox: "
            "they insert the rows the modules read and call the real functions. Follow the same pattern -- never a "
            "stand-in for these modules.\n" + "".join(out))


# ── §17.1850 a step reads only keys a finished function returns ─────────────────────────────────────────────

def _reads_of(fn: ast.AST, name: str) -> list[tuple[str, int]]:
    """Each `name.get("k", …)` / `name["k"]` read in a function's own body: (key, line), in source order."""
    out: list[tuple[str, int]] = []
    for n in _own_nodes(fn):
        k = _get_key(n, {name})
        if (k is None and isinstance(n, ast.Subscript) and isinstance(n.ctx, ast.Load) and isinstance(n.value, ast.Name)
                and n.value.id == name and isinstance(n.slice, ast.Constant) and isinstance(n.slice.value, str)):
            k = n.slice.value
        if k is not None:
            out.append((k, n.lineno))
    return sorted(out, key=lambda kl: kl[1])


def unreturned_keys(code: str, modules: dict[str, str]) -> list[dict]:
    """§17.1850 — each key `code` reads from what a finished module's function returns that the function never
    returns: ``{producer, var, key, carries, line}``. Live (5b4880c0 T6, 2026-10-28): ``context = await
    load_batch_context(...)`` then ``equity=context.get("equity")`` -- it returns ``{day_start_equity, orders_today}``,
    the proposer got None and crashed in the sandbox. Read as §17.1833 reads: a name bound once to ``[await] f(...)``
    of an imported finished function whose dict keys the reader can follow; an unknown producer is never reported."""
    mine = _defs(code)
    if not mine:
        return []
    funcs, imports = mine
    reader = _Reader(modules)
    out: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for fn in funcs.values():
        bound: dict[str, int] = {}
        a = fn.args                                              # type: ignore[attr-defined]
        for p in [*a.posonlyargs, *a.args, *a.kwonlyargs, *([a.vararg] if a.vararg else []),
                  *([a.kwarg] if a.kwarg else [])]:
            bound[p.arg] = 1
        produced: dict[str, tuple[str, str]] = {}
        for n in _own_nodes(fn):
            if isinstance(n, (ast.Assign, ast.AnnAssign)):
                tgts = n.targets if isinstance(n, ast.Assign) else [n.target]
                for t in tgts:
                    for x in ast.walk(t):
                        if isinstance(x, ast.Name):
                            bound[x.id] = bound.get(x.id, 0) + 1
                if len(tgts) == 1 and isinstance(tgts[0], ast.Name) and n.value is not None:
                    call = _unwrap(n.value)
                    if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id in imports:
                        produced[tgts[0].id] = imports[call.func.id]
            elif isinstance(n, (ast.For, ast.AsyncFor, ast.comprehension)):
                for x in ast.walk(n.target):
                    if isinstance(x, ast.Name):
                        bound[x.id] = bound.get(x.id, 0) + 1
            elif isinstance(n, ast.NamedExpr) and isinstance(n.target, ast.Name):
                bound[n.target.id] = bound.get(n.target.id, 0) + 1
            elif isinstance(n, (ast.With, ast.AsyncWith)):
                for item in n.items:
                    for x in ast.walk(item.optional_vars) if item.optional_vars is not None else ():
                        if isinstance(x, ast.Name):
                            bound[x.id] = bound.get(x.id, 0) + 1
        for var, (pmod, pfn) in produced.items():
            if bound.get(var, 0) != 1:
                continue                                         # bound again: not followed
            carries = reader.returns(pmod, pfn)
            if not carries:
                continue                                         # unknown, or no dict literal at all: no claim
            for key, line in _reads_of(fn, var):
                if key in carries or (pmod, pfn, key) in seen:
                    continue
                seen.add((pmod, pfn, key))
                out.append({"producer": f"{pmod}.{pfn}", "var": var, "key": key, "carries": sorted(carries),
                            "line": line})
    return out


def _names_class(ann: ast.AST, cname: str) -> bool:
    return any(isinstance(x, ast.Name) and x.id == cname for x in ast.walk(ann))


def _key_lives(modules: dict[str, str], key: str) -> str:
    """Where the finished modules hold `key`, as far as it can be read: a class field -- and the public functions or
    methods annotated to return that class -- or a function returning dicts with it. What a public call hands back
    comes first, a bare field last. Empty when nothing holds it."""
    parts: list[tuple[int, str]] = []
    reader = _Reader(modules)
    for mod, code in sorted(modules.items()):
        try:
            tree = ast.parse(code)
        except SyntaxError:
            continue
        for c in tree.body:
            if not isinstance(c, ast.ClassDef):
                continue
            fields = {n.target.id for n in c.body if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)}
            fields |= {t.attr for m in c.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
                       for n in ast.walk(m) if isinstance(n, ast.Assign) for t in n.targets
                       if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) and t.value.id == "self"}
            if key not in fields:
                continue
            returners: list[str] = []
            for owner in tree.body:
                if isinstance(owner, ast.ClassDef):
                    for m in owner.body:
                        if (isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) and m.returns is not None
                                and not m.name.startswith("_") and _names_class(m.returns, c.name)):
                            returners.append(f"`{owner.name}.{m.name}()`")
                elif (isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)) and owner.returns is not None
                      and not owner.name.startswith("_") and _names_class(owner.returns, c.name)):
                    returners.append(f"`{mod}.{owner.name}()`")
            parts.append((0 if returners else 2, f"`{key}` is a field of `{c.name}` (`{mod}`)"
                          + (f", returned by {', '.join(returners[:3])}" if returners else "")))
        funcs = reader.parsed[mod][0] if mod in reader.parsed else {}
        for fname in funcs:
            if fname.startswith("_"):
                continue
            got = reader.returns(mod, fname)
            if got and key in got:
                parts.append((1, f"`{mod}.{fname}()` returns dicts with it"))
    return "; ".join(p for _r, p in sorted(parts, key=lambda rp: rp[0])[:3])


def unreturned_reads(files: dict[str, str], modules: dict[str, str]) -> list[str]:
    """§17.1850 — the refusal for each key a step's files read from a finished function's return that it never
    returns: the keys it does return, and where the key lives when a finished module holds it."""
    found: list[str] = []
    for path, code in files.items():
        if not path.endswith(".py"):
            continue
        for g in unreturned_keys(code, modules):
            pmod, pfn = g["producer"].split(".", 1)
            lives = _key_lives(modules, g["key"])
            found.append(f"`{path}:{g['line']}` reads `{g['key']}` from what `{pfn}` (`{pmod}`) returns, and it "
                         f"returns dicts with keys {', '.join(f'`{k}`' for k in g['carries'])} -- never `{g['key']}`"
                         + (f". {lives}" if lives else ""))
    return found


# ── §17.1851 a step reads what the finished functions it runs through say they do ─────────────────────────────

def step_targets(step_text: str, own: set[str], modules: dict[str, str], ran: set[str] = frozenset(),
                 tested: set[str] | None = None) -> set[str]:
    """The finished modules a step runs through: the ones its text names, the ones its earlier attempts imported
    (`ran`, §17.1851 -- a retry clears the attempt's output, so the engine keeps what it imported), and the job
    modules all of those import; a test step naming none of them, the modules without a test (`tested` known, §17.1862).
    Never the step's own."""
    job = set(modules)
    direct = {m for m in job - own if re.search(rf"\b{re.escape(m)}\b", step_text)} | ((set(ran) & job) - own)
    targets = set(direct)
    for m in direct:
        targets |= _job_imports(modules[m], job) - own
    # §17.1862 — a step that writes a test file and names no module of the job tests the ones that have none: they
    # lead. Live (5b4880c0 T5 "tests for each of the twelve untested modules identified in T1"): the block led
    # alphabetically and was cut at its budget after 18 of 21; the approval gate the step had to test was never shown.
    if not direct and tested is not None and re.search(r"\btests/test_\w+\.py\b", step_text or ""):
        targets |= job - own - set(tested)
    return targets


def module_imports(output: str) -> list[str]:
    """The `app.modules.*` leaves a step's output imports, across its Python blocks -- recorded with a refused
    attempt (§17.1851) so the next attempt is shown what those modules' functions say they do."""
    from app.modules.execution_codegen_gate import PYTHON_LANGS, extract_code_blocks
    out: set[str] = set()
    for lang, code in extract_code_blocks(output or ""):
        if lang in PYTHON_LANGS:
            out |= {i.rsplit(".", 1)[1] for i in _imports(code) if i.startswith("app.modules.")}
    return sorted(out)


def _said(code: str) -> list[tuple[str, str]]:
    """Each public function, and each public method of a public class, with what its docstring says: (signature,
    docstring with its whitespace folded). Source order; nothing for a function that says nothing."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []
    from app.modules.engine_source import _local_functions, _param_values, module_values
    consts, local = module_values(tree), _local_functions(tree)
    out: list[tuple[str, str]] = []

    def add(owner: str, fn: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        doc = " ".join((ast.get_docstring(fn) or "").split())
        # §17.1855 — the values a parameter takes come first, so the cut at `per` never drops them
        vals = "; ".join(f"`{p}` is one of {', '.join('`' + v + '`' for v in vs)}"
                         for p, vs in _param_values(fn, consts, local).items())
        if not (doc or vals) or fn.name.startswith("_"):
            return
        args = ast.unparse(fn.args)
        if owner:
            args = re.sub(r"^self(?:, |$)", "", args)
        out.append((f"{owner + '.' if owner else ''}{fn.name}({args})", f"{vals} -- {doc}" if vals and doc else vals or doc))

    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            add("", n)
        elif isinstance(n, ast.ClassDef) and not n.name.startswith("_"):
            for m in n.body:
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    add(n.name, m)
    return out


def contracts_block(step_text: str, own: set[str], modules: dict[str, str], ran: set[str] = frozenset(),
                    budget: int = 8000, per: int = 700) -> str:
    """§17.1851 — what the finished functions this step runs through say they do: each public function's whole
    docstring, the module's own contract. Live (5b4880c0 T6, 2026-10-28): the proposer reached the step as one line,
    its signature and returned keys; its docstring -- "Proposals that fail risk checks are recorded in the audit
    trail as rejected proposals with a reason, and are not returned" -- never did, and the step's test asserted one
    proposal back for a risk-rejected trade. The modules the step names or ran through first, then what they import."""
    job = set(modules)
    direct = {m for m in job - own if re.search(rf"\b{re.escape(m)}\b", step_text)} | ((set(ran) & job) - own)
    targets = step_targets(step_text, own, modules, ran)
    if not targets:
        return ""
    # breadth-first across the modules (direct ones first): each module's first functions before any module's
    # fifteenth -- live, the broker client's twenty documented methods would have spent the budget before the
    # proposer's one function
    per_mod = [[(mod, sig, doc) for sig, doc in _said(modules[mod])] for mod in sorted(direct) + sorted(targets - direct)]
    lines: list[str] = []
    for i in range(max((len(x) for x in per_mod), default=0)):
        for said in per_mod:
            if i < len(said):
                mod, sig, doc = said[i]
                lines.append(f"- `{mod}.{sig}`: {doc if len(doc) <= per else doc[:per].rsplit(' ', 1)[0] + ' …'}")
    if not lines:
        return ""
    head = ("\n\n## What the finished functions this step runs through say they do (§17.1851)\nEach line is that "
            "module's own contract -- what the function takes, what it returns, and what it does NOT return. The code "
            "and its tests follow it: a test asserts what the function says it does, never the opposite.\n")
    out = head
    for line in lines:
        if len(out) + len(line) + 1 > budget:
            out += "- …\n"
            break
        out += line + "\n"
    return out


# ── §17.1852 a stand-in in the tests costs a test redraw, not the attempt ───────────────────────────────────

def _from_tables(sql: str) -> set[str]:
    """The tables a SELECT reads: each `FROM t` / `JOIN t` that is a table, not a CTE or a function's FROM."""
    from app.modules import engine_schema as _sch
    ctes = {m.group(1) for m in _sch._CTE.finditer(sql)}
    out: set[str] = set()
    for m in _sch._FROM.finditer(sql):
        if _sch._FN_FROM.search(sql[:m.start()]):
            continue
        t = m.group(m.lastindex)                                 # the identifier (after an optional schema)
        if t and t not in ctes:
            out.add(t.lower())
    return out


def reads_tables(modules: dict[str, str], mod: str, name: str, depth: int = 0,
                 _seen: set[tuple[str, str]] | None = None) -> set[str]:
    """The tables `mod.name` reads, itself or through the job functions it calls (local, or imported from another
    job module), up to a few hops. Live (5b4880c0 T6): `load_batch_context` reads `pnl` through
    `get_day_start_equity`; `load_batch_signals` reads `ingested_item` through `generate_signals` -> `get_data_as_of`."""
    seen = _seen if _seen is not None else set()
    if depth > _MAX_DEPTH or (mod, name) in seen:
        return set()
    seen.add((mod, name))
    d = _defs(modules.get(mod, "")) if mod in modules else None
    if not d:
        return set()
    funcs, imports = d
    fn = funcs.get(name)
    if fn is None:
        return reads_tables(modules, *imports[name], depth + 1, seen) if name in imports else set()
    out: set[str] = set()
    for n in _own_nodes(fn):
        # SQL only -- never a docstring's "read from the point-in-time store" (live: `the`, `an`, `point`)
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and _is_query(n.value):
            out |= _from_tables(n.value)
        elif isinstance(n, ast.Call):
            callee = n.func.id if isinstance(n.func, ast.Name) else None
            if callee and callee != name and (callee in funcs or callee in imports):
                out |= reads_tables(modules, mod, callee, depth + 1, seen)
    return out


def _is_query(s: str) -> bool:
    return s.lstrip().upper().startswith(("SELECT", "WITH", "UPDATE", "DELETE", "INSERT"))


def _plain_args(fn: ast.AST) -> str:
    """A signature without annotations: `db, cik, *, equity, as_of_ts=…`."""
    a = fn.args                                                  # type: ignore[attr-defined]
    pos = [*a.posonlyargs, *a.args]
    defaults = len(a.defaults)
    out = [p.arg + ("=…" if i >= len(pos) - defaults else "") for i, p in enumerate(pos)]
    if a.vararg:
        out.append("*" + a.vararg.arg)
    elif a.kwonlyargs:
        out.append("*")
    out += [k.arg + ("=…" if d is not None else "") for k, d in zip(a.kwonlyargs, a.kw_defaults, strict=True)]
    if a.kwarg:
        out.append("**" + a.kwarg.arg)
    return ", ".join(out)


def writers_for(modules: dict[str, str], tables: set[str]) -> list[str]:
    """The public job functions that insert into any of `tables` and never leave the engine (a test can call them in
    the sandbox; an ingester that fetches from the network cannot): "`write_x(*, a, b)` (`mod`)", module order."""
    out: list[str] = []
    job = set(modules)
    for mod, code in sorted(modules.items()):
        d = _defs(code)
        if not d or reaches_outside(code, job, modules):
            continue
        for fname, fn in d[0].items():
            if fname.startswith("_"):
                continue
            for call in (n for n in _own_nodes(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                         and n.func.attr == "execute"):
                parts = _insert_parts(_sql_of(call, fn) or "")
                if parts and parts[0].lower() in tables:
                    out.append(f"`{fname}({_plain_args(fn)})` (`{mod}`)")
                    break
    return out


def seed_order(fks: dict[str, dict[str, str]], tables: set[str], hops: int = 6) -> tuple[list[str], list[str]]:
    """§17.1853 — the tables to fill so each of `tables` takes a row, parents first, and the foreign keys that order
    them (`child.col → parent.id`, child → parent, at most `hops` up). Live (5b4880c0 T6, 2026-10-28): the test called
    the named writer, `write_order(approval_id=1, …)`, and the scratch database refused the row -- no `approval` row;
    `approval` needs a `proposal`, which needs a `signal`, which needs an `ingested_item`."""
    parents: dict[str, set[str]] = {}
    edges: list[str] = []
    frontier = sorted(tables)
    for hop in range(hops + 1):
        nxt: list[str] = []
        for t in frontier:
            if t in parents:
                continue
            parents[t] = set()
            if hop == hops:
                continue                                         # the budget's edge: seeded, its parents not followed
            for col, ref in sorted((fks.get(t) or {}).items()):
                p = ref.split(".")[0]
                if p == t:
                    continue                                     # a self-reference orders nothing
                parents[t].add(p)
                edges.append(f"{t}.{col} → {ref}")
                nxt.append(p)
        frontier = nxt
        if not frontier:
            break
    order: list[str] = []
    while parents:
        ready = sorted(t for t, ps in parents.items() if not ps & parents.keys()) or sorted(parents)[:1]
        for t in ready:                                          # `or`: a cycle, broken at the first name
            order.append(t)
            del parents[t]
    return order, edges


def rows_clause(modules: dict[str, str], reads: set[str], fks: dict[str, dict[str, str]] | None = None) -> str:
    """§17.1852 — "-- it reads `pnl`: insert those rows first, with `w(…)` (`mod`)"; §17.1853 — and when a table it
    reads has foreign keys, the parent rows first, in order, each with its finished writer or, where none stays
    inside the engine (live: every `ingested_item` writer is an ingester that fetches from the network), an INSERT."""
    if not reads:
        return ""
    head = f" -- it reads {', '.join(f'`{t}`' for t in sorted(reads))}"
    order, edges = seed_order(fks or {}, reads)
    if not edges:
        writers = writers_for(modules, reads)
        return head + ": insert those rows first" + (f", with {', '.join(writers[:3])}" if writers else "")
    return (head + f"; the scratch database enforces {', '.join(edges)}, so insert in this order: "
            + "; ".join(_seed_steps(modules, order))
            + " -- each child row takes the id its parent's writer returned, never a made-up one")


def _seed_steps(modules: dict[str, str], order: list[str]) -> list[str]:
    """Each table with its finished writer, or an INSERT where no writer stays inside the engine."""
    steps = []
    for t in order:
        w = writers_for(modules, {t})
        steps.append(f"`{t}` with {' or '.join(w[:2])}" if w else
                     f"`{t}` by an INSERT through `async_session` + `text` (no finished writer stays inside the "
                     "engine; its columns are in this job's tables above)")
    return steps


def stand_in_findings(findings: list[str]) -> list[str]:
    """The §17.1836 findings among `findings`: a test file's stand-in for a finished module."""
    return [f for f in findings if f.startswith("`tests/") and " with a stand-in: " in f]


def findings_in_tests(findings: list[str]) -> list[str]:
    """§17.1859 — the findings that are about a test file's own lines: a stand-in (§17.1836), a patch of a name the
    module lacks (§17.1856), a module run into a secret (§17.1857), a made-up parent id (§17.1858), a DELETE from an
    append-only table (§17.1841), a dict bound to a JSON column (§17.1843), a key the function never returns
    (§17.1850). Each is fixed in the test file alone, the module untouched. Live (5b4880c0 T5, kimi round,
    2026-10-29): attempt 2 had seven findings, all of these kinds, the module clean -- and was spent, because the
    redraw admitted stand-ins only; attempt 3 was spent on ONE."""
    return [f for f in findings if f.startswith("`tests/")]


def _finding_path(finding: str) -> str:
    """The test file a finding names: "`tests/x.py:17` hands …" and "`tests/x.py` dropped 3 of …" both give
    `tests/x.py`. §17.1868 — live (5b4880c0 T5, 2026-10-30): the §17.1864 dropped-tests finding has no `:line`, so
    the path ran to the end of its sentence, `redraw_faked_tests` saw two files and stopped before asking, and two of
    four attempts were spent without the redraw every other finding on them had earned."""
    return finding[1:].split("`", 1)[0].split(":", 1)[0]


def replace_file_block(output: str, path: str, block: str) -> str:
    """`output` with the fenced Python block headed `# path` replaced by `block` (a fenced block, or bare code that
    is then fenced); appended when there is none."""
    from app.modules.execution_codegen_gate import PYTHON_LANGS
    new = block.strip()
    if not new.startswith("```"):
        new = f"```python\n{new}\n```"
    fence = re.compile(r"```([A-Za-z0-9_+-]*)[ \t]*\n(.*?)```", re.S)
    for m in fence.finditer(output or ""):
        if m.group(1).lower() not in PYTHON_LANGS:
            continue
        first = m.group(2).lstrip().splitlines()[0] if m.group(2).strip() else ""
        hm = re.match(r"^\s*#\s*((?:[\w.-]+/)*\w+\.py)\b", first)
        if hm and (hm.group(1) == path or hm.group(1) == path.rsplit("/", 1)[-1]):
            return output[:m.start()] + new + output[m.end():]
    return (output or "").rstrip() + "\n\n" + new


def drop_file_block(output: str, path: str) -> str:
    """§17.1860 — `output` without the fenced Python block headed `# path` (unchanged when there is none)."""
    from app.modules.execution_codegen_gate import PYTHON_LANGS
    fence = re.compile(r"```([A-Za-z0-9_+-]*)[ \t]*\n(.*?)```", re.S)
    for m in fence.finditer(output or ""):
        if m.group(1).lower() not in PYTHON_LANGS:
            continue
        first = m.group(2).lstrip().splitlines()[0] if m.group(2).strip() else ""
        hm = re.match(r"^\s*#\s*((?:[\w.-]+/)*\w+\.py)\b", first)
        if hm and hm.group(1) == path:
            return (output[:m.start()].rstrip() + "\n\n" + output[m.end():].lstrip()).strip() + "\n"
    return output


_LINE_REFS = re.compile(r"`tests/[^`\s]+:(\d+)`|\(line (\d+)\)")


def _finding_lines(findings: list[str]) -> list[int]:
    """§17.1877 — every line a finding names in the test file: its own `tests/x.py:N` and each `(line N)` (a helper
    the fake came through)."""
    return sorted({int(a or b) for f in findings for a, b in _LINE_REFS.findall(f)})


_DROPPED_RE = re.compile(r"^`tests/[^`]+` dropped \d+ of the \d+ tests[^:]*: (.*?)(?: -- |$)")


def _dropped_names(finding: str) -> list[str]:
    """§17.1877 — the test names a §17.1864 dropped-tests finding lists; [] for any other finding."""
    m = _DROPPED_RE.match(finding)
    return re.findall(r"`([^`]+)`", m.group(1)) if m else []


def _top_spans(code: str) -> list[tuple[str, int, int]]:
    """(name, first line -- decorators included, last line) of each top-level function and class of `code`."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []
    out: list[tuple[str, int, int]] = []
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.append((n.name, min([n.lineno] + [d.lineno for d in n.decorator_list]), n.end_lineno or n.lineno))
    return out


def definitions_at(code: str, lines: list[int]) -> list[str]:
    """§17.1877 — the top-level definitions of `code` (a test, a test class, a helper) that hold any of `lines`."""
    return [name for name, a, b in _top_spans(code) if any(a <= ln <= b for ln in lines)]


def splice_definitions(code: str, new: str) -> str | None:
    """§17.1877 — `code` with each top-level function or class of `new` put in place of its namesake, a new name
    appended, and an import `new` has that `code` lacks inserted after the last import; everything else byte for
    byte as it was. None when `new` does not parse or defines nothing. Live (5b4880c0 T5, 2026-10-30, the §17.1876
    round): asked for the whole 97-test file again, the redraws rewrote 1,935 lines for five findings, dropped two
    tests, and once returned the identical file; a redraw that returns the definitions it was asked for cannot."""
    new = (new or "").strip()
    if new.startswith("```"):
        new = new.split("\n", 1)[1] if "\n" in new else ""
        new = new.rsplit("```", 1)[0]
    try:
        ntree = ast.parse(new)
    except SyntaxError:
        return None
    defs = [n for n in ntree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    if not defs:
        return None
    lines = code.splitlines()
    spans = {name: (a, b) for name, a, b in _top_spans(code)}
    nlines = new.splitlines()
    pieces: list[tuple[int, int, str]] = []
    appended: list[str] = []
    for n in defs:
        start = min([n.lineno] + [d.lineno for d in n.decorator_list])
        text = "\n".join(nlines[start - 1:(n.end_lineno or n.lineno)])
        if n.name in spans:
            pieces.append((*spans[n.name], text))
        else:
            appended.append(text)
    try:
        body = ast.parse(code).body
    except SyntaxError:
        body = []
    have = {ast.unparse(x) for x in body if isinstance(x, (ast.Import, ast.ImportFrom))}
    imports = [ast.unparse(x) for x in ntree.body if isinstance(x, (ast.Import, ast.ImportFrom)) and ast.unparse(x) not in have]
    for a, b, text in sorted(pieces, key=lambda t: -t[0]):
        lines[a - 1:b] = text.splitlines()
    if appended:
        lines += ["", ""] + "\n\n\n".join(appended).splitlines()
    if imports:
        last = max((x.end_lineno or x.lineno for x in body if isinstance(x, (ast.Import, ast.ImportFrom))), default=0)
        at = last if last else (1 if lines and lines[0].startswith("#") else 0)
        lines[at:at] = imports
    return "\n".join(lines).rstrip("\n") + "\n"


async def redraw_faked_tests(output: str, findings: list[str], *, ask, check, tries: int = 4,
                             tests_only: bool = False) -> tuple[str, list[str]]:
    """§17.1852 — while every finding is a stand-in in one test file, ask for that file alone (`ask(path, refusal,
    output)` -> the file) and check the result (`check(output)` -> findings), at most `tries` times. Live (5b4880c0
    T6, four rounds on two models): three of four attempts were refused for stand-ins in the tests and nothing else --
    the module itself had passed every check -- and each refusal cost the whole attempt. Returns the output to carry
    on with and its findings: empty when the redraw came clean; the latest otherwise (a redraw that brought a finding
    of another kind stops the loop -- that one gets the ordinary refusal).

    §17.1854 — `ask` is handed the output the refusal is about: the CURRENT candidate. Live (5b4880c0 T6, 2026-10-28,
    the §17.1853 round): the executor's ask showed the model the attempt's original file, so the second redraw read a
    refusal whose line numbers belonged to the first redraw's file next to the file it had started from, and gave that
    original back -- five stand-ins, then two, then the same five.

    §17.1877 — an `ask` that takes `only=` is asked for the DEFINITIONS the findings name (when they are few against
    the file), and its answer is spliced in by name (`splice_definitions`); the whole file only otherwise. A pass
    whose findings were all "does not compile" is not counted; an answer identical to the file ends the loop, as does
    a pass that leaves as many findings as it found. Live (5b4880c0 T5, 2026-10-30, 22:01–22:18 UTC): 1 (syntax) ->
    69 -> 5, 56 -> 5, 120 -> 6, 109 -> 2 -> 2 (the identical file) -- two whole-file passes each, none to the sandbox."""
    # §17.1874 — `tests_only`: the output is test files and nothing else, so EVERY finding is about a test file,
    # whatever its prefix. Live (5b4880c0 T5 a1, 2026-10-30): 68 test-file findings and one `SELECT … ticker … FROM
    # fill` (a schema finding, no path: the test's own SQL) -- no redraw, the attempt spent on a refusal the redraw
    # answers; the file is the output's one test file.
    import inspect

    from app.sandbox.engine_tests import files_of, is_test
    by_name = "only" in inspect.signature(ask).parameters
    test_paths = sorted(p for p in files_of({"output_text": output}) if is_test(p))
    passes = asks = 0
    while passes < max(0, tries) and asks <= max(0, tries) and findings:   # one uncounted syntax pass at most
        asks += 1
        fakes = findings if tests_only and len(test_paths) == 1 else findings_in_tests(findings)   # §17.1859
        if fakes != findings:
            break
        paths = {_finding_path(f) for f in findings_in_tests(fakes)} or set(test_paths)
        if len(paths) != 1:
            break
        path = paths.pop()
        code = files_of({"output_text": output}).get(path, "")
        only: list[str] | None = None
        # a dropped test (§17.1864, no line) is a definition to write anew; any other finding without a line
        # (the file's own SQL, a module-side finding that slipped through) keeps the whole-file ask
        dropped = [n for f in findings for n in _dropped_names(f)]
        if by_name and all(_LINE_REFS.search(f) or _dropped_names(f) for f in findings):
            names = definitions_at(code, _finding_lines(findings))
            names += [d for d in dropped if d not in names]
            if names and (len(names) - len(dropped)) * 2 <= len(_top_spans(code)):   # few against the file
                only = names
        refusal = "\n".join(f"  - {f}" for f in findings)
        new_file = await (ask(path, refusal, output, only=only) if by_name else ask(path, refusal, output))
        if not (new_file or "").strip():
            break
        if only:
            spliced = splice_definitions(code, new_file)
            if spliced is None:
                break
            candidate = replace_file_block(output, path, spliced)
        else:
            candidate = replace_file_block(output, path, new_file)
        if candidate == output:
            break
        syntax_pass = all("does not compile" in f for f in findings)
        before = len(findings)
        findings = await check(candidate)
        output = candidate
        if not syntax_pass:
            passes += 1
            if len(findings) >= before:
                break
    return output, findings
