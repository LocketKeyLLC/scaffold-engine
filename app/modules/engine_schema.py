"""§17.1759 — SQL written for the engine names tables and columns that exist.

Live (job 2772acae "Paper-trading research lab", 2026-10-23): T2 wrote the Alembic revision for the lab's tables and
T16, the audit writer that stands on it, inserted

    INSERT INTO risk_checks (…, risk_results, blocking_reasons)        # T2's risk_checks has max_position_pct, …, reason
    INSERT INTO broker_orders (…, status, broker_response)             # no column broker_response
    INSERT INTO audit_events (model_name, data_timestamp, event_type, payload)   # no migration creates audit_events

and passed: §17.1749's gate checks names in Python, not names in SQL strings, and the verifier never read the two
steps side by side. Every one of these fails on the first write. The lab's own README (T28) found the drift after
the fact.

What a table has is known before the code runs: the engine's tables are in the live database (information_schema),
and a table this job adds is in a sibling step's migration (`op.create_table`, `op.add_column`, or SQL in
`op.execute`). `unknown_schema_refs` reads the SQL in the output's Python string literals and refuses an
INSERT/UPDATE/DELETE/SELECT on a table neither has, and an INSERT or UPDATE of a column the table does not have. A
table whose columns cannot all be read (a migration built them in a way this cannot follow) is checked by name only.
"""
from __future__ import annotations

import ast
import re

from app.modules.execution_codegen_gate import PYTHON_LANGS, extract_code_blocks

#: table name -> its columns, or None when the name is known but its columns are not all readable
Tables = dict[str, "set[str] | None"]

_IDENT = r'"?([A-Za-z_][A-Za-z0-9_]*)"?'
_QUAL = r'(?:"?[A-Za-z_][A-Za-z0-9_]*"?\.)?'          # an optional schema (public.x)
#: SQL as code writes it -- the verb in capitals (a docstring "Update the row …" is prose, not a statement)
_SQL_START = re.compile(r"^\s*(?:--[^\n]*\n\s*)*(SELECT|WITH|INSERT|UPDATE|DELETE)\b")
_INSERT = re.compile(r"\bINSERT\s+INTO\s+" + _QUAL + _IDENT + r"\s*\(([^()]*)\)", re.I)
_INSERT_TABLE = re.compile(r"\bINSERT\s+INTO\s+" + _QUAL + _IDENT, re.I)
_UPDATE = re.compile(r"\bUPDATE\s+" + _QUAL + _IDENT + r"\s+(?:AS\s+\w+\s+|\w+\s+)?SET\s+(.*?)(?=\bWHERE\b|\bRETURNING\b|\bFROM\b|$)",
                     re.I | re.S)
_DELETE = re.compile(r"\bDELETE\s+FROM\s+" + _QUAL + _IDENT, re.I)
_FROM = re.compile(r"(?<![A-Za-z_])(?<!DISTINCT )(FROM|JOIN)\s+(?:(?:LATERAL|ONLY)\s+)?(" + _QUAL
                   + r')"?([A-Za-z_][A-Za-z0-9_]*)(?![A-Za-z0-9_])"?(?!\s*[(.])', re.I)
_CTE = re.compile(r"(?:\bWITH\s+(?:RECURSIVE\s+)?|,\s*)" + _IDENT + r"\s+AS\s+(?:NOT\s+)?(?:MATERIALIZED\s+)?\(", re.I)
_SET_COL = re.compile(r'(?:^|,)\s*"?([A-Za-z_][A-Za-z0-9_]*)"?\s*=(?!=)')
_CREATE = re.compile(r"\bCREATE\s+(?:UNLOGGED\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?" + _QUAL + _IDENT + r"\s*\((.*)\)", re.I | re.S)
_ALTER_ADD = re.compile(r"\bALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?" + _QUAL + _IDENT
                        + r"\s+ADD\s+(?:COLUMN\s+)?(?:IF\s+NOT\s+EXISTS\s+)?" + _IDENT, re.I)
#: `EXTRACT(EPOCH FROM ts)`, `SUBSTRING(x FROM 2)`, `TRIM(BOTH ' ' FROM s)`: FROM inside a function, not a table
_FN_FROM = re.compile(r"\b(?:EPOCH|YEAR|MONTH|DAY|HOUR|MINUTE|SECOND|DOW|DOY|WEEK|QUARTER|BOTH|LEADING|TRAILING)\s*$", re.I)
_NOT_COLUMNS = {"PRIMARY", "FOREIGN", "UNIQUE", "CHECK", "CONSTRAINT", "EXCLUDE", "LIKE"}


def _strings(tree: ast.AST) -> list[str]:
    """Every string literal in the code; an f-string's holes become `{}` (a dynamic name is never checked)."""
    out: list[str] = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            out.append(n.value)
        elif isinstance(n, ast.JoinedStr):
            out.append("".join(v.value if isinstance(v, ast.Constant) else "{}" for v in n.values))
    return out


def _columns_of_create(body: str) -> set[str]:
    cols: set[str] = set()
    depth, cur, parts = 0, "", []
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    for p in parts:
        m = re.match(r'\s*"?([A-Za-z_][A-Za-z0-9_]*)"?', p)
        if m and m.group(1).upper() not in _NOT_COLUMNS:
            cols.add(m.group(1).lower())
    return cols


def _sql_tables(sql: str, into: dict) -> None:
    for stmt in sql.split(";"):                            # one statement at a time: the column list ends at its `)`
        m = _CREATE.search(stmt)
        if m:
            into[m.group(1).lower()] = _columns_of_create(m.group(2))
    for m in _ALTER_ADD.finditer(sql):
        t = m.group(1).lower()
        if into.get(t) is not None:
            into[t].add(m.group(2).lower())
        elif t not in into:
            into[t] = None


def _column_name(call: ast.AST, local_fns: dict) -> str | None | bool:
    """The name an `sa.Column(...)` (or a local helper returning one) declares; False when it is not a column."""
    if not isinstance(call, ast.Call):
        return False
    fn = call.func
    fname = fn.attr if isinstance(fn, ast.Attribute) else fn.id if isinstance(fn, ast.Name) else ""
    if fname == "Column":
        a = call.args[0] if call.args else None
        return a.value if isinstance(a, ast.Constant) and isinstance(a.value, str) else None
    if fname in local_fns:                                  # `_uuid_pk()` → the column its body returns
        fdef = local_fns[fname]
        ret = next((r.value for r in ast.walk(fdef) if isinstance(r, ast.Return)), None)
        if isinstance(ret, ast.Call) and (getattr(ret.func, "attr", None) or getattr(ret.func, "id", None)) == "Column":
            a = ret.args[0] if ret.args else None
            if isinstance(a, ast.Constant) and isinstance(a.value, str):
                return a.value
            if isinstance(a, ast.Name):                     # the helper's parameter: the call's argument or default
                params = [p.arg for p in fdef.args.args]
                if a.id in params:
                    i = params.index(a.id)
                    kw = next((k.value for k in call.keywords if k.arg == a.id), None)
                    defaults = fdef.args.defaults
                    j = i - (len(params) - len(defaults))
                    for cand in ((call.args[i] if i < len(call.args) else None), kw,
                                 (defaults[j] if 0 <= j < len(defaults) else None)):
                        if cand is not None:
                            v = cand.value if isinstance(cand, ast.Constant) else None
                            return v if isinstance(v, str) else None
        return None
    return False                                            # sa.Index, sa.UniqueConstraint, … — not a column


def tables_in_code(texts: list[str]) -> Tables:
    """The tables a set of step outputs create or extend, read from their migrations' Python and SQL."""
    found: dict = {}
    for t in texts:
        for lang, code in extract_code_blocks(t or ""):
            if lang in PYTHON_LANGS:
                try:
                    tree = ast.parse(code)
                except SyntaxError:
                    continue
                consts = {n.targets[0].id: n.value for n in tree.body
                          if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name)}
                local_fns = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
                for n in ast.walk(tree):
                    if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)):
                        continue
                    if n.func.attr == "create_table" and n.args and isinstance(n.args[0], ast.Constant):
                        name = str(n.args[0].value).lower()
                        cols: set[str] | None = set()
                        for a in n.args[1:]:
                            items = [a]
                            if isinstance(a, ast.Starred):
                                src = consts.get(a.value.id) if isinstance(a.value, ast.Name) else None
                                # §17.1782 — or `*_audit_columns()`: a local function returning a list of columns
                                # (live f00ea97f T3 -- unread, every table of the job was names-only, so no column
                                # check ran on any step that wrote or read them)
                                if (src is None and isinstance(a.value, ast.Call) and isinstance(a.value.func, ast.Name)
                                        and a.value.func.id in local_fns):
                                    src = next((r.value for r in ast.walk(local_fns[a.value.func.id])
                                                if isinstance(r, ast.Return)), None)
                                items = list(src.elts) if isinstance(src, (ast.List, ast.Tuple)) else [None]
                            for it in items:
                                c = _column_name(it, local_fns) if it is not None else None
                                if c is None:
                                    cols = None
                                elif c is not False and cols is not None:
                                    cols.add(str(c).lower())
                            if cols is None:
                                break
                        found[name] = cols
                    elif n.func.attr == "add_column" and len(n.args) >= 2 and isinstance(n.args[0], ast.Constant):
                        name = str(n.args[0].value).lower()
                        c = _column_name(n.args[1], local_fns)
                        if found.get(name) is not None and isinstance(c, str):
                            found[name].add(c.lower())
                        elif name not in found:
                            found[name] = None
                for s in _strings(tree):
                    _sql_tables(s, found)
            elif lang.lower() in ("sql", "postgres", "postgresql", "psql"):
                _sql_tables(code, found)
    return found


async def live_tables(db) -> Tables:
    """The engine's own tables and their columns, from the database it is running on."""
    from sqlalchemy import text
    rows = (await db.execute(text(
        "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = 'public'"))).all()
    out: dict = {}
    for t, c in rows:
        out.setdefault(str(t).lower(), set()).add(str(c).lower())
    return out


def _known(table: str, tables: Tables) -> bool:
    return table.lower() in tables


def unknown_schema_refs(output: str, tables: Tables) -> list[str]:
    """Every table or column the output's SQL names that neither the engine's database nor this job's migrations
    have. ``tables`` is the union of both; the output's own migrations are added here."""
    known: dict = dict(tables)
    for k, v in tables_in_code([output]).items():
        known[k] = v if k not in known or known[k] is None else (known[k] | v if v is not None else known[k])
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
            continue
        for sql in _strings(tree):
            if not _SQL_START.match(sql) or _CREATE.search(sql):
                continue
            ctes = {m.group(1).lower() for m in _CTE.finditer(sql)}

            def table_ok(t: str, where: str) -> bool:
                if "{" in t or t.lower() in ctes:
                    return False
                if not _known(t, known):
                    note(f"`{where}`: there is no table `{t}` -- not in the engine's database, and no step of this "
                         "job creates it (a new table is an Alembic revision a step of this job writes)")
                    return False
                return known[t.lower()] is not None

            for m in _INSERT_TABLE.finditer(sql):
                table_ok(m.group(1), f"INSERT INTO {m.group(1)}")
            for m in _INSERT.finditer(sql):
                t = m.group(1)
                if "{" in m.group(2) or not (_known(t, known) and known[t.lower()] is not None):
                    continue
                cols = [c.strip().strip('"').lower() for c in m.group(2).split(",") if c.strip()]
                missing = [c for c in cols if c not in known[t.lower()]]
                if missing:
                    have = ", ".join(sorted(known[t.lower()]))
                    note(f"`INSERT INTO {t} (…)`: `{t}` has no column {', '.join('`' + c + '`' for c in missing)} "
                         f"(it has {have})")
            for m in _UPDATE.finditer(sql):
                t = m.group(1)
                if not table_ok(t, f"UPDATE {t}") or "{" in m.group(2):
                    continue
                missing = [c.lower() for c in _SET_COL.findall(m.group(2)) if c.lower() not in known[t.lower()]]
                if missing:
                    note(f"`UPDATE {t} SET …`: `{t}` has no column {', '.join('`' + c + '`' for c in missing)} "
                         f"(it has {', '.join(sorted(known[t.lower()]))})")
            for m in _DELETE.finditer(sql):
                table_ok(m.group(1), f"DELETE FROM {m.group(1)}")
            for m in _FROM.finditer(sql):
                if m.group(2) and m.group(2).strip('".').lower() != "public":
                    continue                                 # information_schema.x, pg_catalog.y
                if _FN_FROM.search(sql[:m.start()]):
                    continue
                if m.group(1).upper() == "FROM" and re.search(r"\bDELETE\s*$", sql[:m.start()], re.I):
                    continue                                 # DELETE FROM is checked above
                table_ok(m.group(3), f"{m.group(1).upper()} {m.group(3)}")
            # §17.1782 — and the columns a plain one-table SELECT reads. Live (job f00ea97f T13): `SELECT …
            # publication_time, fetched_time FROM ingested_item` against a migration that made `published_ts` /
            # `fetched_ts`; the verifier caught it, the mocked tests could not. Only the unambiguous shape is read: one
            # table, no JOIN / subquery / CTE / UNION; bare names in the select list, and `col <op>` in WHERE / ORDER BY.
            for col, t in _plain_select_columns(sql, ctes):
                if _known(t, known) and known[t.lower()] is not None and col not in known[t.lower()]:
                    note(f"`SELECT … {col} … FROM {t}`: `{t}` has no column `{col}` "
                         f"(it has {', '.join(sorted(known[t.lower()]))})")
    return found


_PLAIN_SELECT = re.compile(r"^\s*SELECT\s+(?:DISTINCT\s+)?(?P<cols>.+?)\s+FROM\s+" + _QUAL + r'"?(?P<t>[A-Za-z_][A-Za-z0-9_]*)"?'
                           r"(?:\s+(?:AS\s+)?(?P<alias>[A-Za-z_][A-Za-z0-9_]*))?(?P<rest>(?:\s+(?:WHERE|ORDER|GROUP|LIMIT|OFFSET|FOR|HAVING)\b.*)?)\s*;?\s*$",
                           re.I | re.S)
_SQL_WORDS = {"and", "or", "not", "null", "is", "in", "like", "ilike", "between", "true", "false", "asc", "desc",
              "limit", "offset", "by", "order", "group", "having", "as", "on", "now", "interval", "current_date",
              "current_timestamp", "case", "when", "then", "else", "end", "distinct", "any", "all", "exists",
              "nulls", "first", "last", "for", "update", "share", "skip", "locked", "nowait", "of", "at", "time",
              "zone", "date", "timestamp", "timestamptz", "text", "int", "integer", "bigint", "numeric", "jsonb",
              "json", "uuid", "boolean", "where"}


def _plain_select_columns(sql: str, ctes: set[str]) -> list[tuple[str, str]]:
    """(column, table) pairs a one-table SELECT names unambiguously; [] for any other shape."""
    if re.search(r"\b(?:JOIN|UNION|INTERSECT|EXCEPT|LATERAL)\b|\(\s*SELECT\b", sql, re.I) or "{" in sql:
        return []
    m = _PLAIN_SELECT.match(sql)
    if not m or m.group("t").lower() in ctes:
        return []
    t, alias = m.group("t"), (m.group("alias") or "")
    if alias.upper() in {"WHERE", "ORDER", "GROUP", "LIMIT", "OFFSET", "FOR", "HAVING"}:
        alias = ""
    out: list[tuple[str, str]] = []
    # a name the select list defines (`COUNT(*) AS calls`) is not a column (measured: `ORDER BY calls` twice)
    aliases = {a.lower() for a in re.findall(r"\bAS\s+\"?([A-Za-z_][A-Za-z0-9_]*)", m.group("cols"), re.I)}
    for item in _split_top(m.group("cols")):
        item = re.sub(r"\s+AS\s+\w+\s*$", "", item.strip(), flags=re.I)
        if alias:
            item = re.sub(rf"^{re.escape(alias)}\.", "", item)
        name = re.fullmatch(r'"?([A-Za-z_][A-Za-z0-9_]*)"?', item)
        if name and name.group(1).lower() not in _SQL_WORDS:
            out.append((name.group(1).lower(), t))
    rest = re.sub(r"'(?:[^']|'')*'", "''", m.group("rest") or "")
    rest = re.sub(r":[A-Za-z_]\w*|\$\d+", " ", rest)            # bound parameters are not columns
    for c in re.findall(r"(?:\bWHERE|\bAND|\bOR|\bBY|,)\s+(?:" + (re.escape(alias) + r"\." if alias else "") +
                        r')?"?([A-Za-z_][A-Za-z0-9_]*)"?\s*(?:=|<>|!=|<=|>=|<|>|\bIN\b|\bIS\b|\bLIKE\b|\bILIKE\b|'
                        r"\bBETWEEN\b|\bASC\b|\bDESC\b|,|$)", rest, re.I):
        if c.lower() not in _SQL_WORDS and c.lower() not in aliases:
            out.append((c.lower(), t))
    return out


def _split_top(text: str) -> list[str]:
    parts, depth, cur = [], 0, ""
    for ch in text:
        depth += ch == "("
        depth -= ch == ")"
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    return parts + [cur]
