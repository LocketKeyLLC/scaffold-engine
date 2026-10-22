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
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names.add(node.name)
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
        out[_dotted(path)] = {"doc": doc[0][:160] if doc else "", "names": names, "public": public, "values": values}
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


def _installed(top: str) -> bool:
    if top in sys.stdlib_module_names or top in sys.builtin_module_names:
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
    scored = sorted((-len(want & set(f.split("_"))), len(f), f) for f in fields if want & set(f.split("_")))
    return [f for _, _, f in scored[:3]]


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
                        if mod == "app.config" and al.name == "settings":
                            settings_aliases.add(al.asname or "settings")
                elif mod.split(".")[0] in NOT_IN_THE_ENGINE:
                    note(f"`from {mod} import …`: {NOT_IN_THE_ENGINE[mod.split('.')[0]]}")
                elif not _installed(mod.split(".")[0]) and not _sibling(mod, siblings):
                    note(f"`from {mod} import …`: `{mod.split('.')[0]}` is not installed in the engine")
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
                     + " -- a value the lab needs and the engine does not hold is a module constant, or a field "
                       "a step of this job adds to `app.config.Settings`; never `getattr(settings, …, default)`")
    return found


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
    "- A value the code must not change (a fixed URL) is a module constant, not an environment lookup.")


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


def block_for(node: dict, siblings: set[str] = frozenset(), budget: int = 9000) -> str:
    """What a CodeGen step that extends the engine is shown of the engine (attached after the optimizer, §17.1042)."""
    index = module_index()
    layout: dict[str, list[str]] = {}
    for mod in index:
        head, _, leaf = mod.rpartition(".")
        if head:
            layout.setdefault(head, []).append(leaf)
    hit_fields = matching_settings(_words(str(node.get("title") or "")))
    parts = [
        "\n\n## The engine's own code (read from the running engine, §17.1749)\n"
        "This step writes code that becomes part of the scaffold engine. Import only the engine modules and names "
        "listed here (they exist), packages the engine has installed, or the modules this job's own steps write"
        + (f" ({', '.join(sorted(siblings)[:20])})" if siblings else "") + ". A name not listed here does not "
        "exist: do not guess one, and do not hide a guess behind try/except.\n",
        "### Conventions\n" + CONVENTIONS + "\n",
        "### Modules every change uses\n" + "".join(_render_module(m) for m in _CORE),
    ]
    if hit_fields:
        parts.append("### Settings fields this step may read (`settings.<name>`)\n"
                     + "".join(f"    {f}" + (f" = {settings_fields()[f]}" if settings_fields()[f] not in ("", "PydanticUndefined") else "") + "\n"
                               for f in hit_fields))
    parts.append("### Package layout\n" + "".join(
        f"    {head}: {', '.join(sorted(leaves)[:60])}{' …' if len(leaves) > 60 else ''}\n"
        for head, leaves in sorted(layout.items())))
    out = "\n".join(parts)
    return out if len(out) <= budget else out[:budget].rsplit("\n", 1)[0] + "\n    …\n"
