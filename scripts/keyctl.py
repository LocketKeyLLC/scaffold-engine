#!/usr/bin/env python3
"""§17.807 — scoped API-key CLI (install-time multi-user option).

Mints, lists, and revokes the named keys that ``MULTI_USER_ENABLED`` auth
consults (api_keys, mig 066). Runs INSIDE the orchestrator container so it
shares the app's DATABASE_URL and session plumbing — the Makefile wraps each
subcommand with ``docker exec``:

    make key-add LABEL="alice laptop" [OWNER=alice] [ROLE=user|admin]
    make key-list [ALL=1]
    make key-revoke ID=3        # or:  make key-revoke LABEL="alice laptop"

§17.810 — OWNER is the user identity: several keys with the same OWNER are the
same user (they share job visibility). ROLE is the RBAC tier — 'user' (default,
sees only its own jobs) or 'admin' (sees/manages everything, like the master
key). Ownership + RBAC only take effect when MULTI_USER_ENABLED=true.

A freshly minted raw key is printed ONCE and never recoverable — only its
SHA-256 digest is stored. Distribute it as the X-API-Key value for that user.
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from app.database import async_session
from app.modules import api_keys as ak


async def _add(label: str, owner: str | None, role: str) -> int:
    async with async_session() as session:
        key_id, raw = await ak.add_key(session, label=label, owner=owner, role=role)
    print(
        f"✓ minted key #{key_id}  label={label!r}  role={role}"
        + (f"  owner={owner!r}" if owner else "")
    )
    print()
    print("  Raw key (shown once — store it now, it is not recoverable):")
    print(f"    {raw}")
    print()
    print("  Use it as the X-API-Key header value for this user.")
    return 0


async def _list(include_revoked: bool) -> int:
    async with async_session() as session:
        rows = await ak.list_keys(session, include_revoked=include_revoked)
    if not rows:
        print("(no keys)" if include_revoked else "(no live keys — try ALL=1 to include revoked)")
        return 0
    print(f"{'ID':>4}  {'LABEL':<24} {'OWNER':<16} {'ROLE':<6} {'CREATED':<20} STATUS")
    for r in rows:
        status = "revoked" if r["revoked_at"] else "live"
        created = str(r["created_at"])[:19]
        role = r.get("role") or "user"
        print(
            f"{r['id']:>4}  {(r['label'] or ''):<24} {(r['owner'] or ''):<16} "
            f"{role:<6} {created:<20} {status}"
        )
    return 0


async def _revoke(key_id: int | None, label: str | None) -> int:
    async with async_session() as session:
        n = await ak.revoke_key(session, key_id=key_id, label=label)
    target = f"#{key_id}" if key_id is not None else f"label={label!r}"
    if n:
        print(f"✓ revoked {n} key(s) matching {target}")
        return 0
    print(f"! no live key matching {target} (already revoked, or not found)")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="keyctl", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add", help="mint a new scoped key")
    p_add.add_argument("--label", required=True, help="human label, e.g. 'alice laptop'")
    p_add.add_argument("--owner", default=None, help="owner tag = the user identity (multiple keys can share one owner → one user)")
    p_add.add_argument(
        "--role", default="user", choices=("user", "admin"),
        help="RBAC role (§17.810): 'user' (own jobs only, default) or 'admin' (all jobs)",
    )

    p_list = sub.add_parser("list", help="list keys")
    p_list.add_argument("--all", action="store_true", help="include revoked keys")

    p_rev = sub.add_parser("revoke", help="revoke a key by id or label")
    g = p_rev.add_mutually_exclusive_group(required=True)
    g.add_argument("--id", type=int, help="key id from `list`")
    g.add_argument("--label", help="key label (revokes all live keys with this label)")

    args = parser.parse_args(argv)

    if args.cmd == "add":
        return asyncio.run(_add(args.label, args.owner, args.role))
    if args.cmd == "list":
        return asyncio.run(_list(args.all))
    if args.cmd == "revoke":
        return asyncio.run(_revoke(args.id, args.label))
    parser.error(f"unknown command {args.cmd!r}")  # unreachable (required subparser)
    return 2


if __name__ == "__main__":
    sys.exit(main())
