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
            if len(parts) == 4:
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


def internal_fakes(files: dict[str, str], modules: dict[str, str], own: set[str]) -> list[str]:
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
            found.append(
                f"`{path}:{line}` replaces `{name}` (`{origin}`, a finished module that never leaves the engine) with "
                "a stand-in: call the real one -- against the scratch database, with the rows it reads, when it uses "
                "one -- so the test sees what it really does"
                + (f" (it returns {hint})" if hint else "")
                + ". Mock only what reaches outside the engine -- the broker, the network, model calls, the clock")
    return found


# ── §17.1839 what a test may mock: the exact names through which the job's modules leave the engine ────────────

_KIND = (("app.model_router", "a model call"), ("openai", "a model call"), ("anthropic", "a model call"),
         ("ollama", "a model call"), ("alpaca", "the broker"), ("app.modules.operator_ask", "the operator"),
         ("app.modules.runner_secrets", "a secret"), ("websockets", "the network"), ("httpx", "the network"),
         ("requests", "the network"), ("aiohttp", "the network"), ("urllib", "the network"), ("socket", "the network"))


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
                    pts += [f"`app.modules.{mod}.{a.asname or a.name}` ({kind})" for a in n.names]
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

    def __init__(self, code: str):
        self.tree = ast.parse(code)
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
                return None
            return [("k", frozenset(k.value for k in e.keys), e, fn, env)]           # type: ignore[union-attr]
        if isinstance(e, ast.Call) and isinstance(e.func, ast.Attribute) and e.func.attr == "dumps" and e.args:
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
                if b[0] != "k" or e.slice.value not in b[1]:
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
