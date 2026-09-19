"""§17.1131 (ledger L-6) — the ONE type for a UUID path parameter.

Every route that takes a UUID-keyed id in its path used to declare it as a
bare ``str``; the value reached asyncpg as-is and a mistyped link came back
as a 500 (``DataError: invalid UUID``) plus an on-call error row — or, where
a handler remembered to guard, as a 400 / 404 / 422 depending on the file.
``UuidPath`` makes FastAPI reject a malformed id with a 422 BEFORE the handler
runs, documents the constraint in the OpenAPI schema, and leaves the handler
holding the same ``str`` it always had (no ``uuid.UUID`` conversions to thread
through the SQL). ``tests/test_route_id_params.py`` (ci-tier-0) fails any
router that declares a UUID-backed id path parameter as anything else.

Text-keyed ids (``chat_id`` — the OWUI chat id, ``entry_id`` — a KB slug,
``recipe_id`` — a setup recipe slug) and integer ids stay what they are; the
gate carries that allowlist.
"""
from __future__ import annotations

import re
from typing import Annotated

from fastapi import Path

UUID_PATTERN = (
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

#: Path parameter that must be a canonical hyphenated UUID; still a ``str``.
UuidPath = Annotated[str, Path(pattern=UUID_PATTERN, description="UUID (8-4-4-4-12 hex)")]

_UUID_RE = re.compile(UUID_PATTERN)


def is_uuid(value: object) -> bool:
    """True for a canonical hyphenated UUID string. For code that reads a path
    id RAW (``request.path_params``) — e.g. a router-level dependency, which
    runs before the endpoint's own parameter validation is reported — so it
    can step aside on a malformed id and let the endpoint's 422 win."""
    return isinstance(value, str) and _UUID_RE.fullmatch(value) is not None

#: Integer path ids, bounded to the backing column so a fuzzed
#: 448947330693740494848 is a 422 and not an asyncpg "value out of int32 range" 500.
Int32Path = Annotated[int, Path(ge=1, le=2_147_483_647, description="integer id (int4)")]
Int64Path = Annotated[int, Path(ge=1, le=9_223_372_036_854_775_807, description="integer id (int8)")]
