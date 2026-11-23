# app/modules/paper_enforce_read_only_data.py
"""Structurally read-only access for the operator dashboard's query path.

The dashboard reads the lab's tables and the lab's helpers; nothing on that
path may write. The read-only rule is enforced twice:

1. structurally -- the session is opened inside a ``READ ONLY`` transaction,
   so the database itself refuses any INSERT/UPDATE/DELETE/DDL a helper
   attempts, whether or not the helper goes through this module's guard;
2. in code -- a statement is only handed to the database when its first
   keyword is a read and it carries no statement-changing keyword.

No schema change and no grants: the engine connects as the tables' owner, so
GRANT/REVOKE cannot make anything read-only here. The read-only transaction
can, and does.
"""

from __future__ import annotations

import logging
import re
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, List, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.database import async_session

logger = logging.getLogger("scaffold")

__all__ = [
    "ALLOWED_LEADING_KEYWORDS",
    "FORBIDDEN_KEYWORDS",
    "READ_ONLY_TRANSACTION_SQL",
    "ReadOnlySession",
    "ReadOnlyViolation",
    "assert_read_only_sql",
    "is_read_only_sql",
    "is_read_only_violation",
    "read_only_session",
]


class ReadOnlyViolation(RuntimeError):
    """The dashboard's data path was asked to do something other than read."""


#: The one statement that turns the current transaction into a read-only one.
READ_ONLY_TRANSACTION_SQL = "SET TRANSACTION READ ONLY"

#: A statement may start with one of these and still be a read.
ALLOWED_LEADING_KEYWORDS = frozenset(
    {"select", "with", "show", "explain", "table", "values"}
)

#: Any of these anywhere in the statement means it is not a read.
FORBIDDEN_KEYWORDS = frozenset(
    {
        "insert",
        "update",
        "delete",
        "merge",
        "upsert",
        "truncate",
        "drop",
        "create",
        "alter",
        "rename",
        "comment",
        "grant",
        "revoke",
        "copy",
        "vacuum",
        "analyze",
        "reindex",
        "cluster",
        "refresh",
        "checkpoint",
        "do",
        "call",
        "prepare",
        "execute",
        "deallocate",
        "discard",
        "listen",
        "notify",
        "unlisten",
        "lock",
        "begin",
        "start",
        "commit",
        "rollback",
        "savepoint",
        "release",
        "abort",
        "set",
        "reset",
        "into",
    }
)

_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")
_LOCKING_RE = re.compile(
    r"\bfor\s+(?:no\s+key\s+update|key\s+share|update|share)\b", re.IGNORECASE
)
_READ_ONLY_ERROR_MARKERS = (
    "read-only",
    "read only transaction",
    "readonlysqltransaction",
)


def _strip_literals_and_comments(sql: str) -> str:
    """Blank out string literals, quoted identifiers and comments.

    What is left is the shape of the statement: the keywords actually executed.
    A word inside ``'...'`` or after ``--`` cannot smuggle a write past the guard.
    """
    out: List[str] = []
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        if ch == "'":
            i += 1
            while i < n:
                if sql[i] == "'":
                    if i + 1 < n and sql[i + 1] == "'":
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            out.append(" ")
        elif ch == '"':
            i += 1
            while i < n:
                if sql[i] == '"':
                    if i + 1 < n and sql[i + 1] == '"':
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            out.append(" ")
        elif sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j < 0 else j + 1
            out.append(" ")
        elif sql.startswith("/*", i):
            depth = 0
            while i < n:
                if sql.startswith("/*", i):
                    depth += 1
                    i += 2
                elif sql.startswith("*/", i):
                    depth -= 1
                    i += 2
                    if depth <= 0:
                        break
                else:
                    i += 1
            out.append(" ")
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _statement_sql(statement: Any) -> str:
    """The SQL text behind either a plain string or a SQLAlchemy ``text()``."""
    if isinstance(statement, str):
        return statement
    inner = getattr(statement, "text", None)
    if isinstance(inner, str):
        return inner
    raise TypeError(
        f"expected SQL text, got {type(statement).__name__}"
    )


def assert_read_only_sql(sql: str) -> str:
    """Return ``sql`` unchanged, or raise :class:`ReadOnlyViolation`.

    Refused: an empty statement, more than one statement, a statement that does
    not begin with a read, one that carries a statement-changing keyword
    anywhere (data-modifying CTEs included), and locking reads.
    """
    if not isinstance(sql, str):
        raise TypeError(f"expected SQL text, got {type(sql).__name__}")

    cleaned = _strip_literals_and_comments(sql)
    # Remove bind parameter placeholders like :start so their names are not
    # mistaken for SQL keywords (e.g. :start would otherwise trip "start").
    cleaned = re.sub(r":[A-Za-z_][A-Za-z_0-9]*", " ", cleaned)
    parts = [part for part in cleaned.split(";") if part.strip()]
    if not parts:
        raise ReadOnlyViolation(
            "refusing to run an empty statement on the dashboard's read-only data path"
        )
    if len(parts) > 1:
        raise ReadOnlyViolation(
            "refusing to run more than one statement on the dashboard's read-only data path"
        )

    body = parts[0]
    words = [word.lower() for word in _WORD_RE.findall(body)]
    if not words:
        raise ReadOnlyViolation(
            "refusing to run a statement with no readable keyword on the "
            "dashboard's read-only data path"
        )

    leading = words[0]
    if leading not in ALLOWED_LEADING_KEYWORDS:
        raise ReadOnlyViolation(
            f"{leading.upper()} is not a read: the dashboard's data path is read-only"
        )

    for word in words:
        if word in FORBIDDEN_KEYWORDS:
            raise ReadOnlyViolation(
                f"{word.upper()} is not allowed on the dashboard's read-only data path"
            )

    if _LOCKING_RE.search(body):
        raise ReadOnlyViolation(
            "a locking read (FOR UPDATE / FOR SHARE) is not allowed on the "
            "dashboard's read-only data path"
        )

    return sql


def is_read_only_sql(statement: Any) -> bool:
    """True when the statement would pass :func:`assert_read_only_sql`."""
    try:
        assert_read_only_sql(_statement_sql(statement))
    except (ReadOnlyViolation, TypeError):
        return False
    return True


def is_read_only_violation(exc: BaseException) -> bool:
    """True when ``exc`` is the database refusing a write in a read-only transaction."""
    message = str(exc).lower()
    return any(marker in message for marker in _READ_ONLY_ERROR_MARKERS)


class ReadOnlySession:
    """The only handle :func:`read_only_session` hands out.

    Every statement goes through :func:`assert_read_only_sql` before the
    database sees it. ``raw`` is the underlying session for the rare caller that
    must issue its own statement -- the read-only transaction still refuses any
    write it attempts.
    """

    __slots__ = ("_db",)

    def __init__(self, db: Any) -> None:
        self._db = db

    @property
    def raw(self) -> Any:
        """The underlying ``AsyncSession``; the transaction it runs in is read-only."""
        return self._db

    async def execute(
        self, statement: Any, params: Optional[Mapping[str, Any]] = None
    ) -> Any:
        """Run one read-only statement and return its ``Result``."""
        sql = _statement_sql(statement)
        assert_read_only_sql(sql)
        try:
            return await self._db.execute(text(sql), dict(params) if params else {})
        except SQLAlchemyError as exc:
            if is_read_only_violation(exc):
                raise ReadOnlyViolation(
                    "the database refused a write on the dashboard's read-only "
                    f"data path: {exc}"
                ) from exc
            raise

    async def fetch_all(
        self, statement: Any, params: Optional[Mapping[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        """Run one read-only statement and return every row as a dict."""
        result = await self.execute(statement, params)
        return [dict(row._mapping) for row in result.fetchall()]

    async def fetch_one(
        self, statement: Any, params: Optional[Mapping[str, Any]] = None
    ) -> Optional[Dict[str, Any]]:
        """Run one read-only statement and return the first row as a dict, or None."""
        result = await self.execute(statement, params)
        row = result.fetchone()
        return dict(row._mapping) if row is not None else None

    async def scalar(
        self, statement: Any, params: Optional[Mapping[str, Any]] = None
    ) -> Any:
        """Run one read-only statement and return its first column of its first row."""
        result = await self.execute(statement, params)
        return result.scalar()


@asynccontextmanager
async def read_only_session() -> AsyncIterator[ReadOnlySession]:
    """Yield a session whose transaction the database itself will not let write.

    The transaction is rolled back on exit: the dashboard's path has nothing to
    commit, and rolling back releases the snapshot it was reading.
    """
    async with async_session() as db:
        try:
            await db.execute(text(READ_ONLY_TRANSACTION_SQL))
        except SQLAlchemyError as exc:
            raise ReadOnlyViolation(
                "could not open a read-only transaction for the dashboard's data "
                f"path: {exc}"
            ) from exc
        logger.debug("opened read-only session for the paper dashboard")
        try:
            yield ReadOnlySession(db)
        finally:
            try:
                await db.rollback()
            except SQLAlchemyError as exc:  # pragma: no cover - defensive
                logger.warning("read-only dashboard session did not roll back cleanly: %s", exc)
